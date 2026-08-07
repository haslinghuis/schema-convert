#!/usr/bin/env python3
# This file is part of schema-convert.
#
# Copyright (C) 2026 Mark Haslinghuis
#
# schema-convert is free software. You can redistribute this software
# and/or modify this software under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version.
#
# schema-convert is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this software. If not, see <https://www.gnu.org/licenses/>.

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

The header also carries the provenance: which sheet of the submission the pin
map was read from, and which Betaflight revision validated the pins against.
The second one matters because a config generated against a patched tree looks
exactly like one generated against a release, and does not behave like it.
MANUFACTURER_ID is checked against the config repo's registry - loudly, but
never fatally; see resolve_manufacturer().

Usage:
    python genconfig.py <schematic.pdf> --board NAME --manufacturer ID [-o DIR]
    python genconfig.py <schematic.pdf> --board NAME --manufacturer ID --print
    python genconfig.py <schematic.pdf> ... --page 4    # multi-page submission
"""

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
import manufacturers  # noqa: E402
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
    # Some sheets name both at once - SPI3-FLASH_SCK, SPI2_OSD_SCK,
    # SPI1-ICM1_MOSI - which is the most informative spelling there is, and was
    # matched by neither the bus rule nor the device rule. The bus number in it
    # is redundant: assign_spi_buses resolves the instance from the pins through
    # the firmware map, and the pin decides what the label claims. So the device
    # is what is kept.
    (re.compile(r"^SPI\d[-_](?:GYRO|IMU|MPU|ICM)(\d)?[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"),
     "gyro_spi"),
    (re.compile(r"^SPI\d[-_](?:OSD|MAX7456|AT7456)[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"),
     "osd_spi"),
    (re.compile(r"^SPI\d[-_]FLASH[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "flash_spi"),
    (re.compile(r"^SPI\d[-_]BARO[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "baro_spi"),
    (re.compile(r"^SPI\d[-_]SD(?:CARD)?[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "sdcard_spi"),
    # Some sheets name the bus explicitly (SPI1_SCK) instead of naming the device
    # on it (GYRO-SCK). Take that at face value - it removes the guesswork.
    # The index sits on either side, as it does for the UARTs: SPI3_SCK and SCK3
    # are the same bus.
    # CLK is accepted here for the same reason the no-separator form below
    # accepts it: on a bus named SPIn it is that bus's clock, not the SDIO one.
    # Only the separator form was missing it, which cost two boards a whole SPI4
    # over the single net SPI4_CLK_PIN.
    (re.compile(r"^SPI(\d)[-_](SCK|SCLK|CLK|MISO|MOSI|SDI|SDO)$"), "spi_bus"),
    # A chip select named after its bus rather than its device. The digit is the
    # bus; which device it selects is read at the peripheral end, in
    # identify_bus_cs(). 41 nets over 15 boards, and on those boards nothing was
    # emitted for the devices at all.
    (re.compile(r"^SPI(\d)[-_](?:NSS|SS|CS)(\d)?$"), "bus_cs"),
    (re.compile(r"^(SCK|SCLK|MISO|MOSI|SDI|SDO)[-_]?(\d)$"), "spi_bus"),
    # ...and a third position for the same index. SPI2_SCK, SCK2 and SPI_SCK2
    # are one bus written three ways; the last was read as nothing, which left
    # boards with no SPI bus at all and every device on them unplaceable.
    (re.compile(r"^SPI[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)[-_]?(\d)$"), "spi_bus"),
    # ...and with no separator at all (SPI1CLK), or with the bus digit standing
    # for the whole name (1-SCK). CLK means SCK here, not the SDIO clock.
    (re.compile(r"^SPI(\d)(SCK|SCLK|CLK|MISO|MOSI|SDI|SDO)$"), "spi_bus"),
    (re.compile(r"^(\d)[-_](SCK|SCLK|CLK|MISO|MOSI|SDI|SDO)$"), "spi_bus"),
    # The separator before the index, which GYRO_1_CS and friends already
    # allow. One board draws MOTOR_1..MOTOR_8 and SERVO_1..SERVO_3 and every
    # one of them came out as a net with no config.h role - eleven pins, on a
    # board that reported 100% agreement on what was left.
    (re.compile(r"^MOTOR[-_]?(\d+)$|^M(\d+)$|^S(\d)$"), "motor"),
    (re.compile(r"^SERVO[-_]?(\d+)$"), "servo"),
    # What the rest of the world calls a motor output. Two more spellings, and
    # between them the largest single block of unclassified nets in the corpus:
    # ESCn_SIGNAL on 5 boards (26 nets) and PWMn on 5 more (18). Betaflight's
    # name for the thing is MOTORn; the sheet's is whatever the vendor's ESC
    # connector is labelled. The digit-required form is what keeps ESC_SERIAL
    # above out of this rule.
    (re.compile(r"^PWM[-_]?(\d+)$|^ESC[-_]?(\d+)(?:[-_](?:SIGNAL|SIG))?$"), "motor"),
    # A PPM receiver and the ESC 1-wire passthrough both drive a timer input
    # capture, so the pin has to have a timer channel - checked in build().
    (re.compile(r"^(?:RX[-_]?)?PPM(?:[-_]?(?:IN|SIG|SIGNAL))?$"), "rx_ppm"),
    (re.compile(r"^ESC[-_]?SERIAL$"), "escserial"),
    # The device index is captured on both sides of the name: sheets write the
    # second IMU as GYRO2-CS and as GYRO-CS2, and both mean GYRO_2_CS_PIN.
    # classify() folds it into the role, so a caller never has to remember which
    # group carried it.
    # Vendors also copy Betaflight's own define names onto their nets, which put
    # the index between separators: GYRO_1_CS, GYRO_1_CLKIN, MAX7456_SPI_CS.
    # The separator before the index is what the older patterns did not allow.
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)[-_]?(\d)?[-_]?CS[-_]?(\d)?$"), "gyro_cs"),
    # EXTI takes the index on either side too - GYRO2-EXTI and GYRO-EXTI2 are
    # one thing, 10 nets over 5 boards. Only EXTI: on INT the trailing digit is
    # the sensor's own interrupt line (INT1), not which sensor it is.
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)[-_]?(\d)?[-_]?(?:EXTI[-_]?(\d)?|INT1?)$"),
     "gyro_exti"),
    # The prefix is optional because one sheet names this net just CLKIN. The
    # MCU's own clock input is not a rival for it: that arrives on OSC_IN, which
    # the symbol names as a system pin and which never reaches classification.
    (re.compile(r"^(?:(?:GYRO|IMU)[-_]?(\d)?[-_]?)?(?:CLOCK|CLKIN)$"), "gyro_clkin"),
    # Only CLOCK and CLKIN were read, and the corpus spells this nine ways:
    # GYRO_CLK, GYRO1_CLK, GYRO_4_CLK, IMUCLK, CLK_IMU. Bare CLK is not one of
    # them and must not be - everywhere else in this file CLK means the SPI
    # clock, and the corpus also carries CLK_G473 for an MCU oscillator. So the
    # net has to say which device it belongs to, and the index sits on either
    # side of the prefix exactly as it does for CS and EXTI above.
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)[-_]?(\d)?[-_]?CLK$"), "gyro_clkin"),
    (re.compile(r"^CLK[-_]?(?:GYRO|IMU|MPU|ICM)[-_]?(\d)?$"), "gyro_clkin"),
    # The device index sits on either side here too, exactly as it does for the
    # chip select above: GYRO2-MISO and GYRO_MISO2 are the same second IMU.
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)[-_]?(\d)?[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)[-_]?(\d)?$"),
     "gyro_spi"),
    (re.compile(r"^(?:OSD|MAX7456|AT7456)(?:[-_]SPI)?[-_]?CS$"), "osd_cs"),
    (re.compile(r"^(?:OSD|MAX7456|AT7456)[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "osd_spi"),
    (re.compile(r"^FLASH(?:[-_]SPI)?[-_]?CS$"), "flash_cs"),
    (re.compile(r"^FLASH[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "flash_spi"),
    (re.compile(r"^BARO(?:[-_]SPI)?[-_]?CS$"), "baro_cs"),
    (re.compile(r"^BARO[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "baro_spi"),
    # The _SPI infix that FLASH, OSD and BARO above already allow - vendors copy
    # Betaflight's own SDCARD_SPI_CS_PIN onto the net.
    (re.compile(r"^SD(?:CARD)?(?:[-_]SPI)?[-_]?CS$"), "sdcard_cs"),
    # Without the data nets the card is a CS with nowhere to go, so
    # SDCARD_SPI_INSTANCE - and with it USE_SDCARD_SPI - can never be resolved.
    (re.compile(r"^SD(?:CARD)?[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)$"), "sdcard_spi"),
    # The other way a card is wired, and the one these boards actually use: the
    # MCU's SDMMC peripheral rather than a SPI bus. Every spelling in the corpus
    # - SDMMC1-CK, SDMMC2_D3, SDIO_CMD, SD_SDIO_CK, SD_CLK. The digit is the
    # controller (SDMMC2 -> SDIO_DEVICE SDIODEV_2), not a line number.
    (re.compile(r"^(?:SD[-_]?)?(?:SDIO|SDMMC)(\d)?[-_]?(CK|CLK|CMD|D[0-3])$", re.I),
     "sdio"),
    (re.compile(r"^SD[-_](CLK|CK|CMD|D[0-3])$", re.I), "sdio"),
    (re.compile(r"^SD(?:CARD)?[-_]?DET(?:ECT)?$", re.I), "sdcard_detect"),
    # Both orders occur, on comparable numbers of boards: TX4 / UART-TX4 put the
    # index last, UART4_TX and USART3_RX put it first. Only the first form was
    # recognised, so on a fifth of the corpus the UART nets - the most common
    # thing on a flight controller - reached the config as unclassified.
    (re.compile(r"^(?:.*[-_])?TX(\d)(?:[-_]?R)?$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?RX(\d)(?:[-_]?R)?$"), "uart_rx"),
    (re.compile(r"^(?:U?S?ART|SERIAL)[-_]?(\d)[-_]?TX(?:[-_]?R)?$"), "uart_tx"),
    (re.compile(r"^(?:U?S?ART|SERIAL)[-_]?(\d)[-_]?RX(?:[-_]?R)?$"), "uart_rx"),
    # TXD6/RXD6 - the D is for "data", and it is between the direction and the
    # index where nothing above allows anything. 19 nets over 4 boards.
    (re.compile(r"^(?:.*[-_])?TXD[-_]?(\d)$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?RXD[-_]?(\d)$"), "uart_rx"),
    # The bus is settled by the pins, not the name, so the index is optional -
    # plenty of sheets just write SCL / SDA, or SCL1 / SDA1.
    (re.compile(r"^(?:I2C[-_]?(\d)?[-_]?)?SCL[-_]?(\d)?$"), "i2c_scl"),
    (re.compile(r"^(?:I2C[-_]?(\d)?[-_]?)?SDA[-_]?(\d)?$"), "i2c_sda"),
    # Both orderings appear in the wild: ADC-BATT and VBAT_ADC.
    # The sense nets arrive with the index and the qualifier in every position:
    # ADC_BATT, VBAT-ADC1, VBATT-ADC, BATT_VOLTAGE, BAT1_V. VBUS is deliberately
    # not in here - that is USB detect, not the battery.
    (re.compile(r"^(?:ADC[-_])?V?BATT?\d?(?:[-_](?:ADC|VOLTAGE|VOLT|V))?[-_]?\d?$"),
     "adc_vbat"),
    (re.compile(r"^(?:ADC[-_])?(?:CURR|CURRENT|ISENSE|I?SENSE)\d?(?:[-_]ADC)?\d?$"),
     "adc_curr"),
    (re.compile(r"^(?:ADC[-_])?BATT?\d?[-_](?:CURRENT|CURR|I)$"), "adc_curr"),
    (re.compile(r"^(?:ADC[-_])?RSSI\d?(?:[-_]ADC)?\d?$"), "adc_rssi"),
    (re.compile(r"^LED[-_]?(?:STATUS|STAT)$|^LED[-_]?0$"), "led0"),
    (re.compile(r"^LED[-_]?1$"), "led1"),
    (re.compile(r"^LED[-_]?2$"), "led2"),
    (re.compile(r"^LED[-_]?STRIP$|^WS2812$|^LED[-_]?DATA$"), "led_strip"),
    (re.compile(r"^(?:BEEPER|BUZZER|BUZZ|BEEP|BZ)[-_]?(?:PIN)?$"), "beeper"),
    # CTRL as well as CONTROL, and CAM as well as CAMERA. A board submitted as
    # a PR carried CAM_CTRL and lost camera control entirely - the net was read,
    # classified as nothing, and the feature simply never appeared.
    (re.compile(r"^CAM(?:ERA)?[-_]?(?:CONTROLL?|CTRL)$|^CC$"), "camera_control"),
    (re.compile(r"^USB[-_]?DETECT$|^VBUS[-_]?DETECT$"), "usb_detect"),
    (re.compile(r"^VTX[-_]?SW$|^VTX[-_]?(?:PWR|POWER|EN)$"), "pinio"),
    (re.compile(r"^USER(\d)$|^PINIO(\d)$|^PIO(\d)$"), "pinio"),
    # A trailing switch/enable is a switched rail: CAM_SW, BEC-SWITCH, VTX_EN.
    # These are PINIO outputs, which is different from CAM-Controll (a PWM
    # camera-OSD line). Spelled out in full as well as abbreviated - one board
    # writes BEC-SWITCH, and losing it meant emitting only one of its two PINIOs.
    (re.compile(r"^[A-Z0-9]+[-_](?:SW|SWITCH|EN|ENABLE)$", re.I), "pinio"),
    (re.compile(r"^(?:FC[-_])?SW(?:DIO|CLK)$|^BOOT$|^OTG[+-]$|^DD?[+-]$|^NRST$"), "ignore"),
]


def classify(net: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    'GYRO-SCK'  -> ('gyro_spi', None, 'sck')
    'GYRO2-SCK' -> ('gyro2_spi', None, 'sck')
    'TX4'       -> ('uart_tx', '4', None)
    Returns (role, index, sub).

    A gyro's device index is folded into the role rather than returned as the
    index, because it names a different device, not a different instance of the
    same one: GYRO_2_CS_PIN is a second IMU with its own bus, chip select and
    interrupt. Everything downstream - including the regression suite's own
    re-derivation of the SPI groups - then keys off the role alone.
    """
    n = net.upper().strip()

    # Two spellings that mean nothing to the rules and everything to a reader,
    # normalised here rather than doubled into forty patterns.
    #
    # IIC is I2C. It is the older name for the same bus and vendors still use
    # it, and no rule matched it.
    #
    # A trailing _PIN is Betaflight's own define name copied onto the net -
    # LED0_PIN, UART1_TX_PIN, I2C1_SCL_PIN, SPI1_SDO_PIN. Across the corpus 91
    # unclassified nets end that way and 87 of them classify once it is gone,
    # which is the whole gap: the sheets were naming the target's defines and
    # the tool was not listening.
    n = re.sub(r"^IIC", "I2C", n)
    n = re.sub(r"[-_]PIN$", "", n) or n
    # Some sheets name a net after the part on the other end of it, with a dot:
    # ICM42688.CS, AT7456.SCK, W25Q128.MOSI. The dot is just a separator, and
    # the part number is the device - which the rules already know by family, so
    # the number is folded back to the family word rather than every variant
    # being listed. Only where a separator follows, so a bare part marking on
    # the sheet stays a part marking.
    n = n.replace(".", "_")
    for rx, family in DEVICE_FAMILIES:
        n2 = rx.sub(family, n, count=1)
        if n2 != n:
            n = n2
            break

    for rx, role in ROLE_RULES:
        m = rx.match(n)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        idx = next((g for g in groups if g.isdigit()), None)
        sub = next((g.lower() for g in groups if not g.isdigit()), None)
        if sub in ("sclk",):
            sub = "sck"
        if sub == "clk":
            # The SDMMC clock line is CK; everywhere else CLK is the SPI clock.
            sub = "ck" if role == "sdio" else "sck"
        if sub in ("miso",):
            sub = "sdi"
        if sub in ("mosi",):
            sub = "sdo"
        if role.startswith("gyro"):
            if idx and idx != "1":
                role = f"gyro{idx}{role[len('gyro'):]}"
            idx = None
        return role, idx, sub
    return None, None, None


# The gyro owners this generator emits, in classify()'s vocabulary. Betaflight's
# gyrodev.c carries GYRO_1..GYRO_4, but only a second IMU is common enough to be
# worth inferring, and nothing in reach can be used to check a third.
GYRO_OWNERS = frozenset({"gyro", "gyro2"})


# --------------------------------------------------------------------------- #
# Firmware array limits
# --------------------------------------------------------------------------- #
#
# Betaflight's pin tables are fixed-size arrays, and the size is part of what
# "firmware is the single source of truth" means. `uartHardware[].txPins[]` is
# UARTHARDWARE_MAX_PINS long - 5 on H5, 4 on F4/F7, 3 on G4 - so a UART whose
# list is already full has nowhere to put another pin, however capable the
# silicon is and however clearly the datasheet lists it.
#
# That is not hypothetical: an H5 UART4 already carries 5 tx and 5 rx pins, so
# two perfectly valid datasheet pins cannot be added to that family without
# raising the macro for every UART on it. The advice "add the pin to the
# firmware table" is wrong there, and a reviewer should learn that before
# starting rather than after.
#
# seed_firmware.py harvests the ceilings; older data has no `limits` key at all,
# so every read goes through `limit()` and a missing value simply switches the
# check off rather than inventing one.

def limit(caps: dict, macro: str) -> Optional[int]:
    """A harvested firmware array ceiling, or None when the data cannot say."""
    value = (caps.get("limits") or {}).get(macro)
    return value if isinstance(value, int) and value > 0 else None


def _uart_pins(caps: dict, dev: str, direction: str) -> List[str]:
    return sorted(p for p, ents in caps["uart"].items()
                  if any(e["dev"] == dev and e["dir"] == direction for e in ents))


def _i2c_pins(caps: dict, dev: str, role: str) -> List[str]:
    return sorted(p for p, ents in caps["i2c"].items()
                  if any(e["dev"] == dev and e["role"] == role for e in ents))


def seed_exceeds_limits(caps: dict) -> List[str]:
    """
    Peripherals whose seeded pin list is longer than the firmware array holding
    it. Empty on every target today, and that is the point: it is a check on the
    capability data rather than on the board.

    A table that overflows its own array means the harvest is counting rows this
    build cannot compile - most likely rows from a guarded variant of the table
    for a sibling MCU - and every pin it offers for that peripheral is suspect.
    Reported, not acted on: the fix is in seed_firmware.py, not here.
    """
    out: List[str] = []
    for macro, table, key, kinds in (
            ("UARTHARDWARE_MAX_PINS", "uart", "dir", ("tx", "rx")),
            ("I2C_PIN_SEL_MAX", "i2c", "role", ("scl", "sda"))):
        ceiling = limit(caps, macro)
        if ceiling is None:
            continue
        counts: Dict[Tuple[str, str], int] = defaultdict(int)
        for entries in caps[table].values():
            for e in entries:
                if e[key] in kinds:
                    counts[(e["dev"], e[key])] += 1
        for (dev, kind), n in sorted(counts.items()):
            if n > ceiling:
                out.append(
                    f"the seeded tables give {dev} {n} {kind} pins but {macro} is "
                    f"{ceiling}: either the firmware table initialises more rows "
                    f"than its array holds - in which case the surplus never "
                    f"reaches the build - or the harvest is merging two guarded "
                    f"variants of it. {dev}'s pin list cannot be trusted either "
                    "way; resolve it before using this board's "
                    f"{kind.upper()} pins")
    return out


def table_is_full(caps: dict, net: str) -> Optional[str]:
    """
    Why a rejected net may be more than a missing table row, or None.

    A pin Betaflight's tables lack is usually a one-line addition. It is not when
    the array the row would go into is already at its compile-time ceiling: that
    is a change to every target in the family, not to one table. Reported as
    part of the rejection, since it changes what the fix is.

    Only the peripherals whose ceiling is harvested are answerable - UART and
    I2C. A net of any other kind returns None rather than a guess.
    """
    role, idx, _sub = classify(net)
    if role in ("uart_tx", "uart_rx"):
        # uartHardware[].txPins[UARTHARDWARE_MAX_PINS], and the macro is set per
        # family in platform.h - so raising it moves every UART on that family.
        direction = role[-2:]
        dev, macro = f"UART{idx or '1'}", "UARTHARDWARE_MAX_PINS"
        pins, array = _uart_pins(caps, dev, direction), f"{direction}Pins[]"
        scope = f"every UART on {caps['family']}"
    elif role in ("i2c_scl", "i2c_sda"):
        # i2cHardware[].sclPins[I2C_PIN_SEL_MAX], in drivers/bus_i2c_impl.h,
        # which is one value for every STM32 family at once.
        sub = role[-3:]
        dev, macro = f"I2C{idx or '1'}", "I2C_PIN_SEL_MAX"
        pins, array = _i2c_pins(caps, dev, sub), f"{sub}Pins[]"
        scope = "every I2C bus Betaflight builds"
    else:
        return None

    ceiling = limit(caps, macro)
    if ceiling is None or len(pins) < ceiling:
        return None
    if len(pins) > ceiling:
        # The seeded table can never legitimately exceed the array it was read
        # out of. If it does, the harvest is picking up rows this build cannot
        # compile, and the capability data - not the board - is what to look at.
        return (f"{dev}'s {array} is seeded with {len(pins)} pins but {macro} is "
                f"{ceiling}; the capability data is reading rows the firmware "
                "array cannot hold, so re-seed before trusting any of this")
    return (f"{dev}'s {array} is full - it holds all {ceiling} pins {macro} "
            f"allows - so this is not a row to add: it needs {macro} raised for "
            f"{scope}, which is a much larger firmware change")


# --------------------------------------------------------------------------- #
# MANUFACTURER_ID
# --------------------------------------------------------------------------- #

def resolve_manufacturer(raw: str, cfg: "Config") -> Tuple[str, str]:
    """
    Check --manufacturer against the committed registry snapshot.

    Returns (id to emit, one line for the generated header).

    Never fatal, and that is a decision rather than an omission. Rejecting an
    unregistered id would be wrong in both directions:

      * `CUST` and its siblings are registered placeholders - homebrew targets
        are supposed to use them, and this tool's own examples do;
      * the registry here is a *snapshot*. A manufacturer that registered after
        it was taken is legitimately absent, and so is one whose registration
        PR is travelling alongside the board submission. Failing hard would
        block a real board on stale local data.

    What it must not do is stay quiet. The configurator will not offer a target
    whose id is not in the registry, so an unregistered id produces a board
    nobody can install - and that surfaces only after the config has merged.
    So: loud, specific, with near-misses and with the snapshot's own date, which
    is the fact that decides whether the answer is "fix the id" or "refresh the
    snapshot".
    """
    normalised = manufacturers.normalise(raw)
    try:
        reg = manufacturers.load()
    except manufacturers.RegistryError as exc:
        cfg.warnings.append(
            f"MANUFACTURER_ID {normalised or raw!r} was not checked against the "
            f"registry: {' '.join(str(exc).split())}")
        return normalised or raw, f"Manufacturer: {normalised or raw} (not validated)"

    src = reg.source
    where = (f"betaflight/config {manufacturers.REGISTRY_FILE} @ "
             f"{src.get('rev', '?')} ({src.get('commit_date', '?')})")
    res = reg.check(raw)

    if res.ok and not res.reserved:
        cfg.notes.append(f"MANUFACTURER_ID {res.id} = {res.name}, registered in {where}")
        return res.id, f"Manufacturer: {res.id} - {res.name}"

    if res.ok:
        cfg.notes.append(
            f"MANUFACTURER_ID {res.id} is one of the registry's placeholder ids "
            f"({', '.join(manufacturers.RESERVED_IDS)}) - right for a homebrew "
            "target, but a board built by a manufacturer wants that "
            "manufacturer's own registered id")
        return res.id, f"Manufacturer: {res.id} - {res.name} (registry placeholder)"

    cfg.warnings.append(
        f"{res.message()}. The configurator only offers targets whose "
        f"MANUFACTURER_ID is registered, so this target would be invisible once "
        f"merged. Snapshot: {where} - if the manufacturer registered after that, "
        f"or is registering with this submission, refresh it "
        f"(mcu-parser/manufacturers.py --refresh) rather than changing the id; "
        f"otherwise use the registered id, or CUST for a homebrew board. "
        f"Emitted as given - nothing here can decide which of those it is")
    return res.id or normalised or raw, f"Manufacturer: {res.id} - NOT REGISTERED in {where}"


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


I2C_LINE_RE = re.compile(r"(?:I2C[-_]?(\d)[-_]?)?(SCL|SDA)[-_]?(\d)?$", re.I)


def i2c_bus_for(part: "PartHit", words: Sequence[Word], mcu_labels: Sequence[Word],
                buses: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Which I2C bus a part sits on, read from the nets drawn around it.

    An I2C device has no chip select, so the trick that settles SPI (§3.3) does
    not apply - there is no per-device net to follow. What there is instead is
    the part itself: a baro marked BMP280 is drawn with its SCL and SDA beside
    it, and on a board with two buses those nets say which one.

    Same discipline as trace_cs_bus: the nearest bus must be clearly nearer than
    the next, MCU-side labels are excluded, and where it is not decisive it
    returns None so the caller keeps the default rather than guessing.
    """
    if len(buses) < 2:
        return None, None
    seen = {id(w) for w in mcu_labels}
    ranked: List[Tuple[float, str]] = []
    for dev in buses:
        n = dev[-1]
        best = None
        for w in words:
            if id(w) in seen or w.page != part.page:
                continue
            m = I2C_LINE_RE.fullmatch(w.text)
            if not m or (m.group(1) or m.group(3)) != n:
                continue
            d = ((w.x0 - part.x) ** 2 + (w.y0 - part.y) ** 2) ** 0.5
            best = d if best is None else min(best, d)
        if best is not None:
            ranked.append((best, dev))
    if not ranked:
        return None, None
    ranked.sort()
    near, dev = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else float("inf")
    if runner < near * 3:
        return None, (f"{part.marking} sits between two I2C buses "
                      f"({dev} {near:.0f}pt, {ranked[1][1]} {runner:.0f}pt); "
                      "which one it is on cannot be told from the sheet")
    rival = "no other bus is drawn near it" if runner == float("inf") else \
            f"next bus {runner:.0f}pt"
    return dev, (f"{part.marking} is drawn among {dev}'s nets "
                 f"({near:.0f}pt to the nearest, {rival})")


def _pins_of(pins: Dict[str, str]) -> str:
    return "/".join(pins[r] for r in ("scl", "sda") if pins.get(r)) or "?"


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
    "gyro2": "GYRO_2_SPI_INSTANCE",
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


# What each function needs of the timer, and where firmware says so. The period
# and prescaler belong to the whole TIM unit - timerConfigure(timHw, period, hz)
# and pwmOutputConfig() both set them per unit, not per channel - so two
# functions sharing a unit have to want the same rate.
RATE_CLASS = (
    ("MOTOR", "motor", "its DShot or PWM protocol rate"),
    ("SERVO", "servo", "50Hz by default (servo_pwm_rate)"),
    ("LED_STRIP", "LED strip", "an 800kHz carrier (WS2811_CARRIER_HZ)"),
    ("CAMERA_CONTROL", "camera control", "CAMERA_CONTROL_PWM_RESOLUTION"),
    ("CLKIN", "gyro CLKIN", "clock/32000, a 32kHz square wave"),
    ("PPM", "RX capture", "a 1MHz timebase (PWM_TIMER_1MHZ)"),
    ("ESCSERIAL", "escserial", "a 1MHz timebase"),
)


def _rate_class(label: str) -> Optional[Tuple[str, str, str]]:
    for token, name, needs in RATE_CLASS:
        if token in label:
            return token, name, needs
    return None


def timer_rate_clashes(picks: Sequence["TimerPick"], caps: dict) -> List[str]:
    """
    Two functions wanting different rates from one TIM unit.

    Firmware does not catch this. timerAllocate() refuses only when *that pin's*
    entry already has an owner, and ownership is per pin rather than per unit -
    so two pins on one timer both allocate and whichever configures last wins
    the period. It builds and does not work, which is the shape CLAUDE.md 2 is
    about.

    Reported rather than rearranged. Where a pin carries several timer channels
    the picker could dodge this, but a board whose only LED-strip pin shares a
    unit with its only motor pin has no fix at all, and silently shuffling the
    ones that do would hide which is which. See ROADMAP 4.8.
    """
    units: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    needs: Dict[str, str] = {}
    for p in picks:
        got = _rate_class(p.label)
        if not got:
            continue
        token, name, why = got
        units[p.channel.split("_")[0]][name].append(p.label)
        needs[name] = why

    out: List[str] = []
    for unit, classes in sorted(units.items()):
        if len(classes) < 2:
            continue
        who = "; ".join(f"{n} ({', '.join(sorted(labels))}) wants {needs[n]}"
                        for n, labels in sorted(classes.items()))
        line = (f"{unit} is shared by functions that need different rates of it: "
                f"{who}. A timer's period is set for the whole unit, so only one "
                f"of them gets the rate it asked for")
        # The motor case is the common one and it is usually latent, so say so
        # rather than leaving a reader to discover it is a false alarm on their
        # board and stop believing the next one.
        if "motor" in classes:
            f4 = str(caps.get("family", "")).startswith(("STM32F4", "APM32F4"))
            line += (
                ". With DShot bitbang the motors drive GPIO from DMA and do not "
                "use this timer, which makes the clash harmless" +
                (" - but on this family DSHOT_BITBANG_AUTO only turns bitbang on "
                 "when DShot telemetry is enabled, so it bites without it"
                 if f4 else
                 ", and AUTO means bitbang on this family unless the protocol is "
                 "PROSHOT1000") +
                ". It bites either way if bitbang is turned off - and bitbang "
                "is not a blanket excuse: bbFindPacerTimer() still needs TIM1 or "
                "TIM8 with no non-motor owner on any channel, so parking one of "
                "these functions on both leaves it no pacer at all "
                "(betaflight/config#646)")
        out.append(line)
    return out


def timer_channel_collisions(picks: Sequence["TimerPick"]) -> List[str]:
    """
    Two functions driven from one timer *channel*.

    Worse than sharing a unit, and not the same check: a channel has one compare
    register, so both pins emit the same waveform. Two motors on one channel
    always spin together; a motor and an LED strip put the motor's signal on the
    data line.

    Found by counting the split for ROADMAP 4.8 part 2, and the rate-class
    report cannot see the worst of it - two motors are one class, so a board
    driving MOTOR4 and MOTOR6 from TIM2_CH3 passed that check cleanly.
    """
    by: Dict[str, List[str]] = defaultdict(list)
    for p in picks:
        by[p.channel].append(p.label)
    out = []
    for channel, labels in sorted(by.items()):
        if len(labels) < 2:
            continue
        out.append(
            f"{channel} drives {' and '.join(sorted(labels))}: one timer channel "
            "has one compare register, so both pins carry the same waveform. "
            "Give one of them another channel, or another pin")
    return out


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

    # An ADC row is a peripheral row, so its ceiling is MAX_PERIPHERAL_DMA_OPTIONS
    # rather than the timers' MAX_TIMER_DMA_OPTIONS - the two are different
    # arrays and differ on the fixed-mapping families (3 against 2 on F4/F7).
    per_max = limit(caps, "MAX_PERIPHERAL_DMA_OPTIONS")

    if caps["dma"]["style"] == "mux":
        dev = f"ADC{sorted(common)[0]}"
        pool = caps["dma"]["mux_options"] or 0
        bounds = [(n, why) for n, why in ((pool, f"the part has {pool} DMA channels"),
                                          (per_max, f"MAX_PERIPHERAL_DMA_OPTIONS is "
                                                    f"{per_max}")) if n]
        ceiling, reason = min(bounds) if bounds else (0, "")
        if ceiling and mux_next >= ceiling:
            return dev, None, notes + [
                f"{dev}: no DMA channel left to give it - {reason}"]
        return dev, mux_next, notes

    for n in sorted(common):
        dev = f"ADC{n}"
        opts = caps["dma"]["peripheral"].get(dev) or []
        if per_max:
            opts = opts[:per_max]
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


def assign_fixed_dma_options(cfg: "Config", caps: dict, picks: List["TimerPick"],
                             claimed: Set[str], target: str) -> None:
    """
    Give each timer row on a fixed-mapping part a DMA stream nobody else holds.

    `dmaopt` means something different here than on a DMAMUX part: it indexes
    that channel's own short list of possible streams, so repeated numbers are
    not in themselves wrong. What may not repeat is the *stream* they resolve
    to - and emitting 0 everywhere, which is what "the numbers are independent"
    invites, put MOTOR1/2/3 of one F7 board on TIM8_CH1/CH2/CH3 at option 0,
    all three of which are DMA2_S2_C0. With bitbang off, dmaAllocate() gives
    DMA2_S2 to the first motor and the other two never come up.

    Whether to renumber at all was the real question, because a generator that
    quietly disagrees with the hand-written configs is a problem of its own. The
    corpus settles it: across the 289 fixed-mapping configs in the config repo
    only 4 contain a stream collision, and their options are spread 0/1/2 (121,
    28 and 10 rows) - dodging is the convention, not a departure from it.
    SIMPLIFLYF405, a hand-written F4 config, has the layout that produced the
    collision - three motors on TIM8_CH1..CH3 - and writes option 1 on all
    three. Lowest-free gives 0/1/1 instead. Both resolve to three distinct
    streams, which is the property that matters; the numbers themselves do not.

    Rows are served most-constrained-first so a channel with a single option is
    not starved by one that had alternatives, and a row that cannot be given a
    free stream keeps option 0 and is reported. Silently writing -1 there would
    turn a stream conflict into a "no DMA" with nothing to explain it.
    """
    table = caps["dma"]["timer"]
    ceiling = limit(caps, "MAX_TIMER_DMA_OPTIONS")
    owner: Dict[str, str] = {s: "an earlier allocation" for s in claimed}

    def options(p: "TimerPick") -> List[str]:
        opts = table.get(p.channel) or []
        return opts[:ceiling] if ceiling else opts

    for i in sorted((i for i, p in enumerate(picks) if p.dmaopt >= 0),
                    key=lambda i: (len(options(picks[i])), i)):
        p = picks[i]
        opts = options(p)
        choice = next((n for n, spec in enumerate(opts)
                       if _stream_of(spec) not in owner), None)
        if choice is None:
            if not opts:
                cfg.warnings.append(
                    f"{p.label}: Betaflight's {target} table lists no DMA stream for "
                    f"{p.channel}, so its dmaopt 0 resolves to nothing. PWM and "
                    "bitbang DShot are unaffected; DMA-driven output on that pin is "
                    "not available")
            else:
                taken = ", ".join(sorted({f"{_stream_of(s)} is "
                                          f"{owner[_stream_of(s)]}'s" for s in opts}))
                cfg.warnings.append(
                    f"{p.label} on {p.channel}: every DMA option it has is already "
                    f"taken - {taken}. Option 0 is emitted anyway; DShot bitbang "
                    "(the emitted default) does not use timer DMA, but with "
                    "DSHOT_BITBANG_OFF this output would lose the stream race and "
                    "stay dead")
            p.dmaopt = 0
            continue
        p.dmaopt = choice
        if choice:
            cfg.notes.append(
                f"{p.label} takes DMA option {choice} ({opts[choice]}): option 0 is "
                f"{opts[0]}, and {_stream_of(opts[0])} is taken by "
                f"{owner[_stream_of(opts[0])]}")
        owner[_stream_of(opts[choice])] = p.label

    # What the ADC allocator must then dodge.
    claimed.update(owner)


# --------------------------------------------------------------------------- #
# Burst DShot
# --------------------------------------------------------------------------- #

def _note_dshot_burst(cfg: "Config", caps: dict,
                      tplan: Dict[str, Tuple[int, str, str]], target: str) -> None:
    """
    Say whether burst DShot is a candidate. Deliberately do not decide it.

    `DEFAULT_DSHOT_BURST` is used by 51% of the corpus and this generator emits
    none of it, which looks like a plain gap. It is not, and the reason is worth
    keeping, because "four motors on one timer, therefore burst" is exactly the
    inference that does not survive reading the firmware:

      * Burst drives TIMx_DMAR from the timer's *update* request, so the DMA it
        needs is `timerHardware->dmaTimUPRef` (pwm_output_dshot_hal.c). That
        field is filled in by the `upopt` argument of `DEF_TIM(...)` in
        `src/platform/STM32/timer_stm32*.c`, and every row of every STM32 table
        passes 0. On a DMAMUX part variant 0 of `DEF_TIM_DMA_FULL` is the same
        channel for every timer, so two burst timers collide - the second motor
        fails `dmaAllocate()` and never comes up.

      * `TIMUPn_DMA_OPT` is what a hand-written config uses to move that
        channel, and on STM32 it does not reach the driver. It feeds
        `timerUpConfig()` via `src/main/pg/timerup.c`, and the only readers of
        that PG are `cli.c`'s `dma` command and the X32 platform. The STM32
        DShot path never consults it. Emitting it would be emitting a value the
        build does not honour - the one thing this tool must not do - so it is
        left to a human, who can at least verify it against the board.

      * The hand-written reference for one of the boards in this corpus puts
        four motors on a single timer and still sets `DSHOT_DMAR_OFF` with
        `DSHOT_BITBANG_ON`. Across the corpus `DSHOT_DMAR_ON` appears on 6 of
        ~200 DMAMUX-family boards, against 185 on F4/F7 where each TIMx_UP has
        its own fixed stream.

    So the shared-timer layout is reported as an opportunity, with the caveat,
    and `DSHOT_BITBANG_ON` - which does not use timer DMA at all - stays the
    emitted default.
    """
    timers = {ch.split("_")[0] for _occ, ch, _src in tplan.values()}
    if len(timers) != 1 or len(tplan) < 2:
        return
    only = next(iter(timers))
    if caps["dma"]["style"] == "mux":
        cfg.notes.append(
            f"all {len(tplan)} motors are on {only}, the layout burst DShot "
            f"exists for - but on {target} every timer's update-DMA defaults to "
            "the same channel and TIMUPn_DMA_OPT does not reach the STM32 DShot "
            "driver, so DEFAULT_DSHOT_BURST is not emitted; bitbang is the "
            "default instead")
    else:
        cfg.notes.append(
            f"all {len(tplan)} motors are on {only}, so DEFAULT_DSHOT_BURST "
            "DSHOT_DMAR_ON is worth considering by hand; it is not emitted "
            "because nothing here can check that timer's update-DMA stream is "
            "free, and bitbang (the emitted default) does not need it")


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


# A part number standing in for the device it is. Matched only when a separator
# follows, so 'ICM42688' on its own is still a part marking for detect_parts and
# only 'ICM42688.CS' becomes a gyro chip select.
DEVICE_FAMILIES = (
    (re.compile(r"^(?:ICM|MPU|BMI|LSM|IIM|IAM)\d{3,5}[A-Z]*(?=[-_])"), "GYRO"),
    (re.compile(r"^(?:MAX|AT)7456[A-Z]*(?=[-_])"), "OSD"),
    (re.compile(r"^(?:W25[QNM]|GD25|MT25Q|PY25Q|MX66)[A-Z0-9]*(?=[-_])"), "FLASH"),
    (re.compile(r"^(?:BMP|DPS|SPL|LPS|ICP)\d{2,5}[A-Z]*(?=[-_])"), "BARO"),
)

SPI_LINE_RE = re.compile(r"SPI(\d)[-_](SCK|SCLK|MISO|MOSI|SDI|SDO)", re.I)

# 100K, 10k, 1M, 4K7, 13.7K, 100R, 100kΩ. The suffix carries the decimal point
# in the 4K7 spelling, which is why this is not just a number and a multiplier.
RESISTOR_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMR])(\d*)(?:Ω|OHM)?$", re.I)
DESIGNATOR_RE = re.compile(r"^R\d{1,3}$")
# What the top of a battery divider is tied to. GND is matched separately.
SUPPLY_RE = re.compile(r"^(?:VBAT\w*|VIN|BAT\+?|BATT|V\+|VCC|VDD|\+?\d{1,2}V\d?)$",
                       re.I)
# vbatscale is a uint8_t in voltage.h, so anything above this is not a scale and
# means the two resistors were misread.
MAX_VBAT_SCALE = 255


def resistor_ohms(text: str) -> Optional[float]:
    m = RESISTOR_RE.match(text)
    if not m:
        return None
    whole, mult, frac = m.group(1), m.group(2).upper(), m.group(3)
    value = float(f"{whole}.{frac}") if frac else float(whole)
    return value * {"R": 1.0, "K": 1e3, "M": 1e6}[mult]


def read_vbat_divider(words: Sequence[Word], mcu_labels: Sequence[Word],
                      net: str, pitch: float
                      ) -> Tuple[Optional[int], Optional[str]]:
    """
    DEFAULT_VOLTAGE_METER_SCALE, read off the divider the ADC net sits in.

    The scale is not a free parameter: voltage.c computes

        volts = adc * vbatscale * Vref / 10 / (0xFFF * vbatresdivval)

    which with the shipped vbatresdivval of 10 makes full scale 0.33 * vbatscale
    volts. A divider of ratio r puts the battery's full scale at 3.3 * r, so

        vbatscale = 10 * (R_top + R_bottom) / R_bottom

    exactly. 100K/10K gives 110, which is the firmware default and why so many
    boards need no define at all - and why a board with any other divider gets a
    silently wrong battery voltage if the default is left in place.

    The divider is read structurally rather than by taking the two nearest
    values: the ADC net is drawn at the midpoint, one resistor above it toward
    the battery and one below it toward ground. Picking the two nearest values
    instead reads whatever else is on that part of the sheet - on one board that
    was a filter resistor, and the answer would have been 223 rather than 110.
    """
    seen = {id(w) for w in mcu_labels}
    occ = [w for w in words
           if id(w) not in seen and w.text.upper() == net.upper()]
    if not occ:
        return None, (f"DEFAULT_VOLTAGE_METER_SCALE omitted: {net} is not drawn "
                      "anywhere but the MCU, so its divider is not on this sheet")

    reach = max(pitch * 8, 45.0)
    for h in sorted(occ, key=lambda w: (w.page, w.y0)):
        local = [w for w in words if w.page == h.page
                 and abs(w.x0 - h.x0) <= reach and abs(w.y0 - h.y0) <= reach]

        # A designator takes the value nearest to it; the two are drawn as a
        # pair, well inside the spacing between one resistor and the next.
        placed: List[Tuple[float, float, float]] = []      # x, dy, ohms
        for d in (w for w in local if DESIGNATOR_RE.match(w.text)):
            vals = [(abs(v.x0 - d.x0) + abs(v.y0 - d.y0), resistor_ohms(v.text))
                    for v in local if resistor_ohms(v.text)]
            vals = [(gap, ohms) for gap, ohms in vals if gap <= pitch * 2 + 6]
            if vals:
                placed.append((d.x0, d.y0 - h.y0, min(vals, key=lambda t: t[0])[1]))

        # The divider is two resistors on the *same vertical leg*, one either
        # side of the node. Without that constraint any unrelated resistor that
        # happens to be within reach joins in: on two boards here a third one
        # from a neighbouring circuit sat 34pt to the other side of the node,
        # and taking "one above, one below" alone found two candidates above.
        legs: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
        for x, dy, ohms in placed:
            legs[round(x / 4.0)].append((dy, ohms))
        pairs = [(sorted(v)[0], sorted(v)[-1], k) for k, v in legs.items()
                 if len(v) == 2 and sorted(v)[0][0] < 0 < sorted(v)[-1][0]]
        if len(pairs) != 1:
            continue
        (_, top), (_, bottom), leg = pairs[0]
        if not bottom:
            continue

        # Corroborate the orientation on that leg: the battery above it, or
        # ground below. Either alone is enough - one board labels its supply
        # and draws ground as a bare symbol with no text at all - and reading a
        # divider upside down turns 21:1 into 1.05:1, which the range check
        # below rejects on its own for any ratio a battery sense uses.
        legx = leg * 4.0
        anchored = any(
            (w.y0 > h.y0 and w.text.upper() == "GND")
            or (w.y0 < h.y0 and SUPPLY_RE.match(w.text))
            for w in local if abs(w.x0 - legx) <= reach)
        if not anchored:
            continue
        scale = round(10 * (top + bottom) / bottom)
        if not 20 <= scale <= MAX_VBAT_SCALE:
            return None, (f"DEFAULT_VOLTAGE_METER_SCALE omitted: the divider at "
                          f"{net} reads as {top:g}/{bottom:g} ohms, which is not "
                          "a usable scale - check the sheet")
        return scale, (f"DEFAULT_VOLTAGE_METER_SCALE {scale} from the divider at "
                       f"{net}: {top:g} ohms to the battery over {bottom:g} ohms "
                       f"to ground, so 10 x ({top:g}+{bottom:g})/{bottom:g}")

    return None, ("DEFAULT_VOLTAGE_METER_SCALE omitted: no resistor pair with "
                  f"values reads as a divider around {net} - sheets often give "
                  "the designators without the values, and the firmware default "
                  "of 110 is only right for a 100K/10K divider")


# Pin names that belong to one kind of chip and to nothing else on these sheets.
# Chosen for being distinctive: SCK, SDI, SDO and a bare CS are on every SPI part
# and say nothing about which one it is - a bare CS in this table was enough to
# make an OSD out of a gyro's chip select.
DEVICE_PINS = {
    "osd": re.compile(r"^(SDIN|SDOUT|CLKOUT|LOS|VSYNC|HSYNC|XFB|OSDBP)$", re.I),
    "flash": re.compile(r"^(DQ[0-3]|.*\(DQ[0-3]\)|WP#?|/WP|HOLD#?|/HOLD|CS#)$", re.I),
    "gyro": re.compile(r"^(FSYNC|INT[12]|AD0|IMU\d?(_INT)?|CLKIN|SENS_VDD)$", re.I),
    "baro": re.compile(r"^(CSB|BARO(_INT)?)$", re.I),
}
IMU_INDEX_RE = re.compile(r"^IMU(\d)", re.I)
# How far from the net's far end to read. A chip's own pin names are printed on
# its symbol, so this is the size of a symbol, not of the sheet.
DEVICE_REACH = 80.0


def identify_bus_cs(net: str, words: Sequence[Word], mcu_labels: Sequence[Word],
                    parts: Dict[str, List["PartHit"]]
                    ) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Which device a chip select named after its *bus* belongs to.

    Fifteen boards name every select `SPI1_NSS`, `SPI2_CS`, `SPI4_SS`. The bus is
    stated outright and the device is not, which is the exact inverse of
    trace_cs_bus() above - and on those boards nothing was emitted at all: one
    sheet has four buses, four detected devices, and not one `_CS_PIN` or
    `_SPI_INSTANCE`, a config declaring drivers it cannot reach.

    Read at the peripheral end, from the chip's own pin names. Proximity to a
    detected part marking was tried first and is not good enough - it put a
    select on a baro 350pt away while a gyro sat at 328pt, and "nearest" has no
    honest cutoff. A pin name is evidence of *what the chip is*: SDIN/CLKOUT/LOS
    is a MAX7456 and nothing else; DI(DQ0)/WP#/HOLD# is a SPI flash. Two
    independent tokens are required and a tie decides nothing, so a far end with
    no distinctive names beside it comes back undecided rather than guessed.

    The gyro index comes free where the sheet numbers its IMUs: a board naming
    IMU1_INT and IMU2_INT beside two selects says which is which.
    """
    mcu = {id(w) for w in mcu_labels}
    far = [w for w in words if w.text.upper() == net.upper() and id(w) not in mcu]
    score: Dict[str, set] = {k: set() for k in DEVICE_PINS}
    seen_index: Dict[str, int] = {}
    for w in far:
        for v in words:
            if v.page != w.page:
                continue
            if math.hypot(v.x0 - w.x0, v.y0 - w.y0) > DEVICE_REACH:
                continue
            for cat, rx in DEVICE_PINS.items():
                if rx.match(v.text):
                    score[cat].add(v.text.upper())
            if (m := IMU_INDEX_RE.match(v.text)):
                seen_index[m.group(1)] = seen_index.get(m.group(1), 0) + 1
        # The part marking counts for one token - corroboration, not the case.
        for cat, hits in parts.items():
            cat = "gyro" if cat == "acc" else cat
            if cat not in score:
                continue
            for h in hits:
                if h.fitted and h.page == w.page and \
                        math.hypot(h.x - w.x0, h.y - w.y0) <= DEVICE_REACH:
                    score[cat].add(f"part:{h.marking}")
    ranked = sorted(((len(v), k) for k, v in score.items()), reverse=True)
    top, cat = ranked[0]
    if top < 2 or (len(ranked) > 1 and top == ranked[1][0]):
        return None, None, sorted(t for v in score.values() for t in v)
    index = max(seen_index, key=seen_index.get) if seen_index and cat == "gyro" else None
    return cat, index, sorted(score[cat])


def trace_cs_bus(words: Sequence[Word], mcu_labels: Sequence[Word], cs_net: str,
                 buses: Dict[str, Dict[str, str]], pitch: float
                 ) -> Tuple[Optional[str], Optional[str]]:
    """
    Which bus a chip-select-only device joins, read from the other end of the wire.

    When a sheet names its buses generically - SPI2-SCK rather than OSD-SCK - the
    MCU side says only that some buses exist and that some device has a chip
    select. Which go together is drawn at the *device*, where the same net labels
    appear a second time: the part's SCK/SDI/SDO carry the bus names and its CS
    carries the chip-select name, all within a few points of each other.

    So the bus was never unknown, only stated elsewhere on the sheet. Find the
    chip-select away from the MCU and see whose lines it is sitting among. On one
    four-bus board each chip-select sits 9-32pt from its own bus and 211pt from
    the next, which is the kind of margin this can be decided on.

    Deliberately conservative, because a wrong bus is worse than none: the
    nearest bus must be several times nearer than the runner-up and have at
    least two of its three lines in the same cluster.

    Returns the bus *label*, never an instance. The instance is still settled
    from the pins by assign_spi_buses, so a sheet that mislabels its own bus
    cannot talk the generator past the firmware map - the same rule that stopped
    SPI roles being taken from net names.
    """
    seen = {id(w) for w in mcu_labels}
    outside = [w for w in words if id(w) not in seen]
    hits = [w for w in outside if w.text.upper() == cs_net.upper()]
    if not hits:
        return None, None

    lines: Dict[str, List[Word]] = defaultdict(list)
    for w in outside:
        m = SPI_LINE_RE.fullmatch(w.text)
        if m and f"SPI{m.group(1)}" in buses:
            lines[f"SPI{m.group(1)}"].append(w)
    if not lines:
        return None, None

    def gap(a: Word, b: Word) -> float:
        return ((a.x0 - b.x0) ** 2 + (a.y0 - b.y0) ** 2) ** 0.5

    best: Optional[Tuple[float, str, float, List[Word], Word]] = None
    for h in hits:
        ranked = sorted(
            (min(gap(h, w) for w in ws if w.page == h.page), bus, ws)
            for bus, ws in lines.items() if any(w.page == h.page for w in ws))
        if not ranked:
            continue
        near, bus, ws = ranked[0]
        runner = ranked[1][0] if len(ranked) > 1 else float("inf")
        if best is None or near < best[0]:
            best = (near, bus, runner, ws, h)
    if best is None:
        return None, None

    near, bus, runner, ws, h = best
    radius = max(near * 5, pitch * 3)
    own = sorted(gap(h, w) for w in ws if w.page == h.page)
    roles = {SPI_LINE_RE.fullmatch(w.text).group(2).lower()
             for w in ws if w.page == h.page and gap(h, w) <= radius}
    # Either the bus's lines cluster tightly around the chip select, or - when
    # the part is drawn as a large symbol with its pins spread around it - every
    # one of them is still nearer than anything belonging to another bus. A
    # flash chip is routinely the second shape: its CS and one data line sit
    # together while the other two come off the far side, 130pt away, which is
    # still a third of the distance to the next bus.
    clustered = len(roles) >= 2
    enclosed = len(own) >= 2 and own[-1] < runner
    if not (clustered or enclosed):
        return None, (f"{cs_net}: {bus} is the nearest bus where it is drawn away "
                      f"from the MCU, but only {len(roles)} of its lines are with "
                      "it and the rest are no nearer than another bus, so the "
                      "grouping is not clear enough to use")
    if runner < near * 3:
        return None, (f"{cs_net}: {bus} is nearest where it is drawn away from the "
                      f"MCU ({near:.0f}pt) but another bus is nearly as close "
                      f"({runner:.0f}pt), so which one it joins cannot be told")
    rival = ("no other bus is drawn near it" if runner == float("inf")
             else f"next bus {runner:.0f}pt")
    return bus, (f"{cs_net} is drawn among {bus}'s lines away from the MCU "
                 f"({near:.0f}pt to the nearest, {rival}), so the device shares "
                 "that bus")


# drivers/pinio.h: #define PINIO_COUNT 4
PINIO_COUNT = 4

TRANSISTOR_RE = re.compile(r"^Q\d{1,3}$")


def beeper_driver(words: Sequence[Word], mcu_labels: Sequence[Word], net: str,
                  pitch: float) -> Optional[str]:
    """
    The transistor designator drawn on the beeper net, if there is one.

    Corroboration only: BEEPER_INVERTED is emitted either way, because a sheet
    that does not show a transistor has not shown that there isn't one, and a
    beeper wired the other way round simply never sounds.
    """
    if not net:
        return None
    seen = {id(w) for w in mcu_labels}
    reach = max(pitch * 6, 35.0)
    for h in (w for w in words
              if id(w) not in seen and w.text.upper() == net.upper()):
        near = [(((w.x0 - h.x0) ** 2 + (w.y0 - h.y0) ** 2) ** .5, w.text)
                for w in words
                if w.page == h.page and TRANSISTOR_RE.match(w.text)]
        near = [t for t in near if t[0] <= reach]
        if near:
            return min(near)[1]
    return None


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

# Frequencies that are on these boards for something other than the MCU. 27 MHz
# is the MAX7456 OSD's crystal and appears on most of this corpus; across the
# 619 hand-written configs it is the SYSTEM_HSE_MHZ of exactly none of them
# (they are 8 MHz on 245 boards, then 16, 48, 25 and 24). 40 MHz belongs to an
# RF module. Neither is banned outright - a shared OSC net label is direct
# evidence and still wins - but neither may be claimed on proximity alone,
# which is what put a 27 MHz OSD crystal in front of the MCU on several sheets.
HSE_IMPLAUSIBLE_MHZ = {27, 40}

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
    page: int = 1


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
        near = [v for v in words if v.page == w.page
                and REFDES_RE.match(v.text) and _gap(w, v) < 20]
        ref = min(near, key=lambda v: _gap(w, v)).text if near else ""
        out.append(Crystal(mhz, w.text, ref, w.x0, w.x1, w.y0, w.page))
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
        # A row carries only its place along its own edge; the edge itself
        # supplies the other coordinate, and which is which depends on whether
        # that edge runs down the side of the symbol or across its top.
        part = next((p for p in sym.parts if r in p.rows), sym.parts[0])
        if netmap.ALONG[r.side] == "y":
            x = part.left_edge if r.side == "L" else part.right_edge
            out.append(Word(r.pin, x, r.pos, x, r.pos))
        else:
            y = part.top_edge if r.side == "T" else part.bottom_edge
            out.append(Word(r.pin, r.pos, y, r.pos, y))
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

    at_mcu = _osc_nets_near(words, anchors, radius=100.0)
    boxes = [Word(c.marking, c.x0, c.y, c.x1, c.y, c.page) for c in crystals]
    linked = [c for c, b in zip(crystals, boxes)
              if at_mcu & _osc_nets_near(words, [b], radius=40.0)]
    if len(linked) == 1:
        c = linked[0]
        return c, [f"{c.refdes or 'crystal'} {c.marking} -> {c.mhz:g} MHz, tied to "
                   f"the MCU by its OSC net label"]
    if len(linked) > 1:
        notes.append("more than one crystal carries an OSC net label: "
                     + ", ".join(f"{c.refdes or '?'} {c.marking}" for c in linked))

    # No net label, so proximity is the only evidence - and it is weak, which is
    # why what the crystal *is* has to be weighed with it. A 27 MHz part is the
    # OSD's and a 40 MHz one an RF module's; neither is the HSE of a single
    # board in the 619-config corpus, so neither may be claimed this way. On
    # four boards here the only crystal with a frequency was the OSD's, and
    # calling that "too far" described the symptom rather than the cause.
    page = sym.page
    usable = [(c, b) for c, b in zip(crystals, boxes)
              if c.page == page and round(c.mhz) not in HSE_IMPLAUSIBLE_MHZ]
    if not usable:
        on_page = [c for c in crystals if c.page == page]
        if not on_page:
            where = ", ".join(f"{c.refdes or '?'} {c.marking} on page {c.page}"
                              for c in crystals[:3])
            why = (f"every crystal with a frequency is on another sheet "
                   f"({where}), and a distance measured across sheets means "
                   "nothing - they share a coordinate space")
        else:
            why = ("the only crystal(s) on the MCU's own sheet are "
                   + ", ".join(f"{c.refdes or '?'} {c.marking}" for c in on_page[:3])
                   + " - 27 MHz is the OSD's and 40 MHz an RF module's, and "
                     "neither is the HSE of any board in the config repo")
        return None, notes + [
            f"no crystal on this sheet can be the MCU's HSE: {why}. Pass "
            "--hse-mhz if you know it"]

    # The old cut-off was 30x the symbol's row pitch, which is not a sheet
    # scale: pitch runs from 1.4pt to 18pt across this corpus, so the window
    # ranged from 43pt to 540pt and rejected an 8 MHz crystal sitting 214pt from
    # the OSC pins on one board while accepting anything at all on another. The
    # symbol's own height tracks the drawing scale instead, and the window is
    # generous because the frequency check above now carries the weight.
    span = max(sym.y_max - sym.y_min, sym.right_edge - sym.left_edge, 40.0)
    direct_max = 2.0 * span

    ranked = sorted(((min(_gap(b, a) for a in anchors), c) for c, b in usable),
                    key=lambda t: t[0])
    near = [(d, c) for d, c in ranked if d <= direct_max]
    if not near:
        d, c = ranked[0]
        return None, notes + [
            f"the nearest crystal the MCU could run on ({c.refdes or '?'} "
            f"{c.marking}) is {d:.0f}pt from its OSC pins, more than twice the "
            f"symbol's own size ({span:.0f}pt), so it cannot be attributed to it"]
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
    elif mhz in HSE_IMPLAUSIBLE_MHZ:
        # Only reachable through a shared OSC net label, which is direct
        # evidence and outranks the prior - but the prior is strong enough to
        # say so: 27 MHz is the OSD's part and 40 MHz an RF module's, and
        # neither is the HSE of any board in the 619-config repo. Either this
        # board is genuinely unusual or the net label is shared for another
        # reason, and only the vendor can say which.
        cfg.warnings.append(
            f"SYSTEM_HSE_MHZ {mhz} was taken from a crystal tied to the MCU's "
            f"OSC net, but {mhz} MHz is the OSD/RF frequency on these boards and "
            "is the HSE of no board in the config repo - confirm it with the "
            "vendor before shipping")


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
    # Where it is drawn. An I2C part carries no chip select, so the only way to
    # tell which bus it sits on is what is drawn around it - see i2c_bus_for().
    x: float = 0.0
    y: float = 0.0
    page: int = 1


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
    # Keep one position per distinct token - the first it is drawn at.
    where: Dict[str, Word] = {}
    for w in words:
        where.setdefault(w.text, w)
    tokens = sorted(where)
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
                at = where[tok]
                found[cat].append(
                    PartHit(driver, tok, not NOT_FITTED_RE.search(tok),
                            at.x0, at.y0, at.page))
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
    # Which defines were written, and with what, so a caller can ask what is
    # *not* here - and whether what is here has anything behind it - without
    # re-parsing the text it just produced.
    emitted: set = field(default_factory=set)
    values: Dict[str, str] = field(default_factory=dict)

    def value_of(self, name: str) -> str:
        return self.values.get(name, "")

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def define(self, name: str, value: str = "", width: int = 20) -> None:
        self.emitted.add(name)
        self.values[name] = value
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


# A branch whose tables can be taken as shipped or about to ship: master, and
# the release/maintenance branches (`4.6.0-maintenance`, `release/4.6`).
# Anything else is somebody's working tree, which is the case worth flagging.
RELEASE_BRANCH_RE = re.compile(r"^(?:master|main|release[/-].*|\d[\d.]*(?:[-_].*)?)$")


def firmware_provenance(fw: dict, trusted: Sequence[str]) -> List[str]:
    """
    Header lines recording which firmware these pins were validated against.

    A generated config is only as good as the pin tables it was checked against,
    and two runs of this tool a week apart can disagree about whether a pin
    exists. Recording the revision costs one line and answers "does this target
    need firmware that has not shipped yet?" - which has already been the
    question once, for a board that depended on an unmerged pin-table fix.

    The seeder also records the local path of the tree it read. That is left out
    on purpose: it identifies a developer's machine rather than the firmware,
    and this file travels.
    """
    info = fw.get("firmware") or {}
    rev = info.get("rev") or "unknown"
    branch = info.get("branch") or ""
    seeded = info.get("date") or ""
    stamp = ", ".join(p for p in (f"branch {branch}" if branch else "",
                                  f"seeded {seeded}" if seeded else "") if p)
    out = [f"Pin tables validated against Betaflight {rev}"
           + (f" ({stamp})" if stamp else "")]
    if branch and not RELEASE_BRANCH_RE.match(branch):
        out.append(f"    {branch} is a working branch, not a release one: if this "
                   "target relies")
        out.append("    on a pin only those tables carry, it will not work until "
                   "that change")
        out.append("    has merged.")
    if trusted:
        out.append("    Emitted with --trust-symbol: " + ", ".join(trusted))
        out.append("    Betaflight's tables do not list those pins. The firmware "
                   "needs the")
        out.append("    matching pin-table addition, or they will be silently "
                   "unused.")
    return out


# The functions worth listing as "not found" when they are absent. Named as
# functions rather than as defines - MOTOR6, not MOTOR6_PIN - because that is
# what the reader is being asked about; the _PIN belongs to config.h, and the
# emitter adds it.
#
# Deliberately not every function Betaflight knows: a list that cries about the
# SD-card detect on a board with no card slot teaches the reader to skim past
# it. These are the ones whose absence is nearly always a sheet the tool could
# not follow rather than a board that lacks the feature.
EXPECTED_FUNCTIONS = (
    "MOTOR1", "MOTOR2", "MOTOR3", "MOTOR4",
    "LED0", "BEEPER", "LED_STRIP",
    "UART1_TX", "UART1_RX",
    "GYRO_1_CS", "GYRO_1_EXTI",
    "ADC_VBAT", "ADC_CURR",
)

# What an absence actually costs, where firmware falls back to *nothing* rather
# than to something sensible. battery.c defaults the voltage source to
# VOLTAGE_METER_NONE and blackbox.c the device to BLACKBOX_DEVICE_NONE, so a
# board whose divider could not be read does not get a slightly worse config -
# it gets one with no battery voltage, no low-voltage warning and no logging,
# and nothing about it looks wrong. Naming the consequence is the difference
# between a list of missing pins and a reader knowing which ones matter.
CONSEQUENCE = {
    "ADC_VBAT": "no battery voltage and no low-voltage warning - "
                "DEFAULT_VOLTAGE_METER_SOURCE falls back to VOLTAGE_METER_NONE",
    "ADC_CURR": "no current or consumption reading - "
                "DEFAULT_CURRENT_METER_SOURCE falls back to virtual, MSP or none "
                "depending on build options",
    "GYRO_1_CS": "no gyro, so the target does not fly",
    "MOTOR1": "those motor outputs do not exist",
    "MOTOR2": "those motor outputs do not exist",
    "MOTOR3": "those motor outputs do not exist",
    "MOTOR4": "those motor outputs do not exist",
}

# A DEFAULT_* that only means anything if something else was emitted. Checked
# both ways: the absence is reported above, and asserting one of these with
# nothing behind it is a defect in its own right - the meter would read zero
# rather than report nothing.
DEFAULT_NEEDS = {
    ("DEFAULT_VOLTAGE_METER_SOURCE", "VOLTAGE_METER_ADC"): ("ADC_VBAT_PIN",),
    ("DEFAULT_CURRENT_METER_SOURCE", "CURRENT_METER_ADC"): ("ADC_CURR_PIN",),
    # USE_FLASH says the driver is compiled in; it says nothing about the chip
    # being reachable. A PR shipped FLASH_CS_PIN and BLACKBOX_DEVICE_FLASH with
    # no instance, so pg/flash.c left it NULL and logging was dead on arrival.
    ("DEFAULT_BLACKBOX_DEVICE", "BLACKBOX_DEVICE_FLASH"):
        ("USE_FLASH", ("FLASH_SPI_INSTANCE", "FLASH_QUADSPI_INSTANCE",
                       "FLASH_OCTOSPI_INSTANCE")),
    ("DEFAULT_BLACKBOX_DEVICE", "BLACKBOX_DEVICE_SDCARD"): ("USE_SDCARD",),
}


def _hand_placed(overrides: Dict[str, str], caps: dict) -> Tuple[List[Link], set]:
    """
    Turn `--set NAME=PIN` into the same thing the sheet would have given.

    A function is named as the sheet would name it - MOTOR6, UART3_TX,
    GYRO_1_EXTI - so an override is routed through `classify` rather than
    through a second table of its own, which means it supports every spelling
    the reader does and cannot drift away from it. A trailing _PIN is accepted
    and dropped, since that is how the same thing is spelled in config.h and it
    is an easy way to type it.

    Validated against the firmware map exactly as a read net is. Being told a
    pin by hand is not a reason to emit one the build cannot honour.
    """
    out: List[Link] = []
    keys = set()
    for raw_name, raw_pin in overrides.items():
        name = raw_name.strip().upper()
        pin = raw_pin.strip().upper()
        if not netmap.PIN_RE.match(pin):
            raise SystemExit(f"--set {raw_name}: '{raw_pin}' is not a pin name "
                             "like PA5")
        net = re.sub(r"_PIN$", "", name)
        role, idx, sub = classify(net)
        if role is None or role == "ignore":
            raise SystemExit(
                f"--set {raw_name}: not a function this tool knows how to "
                "place. Name it as the sheet would, e.g. MOTOR6, UART3_TX, "
                "GYRO_1_EXTI, SPI2_SCK")
        req = netmap.net_requirement(net)
        if req and not netmap.pin_supports(caps, pin, req[0], req[1]):
            # Stated flatly. Every STM32 family the seeder harvests has been
            # audited against its ST datasheet by afaudit.py, so the tables are
            # not the doubtful party here: the pin is wrong.
            raise SystemExit(
                f"--set {raw_name}={raw_pin}: {pin} cannot do "
                f"{req[0]}{req[1] or ''} on this MCU. The pin tables are "
                "audited against ST's datasheets, so check the pin")
        out.append(Link(net, pin, "", bool(req), True, [], True, None))
        keys.add((role, idx, sub))
    return out, keys


def build(pdf: Path, board: str, manufacturer: str, target: Optional[str],
          gyro_align: str, args_trust: bool = False,
          reference: Optional[str] = None,
          version: Optional[str] = None,
          hse_mhz: Optional[int] = None,
          page: Optional[int] = None,
          overrides: Optional[Dict[str, str]] = None) -> Tuple[Config, dict]:
    fw = json.loads((DATA_DIR / "firmware.json").read_text())
    aliases = json.loads(ALIAS_FILE.read_text())

    words = extract_words(pdf)
    target = target or netmap.detect_target(words, fw)
    if not target:
        raise SystemExit(netmap.describe_unreadable(words)
                         or netmap.describe_target_miss(words, fw)
                         or "Could not detect FC_TARGET_MCU: no part number on "
                            "the sheet matches a seeded target. Many sheets "
                            "simply never name the MCU - pass --target")
    caps = fw["targets"][target]

    sym = find_symbol(words, page=page)
    labels = find_net_labels(words, sym)
    res = netmap.resolve(sym, labels, caps)
    hints = read_timer_hints(words)
    parts = detect_parts(words, fw["drivers"], aliases)

    cfg = Config()

    # Sanity of the capability data itself, before any of it is believed.
    cfg.warnings.extend(seed_exceeds_limits(caps))

    # Which sheet the pin map came from. netmap prints this; genconfig did not,
    # so a five-page submission produced a config with no record of which sheet
    # was read - and no mention of a rival MCU symbol on another one, which is
    # the case where reading the wrong sheet is possible at all.
    sheet = netmap.describe_pages(sym)
    if sym.split_across_pages:
        cfg.notes.append(
            f"the MCU symbol is drawn across pages {'+'.join(str(p) for p in sym.pages)}"
            " and the halves were merged; check that they are one part")
    elif sym.split:
        cfg.notes.append(
            f"the MCU symbol is drawn as {len(sym.parts)} boxes on page {sym.page} "
            "and they were merged; check that they are one part")
    if sym.ignored_pages:
        cfg.warnings.append(
            f"page(s) {', '.join(str(p) for p in sym.ignored_pages)} also carry an "
            f"MCU symbol; page {sym.page} was read because it has the most GPIO "
            "pins. If that is the wrong sheet, re-run with --page")

    gaps: Set[str] = set()          # nets the firmware lacks but the symbol backs
    for l in res.links:
        if not (l.checked and not l.ok):
            continue
        # A pin the tables lack is usually one row to add. When the array that
        # row belongs to is already full, it is not - and the advice below would
        # send a reviewer down the wrong path without this.
        full = table_is_full(caps, l.net)
        ceiling = f". Note that {full}" if full else ""
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
                                         "to emit it)")
                + ceiling)
        elif l.symbol_ok is False:
            cfg.warnings.append(
                f"{l.net} on {l.pin}: neither Betaflight's {target} tables nor the "
                f"symbol's own AF list ({'/'.join(l.afs)}) support this - likely a "
                "schematic error; omitted" + ceiling)
        else:
            cfg.warnings.append(
                f"{l.net} is on {l.pin}, which Betaflight's {target} tables do not "
                "support for that function, and the symbol carries no AF list to "
                "corroborate it - omitted" + ceiling)

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
    # One entry per bus the sheet names, keyed by the index in the net name.
    # A single {scl, sda} could only ever hold one bus, so a board with I2C1 and
    # I2C2 lost whichever came first and the survivor wore the wrong name.
    i2c_named: Dict[str, Dict[str, str]] = defaultdict(dict)
    sdio: Dict[str, str] = {}
    spi_groups: Dict[str, Dict[str, str]] = defaultdict(dict)
    # (bus index, pin, net) for selects named after the bus - see identify_bus_cs
    bus_cs: List[Tuple[str, str, str]] = []
    spi_named: Dict[str, Dict[str, str]] = defaultdict(dict)   # bus stated on the sheet
    simple: Dict[str, str] = {}
    pinios: List[Tuple[str, str]] = []
    unknown: List[Link] = []
    # pin -> the net drawn on it. A diagnostic that explains why something was
    # left out has to name the net, not just the pin: that is what tells the
    # reader which wire on their sheet is affected, and it is what the
    # regression suite matches on when it checks that no net vanished without
    # either an emitted define or an explanation.
    net_of: Dict[str, str] = {}

    hand, hand_keys = _hand_placed(overrides or {}, caps)
    for l in hand:
        cfg.notes.append(f"{l.net} = {l.pin} supplied by hand, not read "
                         "from the sheet")
    for l in list(res.links) + hand:
        # Anything the firmware map rejected, or that landed on a supply pin, is
        # left out rather than emitted as a define that cannot work. Each one is
        # already recorded as a warning above.
        if not l.gpio:
            continue
        # A hand-placed value replaces whatever was read for the same role -
        # that is the point of supplying one - but say so, because a config that
        # silently drops what the sheet showed is worse than one that never read
        # it.
        if l not in hand and classify(l.net)[:3] in hand_keys:
            cfg.notes.append(f"{l.net} on {l.pin} was read from the sheet but "
                             "is replaced by the value supplied for it")
            continue
        if l.checked and not l.ok and not (args_trust and l.symbol_ok):
            continue
        role, idx, sub = classify(l.net)
        if role == "ignore":
            continue
        net_of[l.pin] = l.net
        if role and role.startswith("gyro") and role.split("_")[0] not in GYRO_OWNERS:
            # gyrodev.c carries GYRO_1..GYRO_4, but nothing here can check a
            # third IMU, so it is reported rather than half-emitted.
            cfg.warnings.append(
                f"{l.net} on {l.pin} names a third or later IMU; only GYRO_1 and "
                "GYRO_2 are generated - add it by hand")
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
            i2c_named[idx or ""][role[-3:]] = l.pin
        elif role == "spi_bus":
            spi_named[f"SPI{idx or '1'}"][sub or "sck"] = l.pin
        elif role.endswith("_spi"):
            spi_groups[role[:-4]][sub or "sck"] = l.pin
        elif role == "sdio":
            sdio[sub or "ck"] = l.pin
            sdio.setdefault("device", idx or "1")
        elif role == "bus_cs":
            bus_cs.append((idx or "1", l.pin, l.net))
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
    #
    # The rest of the block is the other half of the provenance, and it exists
    # because a config generated against a patched firmware tree used to look
    # exactly like one generated against a release. It did not behave like one:
    # a board depended on an unmerged pin-table fix, and nothing in the file
    # said so. Which tree validated these pins is a fact this tool holds, so it
    # is recorded - and, unlike REFERENCE, none of it is invented.
    digest = sha256(pdf.read_bytes()).hexdigest()
    manufacturer, mfr_line = resolve_manufacturer(manufacturer, cfg)
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
    cfg.add(f"    MCU symbol: {len(sym.rows)} pins{sheet}")
    cfg.add(f"    Converted: {date.today().isoformat()}")
    for line in firmware_provenance(fw, sorted(gaps) if args_trust else []):
        cfg.add(f"    {line}")
    cfg.add(f"    {mfr_line}")
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
    if "gyro_clkin" in simple or "gyro2_clkin" in simple:
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
    # ---- SD card over SDMMC ----------------------------------------------
    # The other way a card is wired, and the way every card-carrying board in
    # this corpus does it. Only emitted where the build would read it:
    # pg/sdio.c registers the pin config behind `#if ENABLE_SDIO_PIN_CONFIG`,
    # which common_post.h defaults to 0. On a target that leaves it off - F4,
    # F7, H730, H750 - these defines compile fine and are never read, which is
    # the silent-wrong-config case §1 exists to prevent.
    sdio_pins: Dict[str, str] = {}
    if any(k in sdio for k in ("ck", "cmd", "d0")):
        cap = caps.get("sdio") or {}
        lines = [k for k in ("ck", "cmd", "d0", "d1", "d2", "d3") if k in sdio]
        missing = [k for k in ("ck", "cmd", "d0") if k not in sdio]
        if not cap.get("driver"):
            cfg.warnings.append(
                f"the sheet wires an SD card over SDMMC ({', '.join(lines)}) but "
                f"{target} has no SDIO driver in Betaflight, so it cannot be "
                "used - the card needs wiring to a SPI bus instead")
        elif not cap.get("pin_config"):
            cfg.warnings.append(
                f"the sheet wires an SD card over SDMMC ({', '.join(lines)}) but "
                f"{target} does not set ENABLE_SDIO_PIN_CONFIG, so SDIO_*_PIN "
                "defines are compiled and never read. The pins are left out "
                "rather than emitted inert; the platform's own fixed SDMMC pins "
                "are what that target uses")
        elif missing:
            cfg.warnings.append(
                f"the SD card's SDMMC {'/'.join(missing).upper()} net is not on "
                "the sheet, so SDIO is not emitted - CK, CMD and D0 are the "
                "minimum even in 1-bit mode")
        else:
            sdio_pins = {k: sdio[k] for k in lines}

    if "osd" in parts:
        feats.append("USE_MAX7456")
    if "sdcard" in spi_groups:
        # A config.h target does not get USE_SDCARD for free. common_pre.h only
        # defines it inside `#if !defined(USE_CONFIG)`, which the config-repo
        # build path does define - so the SD chip select alone gave a card the
        # firmware never compiled a driver for. USE_SDCARD_SPI is what makes
        # pg/sdcard.c read SDCARD_SPI_INSTANCE and SDCARD_SPI_CS_PIN at all;
        # common_post.h #undefs it if USE_SDCARD is missing, so both are needed.
        feats += ["USE_SDCARD", "USE_SDCARD_SPI"]
    if sdio_pins:
        # USE_SDCARD_SDIO is *not* emitted: every target.h that has an SDMMC
        # controller already defines it inside `#ifdef USE_SDCARD`, and the
        # hand-written SDIO configs set neither. USE_SDCARD is still needed,
        # for the same reason as the SPI path above.
        feats.append("USE_SDCARD")
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

    # PPM capture and the ESC 1-wire passthrough are timer inputs, not plain
    # GPIO: both reach the pin through timerAllocate(), which only ever returns
    # a channel for a pin listed in TIMER_PIN_MAPPING (drivers/timer_common.c).
    # So the pin is resolved to a timer *before* its define is written - a
    # RX_PPM_PIN on a pin with no timer is a define the build cannot honour.
    # Net names imply nothing testable to netmap, so the check has to happen
    # here rather than being inherited from the agreement score.
    capture: Dict[str, Tuple[str, str, Tuple[int, str, str]]] = {}
    for role, label, hint_keys in (("rx_ppm", "RX_PPM_PIN", ("PPM", "RX-PPM")),
                                   ("escserial", "ESCSERIAL_PIN", ("ESCSERIAL",))):
        pin = simple.get(role)
        if not pin:
            continue
        hint = next((hints[k] for k in hint_keys if k in hints), None)
        got = pick_timer(caps, pin, hint, prefer_advanced=False)
        if got:
            capture[role] = (pin, label, got)
        else:
            cfg.warnings.append(
                f"{net_of.get(pin, label)} on {pin}: Betaflight's {target} timer "
                f"table has no channel there, and {label} is an input capture - "
                "omitted, since timerAllocate() could never hand the driver that "
                "pin")

    for n, pin in sorted(motors.items()):
        cfg.define(f"MOTOR{n}_PIN", pin)
    for n, pin in sorted(servos.items()):
        cfg.define(f"SERVO{n}_PIN", pin)
    if "rx_ppm" in capture:
        cfg.define("RX_PPM_PIN", capture["rx_ppm"][0])
    if motors or servos or "rx_ppm" in capture:
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
    # Every bus the sheet names, not just the last one to be read. The instance
    # still comes from the pins through the firmware map - the index in the net
    # name only says which nets belong together, and infer_i2c_bus reports it
    # when the two disagree.
    i2c_buses: List[Tuple[str, Dict[str, str]]] = []
    for declared, pins in sorted(i2c_named.items()):
        dev, notes = infer_i2c_bus(caps, pins.get("scl"), pins.get("sda"),
                                   declared or None)
        cfg.notes.extend(notes)
        if dev:
            i2c_buses.append((dev, pins))
    seen_dev: Dict[str, Dict[str, str]] = {}
    for dev, pins in i2c_buses:
        if dev in seen_dev:
            cfg.warnings.append(
                f"two sets of I2C nets both resolve to {dev} "
                f"({_pins_of(seen_dev[dev])} and {_pins_of(pins)}); only the "
                "first is emitted - check which pins the board really uses")
            continue
        seen_dev[dev] = pins
        n = dev[-1]
        if pins.get("scl"):
            cfg.define(f"I2C{n}_SCL_PIN", pins["scl"])
        if pins.get("sda"):
            cfg.define(f"I2C{n}_SDA_PIN", pins["sda"])
    i2c_buses = [(d, p) for d, p in i2c_buses if seen_dev.get(d) is p]
    i2c_dev = i2c_buses[0][0] if i2c_buses else None
    if i2c_buses:
        cfg.add()

    if sdio_pins:
        for k in ("ck", "cmd", "d0", "d1", "d2", "d3"):
            if k in sdio_pins:
                cfg.define(f"SDIO_{k.upper()}_PIN", sdio_pins[k])
        cfg.add()

    # ---- SPI buses -------------------------------------------------------
    # A device with only a chip select is not necessarily on an unknown bus: on
    # a sheet that names its buses generically, which device joins which is
    # drawn at the device rather than at the MCU. Hand the traced device that
    # bus's data pins and let assign_spi_buses name the instance from the pins,
    # exactly as it does for a device whose own nets were labelled.
    # A select named after its bus is the inverse: the bus is stated and the
    # device is not, so the device is read at the peripheral end and then handed
    # that bus's data pins, exactly as the traced case below is.
    for bus_index, pin, net in bus_cs:
        cat, index, evidence = identify_bus_cs(net, words, labels, parts)
        bus = f"SPI{bus_index}"
        if not cat:
            cfg.warnings.append(
                f"{net} on {pin} is a chip select on {bus}, but which device it "
                "selects is not readable at the far end of the net"
                + (f" (only {', '.join(evidence)} nearby)" if evidence else
                   " - nothing identifying is drawn there")
                + f". Add the device's _CS_PIN and _SPI_INSTANCE by hand, or "
                  f"--set <DEVICE>_CS={pin}")
            continue
        owner = f"{cat}2" if cat == "gyro" and index == "2" else cat
        if spi_groups.get(owner, {}).get("cs"):
            cfg.notes.append(f"{net} names a {owner} chip select on {bus}, but "
                             f"{owner} already has one - keeping the first")
            continue
        spi_groups[owner]["cs"] = pin
        for role, p in spi_named.get(bus, {}).items():
            spi_groups[owner].setdefault(role, p)
        cfg.notes.append(
            f"{net} on {pin} selects the {owner} on {bus}: the far end of that "
            f"net is drawn beside {', '.join(evidence)}")

    for owner, pins in spi_groups.items():
        if set(pins) != {"cs"}:
            continue
        cs_net = net_of.get(pins["cs"])
        if not cs_net:
            continue
        bus, note = trace_cs_bus(words, labels, cs_net, spi_named, sym.pitch)
        if note:
            cfg.notes.append(note)
        if bus:
            for role, pin in spi_named[bus].items():
                pins.setdefault(role, pin)

    # Emit in bus order so the file reads like the hand-written ones.
    assigned, notes = assign_spi_buses(caps, spi_groups)
    cfg.notes.extend(notes)
    resolved: Dict[str, Tuple[str, Dict[str, str]]] = {}
    for owner, pins in spi_groups.items():
        dev = assigned.get(owner)
        if dev:
            resolved[owner] = (dev, pins)
        elif set(pins) == {"cs"} and owner == "gyro2":
            # The second IMU is the one device whose CS pin cannot be emitted on
            # its own. common_pre.h counts gyros by which GYRO_n_CS_PIN exist, so
            # a lone GYRO_2_CS_PIN raises GYRO_COUNT to 2 and then gyrodev.c
            # leaves devconf[1] at BUS_TYPE_NONE - a second gyro slot wired to
            # nothing. And the bus cannot be guessed from gyro 1's: across the
            # 619-board corpus 66 of the 119 dual-gyro boards that name both
            # instances put the second IMU on a *different* SPI bus, so
            # "it shares gyro 1's" is wrong more often than right.
            resolved[owner] = ("", pins)
            cfg.warnings.append(
                f"a second IMU chip select ({net_of.get(pins['cs'], 'GYRO2-CS')}) "
                f"is on {pins['cs']} but the sheet "
                "gives it no SCK/SDI/SDO nets, so its SPI bus is unknown. "
                "GYRO_2_* is not emitted: GYRO_2_CS_PIN alone would raise "
                "GYRO_COUNT to 2 with no bus behind it. Add "
                "GYRO_2_SPI_INSTANCE, GYRO_2_CS_PIN and GYRO_2_EXTI_PIN by hand")
        elif set(pins) == {"cs"}:
            # CS-only: the device shares another bus but the sheet does not say
            # which, so emit the CS pin and leave the instance to the reviewer.
            resolved[owner] = ("", pins)
            cfg.warnings.append(
                f"{owner} has only a CS net on this sheet; set "
                f"{INSTANCE_DEFINE.get(owner, owner.upper() + '_SPI_INSTANCE')} by hand")
        else:
            cfg.warnings.append(f"could not resolve the SPI bus for {owner}")

    # SDCARD's chip select is SDCARD_SPI_CS_PIN, not SDCARD_CS_PIN: pg/sdcard.c
    # reads only the former, and no config in the 619-board corpus spells it the
    # other way. The wrong name compiled fine and did nothing - the build proves
    # nothing failure mode - so it is checked against the firmware here.
    CS_DEFINE = {
        "gyro": "GYRO_1_CS_PIN",
        "gyro2": "GYRO_2_CS_PIN",
        "osd": "MAX7456_SPI_CS_PIN",
        "flash": "FLASH_CS_PIN",
        "baro": "BARO_CS_PIN",
        "sdcard": "SDCARD_SPI_CS_PIN",
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
    #
    # The exception is the second IMU: see the CS-only branch above for why its
    # pins travel with its bus instead of being emitted regardless.
    gyro2 = bool(resolved.get("gyro2", ("", {}))[0])
    wrote_cs = False
    for owner, (_dev, pins) in sorted(resolved.items()):
        cs = pins.get("cs")
        if cs and owner in CS_DEFINE and (owner != "gyro2" or gyro2):
            cfg.define(CS_DEFINE[owner], cs)
            wrote_cs = True
    gyro_io = [("gyro_exti", "GYRO_1_EXTI_PIN"), ("gyro_clkin", "GYRO_1_CLKIN_PIN")]
    if gyro2:
        gyro_io += [("gyro2_exti", "GYRO_2_EXTI_PIN"),
                    ("gyro2_clkin", "GYRO_2_CLKIN_PIN")]
    elif "gyro2_exti" in simple or "gyro2_clkin" in simple:
        orphaned = [f"{net_of.get(simple[r], r)}({simple[r]})"
                    for r in ("gyro2_exti", "gyro2_clkin") if r in simple]
        cfg.warnings.append(
            f"{', '.join(orphaned)} belong to a second IMU that was not emitted, "
            "so they are left out with it")
    for role, name in gyro_io:
        if role in simple:
            cfg.define(name, simple[role])
            wrote_cs = True
    if wrote_cs:
        cfg.add()

    # ---- discrete IO -----------------------------------------------------
    IO_DEFINE = [
        ("led0", "LED0_PIN"), ("led1", "LED1_PIN"), ("led2", "LED2_PIN"),
        ("beeper", "BEEPER_PIN"), ("led_strip", "LED_STRIP_PIN"),
        ("camera_control", "CAMERA_CONTROL_PIN"),
        ("usb_detect", "USB_DETECT_PIN"),
        ("sdcard_detect", "SDCARD_DETECT_PIN"),
    ]
    wrote = False
    for role, name in IO_DEFINE:
        if role in simple:
            cfg.define(name, simple[role])
            wrote = True
    if "escserial" in capture:
        cfg.define("ESCSERIAL_PIN", capture["escserial"][0])
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

    # The numbering carries no meaning - a PINIO is just a pin the user can
    # toggle, and vendors assign the indices however they like - so this only
    # needs to be deterministic, not clever. drivers/pinio.h caps it at 4.
    pinios.sort(key=lambda t: (0 if "VTX" in t[0].upper() else 1, t[0]))
    if len(pinios) > PINIO_COUNT:
        cfg.warnings.append(
            f"{len(pinios)} PINIO nets but the firmware carries {PINIO_COUNT}: "
            f"{', '.join(n for n, _ in pinios[PINIO_COUNT:])} left out")
        pinios = pinios[:PINIO_COUNT]
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
    # Input captures need a row so timerAllocate() can find the pin, but never a
    # DMA option: the PPM and escserial drivers read the capture register from
    # the ISR. That is also how the corpus writes them - every RX_PPM_PIN row in
    # the config repo carries dmaopt -1.
    for _role, (pin, label, got) in sorted(capture.items()):
        if any(p.pin == pin for p in picks):
            continue
        picks.append(TimerPick(pin, label, got[0], got[1], -1, got[2]))
    timed_io = [("led_strip", "LED_STRIP_PIN", 0),
                ("camera_control", "CAMERA_CONTROL_PIN", -1),
                ("gyro_clkin", "GYRO_1_CLKIN_PIN", -1)]
    if gyro2:
        timed_io.append(("gyro2_clkin", "GYRO_2_CLKIN_PIN", -1))
    for role, label, dmaopt in timed_io:
        pin = simple.get(role)
        if not pin:
            continue
        hint = hints.get(role.replace("_", "-").upper()) or hints.get(
            {"led_strip": "LED-STRIP", "camera_control": "CAM-CONTROLL",
             "gyro_clkin": "GYRO-CLOCK", "gyro2_clkin": "GYRO2-CLOCK"}[role])
        got = pick_timer(caps, pin, hint, prefer_advanced=False)
        if got:
            picks.append(TimerPick(pin, label, got[0], got[1], dmaopt, got[2]))
        else:
            cfg.warnings.append(f"{label} on {pin} has no timer; LED strip needs one"
                                if role == "led_strip" else
                                f"{label} on {pin} has no timer function")

    # TIMER_PIN_MAP refers to pins by macro name, so a row naming a define that
    # was never emitted is an undeclared identifier at build time, not a missing
    # feature. Anything upstream may have dropped its define - an unresolvable
    # bus, a firmware-rejected pin - so check rather than assume.
    #
    # This runs before the DMA numbering below, not after: a dropped row must
    # not take a DMA channel with it, and must not appear in the reasoning for
    # why another row was moved.
    defined = {m.group(1) for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]+)",
                                               "\n".join(cfg.lines), re.M)}
    for p in list(picks):
        if p.label not in defined:
            cfg.warnings.append(
                f"{p.label} has a timer mapping but no pin define was emitted; "
                "the row is dropped, as it would not compile")
            picks.remove(p)

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
        # Two ceilings, and the lower one binds: the part has `mux_options`
        # channels, and dmaopt indexes dmaChannelSpec[], which the firmware
        # sizes at MAX_TIMER_DMA_OPTIONS. Numbering past either is a row the
        # build cannot resolve, so say which one ran out rather than emitting it.
        pool = caps["dma"]["mux_options"] or 0
        table_max = limit(caps, "MAX_TIMER_DMA_OPTIONS")
        bounds = [(n, why) for n, why in ((pool, f"{target} has {pool} DMA channels"),
                                          (table_max, f"MAX_TIMER_DMA_OPTIONS is "
                                                      f"{table_max}")) if n]
        ceiling, reason = min(bounds) if bounds else (0, "")
        nxt = 0
        for p in picks:
            if p.dmaopt < 0:
                continue
            if ceiling and nxt >= ceiling:
                cfg.warnings.append(
                    f"{p.label}: no DMA option left to give it - {reason}; "
                    "assigned no DMA")
                p.dmaopt = -1
                continue
            p.dmaopt = nxt
            nxt += 1
        mux_next = nxt
    else:
        mux_next = 0
        assign_fixed_dma_options(cfg, caps, picks, claimed, target)

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
        for line in timer_channel_collisions(picks):
            cfg.warnings.append(line)
        for line in timer_rate_clashes(picks, caps):
            cfg.warnings.append(line)

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
        # exception and would need this removed. 554 of the 582 configs in the
        # corpus that drive a beeper set it, so it stays the default.
        #
        # A transistor drawn on the beeper net corroborates it. Its absence does
        # not refute it: what a sheet omits is not evidence, and a wrong flip
        # here is a beeper that never sounds. So this only ever strengthens the
        # note - the define does not depend on it.
        cfg.define("BEEPER_INVERTED", width=29)
        cfg.add()
        q = beeper_driver(words, labels, net_of.get(simple["beeper"], ""),
                          sym.pitch)
        cfg.notes.append(
            f"BEEPER_INVERTED: {q} is drawn on the beeper net, which is the "
            "low-side driver it selects" if q else
            "BEEPER_INVERTED assumes a transistor low-side driver; no transistor "
            "was found on the beeper net, which does not mean there is none - "
            "remove it only if the buzzer is driven straight from the pin")

    if adc_dev and adc_dev != "ADC1":
        cfg.define("ADC_INSTANCE", adc_dev, width=29)
    if i2c_dev:
        # With one bus there is nothing to decide. With two, the baro is often
        # on the other one from the mag - so read each part's own nets rather
        # than putting both on the first bus, which was wrong on the one board
        # here with a hand-written config to check against.
        devs = [d for d, _ in i2c_buses]
        if "baro" in parts and "baro" not in spi_groups:
            hit = parts["baro"][0]
            found, why = i2c_bus_for(hit, words, labels, devs)
            if why:
                cfg.notes.append(f"BARO_I2C_INSTANCE: {why}")
            cfg.define("BARO_I2C_INSTANCE",
                       f"I2CDEV_{(found or i2c_dev)[-1]}", width=29)
        mag = parts.get("mag") or []
        found, why = i2c_bus_for(mag[0], words, labels, devs) if mag else (None, None)
        if why:
            cfg.notes.append(f"MAG_I2C_INSTANCE: {why}")
        cfg.define("MAG_I2C_INSTANCE", f"I2CDEV_{(found or i2c_dev)[-1]}", width=29)
        if len(devs) > 1 and not mag:
            cfg.warnings.append(
                f"this board has {len(devs)} I2C buses ({', '.join(devs)}) and no "
                f"magnetometer on the sheet; MAG_I2C_INSTANCE is set to "
                f"{i2c_dev} because an external compass has to go somewhere - "
                "check which header it is wired to")
    cfg.add()

    cfg.define("GYRO_1_ALIGN", gyro_align, width=29)
    if gyro2:
        # Same placeholder, same reason: a second IMU is usually rotated
        # differently from the first, and neither orientation is on the sheet.
        cfg.define("GYRO_2_ALIGN", gyro_align, width=29)
    # Only buses that were actually emitted. A device may resolve onto a bus
    # that the loop above then refused for being incomplete, and naming it here
    # anyway would contradict the warning that said it was not emitted: the
    # define compiles, but spiDeviceByInstance() hands back a bus whose pins
    # were never configured, so the device is wired to a dead peripheral.
    # Leaving it out is the outcome firmware already has a path for - pg/max7456.c
    # and pg/flash.c both default the instance to NULL - so the driver stays
    # compiled in and the CS pin stays emitted, ready for the bus to be filled
    # in by hand.
    for owner, define in INSTANCE_DEFINE.items():
        dev = resolved.get(owner, ("", {}))[0]
        if not dev:
            continue
        if dev not in emitted_buses:
            cfg.warnings.append(
                f"{define} is left out: {owner} is on {dev}, which was not "
                f"emitted; add {dev}'s three pins by hand and this define with "
                "them")
            continue
        cfg.define(define, dev, width=29)
    cfg.add()

    # ---- PINIO boxes -----------------------------------------------------
    # The config byte is two things at once: bit 0 puts the pin in push-pull
    # output mode and bit 7 inverts it. drivers/pinio.c switches only on the
    # mode bit, so leaving PINIOn_CONFIG out entirely never configures the pin
    # as an output at all and the box does nothing - a value has to be emitted.
    #
    # Which one is a board property that no schematic states. Shipped configs
    # use both, and at least one board inverts in hardware as well, so the net
    # name is not evidence either. 129 is emitted because of how the two fail:
    # pinioInit drives an inverted pin high at boot and a plain one low, so on a
    # switched rail 129 comes up powered and 1 comes up dead - which looks like
    # broken hardware rather than an inverted switch. It is a safe default, not
    # a reading of the sheet, and it is flagged as such on every PINIO.
    for i, (net, _pin) in enumerate(pinios, start=1):
        cfg.define(f"PINIO{i}_BOX", str(39 + i), width=29)
        cfg.define(f"PINIO{i}_CONFIG", "129", width=29)
        cfg.define(f"BOX_USER{i}_NAME", f'"{_box_name(net)}"', width=29)
        cfg.add()
        cfg.warnings.append(
            f"PINIO{i} ({net}) is set to 129 (boot high, box switches off). The "
            "polarity is a board property, not something a schematic shows - "
            "shipped configs use both 129 and 1, and some boards invert in "
            "hardware. Confirm it with the vendor; 1 boots the rail off")

    # ---- defaults --------------------------------------------------------
    # Which log device to default to. Both can be fitted; the corpus splits
    # almost evenly when they are (8 boards pick the card, 7 the flash), so the
    # card only wins when it is the only one. With just a card it is decisive:
    # 79 of the 85 card-only boards default to it.
    sdcard = bool(resolved.get("sdcard", ("", {}))[0]) or bool(sdio_pins)
    if "flash" in parts:
        cfg.define("DEFAULT_BLACKBOX_DEVICE", "BLACKBOX_DEVICE_FLASH", width=29)
        if sdcard:
            cfg.notes.append(
                "both an SD card and a flash chip are fitted; blackbox defaults "
                "to the flash - the corpus is split roughly evenly on that, so "
                "confirm the vendor's intent")
    elif sdcard:
        cfg.define("DEFAULT_BLACKBOX_DEVICE", "BLACKBOX_DEVICE_SDCARD", width=29)
    if "sdcard_detect" in simple:
        # A card-detect switch grounds the pin when a card is seated, so the
        # line reads low with a card in - which is what INVERTED means. 47 of
        # the 58 configs that wire one set it. Which way round the switch is
        # wired is not on the schematic any more than the beeper's driver is,
        # so this is a default with a reason, not a reading.
        cfg.define("SDCARD_DETECT_INVERTED", width=29)
        cfg.warnings.append(
            "SDCARD_DETECT_INVERTED is assumed: a detect switch normally "
            "grounds the pin when a card is seated, and 47 of the 58 configs "
            "that wire one set it. Remove it if this board's switch pulls high")
    if sdio_pins:
        # pg/sdio.c defaults SDIO_DEVICE to SDIOINVALID and SDIO_USE_4BIT to
        # false, so both have to be stated or the controller is never selected
        # and the card runs one-bit. The width comes from the sheet: four data
        # lines drawn means four wired.
        cfg.define("SDIO_DEVICE", f"SDIODEV_{sdio.get('device', '1')}", width=29)
        cfg.define("SDIO_USE_4BIT",
                   "1" if all(f"d{i}" in sdio_pins for i in range(4)) else "0",
                   width=29)
    cfg.define("DEFAULT_DSHOT_BITBANG", "DSHOT_BITBANG_ON", width=29)
    _note_dshot_burst(cfg, caps, tplan, target)
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
    for declared, pins in sorted(i2c_named.items()):
        if (pins.get("scl") is None) != (pins.get("sda") is None):
            cfg.warnings.append(
                f"I2C{declared or ''} has only "
                f"{'SCL' if pins.get('scl') else 'SDA'} defined; the bus will "
                "not work until the other pin is resolved")

    if unknown:
        cfg.notes.append("nets with no config.h role: "
                         + ", ".join(f"{l.net}({l.pin})" for l in unknown))
    if res.unmapped:
        cfg.notes.append("unconnected pins: " + " ".join(res.unmapped))

    # Name what is not there. The other two lines say what was seen and not
    # understood; this one says what a target normally has and this one does
    # not, which is the question a reader actually arrives with. Some sheets
    # genuinely do not carry these nets - they are on a page that was not
    # supplied, or drawn in a way nothing here follows - and then the only way
    # forward is to be told, so the line that reports the gap also says how.
    absent = [f for f in EXPECTED_FUNCTIONS if f"{f}_PIN" not in cfg.emitted]
    if absent:
        cfg.warnings.append(
            f"not produced from this sheet: {', '.join(absent)}. If the board "
            f"has them, supply each with --set NAME=PIN (e.g. --set "
            f"{absent[0]}=PA5); every value is checked against the firmware "
            "tables before it is emitted")
        # Spelled out only where firmware falls back to nothing. A list of names
        # says what is missing; this says which of them stop the board working.
        # Grouped by consequence: four motors missing is one fact, and four
        # near-identical lines is how a warning stops being read.
        grouped: Dict[str, List[str]] = defaultdict(list)
        for f in absent:
            if f in CONSEQUENCE:
                grouped[CONSEQUENCE[f]].append(f)
        for why, names in sorted(grouped.items(), key=lambda kv: kv[1]):
            cfg.warnings.append(f"  ...without {', '.join(names)}: {why}")

    # And the reverse. A source naming a peripheral that was never configured
    # reads zero rather than reporting nothing, which is worse than omitting it.
    for (name, value), needs in sorted(DEFAULT_NEEDS.items()):
        if name not in cfg.emitted or cfg.value_of(name) != value:
            continue
        # An element may be a tuple, meaning any one of those will do - a flash
        # is reachable over SPI, QUADSPI or OCTOSPI.
        lacking = [n if isinstance(n, str) else " or ".join(n) for n in needs
                   if (n not in cfg.emitted) if isinstance(n, str)
                   or not any(x in cfg.emitted for x in n)]
        if lacking:
            cfg.warnings.append(
                f"{name} is {value} but {', '.join(lacking)} was not emitted; "
                "the meter or device has nothing behind it and will read zero "
                "rather than report nothing")
    aligns = "GYRO_1_ALIGN and GYRO_2_ALIGN are" if gyro2 else "GYRO_1_ALIGN is a"
    cfg.warnings.append(f"{aligns} placeholder{'s' if gyro2 else ''} "
                        f"({gyro_align}); orientation cannot be read from a "
                        "schematic")
    if "adc_curr" in simple:
        cfg.warnings.append("DEFAULT_CURRENT_METER_SCALE omitted; it depends on "
                            "the ESC shunt, not the FC")
    if "adc_vbat" in simple:
        scale, why = read_vbat_divider(words, labels,
                                       net_of.get(simple["adc_vbat"], ""),
                                       sym.pitch)
        if scale is not None:
            cfg.define("DEFAULT_VOLTAGE_METER_SCALE", str(scale), width=29)
            cfg.notes.append(why)
        else:
            cfg.warnings.append(why)

    # The shape of a complete target, as a trailing comment block. A generated
    # file is judged by what it has, and nothing else in it says what a normal
    # one carries - so a reviewer diffing against a hand-written target has to
    # remember the list. Commented, because every one of these is a value the
    # sheet did not yield and inventing it is the failure this tool exists to
    # avoid; the point is that the gap is visible in the file, not only in a
    # report that may not travel with it.
    if absent:
        cfg.add()
        cfg.add("/*")
        cfg.add(" * Not produced from this sheet. If the board has them, fill in")
        cfg.add(" * and uncomment - or re-run with --set NAME=PIN, which checks")
        cfg.add(" * each value against the firmware tables first.")
        cfg.add(" *")
        for f in absent:
            note = CONSEQUENCE.get(f)
            cfg.add(f" * #define {f + '_PIN':<22}"
                    + ("P??" if not note else f"P??   // else {note.split(' - ')[0]}"))
        cfg.add(" */")

    meta = {
        "target": target,
        "manufacturer": manufacturer,      # canonical id, as emitted
        "parts": {k: [vars(h) for h in v] for k, v in parts.items()},
        "agreement": res.agreement,
        "offset": res.offset,
        "links": [vars(l) for l in res.links],
        "unmapped": res.unmapped,
        "timers": [vars(p) for p in picks],
        "hse_mhz": hse,
        "firmware": fw["firmware"],
        "limits": caps.get("limits") or {},
        "pages": sym.pages,
        "page_count": sym.page_count,
        "page_description": sheet,
        "ignored_pages": sym.ignored_pages,
        # Functions a target normally has that this sheet did not yield, and
        # the ones supplied by hand. As data, so a GUI can offer a box per
        # function instead of scraping them back out of a warning string.
        "absent": absent,
        "placed": {l.net: l.pin for l in hand},
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
    ap.add_argument("--page", type=int,
                    help="sheet holding the MCU symbol. Auto-detected as the "
                         "page with the most GPIO pins; pass this when a "
                         "multi-page submission draws the MCU somewhere else.")
    ap.add_argument("--gyro-align", default="CW0_DEG")
    ap.add_argument("--hse-mhz", type=int,
                    help="HSE crystal frequency in MHz, when the vendor states "
                         "it and the sheet does not let it be read. Overrides "
                         "detection; ignored on families whose clock tree does "
                         "not derive from SYSTEM_HSE_MHZ.")
    ap.add_argument("--set", dest="overrides", action="append", default=[],
                    metavar="NAME=PIN",
                    help="place a function the sheet did not give, e.g. --set "
                         "MOTOR6=PE11. Repeatable. NAME is the function as the "
                         "sheet would name it (a trailing _PIN is accepted); "
                         "the value is checked against the firmware tables and "
                         "refused if the pin cannot do the job, and it replaces "
                         "anything read for the same role. The report lists "
                         "what a target normally has and this sheet did not "
                         "produce.")
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
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="write the config and the whole report to stdout as "
                         "JSON, for a caller that is not a terminal")
    args = ap.parse_args()

    if not (DATA_DIR / "firmware.json").exists():
        raise SystemExit("data/firmware.json missing - run seed_firmware.py first")

    overrides = {}
    for item in args.overrides:
        if "=" not in item:
            raise SystemExit(f"--set {item}: expected NAME=PIN, e.g. "
                             "MOTOR6_PIN=PE11")
        k, v = item.split("=", 1)
        if k.strip().upper() in overrides:
            raise SystemExit(f"--set {k.strip()}: given twice")
        overrides[k.strip().upper()] = v

    cfg, meta = build(args.pdf, args.board, args.manufacturer,
                      args.target, args.gyro_align, args.trust_symbol,
                      args.reference, args.fw_version, args.hse_mhz,
                      args.page, overrides)
    text = "\n".join(cfg.lines).rstrip() + "\n"

    if args.as_json:
        # One structured document instead of a file plus a human report split
        # across two streams. A GUI needs the warnings as data - which pin, which
        # net - not as lines to scrape back out of stderr.
        json.dump({
            "config": text,
            "warnings": cfg.warnings,
            "notes": cfg.notes,
            "meta": meta,
        }, sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
        return 0

    if args.to_stdout or not args.outdir:
        print(text)
    if args.outdir:
        # Mirror the config repo's manufacturer-grouped layout so the directory
        # works as-is with `make CONFIG=<board> CONFIG_DIR=<outdir>`. The
        # directory takes the registry's spelling of the id, not the argument's,
        # so the path and MANUFACTURER_ID cannot disagree.
        dest = args.outdir / "configs" / meta["manufacturer"] / args.board / "config.h"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        print(f"wrote {dest}", file=sys.stderr)

    fwinfo = meta["firmware"]
    print(f"\ntarget {meta['target']}  agreement {meta['agreement']:.0%}  "
          f"offset {meta['offset']:+.2f}pt", file=sys.stderr)
    print(f"  symbol {meta['page_description'].lstrip() or 'on the only page'}, "
          f"firmware {fwinfo.get('rev', '?')} ({fwinfo.get('branch', '?')})",
          file=sys.stderr)
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
