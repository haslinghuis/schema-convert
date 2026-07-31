#!/usr/bin/env python3
"""
genconfig.py - Turn a schematic PDF into a Betaflight config.h.

Pipeline:

  netmap.py         geometry -> which net is on which MCU pin
  seed_firmware.py  firmware -> what each pin can actually do
  this script       net names -> config.h roles, buses, timers and DMA

Nothing here guesses a peripheral instance. The SPI bus behind GYRO-SCK/MISO/MOSI
is found by intersecting those three pins' capabilities in the firmware map; the
TIMER_PIN_MAP occurrence index is counted out of the firmware's own timer table;
ADC DMA options are checked against the streams the motors already claimed. When
the schematic annotates its own intent (`MOTOR1-TIM8 CH1`), that is preferred
over inference and reported as such.

What it cannot know is called out in the emitted header and on stderr: gyro
orientation is a board-layout property, and the current-sense scale depends on
the ESC. Those need the vendor.

Usage:
    python genconfig.py <schematic.pdf> --board NAME --manufacturer ID [-o DIR]
    python genconfig.py <schematic.pdf> --board NAME --manufacturer ID --print
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
import netmap  # noqa: E402
from netmap import (Link, Result, Symbol, Word, extract_words,  # noqa: E402
                    find_net_labels, find_symbol)

DATA_DIR = Path(__file__).parent / "data"
ALIAS_FILE = DATA_DIR / "aliases.json"


# --------------------------------------------------------------------------- #
# Net name -> config.h role
# --------------------------------------------------------------------------- #

# Each rule: (regex, role, group-is-index). Order matters; first match wins.
ROLE_RULES: List[Tuple[re.Pattern, str]] = [
    # Some sheets name the bus explicitly (SPI1_SCK) instead of naming the device
    # on it (GYRO-SCK). Take that at face value - it removes the guesswork.
    (re.compile(r"^SPI(\d)[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "spi_bus"),
    (re.compile(r"^MOTOR(\d+)$|^M(\d+)$|^S(\d)$"), "motor"),
    (re.compile(r"^SERVO(\d+)$"), "servo"),
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)\d?[-_]?CS\d?$"), "gyro_cs"),
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)\d?[-_]?(?:EXTI|INT1?)$"), "gyro_exti"),
    (re.compile(r"^(?:GYRO|IMU)\d?[-_]?(?:CLOCK|CLKIN)$"), "gyro_clkin"),
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)\d?[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "gyro_spi"),
    (re.compile(r"^(?:OSD|MAX7456|AT7456)[-_]?CS$"), "osd_cs"),
    (re.compile(r"^(?:OSD|MAX7456|AT7456)[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "osd_spi"),
    (re.compile(r"^FLASH[-_]?CS$"), "flash_cs"),
    (re.compile(r"^FLASH[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "flash_spi"),
    (re.compile(r"^BARO[-_]?CS$"), "baro_cs"),
    (re.compile(r"^BARO[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "baro_spi"),
    (re.compile(r"^SD(?:CARD)?[-_]?CS$"), "sdcard_cs"),
    (re.compile(r"^(?:.*[-_])?TX(\d)(?:[-_]?R)?$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?RX(\d)(?:[-_]?R)?$"), "uart_rx"),
    (re.compile(r"^I2C(\d)[-_]SCL$"), "i2c_scl"),
    (re.compile(r"^I2C(\d)[-_]SDA$"), "i2c_sda"),
    # Both orderings appear in the wild: ADC-BATT and VBAT_ADC.
    (re.compile(r"^(?:ADC[-_])?(?:BATT|VBAT|BAT)(?:[-_]ADC)?$"), "adc_vbat"),
    (re.compile(r"^(?:ADC[-_])?(?:CURR|CURRENT|ISENSE)(?:[-_]ADC)?$"), "adc_curr"),
    (re.compile(r"^(?:ADC[-_])?RSSI(?:[-_]ADC)?$"), "adc_rssi"),
    (re.compile(r"^LED[-_]?(?:STATUS|STAT)$|^LED0$"), "led0"),
    (re.compile(r"^LED1$"), "led1"),
    (re.compile(r"^LED[-_]?STRIP$|^WS2812$|^LED[-_]?DATA$"), "led_strip"),
    (re.compile(r"^(?:BEEPER|BUZZER|BZ)[-_]?$"), "beeper"),
    (re.compile(r"^CAM[-_]?CONTROLL?$|^CAMERA[-_]?CONTROL$|^CC$"), "camera_control"),
    (re.compile(r"^USB[-_]?DETECT$|^VBUS[-_]?DETECT$"), "usb_detect"),
    (re.compile(r"^VTX[-_]?SW$|^VTX[-_]?(?:PWR|POWER|EN)$"), "pinio"),
    (re.compile(r"^USER(\d)$|^PINIO(\d)$"), "pinio"),
    # A trailing _SW or _EN is a switched rail: CAM_SW, BEC_EN, VTX_EN. These are
    # PINIO outputs, which is different from CAM-Controll (a PWM camera-OSD line).
    (re.compile(r"^[A-Z0-9]+[-_](?:SW|EN)$", re.I), "pinio"),
    (re.compile(r"^(?:FC[-_])?SW(?:DIO|CLK)$|^BOOT$|^OTG[+-]$|^DD?[+-]$|^NRST$"), "ignore"),
]


def classify(net: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    'GYRO-SCK' -> ('gyro_spi', None, 'sck')
    'TX4'      -> ('uart_tx', '4', None)
    Returns (role, index, sub).
    """
    n = net.upper().strip()
    for rx, role in ROLE_RULES:
        m = rx.match(n)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        idx = next((g for g in groups if g.isdigit()), None)
        sub = next((g.lower() for g in groups if not g.isdigit()), None)
        if sub in ("sclk",):
            sub = "sck"
        if sub in ("miso",):
            sub = "sdi"
        if sub in ("mosi",):
            sub = "sdo"
        return role, idx, sub
    return None, None, None


# --------------------------------------------------------------------------- #
# Bus inference
# --------------------------------------------------------------------------- #

DATA_ROLES = ("sck", "sdi", "sdo")


def spi_roles_on(caps: dict, dev: str, pin: str) -> Set[str]:
    """Which roles this pin can serve on this bus, per the firmware map."""
    return {e["role"] for e in caps["spi"].get(pin, []) if e["dev"] == dev}


def resolve_spi_roles(caps: dict, dev: str, pins: Dict[str, str]
                      ) -> Tuple[Dict[str, str], List[str]]:
    """
    Decide what each pin of a bus actually is, from the firmware map.

    The role written on a net is not authoritative. MISO/MOSI are stated from
    somebody's point of view and vendors disagree about whose: one sheet labels
    the wire on the MCU's SDI pin `SPI1_MOSI` and the wire on its SDO pin
    `SPI1_MISO`. Believing the label there yields a bus with two SDIs and no
    SDO, which cannot compile - and quietly contradicts this tool's own rule
    that firmware is the source of truth.

    So the pin decides. Where a pin has exactly one role on the bus, that is the
    answer whatever the label claims. The label only breaks a genuine tie, and a
    disagreement is reported because it says something real about the sheet.
    """
    out: Dict[str, str] = {}
    notes: List[str] = []
    for label_role, pin in sorted(pins.items()):
        if label_role not in DATA_ROLES:
            continue
        actual = spi_roles_on(caps, dev, pin)
        if len(actual) == 1:
            role = next(iter(actual))
            if role != label_role:
                notes.append(f"{pin} is labelled {label_role.upper()} but is "
                             f"{dev} {role.upper()}; following the firmware map")
        elif label_role in actual:
            role = label_role            # ambiguous pin, label breaks the tie
        else:
            notes.append(f"{pin} has no {dev} data role")
            continue
        if role in out and out[role] != pin:
            notes.append(f"{dev} {role.upper()} claimed by both {out[role]} and {pin}")
            continue
        out[role] = pin
    return out, notes


def spi_candidates(caps: dict, pins: Dict[str, str]) -> Tuple[Set[str], List[str]]:
    """
    Which SPI instances could drive this trio of pins?

    A bus qualifies when its pins can take *distinct* roles on it - not when
    each pin happens to support the role its label claims. Asking the weaker
    question rejects a perfectly good bus whenever a sheet's MISO/MOSI naming
    runs the other way round.
    """
    notes: List[str] = []
    devs = {e["dev"] for p in pins.values() for e in caps["spi"].get(p, [])}
    common = set()
    for dev in sorted(devs):
        resolved, _ = resolve_spi_roles(caps, dev, pins)
        if len(resolved) == len([r for r in pins if r in DATA_ROLES]):
            common.add(dev)
    if not common:
        notes.append(f"no single SPI bus gives every pin of {pins} a distinct role")
    return common, notes


def assign_spi_buses(caps: dict, groups: Dict[str, Dict[str, str]]
                     ) -> Tuple[Dict[str, str], List[str]]:
    """
    Work out which SPI instance each device sits on, solving all of them together.

    Picking greedily per device does not work. On an F7, PB3/PB4/PB5 are valid
    SPI1 *and* SPI3 pins, so a flash chip wired there looks like SPI1 in
    isolation and steals the bus from a gyro on PA5/PA6/PA7 that has nowhere else
    to go. Devices sharing identical data pins genuinely share a bus (an OSD and
    a baro on one SPI2 is normal); devices with different data pins cannot. So
    group by data pins, then assign distinct instances to distinct groups,
    most-constrained group first.
    """
    notes: List[str] = []
    keyed: Dict[Tuple, List[str]] = defaultdict(list)
    for owner, pins in groups.items():
        data = tuple(sorted((r, p) for r, p in pins.items() if r != "cs"))
        if data:
            keyed[data].append(owner)

    cands: Dict[Tuple, Set[str]] = {}
    for data, owners in keyed.items():
        common, ns = spi_candidates(caps, dict(data))
        notes.extend(f"{'/'.join(owners)}: {n}" for n in ns)
        cands[data] = common

    order = sorted(cands, key=lambda d: (len(cands[d]), str(d)))
    solution: Dict[Tuple, str] = {}

    def solve(i: int) -> bool:
        if i == len(order):
            return True
        data = order[i]
        for dev in sorted(cands[data]):
            if dev in solution.values():
                continue
            solution[data] = dev
            if solve(i + 1):
                return True
            del solution[data]
        return False

    if not solve(0):
        notes.append("no conflict-free SPI assignment exists; falling back to "
                     "per-device best guess - verify every SPI instance")
        for data in order:
            if cands[data]:
                solution[data] = sorted(cands[data])[0]

    out: Dict[str, str] = {}
    for data, owners in keyed.items():
        dev = solution.get(data)
        if not dev:
            continue
        for owner in owners:
            out[owner] = dev
        if len(cands[data]) > 1:
            notes.append(f"{'/'.join(owners)}: {sorted(cands[data])} both possible; "
                         f"{dev} chosen so the other buses still fit")
    return out, notes


def infer_i2c_bus(caps: dict, scl: Optional[str], sda: Optional[str],
                  declared: Optional[str]) -> Tuple[Optional[str], List[str]]:
    notes: List[str] = []
    sets = []
    for pin, role in ((scl, "scl"), (sda, "sda")):
        if not pin:
            continue
        devs = {e["dev"] for e in caps["i2c"].get(pin, []) if e["role"] == role}
        if devs:
            sets.append(devs)
        else:
            notes.append(f"{pin} has no I2C {role} function")
    if not sets:
        return None, notes
    common = set.intersection(*sets)
    if not common:
        return None, notes + ["SCL and SDA are not on the same I2C bus"]
    dev = sorted(common)[0]
    if declared and f"I2C{declared}" != dev:
        notes.append(f"net names say I2C{declared} but the pins are {dev}")
    return dev, notes


# --------------------------------------------------------------------------- #
# Timer allocation
# --------------------------------------------------------------------------- #

# Advanced-control timers, preferred for motor output: they live on the faster
# bus and carry the complementary/brake features DShot benefits from.
ADVANCED = ("TIM1", "TIM8", "TIM20")

# Device owner -> the config.h define naming its SPI bus.
INSTANCE_DEFINE = {
    "gyro": "GYRO_1_SPI_INSTANCE",
    "osd": "MAX7456_SPI_INSTANCE",
    "flash": "FLASH_SPI_INSTANCE",
    "baro": "BARO_SPI_INSTANCE",
    "sdcard": "SDCARD_SPI_INSTANCE",
}


@dataclass
class TimerPick:
    pin: str
    label: str          # the config.h symbol used in TIMER_PIN_MAPPING
    occurrence: int     # 1-based index into the firmware timer table
    channel: str        # TIM8_CH1
    dmaopt: int
    source: str         # 'schematic' | 'inferred'


def read_timer_hints(words: Sequence[Word]) -> Dict[str, str]:
    """
    Pick up annotations the schematic author wrote, e.g. the words
    'MOTOR1-TIM8' followed by 'CH1'. Returns {'MOTOR1': 'TIM8_CH1'}.
    """
    hints: Dict[str, str] = {}
    for w in words:
        m = re.match(r"^(.+?)[-_](TIM\d+)$", w.text)
        if not m:
            continue
        net, tim = m.group(1), m.group(2)
        # The channel is the next word to the right on the same line.
        ch = next(
            (v.text for v in words
             if abs(v.y0 - w.y0) < 1.0 and 0 <= v.x0 - w.x1 < 6
             and re.fullmatch(r"CH\d", v.text)),
            None,
        )
        if ch:
            hints[net.upper()] = f"{tim}_{ch}"
    return hints


def pick_timer(caps: dict, pin: str, hint: Optional[str],
               prefer_advanced: bool, avoid_complementary: bool = True
               ) -> Optional[Tuple[int, str, str]]:
    """Return (occurrence, channel, source) for a pin needing a timer."""
    options = caps["timers"].get(pin) or []
    if not options:
        return None
    if hint:
        for i, ch in enumerate(options, start=1):
            if ch == hint:
                return i, ch, "schematic"
    ranked = list(enumerate(options, start=1))
    if avoid_complementary:
        plain = [(i, c) for i, c in ranked if not c.endswith("N")]
        ranked = plain or ranked
    if prefer_advanced:
        ranked.sort(key=lambda t: (t[1].split("_")[0] not in ADVANCED, t[0]))
    return (*ranked[0], "inferred")


def motor_timer_plan(caps: dict, motors: Dict[int, str],
                     hints: Dict[str, str]) -> Dict[str, Tuple[int, str, str]]:
    """
    Choose timers for the motor pins, preferring one shared timer so burst DShot
    stays available. Schematic annotations override the choice.
    """
    if not motors:
        return {}
    # Which timers can serve every motor pin on a plain (non-complementary) channel?
    per_pin = {
        pin: {c.split("_")[0] for c in caps["timers"].get(pin, []) if not c.endswith("N")}
        for pin in motors.values()
    }
    shared = set.intersection(*per_pin.values()) if per_pin else set()
    preferred: Optional[str] = None
    if shared:
        preferred = sorted(shared, key=lambda t: (t not in ADVANCED, t))[0]

    plan: Dict[str, Tuple[int, str, str]] = {}
    for n, pin in sorted(motors.items()):
        hint = hints.get(f"MOTOR{n}")
        if not hint and preferred:
            for c in caps["timers"].get(pin, []):
                if c.startswith(preferred + "_") and not c.endswith("N"):
                    hint = c
                    break
        got = pick_timer(caps, pin, hint, prefer_advanced=True)
        if got:
            plan[pin] = got
    return plan


def dma_streams(caps: dict, key: str, opt: int) -> Optional[str]:
    table = caps["dma"]["timer"] if key.startswith("TIM") else caps["dma"]["peripheral"]
    opts = table.get(key) or []
    return opts[opt] if 0 <= opt < len(opts) else None


def choose_adc(caps: dict, pins: Sequence[str], claimed: Set[str],
               mux_next: int = 0) -> Tuple[Optional[str], Optional[int], List[str]]:
    """
    Pick an ADC instance that can read every ADC pin, plus a DMA option that
    nothing else has taken.

    On DMAMUX parts the option is an index into the one shared channel table, so
    it must continue past whatever the timers already claimed; on fixed-mapping
    parts it indexes the ADC's own stream list and only has to dodge the streams
    the timers occupy.
    """
    notes: List[str] = []
    if not pins:
        return None, None, notes
    sets = []
    for p in pins:
        entry = caps["adc"].get(p)
        if not entry:
            notes.append(f"{p} is not an ADC-capable pin")
            continue
        sets.append(set(entry["devices"]))     # '123' -> {'1','2','3'}
    if not sets:
        return None, None, notes
    common = set.intersection(*sets)
    if not common:
        return None, None, notes + ["no single ADC instance covers all ADC pins"]

    if caps["dma"]["style"] == "mux":
        dev = f"ADC{sorted(common)[0]}"
        pool = caps["dma"]["mux_options"] or 0
        if pool and mux_next >= pool:
            return dev, None, notes + [
                f"{dev}: no DMA channel left on this part ({pool} total)"]
        return dev, mux_next, notes

    for n in sorted(common):
        dev = f"ADC{n}"
        opts = caps["dma"]["peripheral"].get(dev) or []
        for opt, stream in enumerate(opts):
            if _stream_of(stream) not in claimed:
                return dev, opt, notes
        notes.append(f"{dev}: every DMA option collides with a timer stream")
    dev = f"ADC{sorted(common)[0]}"
    return dev, 0, notes + [f"{dev}_DMA_OPT 0 may collide - verify"]


def _stream_of(spec: str) -> str:
    """'DMA2_S4_C7' -> 'DMA2_S4' (the contended resource is the stream)."""
    m = re.match(r"(DMA\d+_S\d+)", spec)
    return m.group(1) if m else spec


# --------------------------------------------------------------------------- #
# Connector-derived UART roles
# --------------------------------------------------------------------------- #

# Keyword on a connector's silkscreen -> the config.h role define. Only roles
# with an unambiguous meaning are listed; a 'VTX' header could be analog
# SmartAudio or MSP, so it is deliberately left out rather than guessed.
CONNECTOR_ROLES: List[Tuple[re.Pattern, str]] = [
    # Matched against the whole label ("J5 GPS"), so keep these unanchored and
    # word-bounded rather than anchored to the start of the string.
    (re.compile(r"\b(?:GPS|GNSS|COMPASS)\b", re.I), "GPS_UART"),
    (re.compile(r"\b(?:RECEIVER|ELRS|CRSF|SBUS|RX)\b(?!\d)", re.I), "SERIALRX_UART"),
    (re.compile(r"\bESC\b", re.I), "ESC_SENSOR_UART"),
    (re.compile(r"\b(?:DJI|O[34]|AIR.?UNIT|VISTA|GOGGLE)\b", re.I), "MSP_UART"),
]

SERIAL_PORT_NAMES = {  # configs spell 1/2/3/6 as USART, the rest as UART
    "1": "SERIAL_PORT_USART1", "2": "SERIAL_PORT_USART2",
    "3": "SERIAL_PORT_USART3", "6": "SERIAL_PORT_USART6",
}


def read_connector_roles(words: Sequence[Word]) -> Tuple[Dict[str, str], List[str]]:
    """
    Read the labelled connectors and work out what each UART is for.

    Vendor sheets name their headers (`J5 GPS`, `J7 Receiver`, `J2 ESC`), and the
    UART broken out on each header says what that port is meant to do. Each
    TX/RX net is attributed to its nearest connector designator, which is more
    robust than a bounding box when headers sit close together.
    """
    notes: List[str] = []
    designators = [w for w in words if re.fullmatch(r"[JP]\d{1,2}", w.text)]
    if not designators:
        return {}, notes

    # The header's name is whatever sits just to the right of its designator.
    names: Dict[int, str] = {}
    for d in designators:
        tail = [w.text for w in words
                if abs(w.y0 - d.y0) < 1.5 and 0 <= w.x0 - d.x1 < 12
                and not re.fullmatch(r"[\d.]+", w.text)]
        names[id(d)] = " ".join([d.text] + tail[:2])

    # dir -> uart index, per connector
    seen: Dict[int, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for w in words:
        m = re.fullmatch(r"(TX|RX)(\d)(?:[-_]R)?", w.text, re.I)
        if not m:
            continue
        near = min(designators,
                   key=lambda d: (d.x0 - w.x0) ** 2 + (d.y0 - w.y0) ** 2)
        dist = ((near.x0 - w.x0) ** 2 + (near.y0 - w.y0) ** 2) ** 0.5
        if dist > 80:            # too far to belong to any header
            continue
        seen[id(near)][m.group(1).lower()].add(m.group(2))

    roles: Dict[str, str] = {}
    for d in designators:
        label = names.get(id(d), d.text)
        role = next((r for rx, r in CONNECTOR_ROLES if rx.search(label)), None)
        if not role or id(d) not in seen:
            continue
        dirs = seen[id(d)]
        both = dirs.get("tx", set()) & dirs.get("rx", set())
        # A full-duplex port on the header beats one that only appears one way:
        # a DJI header carries its MSP link both ways plus an SBUS output.
        idx = (sorted(both) or sorted(dirs.get("tx", set()) | dirs.get("rx", set())))
        if not idx:
            continue
        if role in roles and roles[role] != idx[0]:
            notes.append(f"{role}: {label} suggests UART{idx[0]} but "
                         f"UART{roles[role]} was already chosen")
            continue
        roles[role] = idx[0]
        notes.append(f"{role} = UART{idx[0]} from connector '{label}'")
    return roles, notes


# --------------------------------------------------------------------------- #
# HSE crystal
# --------------------------------------------------------------------------- #

# A crystal marking carries its frequency, but so does a ferrite bead's impedance
# spec (`/200R@100MHZ`). Anything outside the range real MCU/peripheral crystals
# are cut in is a different kind of part, not a crystal.
XTAL_MIN_MHZ, XTAL_MAX_MHZ = 4.0, 50.0

# Values seen across the 619-board config corpus plus the rest of the standard
# crystal series. A detected frequency outside this set is still emitted - it may
# be a genuinely unusual board - but it is called out, because a misread digit
# looks exactly like this.
HSE_TYPICAL_MHZ = {8, 12, 16, 24, 25, 26, 27, 32, 48}

FREQ_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?M(?:HZ|H)\b", re.I)

# The MCU symbol's own OSC pin names: 'PH0/OSC-IN', 'PF0-OSC_OUT'. The port-pin
# prefix is what makes this the MCU rather than some other chip with an OSCIN
# pin, and OSC32 is deliberately not matched - that is the 32.768 kHz LSE.
OSC_PIN_RE = re.compile(r"^P[A-K]\d{1,2}[-/]OSC[-_]?(IN|OUT)$", re.I)

# The net drawn between crystal and MCU, when the sheet uses one instead of
# wiring the two symbols side by side: OSCI/OSC_I/OSC_IN/XTAL_IN/HSE_IN.
OSC_NET_RE = re.compile(r"^(?:MCU[-_]?)?(?:X?OSC|XTAL|HSE)[-_]?(IN|OUT|I|O)$", re.I)

REFDES_RE = re.compile(r"^[XY]\d{1,3}$")

# Not every symbol spells the function out - plenty label the row just `PH0`.
# Which port pin is the oscillator is family-dependent, but it does not need a
# hand-written table: the firmware's own capability map answers it. A pin the
# tables route nothing to is not a GPIO on this part, and among these candidates
# that leaves the oscillator. PH0/PH1 appear in no STM32 table at all; PF0/PF1
# are real GPIOs on F4/F7/H5/H7 and the oscillator on G4.
OSC_PIN_CANDIDATES = ("PH0", "PH1", "PF0", "PF1", "PD0", "PD1")

# Families whose clock tree really is derived from SYSTEM_HSE_MHZ, checked
# against the firmware rather than the one-line comment in config.c.
#
# Two independent mechanisms, and `config.c`'s "Only used for F4 and G4 targets"
# describes just the first:
#
#   runtime       config.c seeds systemConfig()->hseMhz, fc/init.c hands it to
#                 systemClockSetHSEValue(), which persists it and re-derives the
#                 PLL M/N dividers on the next boot. Guarded by
#                 PLATFORM_TRAIT_CONFIG_HSE, defined for STM32F4 and STM32G4
#                 (src/platform/STM32/include/platform/platform.h) and
#                 unconditionally for APM32.
#
#   compile time  mk/config.mk preprocesses the define out of config.h and
#                 passes it as -DHSE_VALUE. H5 and C5 select PLL ratios from a
#                 whitelist of HSE_VALUE and #error on anything else; H7 and N6
#                 compute the PLL N divider by dividing a fixed VCO target by it.
#
# The second mechanism is the reason omitting the define is not a safe no-op:
# the top-level Makefile ends `HSE_VALUE ?= 8000000`, so a config with no
# SYSTEM_HSE_MHZ builds as though the board had an 8 MHz crystal. On a 25 MHz H5
# that silently asks PLL1 for a 1.5 GHz VCO.
HSE_FAMILIES = {"STM32F4", "STM32G4", "STM32H5", "STM32H7", "STM32C5", "STM32N6"}

# F7 is the exception that motivated dropping the define in the first place, and
# it stays dropped: system_stm32f7xx.c hardcodes PLL_M to 8 and only reads
# HSE_VALUE to fill in SystemCoreClock for reporting. Emitting it there would
# suggest the PLL had been retuned when it has not - but a non-8 MHz F7 crystal
# is still worth saying out loud, because that board needs a firmware change.
HSE_COSMETIC_FAMILIES = {"STM32F7": 8}


@dataclass
class Crystal:
    mhz: float
    marking: str        # the token as printed, e.g. '3X2.1X1_8MHZ'
    refdes: str         # Y1 / X3, when one sits next to it
    x0: float
    x1: float
    y: float


def _gap(a: Word, b: Word) -> float:
    """Distance between two text boxes, not between their origins.

    Pin names and crystal markings are long strings; measuring origin-to-origin
    makes a wide label look further away than a narrow one at the same place.
    """
    dx = max(0.0, b.x0 - a.x1, a.x0 - b.x1)
    dy = abs(a.y0 - b.y0)
    return (dx * dx + dy * dy) ** 0.5


def find_crystals(words: Sequence[Word]) -> List[Crystal]:
    """Every crystal marking on the sheet, with its designator when there is one."""
    out: List[Crystal] = []
    for w in words:
        if "@" in w.text:          # 200R@100MHZ - a ferrite bead, not a crystal
            continue
        m = None
        for m in FREQ_RE.finditer(w.text):
            pass                   # keep the last: '3X2.1X1_8MHZ' is an 8 MHz part
        if not m:
            continue
        mhz = float(m.group(1))
        if not XTAL_MIN_MHZ <= mhz <= XTAL_MAX_MHZ:
            continue
        near = [v for v in words if REFDES_RE.match(v.text) and _gap(w, v) < 20]
        ref = min(near, key=lambda v: _gap(w, v)).text if near else ""
        out.append(Crystal(mhz, w.text, ref, w.x0, w.x1, w.y0))
    return out


def _osc_nets_near(words: Sequence[Word], anchors: Sequence[Word],
                   radius: float) -> Set[str]:
    """Canonical OSC net names within `radius` of any anchor: {'IN', 'OUT'}."""
    found: Set[str] = set()
    for w in words:
        m = OSC_NET_RE.match(w.text)
        if not m:
            continue
        if any(_gap(w, a) <= radius for a in anchors):
            found.add("IN" if m.group(1).upper() in ("IN", "I") else "OUT")
    return found


def osc_anchors(words: Sequence[Word], sym: Symbol, caps: dict) -> List[Word]:
    """Where the MCU's OSC_IN/OSC_OUT pins sit on the sheet."""
    named = [w for w in words if OSC_PIN_RE.match(w.text)]
    if named:
        return named
    routed: Set[str] = set()
    for table in ("timers", "uart", "spi", "i2c", "adc"):
        routed |= set(caps[table])
    out: List[Word] = []
    for r in sym.rows:
        if r.pin not in OSC_PIN_CANDIDATES or r.pin in routed:
            continue
        # A row carries no x of its own, only its side; the edge it is aligned
        # to is what a crystal drawn outside the symbol is measured from.
        edge = sym.left_edge if r.side == "L" else sym.right_edge
        out.append(Word(r.pin, edge, r.y, edge, r.y))
    return out


def find_mcu_crystal(words: Sequence[Word], sym: Symbol, caps: dict
                     ) -> Tuple[Optional[Crystal], List[str]]:
    """
    Which crystal on this sheet clocks the MCU.

    A flight controller carries several: the MCU's HSE, the OSD chip's 27 MHz,
    sometimes an RF module's. Taking the first one found gives a 27 MHz HSE and a
    clock tree three times too fast, so the crystal has to be tied to the MCU's
    own OSC_IN/OSC_OUT pins before its frequency means anything.

    Two ways a sheet expresses that connection, and both are checked:

      * a net name shared by both ends (`OSCI` at the crystal, `OSC_I` in the
        MCU's gutter) - unambiguous, so it wins outright;
      * the crystal drawn hard up against the MCU's OSC pins with no net label at
        all, which is the more common layout. Distance is then the only evidence,
        so it must also be decisive: the winner has to be far nearer than any
        other crystal, or nothing is claimed.

    Returns (crystal, evidence). A None crystal means the caller must not guess.
    """
    notes: List[str] = []
    crystals = find_crystals(words)
    if not crystals:
        return None, ["no crystal frequency marking found on the sheet"]

    anchors = osc_anchors(words, sym, caps)
    if not anchors:
        return None, ["the MCU symbol has no OSC_IN/OSC_OUT row, so no crystal "
                      "can be tied to the MCU"]

    # Sheet scale varies by an octave between vendors; the symbol's own row pitch
    # is the natural unit for "next to the MCU" and travels across scales.
    direct_max = 30 * sym.pitch

    at_mcu = _osc_nets_near(words, anchors, radius=100.0)
    boxes = [Word(c.marking, c.x0, c.y, c.x1, c.y) for c in crystals]
    linked = [c for c, b in zip(crystals, boxes)
              if at_mcu & _osc_nets_near(words, [b], radius=40.0)]
    if len(linked) == 1:
        c = linked[0]
        return c, [f"{c.refdes or 'crystal'} {c.marking} -> {c.mhz:g} MHz, tied to "
                   f"the MCU by its OSC net label"]
    if len(linked) > 1:
        notes.append("more than one crystal carries an OSC net label: "
                     + ", ".join(f"{c.refdes or '?'} {c.marking}" for c in linked))

    ranked = sorted(((min(_gap(b, a) for a in anchors), c)
                     for c, b in zip(crystals, boxes)), key=lambda t: t[0])
    near = [(d, c) for d, c in ranked if d <= direct_max]
    if not near:
        return None, notes + [
            f"the nearest crystal ({ranked[0][1].refdes or '?'} "
            f"{ranked[0][1].marking}) is {ranked[0][0]:.0f}pt from the MCU's OSC "
            "pins, too far to attribute to them"]
    if len(near) > 1 and near[1][0] < 3 * near[0][0]:
        return None, notes + [
            "two crystals sit equally close to the MCU's OSC pins: "
            + ", ".join(f"{c.refdes or '?'} {c.marking} ({d:.0f}pt)"
                        for d, c in near[:2])]
    d, c = near[0]
    return c, notes + [f"{c.refdes or 'crystal'} {c.marking} -> {c.mhz:g} MHz, "
                       f"{d:.0f}pt from the MCU's OSC pins"]


def resolve_hse(words: Sequence[Word], sym: Symbol, caps: dict,
                override: Optional[int], cfg: "Config") -> Optional[int]:
    """The SYSTEM_HSE_MHZ value to emit, or None. Records its reasoning in `cfg`."""
    family = caps["family"]
    needed = family in HSE_FAMILIES
    if override is not None:
        if not needed:
            cfg.warnings.append(
                f"--hse-mhz {override} ignored: {family}'s clock tree does not "
                "derive from SYSTEM_HSE_MHZ, so the define would be inert")
            return None
        cfg.notes.append(f"SYSTEM_HSE_MHZ {override} given on the command line")
        _check_plausible(override, family, cfg)
        return override

    xtal, why = find_mcu_crystal(words, sym, caps)

    if not needed:
        cosmetic = HSE_COSMETIC_FAMILIES.get(family)
        if xtal and cosmetic is not None and xtal.mhz != cosmetic:
            cfg.warnings.append(
                f"this board's HSE crystal is {xtal.mhz:g} MHz, but {family}'s "
                f"clock setup hardcodes a {cosmetic} MHz PLL input - the firmware "
                "needs a code change; SYSTEM_HSE_MHZ alone would not fix it")
        return None

    if not xtal:
        cfg.warnings.append(
            f"SYSTEM_HSE_MHZ omitted: {'; '.join(why)}. {family} derives its "
            "clock tree from this value and the build defaults to 8 MHz when it "
            "is absent, so confirm the crystal with the vendor and re-run with "
            "--hse-mhz")
        return None

    cfg.notes.extend(why)
    if xtal.mhz != int(xtal.mhz):
        cfg.warnings.append(
            f"SYSTEM_HSE_MHZ omitted: the crystal reads {xtal.mhz:g} MHz and the "
            "define is a whole number of MHz - set it by hand with --hse-mhz")
        return None
    mhz = int(xtal.mhz)
    _check_plausible(mhz, family, cfg)
    return mhz


# Families that reject a zero HSE at compile time rather than falling back to the
# internal oscillator: H5 #errors on an unlisted HSE_VALUE and H7 divides its VCO
# target by it. G4, C5 and N6 do treat 0 as "no crystal, run from HSI".
HSE_ZERO_UNSUPPORTED = {"STM32H5", "STM32H7"}


def _check_plausible(mhz: int, family: str, cfg: "Config") -> None:
    if mhz == 0:
        # Not a crystal frequency - the explicit "this board has no HSE" value.
        if family in HSE_ZERO_UNSUPPORTED:
            cfg.warnings.append(
                f"SYSTEM_HSE_MHZ 0 will not compile on {family}; its clock setup "
                "has no HSI fallback path")
        return
    if mhz not in HSE_TYPICAL_MHZ:
        cfg.warnings.append(
            f"SYSTEM_HSE_MHZ {mhz} is not a value any board in the config repo "
            f"uses ({', '.join(str(v) for v in sorted(HSE_TYPICAL_MHZ))}) - "
            "verify it before shipping")


# --------------------------------------------------------------------------- #
# Part detection
# --------------------------------------------------------------------------- #

# A footprint marked not-fitted. Vendors put the alternate part on the same sheet
# so one PCB can be built either way.
NOT_FITTED_RE = re.compile(r"\((?:NC|DNP|DNI|NF|NP)\)\s*$", re.I)


def _norm(part: str) -> str:
    return re.sub(r"[-_\s]", "", NOT_FITTED_RE.sub("", part)).upper()


@dataclass
class PartHit:
    driver: str        # the firmware's key, e.g. ICM42688P
    marking: str       # as printed on the sheet
    fitted: bool       # False when marked (NC)/(DNP)


def detect_parts(words: Sequence[Word], drivers: dict, aliases: dict
                 ) -> Dict[str, List[PartHit]]:
    """
    category -> every part found, fitted ones first.

    A sheet often carries alternates: `MPU-6000` fitted next to `ICM42688(NC)`
    and `QFN24-MPU6000(NC)`. Returning all of them lets one firmware serve every
    build option, and the ordering is deterministic - iterating a set of tokens
    made the chosen part vary between runs for no visible reason.
    """
    found: Dict[str, List[PartHit]] = defaultdict(list)
    tokens = sorted({w.text for w in words})
    for cat, parts in drivers.items():
        lookup = {_norm(p): p for p in parts}
        alias = {_norm(k): v for k, v in aliases.get(cat, {}).items()}
        seen: Set[str] = set()
        for tok in tokens:
            key = _norm(tok)
            # Longest sensible match: the whole token, then leading chunks
            # (W25Q128JVEIQ, AT7456E-LGA16, SPA06-003).
            for cand in (key, *(key[:i] for i in range(len(key) - 1, 3, -1))):
                driver = alias.get(cand) if alias.get(cand) in parts else lookup.get(cand)
                if not driver or driver in seen:
                    continue
                seen.add(driver)
                found[cat].append(
                    PartHit(driver, tok, not NOT_FITTED_RE.search(tok)))
                break
    for cat in found:
        found[cat].sort(key=lambda h: (not h.fitted, h.driver))
    return dict(found)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    lines: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def define(self, name: str, value: str = "", width: int = 20) -> None:
        self.add(f"#define {name:<{width}}{value}".rstrip())


HEADER = """/*
 * This file is part of Betaflight.
 *
 * Betaflight is free software. You can redistribute this software
 * and/or modify this software under the terms of the GNU General
 * Public License as published by the Free Software Foundation,
 * either version 3 of the License, or (at your option) any later
 * version.
 *
 * Betaflight is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 *
 * See the GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public
 * License along with this software.
 *
 * If not, see <http://www.gnu.org/licenses/>.
 */
"""


def build(pdf: Path, board: str, manufacturer: str, target: Optional[str],
          gyro_align: str, args_trust: bool = False,
          reference: Optional[str] = None,
          version: Optional[str] = None,
          hse_mhz: Optional[int] = None) -> Tuple[Config, dict]:
    fw = json.loads((DATA_DIR / "firmware.json").read_text())
    aliases = json.loads(ALIAS_FILE.read_text())

    words = extract_words(pdf)
    target = target or netmap.detect_target(words, fw)
    if not target:
        raise SystemExit("Could not detect FC_TARGET_MCU - pass --target")
    caps = fw["targets"][target]

    sym = find_symbol(words)
    labels = find_net_labels(words, sym)
    res = netmap.resolve(sym, labels, caps)
    hints = read_timer_hints(words)
    parts = detect_parts(words, fw["drivers"], aliases)

    cfg = Config()
    gaps: Set[str] = set()          # nets the firmware lacks but the symbol backs
    for l in res.links:
        if not (l.checked and not l.ok):
            continue
        if l.symbol_ok:
            # The symbol independently claims this function, so the likely fix is
            # in Betaflight's pin tables, not on the board. Worth a firmware PR.
            gaps.add(l.net)
            cfg.warnings.append(
                f"{l.net} on {l.pin}: the symbol's AF list backs this, but "
                f"Betaflight's {target} tables have no such option for {l.pin}. "
                "Either a firmware pin-table gap or a wrong symbol - vendor AF "
                "lists do contain errors, so confirm in the datasheet AF table "
                "before adding it to the firmware"
                + ("" if args_trust else "; omitted (re-run with --trust-symbol "
                                         "to emit it)"))
        elif l.symbol_ok is False:
            cfg.warnings.append(
                f"{l.net} on {l.pin}: neither Betaflight's {target} tables nor the "
                f"symbol's own AF list ({'/'.join(l.afs)}) support this - likely a "
                "schematic error; omitted")
        else:
            cfg.warnings.append(
                f"{l.net} is on {l.pin}, which Betaflight's {target} tables do not "
                "support for that function, and the symbol carries no AF list to "
                "corroborate it - omitted")

    for l in res.on_power_pin:
        cfg.warnings.append(
            f"{l.net} is wired to {l.pin}, a supply/system pin on this symbol - "
            "omitted; check the schematic")
    if res.orphans:
        cfg.warnings.append("net label(s) that matched no pin row and were "
                            "omitted: " + ", ".join(res.orphans))

    # ---- group the links by role -----------------------------------------
    motors: Dict[int, str] = {}
    servos: Dict[int, str] = {}
    uart: Dict[Tuple[str, str], str] = {}
    i2c: Dict[str, str] = {}
    spi_groups: Dict[str, Dict[str, str]] = defaultdict(dict)
    spi_named: Dict[str, Dict[str, str]] = defaultdict(dict)   # bus stated on the sheet
    simple: Dict[str, str] = {}
    pinios: List[Tuple[str, str]] = []
    unknown: List[Link] = []

    for l in res.links:
        # Anything the firmware map rejected, or that landed on a supply pin, is
        # left out rather than emitted as a define that cannot work. Each one is
        # already recorded as a warning above.
        if not l.gpio:
            continue
        if l.checked and not l.ok and not (args_trust and l.symbol_ok):
            continue
        role, idx, sub = classify(l.net)
        if role == "ignore":
            continue
        if role is None:
            unknown.append(l)
        elif role == "motor":
            motors[int(idx or 1)] = l.pin
        elif role == "servo":
            servos[int(idx or 1)] = l.pin
        elif role in ("uart_tx", "uart_rx"):
            uart[(idx or "1", role[-2:])] = l.pin
        elif role in ("i2c_scl", "i2c_sda"):
            i2c[role[-3:]] = l.pin
            i2c.setdefault("declared", idx or "1")
        elif role == "spi_bus":
            spi_named[f"SPI{idx or '1'}"][sub or "sck"] = l.pin
        elif role.endswith("_spi"):
            spi_groups[role[:-4]][sub or "sck"] = l.pin
        elif role.endswith("_cs"):
            spi_groups[role[:-3]]["cs"] = l.pin
        elif role == "pinio":
            pinios.append((l.net, l.pin))
        else:
            simple[role] = l.pin

    # ---- header ----------------------------------------------------------
    #
    # REFERENCE is NOT a hash of anything we hold. It is a token the Betaflight
    # team issues once a target has been reviewed, validated against the
    # upper-cased BOARD_NAME, and it cannot be computed locally. Emitting a
    # locally-derived digest there would be inventing provenance, so the
    # supported-target block is written only when a real value is supplied via
    # --reference.
    #
    # The schematic's own sha256 is still worth recording - it pins down which
    # revision the config came from - just not in that field.
    digest = sha256(pdf.read_bytes()).hexdigest()
    cfg.add(HEADER)
    if reference:
        cfg.add("/*")
        cfg.add("    SUPPORTED TARGET - THANK YOU")
        cfg.add(f"    REFERENCE: {reference}")
        cfg.add(f"    DATE: {date.today().isoformat()}")
        if version:
            cfg.add(f"    VERSION: {version}")
        cfg.add("*/")
        cfg.add()
    cfg.add("/*")
    cfg.add(f"    Generated from {pdf.name}")
    cfg.add(f"    Schematic sha256: {digest}")
    cfg.add(f"    Converted: {date.today().isoformat()}")
    if not reference:
        cfg.add("")
        cfg.add("    No REFERENCE directive: this target has not been reviewed by")
        cfg.add("    the Betaflight team. They issue that value; it cannot be")
        cfg.add("    computed here. Re-run with --reference once it is provided.")
    cfg.add("*/")
    cfg.add()
    cfg.add("#pragma once")
    cfg.add()
    cfg.define("FC_TARGET_MCU", target)
    cfg.add()
    cfg.define("BOARD_NAME", board)
    cfg.define("MANUFACTURER_ID", manufacturer)
    cfg.add()

    # ---- feature defines -------------------------------------------------
    gyro_bus = "spi" if "gyro" in spi_groups else "i2c"
    feats: List[str] = []

    def driver_for(cat: str, hit: PartHit, bus: str) -> Optional[str]:
        buses = fw["drivers"][cat].get(hit.driver) or {}
        return buses.get(bus) or buses.get("any") or next(iter(buses.values()), None)

    if "gyro" in parts:
        feats += ["USE_ACC", "USE_GYRO"]
        # Emit every variant on the sheet, fitted or not, so one firmware covers
        # both build options - which is what the alternate footprint is for.
        for cat in ("acc", "gyro"):
            for hit in parts.get(cat, []):
                d = driver_for(cat, hit, gyro_bus)
                if d:
                    feats.append(d)
    else:
        cfg.warnings.append("no gyro part recognised on the sheet")
    if "gyro_clkin" in simple:
        feats.append("USE_GYRO_CLKIN")
    if "baro" in parts:
        bus = "spi" if "baro" in spi_groups else "i2c"
        feats.append("USE_BARO")
        for hit in parts["baro"]:
            d = driver_for("baro", hit, bus)
            if d:
                feats.append(d)
    if "flash" in parts:
        feats.append("USE_FLASH")
        for hit in parts["flash"]:
            d = driver_for("flash", hit, "spi")
            if d:
                feats.append(d)
    if "osd" in parts:
        feats.append("USE_MAX7456")
    for f in dict.fromkeys(feats):
        cfg.define(f)
    cfg.add()

    for cat, hits in sorted(parts.items()):
        unfitted = [h for h in hits if not h.fitted]
        if unfitted and not any(h.fitted for h in hits):
            cfg.warnings.append(
                f"the only {cat} on the sheet is marked not-fitted "
                f"({', '.join(h.marking for h in unfitted)}); driver enabled anyway "
                "- confirm the populated variant with the vendor")
        elif unfitted:
            cfg.notes.append(f"{cat} alternates marked not-fitted, drivers included: "
                             + ", ".join(h.marking for h in unfitted))

    # ---- motors ----------------------------------------------------------
    tplan = motor_timer_plan(caps, motors, hints)
    for n, pin in sorted(motors.items()):
        cfg.define(f"MOTOR{n}_PIN", pin)
    for n, pin in sorted(servos.items()):
        cfg.define(f"SERVO{n}_PIN", pin)
    if motors or servos:
        cfg.add()

    # ---- UARTs -----------------------------------------------------------
    for n in sorted({k[0] for k in uart}, key=int):
        for d in ("tx", "rx"):
            pin = uart.get((n, d))
            if pin:
                cfg.define(f"UART{n}_{d.upper()}_PIN", pin)
    if uart:
        cfg.add()

    # ---- I2C -------------------------------------------------------------
    i2c_dev = None
    if i2c.get("scl") or i2c.get("sda"):
        i2c_dev, notes = infer_i2c_bus(caps, i2c.get("scl"), i2c.get("sda"),
                                       i2c.get("declared"))
        cfg.notes.extend(notes)
        if i2c_dev:
            n = i2c_dev[-1]
            if i2c.get("scl"):
                cfg.define(f"I2C{n}_SCL_PIN", i2c["scl"])
            if i2c.get("sda"):
                cfg.define(f"I2C{n}_SDA_PIN", i2c["sda"])
            cfg.add()

    # ---- SPI buses -------------------------------------------------------
    # Emit in bus order so the file reads like the hand-written ones.
    assigned, notes = assign_spi_buses(caps, spi_groups)
    cfg.notes.extend(notes)
    resolved: Dict[str, Tuple[str, Dict[str, str]]] = {}
    for owner, pins in spi_groups.items():
        dev = assigned.get(owner)
        if dev:
            resolved[owner] = (dev, pins)
        elif set(pins) == {"cs"}:
            # CS-only: the device shares another bus but the sheet does not say
            # which, so emit the CS pin and leave the instance to the reviewer.
            resolved[owner] = ("", pins)
            cfg.warnings.append(
                f"{owner} has only a CS net on this sheet; set "
                f"{INSTANCE_DEFINE.get(owner, owner.upper() + '_SPI_INSTANCE')} by hand")
        else:
            cfg.warnings.append(f"could not resolve the SPI bus for {owner}")

    CS_DEFINE = {
        "gyro": "GYRO_1_CS_PIN",
        "osd": "MAX7456_SPI_CS_PIN",
        "flash": "FLASH_CS_PIN",
        "baro": "BARO_CS_PIN",
        "sdcard": "SDCARD_CS_PIN",
    }
    # Cross-check any bus the sheet named itself against the firmware map.
    # Check the pin belongs to the bus its label names - not that it serves the
    # exact role the label claims, which resolve_spi_roles settles from the
    # firmware map and reports on separately.
    for dev, pins in spi_named.items():
        for role, pin in pins.items():
            if not spi_roles_on(caps, dev, pin):
                cfg.warnings.append(
                    f"{pin} is labelled {dev}_{role.upper()} but has no {dev} "
                    "function at all")

    all_buses = sorted({d for d, _ in resolved.values() if d} | set(spi_named))
    emitted_buses: Set[str] = set()
    for dev in all_buses:
        owners = [o for o, (d, _) in resolved.items() if d == dev]
        n = dev[-1]
        pins = dict(spi_named.get(dev, {}))
        for o in owners:
            pins.update({k: v for k, v in resolved[o][1].items() if k != "cs"})
        # Keyed by label so far; the firmware map has the last word on which
        # line is which.
        pins, role_notes = resolve_spi_roles(caps, dev, pins)
        cfg.notes.extend(f"{dev}: {n}" for n in role_notes)

        # A bus must be all three lines or none. Firmware has no default for
        # SPIn_SDO_PIN, so declaring SCK and SDI without SDO is not a partial
        # config - it is a build error (`DEFIO_TAG__SPI1_SDO_PIN undeclared`).
        # This happens for real: a sheet that swaps its MISO/MOSI labels gets
        # one of them rejected by the firmware check, and what is left is half
        # a bus. Emitting nothing and saying so beats emitting something that
        # cannot compile.
        missing = [r for r in ("sck", "sdi", "sdo") if r not in pins]
        if missing:
            cfg.warnings.append(
                f"{dev} is incomplete - no {', '.join(m.upper() for m in missing)} "
                f"(have {', '.join(sorted(k.upper() for k in pins))}); the bus is "
                "not emitted, since a partial one does not compile")
            continue
        for role, key in (("sck", "SCK"), ("sdi", "SDI"), ("sdo", "SDO")):
            cfg.define(f"SPI{n}_{key}_PIN", pins[role])
        emitted_buses.add(dev)
        cfg.add()

    # Chip selects and the gyro's EXTI/CLKIN are plain GPIO: they do not depend
    # on the bus being resolvable, so they are emitted here rather than inside
    # the loop above. Nesting them there meant an unresolved bus silently took
    # GYRO_1_CS_PIN, FLASH_CS_PIN and GYRO_1_EXTI_PIN down with it.
    wrote_cs = False
    for owner, (_dev, pins) in sorted(resolved.items()):
        cs = pins.get("cs")
        if cs and owner in CS_DEFINE:
            cfg.define(CS_DEFINE[owner], cs)
            wrote_cs = True
    for role, name in (("gyro_exti", "GYRO_1_EXTI_PIN"),
                       ("gyro_clkin", "GYRO_1_CLKIN_PIN")):
        if role in simple:
            cfg.define(name, simple[role])
            wrote_cs = True
    if wrote_cs:
        cfg.add()

    # ---- discrete IO -----------------------------------------------------
    IO_DEFINE = [
        ("led0", "LED0_PIN"), ("led1", "LED1_PIN"),
        ("beeper", "BEEPER_PIN"), ("led_strip", "LED_STRIP_PIN"),
        ("camera_control", "CAMERA_CONTROL_PIN"),
        ("usb_detect", "USB_DETECT_PIN"),
    ]
    wrote = False
    for role, name in IO_DEFINE:
        if role in simple:
            cfg.define(name, simple[role])
            wrote = True
    if wrote:
        cfg.add()

    adc_pins = [simple[k] for k in ("adc_vbat", "adc_curr", "adc_rssi") if k in simple]
    for role, name in (("adc_vbat", "ADC_VBAT_PIN"), ("adc_curr", "ADC_CURR_PIN"),
                       ("adc_rssi", "ADC_RSSI_PIN")):
        if role in simple:
            cfg.define(name, simple[role])
    if adc_pins:
        cfg.add()

    # PINIO order follows the sheet: a VTX switch is conventionally PINIO1.
    pinios.sort(key=lambda t: (0 if "VTX" in t[0].upper() else 1, t[0]))
    for i, (_net, pin) in enumerate(pinios, start=1):
        cfg.define(f"PINIO{i}_PIN", pin)
    if pinios:
        cfg.add()

    # ---- timer mapping ---------------------------------------------------
    picks: List[TimerPick] = []
    claimed: Set[str] = set()
    for n, pin in sorted(motors.items()):
        got = tplan.get(pin)
        if not got:
            cfg.warnings.append(f"MOTOR{n} on {pin} has no timer in the firmware table")
            continue
        occ, ch, src = got
        picks.append(TimerPick(pin, f"MOTOR{n}_PIN", occ, ch, 0, src))
    for n, pin in sorted(servos.items()):
        got = pick_timer(caps, pin, hints.get(f"SERVO{n}"), prefer_advanced=True)
        if got:
            picks.append(TimerPick(pin, f"SERVO{n}_PIN", got[0], got[1], -1, got[2]))
    for role, label, dmaopt in (("led_strip", "LED_STRIP_PIN", 0),
                                ("camera_control", "CAMERA_CONTROL_PIN", -1),
                                ("gyro_clkin", "GYRO_1_CLKIN_PIN", -1)):
        pin = simple.get(role)
        if not pin:
            continue
        hint = hints.get(role.replace("_", "-").upper()) or hints.get(
            {"led_strip": "LED-STRIP", "camera_control": "CAM-CONTROLL",
             "gyro_clkin": "GYRO-CLOCK"}[role])
        got = pick_timer(caps, pin, hint, prefer_advanced=False)
        if got:
            picks.append(TimerPick(pin, label, got[0], got[1], dmaopt, got[2]))
        else:
            cfg.warnings.append(f"{label} on {pin} has no timer; LED strip needs one"
                                if role == "led_strip" else
                                f"{label} on {pin} has no timer function")

    # DMA option numbering means two different things depending on the part.
    #
    # On F4/F7 the mapping is fixed: dmaopt indexes that timer channel's own
    # short list of possible streams, so several peripherals may all use opt 0
    # and still land on different streams.
    #
    # On DMAMUX/GPDMA parts (G4, H5, H7, C5, N6) any request can be routed to
    # any channel, so dmaopt is a direct index into one shared channel table.
    # There, opt 0 on four motors puts all four on the same channel. Each user
    # needs its own number - see how the G4 and H7 configs in the config repo
    # number their motors 0, 1, 2, 3...
    mux = caps["dma"]["style"] == "mux"
    if mux:
        pool = caps["dma"]["mux_options"] or 0
        nxt = 0
        for p in picks:
            if p.dmaopt < 0:
                continue
            if pool and nxt >= pool:
                cfg.warnings.append(
                    f"{p.label}: only {pool} DMA channels exist on {target}; "
                    "assigned no DMA")
                p.dmaopt = -1
                continue
            p.dmaopt = nxt
            nxt += 1
        mux_next = nxt
    else:
        mux_next = 0
        for p in picks:
            if p.dmaopt >= 0:
                spec = dma_streams(caps, p.channel, p.dmaopt)
                if spec:
                    claimed.add(_stream_of(spec))

    # TIMER_PIN_MAP refers to pins by macro name, so a row naming a define that
    # was never emitted is an undeclared identifier at build time, not a missing
    # feature. Anything upstream may have dropped its define - an unresolvable
    # bus, a firmware-rejected pin - so check rather than assume.
    defined = {m.group(1) for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]+)",
                                               "\n".join(cfg.lines), re.M)}
    for p in list(picks):
        if p.label not in defined:
            cfg.warnings.append(
                f"{p.label} has a timer mapping but no pin define was emitted; "
                "the row is dropped, as it would not compile")
            picks.remove(p)

    if picks:
        width = max(len(p.label) for p in picks) + 1
        cfg.add("#define TIMER_PIN_MAPPING \\")
        for i, p in enumerate(picks):
            cont = " \\" if i < len(picks) - 1 else ""
            cfg.add(f"    TIMER_PIN_MAP( {i}, {p.label + ',':<{width}}"
                    f"{p.occurrence:>2}, {p.dmaopt:>2}){cont}")
        cfg.add()
        for p in picks:
            if p.source == "inferred":
                cfg.notes.append(f"{p.label} -> {p.channel} inferred (no annotation)")

    # ---- DMA and instances ----------------------------------------------
    adc_dev, adc_opt, notes = choose_adc(caps, adc_pins, claimed, mux_next)
    cfg.notes.extend(notes)
    if adc_dev and adc_opt is not None:
        cfg.define(f"{adc_dev}_DMA_OPT", str(adc_opt), width=29)
        cfg.add()

    # The hand-written configs put this straight after the DMA options.
    hse = resolve_hse(words, sym, caps, hse_mhz, cfg)
    if hse is not None:
        cfg.define("SYSTEM_HSE_MHZ", str(hse), width=29)
        cfg.add()

    if "beeper" in simple:
        # An NPN/transistor low-side driver sounds when the pin is driven high,
        # which is what BEEPER_INVERTED selects. Bare open-drain buzzers are the
        # exception and would need this removed.
        cfg.define("BEEPER_INVERTED", width=29)
        cfg.add()
        cfg.notes.append("BEEPER_INVERTED assumes a transistor low-side driver")

    if adc_dev and adc_dev != "ADC1":
        cfg.define("ADC_INSTANCE", adc_dev, width=29)
    if i2c_dev:
        n = i2c_dev[-1]
        if "baro" in parts and "baro" not in spi_groups:
            cfg.define("BARO_I2C_INSTANCE", f"I2CDEV_{n}", width=29)
        cfg.define("MAG_I2C_INSTANCE", f"I2CDEV_{n}", width=29)
    cfg.add()

    cfg.define("GYRO_1_ALIGN", gyro_align, width=29)
    for owner, define in INSTANCE_DEFINE.items():
        dev = resolved.get(owner, ("", {}))[0]
        if dev:
            cfg.define(define, dev, width=29)
    cfg.add()

    # ---- PINIO boxes -----------------------------------------------------
    for i, (net, _pin) in enumerate(pinios, start=1):
        # A switch feeding a regulator enable is held on by its own divider, so
        # boot-high (inverted) keeps the rail up and makes the box an off switch.
        inverted = "VTX" in net.upper()
        cfg.define(f"PINIO{i}_BOX", str(39 + i), width=29)
        cfg.define(f"PINIO{i}_CONFIG", "129" if inverted else "1", width=29)
        cfg.define(f"BOX_USER{i}_NAME", f'"{_box_name(net)}"', width=29)
        cfg.add()
        if inverted:
            cfg.notes.append(
                f"PINIO{i} ({net}) set to 129 = boot high; confirm the rail's "
                "default state with the vendor")

    # ---- defaults --------------------------------------------------------
    if "flash" in parts:
        cfg.define("DEFAULT_BLACKBOX_DEVICE", "BLACKBOX_DEVICE_FLASH", width=29)
    cfg.define("DEFAULT_DSHOT_BITBANG", "DSHOT_BITBANG_ON", width=29)
    if "adc_curr" in simple:
        cfg.define("DEFAULT_CURRENT_METER_SOURCE", "CURRENT_METER_ADC", width=29)
    if "adc_vbat" in simple:
        cfg.define("DEFAULT_VOLTAGE_METER_SOURCE", "VOLTAGE_METER_ADC", width=29)

    roles, role_notes = read_connector_roles(words)
    if roles:
        cfg.add()
        for define in ("MSP_UART", "SERIALRX_UART", "GPS_UART", "ESC_SENSOR_UART"):
            n = roles.get(define)
            if n and any(k[0] == n for k in uart):
                cfg.define(define, SERIAL_PORT_NAMES.get(n, f"SERIAL_PORT_UART{n}"),
                           width=29)
        cfg.notes.extend(role_notes)
        cfg.notes.append("UART roles come from connector silkscreen, not from "
                         "wiring; they are defaults a user can change")

    # A bus with only half its pins is legal but almost always means a net was
    # dropped, so say so next to the warning that explains why.
    for n in sorted({k[0] for k in uart}, key=int):
        have = {d for (i, d) in uart if i == n}
        if len(have) == 1:
            cfg.warnings.append(
                f"UART{n} has only {next(iter(have)).upper()} defined")
    if (i2c.get("scl") is None) != (i2c.get("sda") is None):
        cfg.warnings.append(
            f"I2C has only {'SCL' if i2c.get('scl') else 'SDA'} defined; "
            "the bus will not work until the other pin is resolved")

    if unknown:
        cfg.notes.append("nets with no config.h role: "
                         + ", ".join(f"{l.net}({l.pin})" for l in unknown))
    if res.unmapped:
        cfg.notes.append("unconnected pins: " + " ".join(res.unmapped))
    cfg.warnings.append(f"GYRO_1_ALIGN is a placeholder ({gyro_align}); "
                        "orientation cannot be read from a schematic")
    if "adc_curr" in simple:
        cfg.warnings.append("DEFAULT_CURRENT_METER_SCALE omitted; it depends on "
                            "the ESC shunt, not the FC")

    meta = {
        "target": target,
        "parts": {k: [vars(h) for h in v] for k, v in parts.items()},
        "agreement": res.agreement,
        "offset": res.offset,
        "links": [vars(l) for l in res.links],
        "unmapped": res.unmapped,
        "timers": [vars(p) for p in picks],
        "hse_mhz": hse,
        "firmware": fw["firmware"],
    }
    return cfg, meta


def _box_name(net: str) -> str:
    n = net.upper().replace("-", " ").replace("_", " ").strip()
    return "VTX PWR" if "VTX" in n else n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--board", required=True, help="BOARD_NAME")
    ap.add_argument("--manufacturer", required=True, help="MANUFACTURER_ID (4 chars)")
    ap.add_argument("--target", help="FC_TARGET_MCU (auto-detected if omitted)")
    ap.add_argument("--gyro-align", default="CW0_DEG")
    ap.add_argument("--hse-mhz", type=int,
                    help="HSE crystal frequency in MHz, when the vendor states "
                         "it and the sheet does not let it be read. Overrides "
                         "detection; ignored on families whose clock tree does "
                         "not derive from SYSTEM_HSE_MHZ.")
    ap.add_argument("--reference",
                    help="the sha256_... REFERENCE token issued by the Betaflight "
                         "team for a reviewed target. Cannot be computed locally; "
                         "without it no supported-target block is written.")
    ap.add_argument("--version", dest="fw_version",
                    help="VERSION directive (first firmware release the target "
                         "is valid for), e.g. 4.6.0")
    ap.add_argument("--trust-symbol", action="store_true",
                    help="emit nets that the symbol's AF list supports but "
                         "Betaflight's pin tables do not yet list. Use when you "
                         "intend to add the missing pin option to the firmware.")
    ap.add_argument("-o", "--outdir", type=Path,
                    help="write <outdir>/configs/<MANUFACTURER>/<BOARD>/config.h, "
                         "the layout the config repo uses - so <outdir> can be "
                         "passed straight to make as CONFIG_DIR")
    ap.add_argument("--print", dest="to_stdout", action="store_true")
    args = ap.parse_args()

    if not (DATA_DIR / "firmware.json").exists():
        raise SystemExit("data/firmware.json missing - run seed_firmware.py first")

    cfg, meta = build(args.pdf, args.board, args.manufacturer,
                      args.target, args.gyro_align, args.trust_symbol,
                      args.reference, args.fw_version, args.hse_mhz)
    text = "\n".join(cfg.lines).rstrip() + "\n"

    if args.to_stdout or not args.outdir:
        print(text)
    if args.outdir:
        # Mirror the config repo's manufacturer-grouped layout so the directory
        # works as-is with `make CONFIG=<board> CONFIG_DIR=<outdir>`.
        dest = args.outdir / "configs" / args.manufacturer / args.board / "config.h"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        print(f"wrote {dest}", file=sys.stderr)

    print(f"\ntarget {meta['target']}  agreement {meta['agreement']:.0%}  "
          f"offset {meta['offset']:+.2f}pt", file=sys.stderr)
    for cat, hits in sorted(meta["parts"].items()):
        shown = ", ".join(f"{h['marking']}->{h['driver']}"
                          + ("" if h["fitted"] else " [not fitted]") for h in hits)
        print(f"  {cat:6} {shown}", file=sys.stderr)
    for n in cfg.notes:
        print(f"  note: {n}", file=sys.stderr)
    for w in cfg.warnings:
        print(f"  WARN: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
