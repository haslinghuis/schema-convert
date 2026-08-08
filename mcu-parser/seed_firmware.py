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
seed_firmware.py - Harvest Betaflight's hardware capability tables into JSON.

Not every schematic PDF spells out a pin's alternate functions. Altium symbols
sometimes carry them (`PA0/TIM2-CH1/TIM5-CH1/UART4-TX/ADC123-IN0`) and sometimes
just say `PA0`. The firmware, however, always knows: every peripheral-to-pin
possibility is enumerated in tables under `src/platform/`. This script parses
those tables so `genconfig.py` can resolve and validate a net map without
depending on the PDF being generous.

What it harvests, per MCU target (e.g. STM32F722):

  targets      FC_TARGET_MCU name -> TARGET_MCU / TARGET_MCU_FAMILY
  timers       pin -> ordered [TIMx_CHy, ...]   (the 1-based TIMER_PIN_MAP index)
  timer_dma    "TIM8_CH1" -> [dma option, ...]  (the TIMER_PIN_MAP dmaopt)
  periph_dma   "ADC1" -> [dma option, ...]      (the ADCn_DMA_OPT value)
  uart         pin -> [{dev, dir}, ...]
  spi          pin -> [{dev, role}, ...]
  i2c          pin -> [{dev, role}, ...]
  adc          pin -> {devices, channel}
  limits       firmware array ceiling -> int   (UARTHARDWARE_MAX_PINS, ...)
  drivers      category -> {part -> USE_ define}

Run this whenever the firmware moves. The output records the firmware git rev so
drift is visible; `genconfig.py` prints a warning when its data file is stale.

Usage:  python seed_firmware.py [--firmware PATH] [--out PATH] [--quiet]
Needs:  a Betaflight source tree (no compiler, no toolchain)
"""

import argparse
import json
import re
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Guards we deliberately refuse to assume, even though they start with USE_.
# USE_CONFIG would flip common_pre.h into "unified target" mode and USE_EXST is
# for externally-stored firmware; neither describes silicon capability.
GUARD_DENY = {"USE_CONFIG", "USE_EXST", "USE_SDCARD_SDIO"}

# Guards that expand the pin set beyond the default build. Entries needing one
# are kept but tagged, so genconfig can warn rather than silently emit a pin the
# board's build will not enable.
GUARD_NOTABLE = {"USE_EXTENDED_SPI_DEVICE", "USE_QUADSPI", "USE_OCTOSPI"}

PIN_RE = r"P[A-K]\d{1,2}"


# --------------------------------------------------------------------------- #
# C preprocessor guard tracking
# --------------------------------------------------------------------------- #

class GuardScanner:
    """
    Walks C source and yields (line, guards) where `guards` is the conjunction
    of #if conditions in force. #elif/#else branches carry the negation of the
    branches above them, so `#elif defined(STM32H7)` inside an `#if
    defined(STM32F4)` yields ['defined(STM32H7)', '!(defined(STM32F4))'].
    """

    def __init__(self, text: str):
        self.text = _strip_comments(text)

    def __iter__(self):
        # Each stack frame: (branches_taken_so_far, current_condition_or_None)
        stack: List[Tuple[List[str], Optional[str]]] = []
        for raw in self.text.splitlines():
            line = raw.strip()
            m = re.match(r"#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)", line)
            if m:
                kind, rest = m.group(1), m.group(2).strip()
                if kind == "ifdef":
                    stack.append(([], f"defined({rest})"))
                elif kind == "ifndef":
                    stack.append(([], f"!defined({rest})"))
                elif kind == "if":
                    stack.append(([], rest))
                elif kind in ("elif", "else"):
                    if stack:
                        prior, cond = stack[-1]
                        if cond is not None:
                            prior = prior + [cond]
                        stack[-1] = (prior, rest if kind == "elif" else None)
                elif kind == "endif":
                    if stack:
                        stack.pop()
                continue

            guards: List[str] = []
            for prior, cond in stack:
                guards.extend(f"!({p})" for p in prior)
                if cond is not None:
                    guards.append(cond)
            yield line, guards


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def guards_hold(guards: List[str], defs: set) -> Tuple[bool, List[str]]:
    """
    Decide whether a guard conjunction can hold for this target.

    The MCU/family macros are fixed facts (an F7 build is not an H7 build), but
    the USE_* macros are the board author's choice. So the question is not "is
    this guard true under some fixed guess" but "does ANY build of this MCU reach
    this line" - i.e. satisfiability over the free USE_* macros.

    That distinction matters. The I2C table is wrapped in
    `defined(USE_I2C) && !defined(USE_SOFT_I2C) && !defined(USE_I3C_AS_I2C)`;
    assuming every USE_* is defined makes it unreachable and silently drops
    every I2C pin on every MCU.

    Returns (reachable, notable_macros) where notable_macros lists any
    non-default switch the entry depends on, so callers can flag it.
    """
    exprs = [_to_python(g) for g in guards]
    free = sorted({
        ident
        for g in guards
        for ident in re.findall(r"\b(USE_[A-Z0-9_]+)\b", g)
        if ident not in defs
    })
    notable = [i for i in free if i in GUARD_NOTABLE]

    if len(free) > 12:  # runaway guard; assume reachable rather than blow up
        return True, notable

    # A guard may also test a macro's *value* (`TARGET_FLASH_SIZE > 512`). Only
    # macro names are harvested, never their values, so such a test is a second
    # kind of free variable: try it both ways rather than picking one.
    unknowns = (True, False) if any(_COMPARISON.search(e) for e in exprs) else (True,)

    for assignment in range(1 << len(free)):
        chosen = {
            name for bit, name in enumerate(free) if assignment >> bit & 1
        }
        for unknown in unknowns:
            if all(_eval(e, defs | chosen, unknown) for e in exprs):
                return True, notable
    return False, notable


def _to_python(expr: str) -> str:
    py = re.sub(r"defined\s*\(\s*([A-Za-z_]\w*)\s*\)", r'D("\1")', expr)
    py = re.sub(r"defined\s+([A-Za-z_]\w*)", r'D("\1")', py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    # Any bare identifier left over is an #ifdef-style truth test.
    return re.sub(r'(?<!")\b(?!and\b|or\b|not\b|D\b)([A-Za-z_]\w*)\b(?!")', r'D("\1")', py)


_COMPARISON = re.compile(r"[<>]|==|!=")


class _Macro:
    """
    A macro name standing in a preprocessor expression.

    In a boolean context it answers the only question the harvest can answer -
    is this macro defined - which is what `#ifdef`-style guards ask. Arithmetic
    and comparison ask a different question, the macro's *value*, which is not
    harvested. Answering that one by accident is what made
    `TARGET_FLASH_SIZE > 512` silently gate a line off: the name resolved to
    `False`, and `False > 512` is False. So value tests yield the caller's
    `unknown` verdict instead, and `guards_hold` tries both verdicts.
    """

    __slots__ = ("name", "defined", "unknown")

    def __init__(self, name: str, defined: bool, unknown: bool):
        self.name, self.defined, self.unknown = name, defined, unknown

    def __bool__(self) -> bool:
        return self.defined

    def _value_test(self, *_) -> bool:
        return self.unknown

    def _still_unknown(self, *_) -> "_Macro":
        # Arithmetic keeps the value unknown, so a later comparison still
        # reaches _value_test rather than comparing a bool against an int.
        return self

    __lt__ = __le__ = __gt__ = __ge__ = __eq__ = __ne__ = _value_test
    __hash__ = None  # comparison is not an equivalence here
    __add__ = __radd__ = __sub__ = __rsub__ = _still_unknown
    __mul__ = __rmul__ = __truediv__ = __rtruediv__ = _still_unknown
    __floordiv__ = __rfloordiv__ = __mod__ = __rmod__ = _still_unknown
    __lshift__ = __rlshift__ = __rshift__ = __rrshift__ = _still_unknown
    __and__ = __rand__ = __or__ = __ror__ = __xor__ = __rxor__ = _still_unknown
    __neg__ = __pos__ = __invert__ = _still_unknown


def _eval(py: str, defs: set, unknown: bool = True) -> bool:
    """
    Evaluate one translated guard. `unknown` is the verdict to give a test of
    some macro's value, which this data cannot decide either way.
    """
    try:
        return bool(eval(py, {"__builtins__": {}},  # noqa: S307 - constrained input
                         {"D": lambda n: _Macro(n, n in defs, unknown)}))
    except Exception:
        # A guard the translator cannot turn into Python at all (`?:`, a cast)
        # is a parsing failure, not a capability statement; no obstacle.
        return True


# --------------------------------------------------------------------------- #
# Target discovery
# --------------------------------------------------------------------------- #

def parse_targets(fw: Path) -> Dict[str, Dict[str, str]]:
    """FC_TARGET_MCU name -> {mcu, family, platform} from every target.mk."""
    targets: Dict[str, Dict[str, str]] = {}
    for mk in sorted(fw.glob("src/platform/*/target/*/target.mk")):
        name = mk.parent.name
        body = mk.read_text(errors="replace")
        mcu = _mk_var(body, "TARGET_MCU")
        family = _mk_var(body, "TARGET_MCU_FAMILY")
        if not mcu:
            continue
        targets[name] = {
            "mcu": mcu,
            "family": family or "",
            "platform": mk.parents[2].name,
        }
    return targets


def _mk_var(body: str, var: str) -> Optional[str]:
    m = re.search(rf"^\s*{var}\s*:?=\s*(\S+)", body, re.M)
    return m.group(1) if m else None


def target_defs(info: Dict[str, str]) -> set:
    """The macro set a build of this target would have for guard evaluation."""
    defs = {info["mcu"], info["family"]}
    # STM32F722xx also implies the coarser STM32F7 / STM32 tokens, and an
    # AT32F435 build implies AT32F4 and the driver switch its tables are
    # guarded by - USE_ATBSP_DRIVER is not a board option there, it is what
    # selects the vendor BSP the whole port is built on.
    fam = info["family"]
    if fam.startswith("STM32"):
        defs.add("STM32")
    if fam.startswith("AT32"):
        defs |= {"AT32", "USE_ATBSP_DRIVER"}
    defs.discard("")
    return defs


# --------------------------------------------------------------------------- #
# Timer tables
# --------------------------------------------------------------------------- #

def parse_timers(fw: Path, family: str, defs: set) -> Dict[str, List[str]]:
    """
    pin -> ordered list of "TIMx_CHy". The list index + 1 is exactly the
    `occurrence` argument of TIMER_PIN_MAP (see src/main/pg/timerio.c and
    timerGetByTagAndIndex() in src/main/drivers/timer_common.c).
    """
    src = _platform_file(fw, family, "timer_{lower}.c")
    if not src:
        return {}
    order: Dict[str, List[str]] = OrderedDict()
    inside = False
    for line, guards in GuardScanner(src.read_text(errors="replace")):
        if "fullTimerHardware[" in line:
            inside = True
            continue
        if inside and line.startswith("};"):
            break
        if not inside:
            continue
        # AT32 spells its timers TMR. The name only labels the channel
        # here - what a config.h carries is the occurrence index - so both
        # are kept as written rather than normalised to one.
        m = re.search(rf"DEF_TIM\(\s*((?:TIM|TMR)\d+)\s*,\s*(\w+)\s*,\s*({PIN_RE})\s*,", line)
        if not m:
            continue
        holds, _ = guards_hold(guards, defs)
        if not holds:
            continue
        tim, ch, pin = m.groups()
        order.setdefault(pin, []).append(f"{tim}_{ch}")
    return order


def parse_dma(fw: Path, family: str, defs: set) -> Dict[str, object]:
    """
    DMA options for timer channels and for peripherals (ADC, SPI, UART).

    Two shapes exist. F4/F7 enumerate concrete streams per resource, so the
    dmaopt indexes that resource's own list. DMAMUX parts (G4/H7/H5/C5/N6) share
    one flat dmaChannelSpec[] table, so any index up to its length is valid for
    any resource.
    """
    spec = platform_of(family)
    src = fw / "src/platform" / (spec["dir"] if spec else "STM32") / "dma_reqmap_mcu.c"
    if not src.exists():
        return {}

    text = src.read_text(errors="replace")
    timer_dma: Dict[str, List[str]] = {}
    periph_dma: Dict[str, List[str]] = {}
    mux_len = 0
    style = "mux"

    section: Optional[str] = None
    for line, guards in GuardScanner(text):
        holds, _ = guards_hold(guards, defs)
        if "dmaTimerMapping[" in line:
            section = "timer" if holds else None
            continue
        if "dmaPeripheralMapping[" in line:
            section = "periph" if holds else None
            continue
        if "dmaChannelSpec[" in line:
            section = "mux" if holds else None
            continue
        if line.startswith("};"):
            section = None
            continue
        if section is None or not holds:
            continue

        if section == "mux":
            # The channel table is spelled DMA(...) on most parts but DMA1(...)
            # / DMA2(...) on STM32H5, whose two GPDMA controllers get separate
            # macros. Missing those silently reported "no DMA options", which in
            # turn let every peripheral be assigned the same channel.
            if re.match(r"DMA\d?\s*\(", line):
                mux_len += 1
            continue

        opts = re.findall(r"DMA\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", line)
        if section == "timer":
            m = re.search(r"\)\s*(TIM\d+)\s*,\s*TC\(\s*(CH\d+)\s*\)", line)
            if m and opts:
                style = "fixed"
                timer_dma[f"{m.group(1)}_{m.group(2)}"] = [
                    f"DMA{d}_S{s}_C{c}" for d, s, c in opts
                ]
            continue

        if section == "periph":
            m = re.search(r"DMA_PERIPH_(\w+)\s*,\s*(\w+)", line)
            if m and opts:
                style = "fixed"
                periph, dev = m.group(1), m.group(2)
                key = _periph_key(periph, dev)
                if key:
                    periph_dma[key] = [f"DMA{d}_S{s}_C{c}" for d, s, c in opts]

    return {
        "style": style,
        "mux_options": mux_len,
        "timer": timer_dma,
        "peripheral": periph_dma,
    }


def _periph_key(periph: str, dev: str) -> Optional[str]:
    """DMA_PERIPH_ADC + ADCDEV_1 -> 'ADC1'; SPI_SDO + SPIDEV_3 -> 'SPI3_SDO'."""
    m = re.match(r"(\w+?)DEV_(\w+)", dev)
    if not m:
        return None
    n = m.group(2)
    if periph == "ADC":
        return f"ADC{n}"
    if periph.startswith("SPI_"):
        return f"SPI{n}_{periph.split('_', 1)[1]}"
    if periph.startswith("UART_"):
        return f"UART{n}_{periph.split('_', 1)[1]}"
    if periph == "TIMUP":
        return f"TIMUP{n}"
    return f"{periph}{n}"


# --------------------------------------------------------------------------- #
# UART / SPI / I2C / ADC pin capability
# --------------------------------------------------------------------------- #

def parse_uart(fw: Path, family: str, defs: set) -> Dict[str, List[Dict[str, str]]]:
    """pin -> [{dev: 'UART1', dir: 'tx'|'rx'}, ...]"""
    src = _platform_file(fw, family, "serial_uart_{lower}.c")
    if not src:
        return {}
    out: Dict[str, List[Dict[str, str]]] = {}
    dev: Optional[str] = None
    direction: Optional[str] = None
    for line, guards in GuardScanner(src.read_text(errors="replace")):
        m = re.search(r"\.identifier\s*=\s*SERIAL_PORT_(?:US|U)ART(\d+)", line)
        if m:
            dev, direction = f"UART{m.group(1)}", None
            continue
        m = re.search(r"\.identifier\s*=\s*SERIAL_PORT_LPUART(\d+)", line)
        if m:
            dev, direction = f"LPUART{m.group(1)}", None
            continue
        if ".rxPins" in line:
            direction = "rx"
        elif ".txPins" in line:
            direction = "tx"
        elif re.match(r"\.\w+\s*=", line) and "Pins" not in line:
            direction = None
        if dev and direction:
            holds, _ = guards_hold(guards, defs)
            if not holds:
                continue
            for pin in re.findall(rf"DEFIO_TAG_E\(\s*({PIN_RE})\s*\)", line):
                out.setdefault(pin, []).append({"dev": dev, "dir": direction})
    return out


def parse_spi(fw: Path, family: str, defs: set) -> Dict[str, List[Dict[str, str]]]:
    """pin -> [{dev: 'SPI1', role: 'sck'|'sdi'|'sdo'}, ...]"""
    src = fw / "src/platform/common/stm32/bus_spi_pinconfig.c"
    if not src.exists():
        return {}
    out: Dict[str, List[Dict[str, str]]] = {}
    dev: Optional[str] = None
    role: Optional[str] = None
    # Betaflight's config.h vocabulary is SDI/SDO, the table's is MISO/MOSI.
    ROLES = {"sckPins": "sck", "misoPins": "sdi", "mosiPins": "sdo"}
    for line, guards in GuardScanner(src.read_text(errors="replace")):
        m = re.search(r"\.device\s*=\s*SPIDEV_(\d+)", line)
        if m:
            dev, role = f"SPI{m.group(1)}", None
            continue
        for key, val in ROLES.items():
            if f".{key}" in line:
                role = val
                break
        if dev and role:
            holds, notable = guards_hold(guards, defs)
            if not holds:
                continue
            for pin in re.findall(rf"DEFIO_TAG_E\(\s*({PIN_RE})\s*\)", line):
                entry = {"dev": dev, "role": role}
                if notable:
                    entry["requires"] = sorted(set(notable))
                out.setdefault(pin, []).append(entry)
    return out


def parse_i2c(fw: Path, family: str, defs: set) -> Dict[str, List[Dict[str, str]]]:
    """
    pin -> [{dev: 'I2C1', role: 'scl'|'sda'}, ...]

    The F4 driver is a separate file that the build selects by family rather than
    by #if, so its table would look reachable to every MCU. Pick the file the way
    the makefiles do instead of merging both.
    """
    rel = ("src/platform/AT32/bus_i2c_atbsp_init.c" if family.startswith("AT32")
           else "src/platform/STM32/bus_i2c_stm32f4xx.c" if family == "STM32F4"
           else "src/platform/STM32/bus_i2c_ll_init.c")
    src = fw / rel
    if not src.exists():
        return {}

    out: Dict[str, List[Dict[str, str]]] = {}
    dev: Optional[str] = None
    role: Optional[str] = None
    for line, guards in GuardScanner(src.read_text(errors="replace")):
        m = re.search(r"\.device\s*=\s*I2CDEV_(\d+)", line)
        if m:
            dev, role = f"I2C{m.group(1)}", None
            continue
        if ".sclPins" in line:
            role = "scl"
        elif ".sdaPins" in line:
            role = "sda"
        if not (dev and role):
            continue
        holds, _ = guards_hold(guards, defs)
        if not holds:
            continue
        # F4 lists pins on their own lines under `.sclPins = {`, F7+ inline.
        for pin in re.findall(rf"I2CPINDEF\(\s*({PIN_RE})\b", line):
            out.setdefault(pin, []).append({"dev": dev, "role": role})
    return out


def parse_adc(fw: Path, family: str, defs: set) -> Dict[str, Dict[str, str]]:
    """pin -> {devices: '123', channel: '11'}"""
    src = _platform_file(fw, family, "adc_{lower}.c")
    if not src:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for line, guards in GuardScanner(src.read_text(errors="replace")):
        # F4 spells it ADC_Channel_9, F7 and later ADC_CHANNEL_9.
        m = re.search(
            rf"DEFIO_TAG_E__({PIN_RE})\s*,\s*ADC_DEVICES_(\w+)\s*,\s*ADC_CHANNEL_(\w+)",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        holds, _ = guards_hold(guards, defs)
        if not holds:
            continue
        pin, devices, channel = m.groups()
        out[pin] = {"devices": devices, "channel": channel}
    return out


# Where each vendor's tables live and what they are called. The shapes are the
# same - a table of DEFIO_TAG_E(pin) grouped by device - because AT32's port of
# Betaflight kept them; only the directory, the file names and the timer's
# spelling differ. That is the whole reason a second platform is a matter of
# pointing the parsers rather than writing new ones.
PLATFORMS = {
    "STM32": {"dir": "STM32", "stem": "stm32{lower}",
              "families": ("STM32",)},
    "AT32":  {"dir": "AT32",  "stem": "at32{lower}",
              "families": ("AT32",)},
}


def platform_of(family: str) -> Optional[dict]:
    for spec in PLATFORMS.values():
        if family.startswith(spec["families"]):
            return spec
    return None


def _platform_file(fw: Path, family: str, pattern: str) -> Optional[Path]:
    """
    timer_{stem}.c + STM32F7 -> src/platform/STM32/timer_stm32f7xx.c
                  + AT32F4   -> src/platform/AT32/timer_at32f43x.c

    The AT32 files are named after the part rather than the family - f43x, not
    f4xx - so the candidates are tried in the order a build would find them.
    """
    spec = platform_of(family)
    if not spec:
        return None
    prefix = next(f for f in spec["families"] if family.startswith(f))
    suffix = family[len(prefix):].lower()          # F7 -> f7, F4 -> f4
    for cand in (f"{suffix}xx", f"{suffix}3x", suffix):
        p = fw / "src/platform" / spec["dir"] / pattern.format(
            lower=spec["stem"].format(lower=cand))
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------- #
# Firmware array limits
# --------------------------------------------------------------------------- #

# Ceilings the firmware's own fixed-size arrays impose, per family. A pin past
# UARTHARDWARE_MAX_PINS is not in uartHardware[].txPins[] at all, so no config
# can select it however capable the silicon is - the table is the limit, in
# exactly the sense of "firmware is the single source of truth". The parsed
# capability tables should already respect these, so a harvested count above a
# limit means the parser is reading rows the build cannot use.
#
# Keyed by the macro's own name so a value can be traced back to its #define.
# {platform} is filled in per target: AT32 carries its own copies of these and
# they do not agree with STM32's - MAX_TIMER_DMA_OPTIONS is 22 there against 3
# on F7 - so reading STM32's for an AT32 build would bound the tables by a
# number that part never had.
LIMIT_SOURCES = (
    ("UARTHARDWARE_MAX_PINS",
     "src/platform/{platform}/include/platform/platform.h"),
    ("I2C_PIN_SEL_MAX",
     "src/main/drivers/bus_i2c_impl.h"),
    ("MAX_TIMER_DMA_OPTIONS",
     "src/platform/{platform}/dma_reqmap_mcu.h"),
    ("MAX_PERIPHERAL_DMA_OPTIONS",
     "src/platform/{platform}/dma_reqmap_mcu.h"),
)


def parse_sdio(fw: Path, name: str, info: dict) -> Dict[str, bool]:
    """
    Whether this target can take SDIO pins from a config, and has a driver.

    Both are needed and neither is family-wide. `pg/sdio.c` registers the pin
    config behind `#if ENABLE_SDIO_PIN_CONFIG`, which `common_post.h` defaults
    to **0** - so on a target that does not turn it on, `SDIO_CK_PIN` and the
    rest compile fine and are never read. Emitting them there would be exactly
    the "config the build cannot honour" case: silent, and indistinguishable
    from a working card.

    Only the target's own `target.h` turns it on, so this is parsed per target
    rather than per family: H743 and H750 are both STM32H7 and only one of them
    enables it.
    """
    th = fw / "src/platform" / info.get("platform", "") / "target" / name / "target.h"
    text = th.read_text(errors="replace") if th.exists() else ""
    pin_config = bool(re.search(
        r"^\s*#define\s+ENABLE_SDIO_PIN_CONFIG\s+1\s*$", text, re.M))
    # sdio_h7xx.c, sdio_f4xx.c ... named after the family, not the target.
    suffix = info.get("family", "")[5:].lower()          # STM32H7 -> h7
    driver = (fw / "src/platform" / info.get("platform", "")
              / f"sdio_{suffix}xx.c").exists()
    return {"pin_config": pin_config, "driver": driver}


def parse_limits(fw: Path, defs: set, platform: str = "STM32"
                 ) -> Dict[str, Optional[int]]:
    """
    macro -> int, or None when this family's build does not define it.

    None is recorded rather than a plausible default: a guessed ceiling is worse
    than a missing one, because a caller cannot tell it was guessed.
    """
    out: Dict[str, Optional[int]] = {}
    texts: Dict[str, Optional[str]] = {}
    for macro, template in LIMIT_SOURCES:
        rel = template.format(platform=platform)
        if rel not in texts:
            src = fw / rel
            texts[rel] = src.read_text(errors="replace") if src.exists() else None
        text = texts[rel]
        out[macro] = _define_value(text, macro, defs) if text is not None else None
    return out


def _define_value(text: str, macro: str, defs: set) -> Optional[int]:
    """
    The value a build with `defs` would see for `#define macro <int>`.

    The preprocessor takes the first branch of an #if/#elif chain that holds, so
    so does this. A definition whose value is not a plain integer literal (a
    macro reference, an expression) is not resolvable here and is skipped, which
    leaves None rather than a wrong number.
    """
    pattern = re.compile(rf"#\s*define\s+{re.escape(macro)}\s+(\S+)")
    for line, guards in GuardScanner(text):
        m = pattern.match(line)
        if not m:
            continue
        holds, _ = guards_hold(guards, defs)
        if not holds:
            continue
        try:
            return int(m.group(1).strip("()"), 0)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Driver catalogue
# --------------------------------------------------------------------------- #

# Which USE_ define families name a physical part, per config.h category, and
# which bus wiring each spelling implies. A part can appear under more than one
# bus (DPS310 exists as both USE_BARO_DPS310 and USE_BARO_SPI_DPS310), so they
# are recorded per bus rather than fighting over one key.
DRIVER_SCAN = {
    "gyro": [("spi", r"USE_GYRO_SPI_(\w+)"), ("any", r"USE_ACCGYRO_(\w+)"),
             ("i2c", r"USE_GYRO_(\w+)")],
    "acc": [("spi", r"USE_ACC_SPI_(\w+)"), ("any", r"USE_ACCGYRO_(\w+)"),
            ("i2c", r"USE_ACC_(\w+)")],
    "baro": [("spi", r"USE_BARO_SPI_(\w+)"), ("i2c", r"USE_BARO_(\w+)")],
    "mag": [("spi", r"USE_MAG_SPI_(\w+)"), ("i2c", r"USE_MAG_(\w+)")],
    "flash": [("spi", r"USE_FLASH_(\w+)")],
    "osd": [("spi", r"USE_(MAX7456)\b")],
}

# Tokens that match the define shape but are feature switches, not parts.
DRIVER_REJECT = {
    "BOTH", "LPF2", "OVERFLOW_CHECK", "SLEW_LIMITER", "DATA_READY_SIGNAL",
    "MEMORY_MAPPED", "OCTOSPI", "QUADSPI", "READS_USING_4LINES",
    "WRITES_USING_4LINES", "CHIP", "TOOLS", "CLKIN", "EXTI", "REGISTER_DUMP",
    "DLPF_EXPERIMENTAL", "DATA_ANALYSE", "RATE", "CALIBRATION", "COUNT",
    "M25P16", "W25M", "W25N",
}


def parse_drivers(fw: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """category -> PART -> {bus: USE_ define}, harvested from src/main."""
    text = []
    for rel in ("src/main/target/common_pre.h", "src/main/target/common_post.h"):
        p = fw / rel
        if p.exists():
            text.append(p.read_text(errors="replace"))
    for sub in ("sensors", "drivers"):
        for p in sorted((fw / "src/main" / sub).rglob("*.[ch]")):
            text.append(p.read_text(errors="replace"))
    blob = "\n".join(text)

    out: Dict[str, Dict[str, Dict[str, str]]] = {k: {} for k in DRIVER_SCAN}
    for category, patterns in DRIVER_SCAN.items():
        for bus, pat in patterns:
            for m in re.finditer(pat, blob):
                part = m.group(1)
                # The generic pattern also matches the SPI spelling; the
                # SPI-specific pass already recorded those correctly.
                if part.startswith("SPI_") or part in DRIVER_REJECT or len(part) < 4:
                    continue
                out[category].setdefault(part, {}).setdefault(bus, m.group(0))
    return {k: {p: dict(sorted(b.items())) for p, b in sorted(v.items())}
            for k, v in out.items()}


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def firmware_revision(fw: Path) -> Dict[str, str]:
    def git(*args) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(fw), *args],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except Exception:
            return ""
    # Deliberately no path. This file is committed, and a checkout path is both
    # a local absolute path and, on these machines, a board codename - one tree
    # here is named after the vendor submission it was cut for. The rev, date
    # and branch are what make the seed traceable; where it was generated is
    # nobody's business and cannot be scrubbed once published.
    return {
        "rev": git("rev-parse", "--short=9", "HEAD"),
        "date": git("log", "-1", "--format=%cs"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
    }


def build(fw: Path, quiet: bool = False) -> dict:
    targets = parse_targets(fw)
    if not targets:
        raise SystemExit(f"No target.mk files under {fw}/src/platform - wrong path?")

    drivers = parse_drivers(fw)
    per_target: Dict[str, dict] = {}

    # Families share their tables; parse each family once.
    cache: Dict[str, dict] = {}
    for name, info in sorted(targets.items()):
        family = info["family"]
        spec = platform_of(family)
        if not spec:
            continue  # APM32 and PICO are not harvested yet
        defs = target_defs(info)
        key = f"{family}|{info['mcu']}"
        if key not in cache:
            cache[key] = {
                "timers": parse_timers(fw, family, defs),
                "dma": parse_dma(fw, family, defs),
                "uart": parse_uart(fw, family, defs),
                "spi": parse_spi(fw, family, defs),
                "i2c": parse_i2c(fw, family, defs),
                "adc": parse_adc(fw, family, defs),
                "limits": parse_limits(fw, defs, spec["dir"]),
            }
            if not quiet:
                c = cache[key]
                lim = c["limits"]
                shown = "  ".join(
                    f"{k}={'?' if lim[k] is None else lim[k]}"
                    for k in ("UARTHARDWARE_MAX_PINS", "MAX_TIMER_DMA_OPTIONS")
                )
                print(f"  {family:9} {info['mcu']:14} "
                      f"timers={len(c['timers']):3}  uart={len(c['uart']):3}  "
                      f"spi={len(c['spi']):3}  i2c={len(c['i2c']):2}  adc={len(c['adc']):3}"
                      f"  {shown}")
        per_target[name] = {**info, **cache[key],
                            "sdio": parse_sdio(fw, name, info)}

    return {
        "schema": 1,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "firmware": firmware_revision(fw),
        "drivers": drivers,
        "targets": per_target,
    }


def find_firmware() -> Optional[Path]:
    """
    Locate a Betaflight tree near this repo.

    A dev machine typically has many worktrees, several of them on master at
    different ages. Taking the first one found silently seeds from stale
    firmware - which is how you end up emitting a target name the current build
    system no longer knows. So prefer master, and among those the newest commit.
    """
    here = Path(__file__).resolve()
    roots = {here.parents[2], *list(here.parents[2].parents)[:1]}
    candidates: List[Path] = []
    for root in roots:
        candidates.append(root / "betaflight")
        candidates.extend(sorted((root / "betaflight").glob("*/betaflight")))

    scored: List[Tuple[int, str, Path]] = []
    seen = set()
    for c in candidates:
        if not (c / "src/main/target/common_pre.h").exists():
            continue
        c = c.resolve()
        if c in seen:
            continue
        seen.add(c)
        def git(*a: str) -> str:
            return subprocess.run(["git", "-C", str(c), *a],
                                  capture_output=True, text=True).stdout.strip()
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        stamp = git("log", "-1", "--format=%ct")
        scored.append((1 if branch == "master" else 0,
                       stamp or "0", c))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", type=Path, help="Betaflight source tree")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).parent / "data" / "firmware.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    fw = args.firmware or find_firmware()
    if not fw or not (fw / "src/main/target/common_pre.h").exists():
        print("Could not locate a Betaflight tree. Pass --firmware PATH.", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"Seeding from {fw}")
    data = build(fw, quiet=args.quiet)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")

    if not args.quiet:
        rev = data["firmware"]
        drv = sum(len(v) for v in data["drivers"].values())
        print(f"\n{len(data['targets'])} targets, {drv} driver defines")
        print(f"firmware {rev['rev']} ({rev['branch']}, {rev['date']})")
        print(f"wrote {args.out}  ({args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
