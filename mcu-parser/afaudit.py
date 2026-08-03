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
afaudit.py - Audit Betaflight's pin tables against an ST datasheet.

Betaflight's tables under `src/platform/` are the single source of truth for
what a target may use: a pin the firmware does not list cannot be emitted,
whatever the silicon supports. That makes a wrong entry in those tables
invisible from the inside - `genconfig.py` will happily place a peripheral on a
pin whose AF number is off by one, the build succeeds, and the board is dead.

This is the instrument that catches it. It reads the alternate-function tables
out of an ST datasheet PDF by column geometry and cross-checks every
`(pin, AF number)` pair the firmware declares for UART, SPI, I2C and the timer
AF map.

Its output is a firmware bug report, not generator input. The datasheet is
deliberately never wired into the runtime path: a PDF recovered by geometry is
evidence to be argued with, not a table to be trusted. Fix the firmware, then
re-seed - the generator keeps taking its facts from the firmware alone.

Three categories, reported separately because they mean different things:

  wrong AF number       right pin, wrong AF - silently mis-programs the GPIO mux
  pin not in datasheet  a pin the silicon does not offer for that function -
                        the peripheral simply never appears on the header
  missing pin           the datasheet has an option the firmware lacks. Not a
                        defect; a board wanting that pin just cannot be built.
                        Flagged with the UARTHARDWARE_MAX_PINS ceiling, because
                        adding one is sometimes not possible without a struct
                        change.

Only the first two fail the run (exit 1), so this can gate CI.

Usage:  python3 afaudit.py --datasheet DS.pdf --firmware PATH [--family STM32H5]
        python3 afaudit.py --datasheet DS.pdf --firmware PATH --peripheral uart,i2c
Needs:  pdftotext (poppler), a Betaflight source tree, Python 3.12 stdlib
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

# The guard logic is not worth having twice: seed_firmware already knows that a
# USE_* macro is the board author's choice while an MCU macro is a fixed fact,
# and getting that wrong silently drops whole tables. `_platform_file` is
# private only because nothing outside the seeder needed it before.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_firmware import (  # noqa: E402
    GuardScanner,
    _platform_file,
    guards_hold,
    parse_targets,
    target_defs,
)

PIN_RE = re.compile(r"^P[A-K]\d{1,2}$")
FUNC_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
AF_HDR_RE = re.compile(r"^AF(\d{1,2})$")
WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>',
    re.S,
)

PERIPHERALS = ("uart", "i2c", "spi", "timer")

# Categories, in report order. The first two are defects, the third is context.
WRONG_AF = "wrong-af"
NOT_IN_DATASHEET = "not-in-datasheet"
ABSENT_PERIPHERAL = "absent-peripheral"
MISSING_PIN = "missing-pin"
FAILING = (WRONG_AF, NOT_IN_DATASHEET)


# --------------------------------------------------------------------------- #
# Datasheet: alternate-function tables, recovered by column geometry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Word:
    """One `<word>` from `pdftotext -bbox-layout`, with its bounding box."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class AfTable:
    """pin -> {function -> AF number}, plus what it took to get there."""

    pins: Dict[str, Dict[str, int]] = field(default_factory=dict)
    pages: int = 0
    cells: int = 0

    def af(self, pin: str, names: Sequence[str]) -> Optional[Tuple[str, int]]:
        """First of `names` the datasheet offers on `pin`, with its AF number."""
        row = self.pins.get(pin)
        if not row:
            return None
        for name in names:
            if name in row:
                return name, row[name]
        return None

    def index(self) -> Dict[str, Dict[str, int]]:
        """Inverse view: function -> {pin -> AF number}."""
        out: Dict[str, Dict[str, int]] = defaultdict(dict)
        for pin, funcs in self.pins.items():
            for func, af in funcs.items():
                out[func][pin] = af
        return out


def pdf_pages(pdf: Path) -> List[List[Word]]:
    """Every page of the PDF as a word list. Shells out to poppler."""
    try:
        proc = subprocess.run(
            ["pdftotext", "-bbox-layout", str(pdf), "-"],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        raise SystemExit("pdftotext not found - install poppler-utils")
    if proc.returncode != 0:
        raise SystemExit(f"pdftotext failed on {pdf}: {proc.stderr.strip()[:200]}")

    pages = []
    for chunk in proc.stdout.split("<page ")[1:]:
        pages.append([
            Word(float(a), float(b), float(c), float(d), unescape(t).strip())
            for a, b, c, d, t in WORD_RE.findall(chunk)
        ])
    return pages


def _header_row(words: Sequence[Word]) -> List[Tuple[float, int]]:
    """
    Column anchors from the `AF0..AF15` header cells.

    The table caption repeats them ("Alternate functions AF0 to AF7
    (continued)"), and those two decoys sit at plausible x positions, so take
    only the horizontal band that holds the most of them.
    """
    bands: Dict[int, List[Tuple[float, int]]] = defaultdict(list)
    for w in words:
        m = AF_HDR_RE.match(w.text)
        if m:
            bands[round(w.yc / 3)].append((w.xc, int(m.group(1))))
    if not bands:
        return []
    best = max(bands.values(), key=len)
    return sorted(best)


def _column_spans(cols: Sequence[Tuple[float, int]]) -> List[Tuple[float, float, int]]:
    """Half-open x spans per column, split at the midpoints between anchors."""
    xs = [c[0] for c in cols]
    pitch = statistics.median(b - a for a, b in zip(xs, xs[1:])) if len(xs) > 1 else 60.0
    spans = []
    for i, (xc, af) in enumerate(cols):
        lo = (xs[i - 1] + xc) / 2 if i else xc - pitch / 2
        hi = (xc + xs[i + 1]) / 2 if i + 1 < len(xs) else xc + pitch / 2
        spans.append((lo, hi, af))
    return spans


def _cell_functions(cell: Sequence[Word]) -> Set[str]:
    """
    Function names in one table cell.

    A cell holds several names separated by `/` or `,` and breaks a name in two
    whenever it does not fit the column - across lines (`ETH_MII_TX_EN/ET` then
    `H_RMII_TX_EN`) and, on the denser F7 and G4 layouts, within one line
    (`UART4_` then `TX`, 2pt apart). Both look identical to poppler.

    So rather than guess which gaps are real, tokenise every reading of the
    cell - each line spaced, each line closed up, and all lines closed up - and
    take the union. A wrong reading yields a token like `TIM3_CH1TIM4_CH2` that
    matches no firmware signal and no signal of interest, so it costs nothing;
    a missing reading silently invents a firmware bug, which costs a lot.
    """
    lines: Dict[int, List[Word]] = defaultdict(list)
    for w in cell:
        lines[round(w.yc)].append(w)
    ordered = [sorted(ws, key=lambda w: w.x0) for _, ws in sorted(lines.items())]
    spaced = [" ".join(w.text for w in ws) for ws in ordered]
    closed = ["".join(w.text for w in ws) for ws in ordered]

    found: Set[str] = set()
    for blob in spaced + closed + ["".join(closed)]:
        for tok in re.split(r"[,\s/]+", blob):
            # The F7 tables spell it `UART7_Rx`; every other family shouts it.
            tok = tok.strip("().*-_").upper()
            if FUNC_RE.match(tok):
                found.add(tok)
    return found


def extract_af_table(pages: Sequence[Sequence[Word]]) -> AfTable:
    """
    Read every AF table in the datasheet into pin -> {function: AF number}.

    A page qualifies when it has an `AFn` header band and pin labels to the left
    of the first column - which rejects the pin-definition table, whose
    "alternate functions" column is one comma-separated blob with no AF numbers
    and rows half the pitch (assigning tokens there would be nonsense).
    """
    table = AfTable()
    for words in pages:
        cols = _header_row(words)
        if len(cols) < 4:
            continue
        spans = _column_spans(cols)
        left = spans[0][0]

        pins = sorted((w for w in words if PIN_RE.match(w.text) and w.xc < left),
                      key=lambda w: w.yc)
        if len(pins) < 4:
            continue

        # Row tolerance follows the row pitch: wrapped cell lines sit within
        # half a row of their label, page furniture does not.
        gaps = [b.yc - a.yc for a, b in zip(pins, pins[1:]) if b.yc - a.yc > 1]
        pitch = statistics.median(gaps) if gaps else 12.0
        tol = min(max(pitch / 2, 6.0), 14.0)

        cells: Dict[Tuple[str, int], List[Word]] = defaultdict(list)
        for w in words:
            if w.xc < left or w.xc > spans[-1][1] or AF_HDR_RE.match(w.text):
                continue
            row = min(pins, key=lambda p: abs(p.yc - w.yc))
            if abs(row.yc - w.yc) > tol:
                continue
            col = next((af for lo, hi, af in spans if lo <= w.xc < hi), None)
            if col is None:
                continue
            cells[(row.text, col)].append(w)

        table.pages += 1
        for (pin, af), cell in cells.items():
            for func in _cell_functions(cell):
                table.pins.setdefault(pin, {})[func] = af
                table.cells += 1

    table.pins = dict(sorted(table.pins.items()))
    return table


def detect_devices(pages: Sequence[Sequence[Word]]) -> Tuple[List[str], str]:
    """
    (device codes, family) from the datasheet's own title page.

    A datasheet covers a named handful of parts - "STM32H562xx STM32H563xx" -
    and nothing else. That list is what decides which Betaflight targets this
    audit is entitled to judge: an H743 datasheet says nothing about the TIM23
    and USART10 rows that only an H723 build compiles.
    """
    head = " ".join(w.text for w in pages[0]) if pages else ""
    devices = sorted({m.group(0) for m in re.finditer(r"STM32[A-Z0-9x]{3,}", head)})
    m = re.match(r"STM32([A-Z]\d)", devices[0]) if devices else None
    return devices, f"STM32{m.group(1)}" if m else ""


def covers(devices: Sequence[str], name: str, mcu: str) -> bool:
    """
    Is this target one of the parts the datasheet describes?

    Compared on the nine-character device code with `x` as a wildcard, since ST
    titles one datasheet `STM32N6x5xx` for a target Betaflight calls STM32N657.
    """
    for dev in devices:
        for cand in (name, mcu):
            a, b = dev[:9].upper(), cand[:9].upper()
            if len(a) == len(b) == 9 and all(
                    x == "X" or y == "X" or x == y for x, y in zip(a, b)):
                return True
    return False


# --------------------------------------------------------------------------- #
# Firmware: the tables being audited
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PinEntry:
    """One `(signal, pin, AF)` claim made by a firmware table."""

    peripheral: str      # uart | i2c | spi | timer
    signal: str          # USART10_RX, I2C4_SCL, SPI1_SCK, TIM2_CH1
    pin: str
    af: Optional[int]    # None when the token could not be resolved to a number
    file: str
    line: int


def _strip_comments_in_place(text: str) -> str:
    """Comments removed, line count preserved so line numbers survive."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


class Source:
    """
    A C file plus a way back to physical line numbers.

    A bug report has to cite a line, and `timer_def.h` repeats an identical
    table per family - so "the first line that looks like this" points at the
    wrong family's copy. `GuardScanner` does not number its output and drops
    block comments in a way that shifts every line after them, so the file is
    stripped here first, keeping one line per line, and the scanner is walked
    in step with it.
    """

    def __init__(self, path: Path):
        self.path = path
        self.text = _strip_comments_in_place(path.read_text(errors="replace"))
        self._pos = 0

    def lineno(self) -> int:
        """Physical line number of the line the scan is on."""
        return self._pos

    def scan(self):
        """(line, guards) from GuardScanner, with `lineno()` kept in step."""
        numbered = iter(enumerate(self.text.splitlines(), 1))
        self._pos = 0
        for line, guards in GuardScanner(self.text):
            # GuardScanner skips #if/#else/#endif and yields the rest in order,
            # so walking forward to the next line with this exact text lands on
            # the right one - no guessing, and no drift.
            for n, raw in numbered:
                if raw.strip() == line:
                    self._pos = n
                    break
            yield line, guards


class AfToken:
    """
    `GPIO_AF7_USART1` -> 7, and `HAL_GPIO_AF11_USART1` too - C5 prefixes them.

    F4's StdPeriph driver spells it `GPIO_AF_USART1` with the number hidden in
    a header, and the SPI table puts one such token on the device rather than
    on each pin. Resolve those from the vendor header the F4 build compiles
    against rather than hard-coding ST's numbering.

    What it cannot resolve it remembers: an unrecognised spelling makes a whole
    family's AF numbers unverifiable, and it did exactly that on C5 for a while
    without the report showing anything amiss.
    """

    def __init__(self, fw: Path):
        self.fw = fw
        self.unresolved: Set[str] = set()
        self._map: Optional[Dict[str, int]] = None

    def __call__(self, token: str) -> Optional[int]:
        m = re.fullmatch(r"(?:\w+_)?GPIO_AF(\d+)_\w+", token)
        if m:
            return int(m.group(1))
        if self._map is None:
            self._map = {}
            for header in self.fw.glob("lib/main/**/stm32*_gpio.h"):
                for d in re.finditer(
                        r"#define\s+(GPIO_AF_\w+)\s+\(\(uint8_t\)\s*0x([0-9A-Fa-f]+)\)",
                        header.read_text(errors="replace")):
                    self._map.setdefault(d.group(1), int(d.group(2), 16))
        af = self._map.get(token)
        if af is None:
            self.unresolved.add(token)
        return af


class Device:
    """
    Buffers one device's pin rows until a trailing `.af = ...` can fill them in.

    F4 states the AF once per device, after the pin arrays
    (`.af = GPIO_AF_USART1`), where every later family repeats it on each row -
    and the F7 I2C table states it nowhere at all, in a file-scope #define.
    Buffering is what lets one parser read all three shapes instead of
    reporting "0 pairs checked" for two of the families that matter most.
    """

    def __init__(self) -> None:
        self.done: List[PinEntry] = []
        self.rows: List[PinEntry] = []

    def add(self, entry: PinEntry) -> None:
        self.rows.append(entry)

    def shared_af(self, value: Optional[int]) -> None:
        self.rows = [e if e.af is not None else
                     PinEntry(e.peripheral, e.signal, e.pin, value, e.file, e.line)
                     for e in self.rows]

    def close(self) -> List[PinEntry]:
        self.done.extend(self.rows)
        self.rows = []
        return self.done


def fw_uart(src: Source, defs: Set[str], af: AfToken) -> List[PinEntry]:
    """`serial_uart_stm32*.c` -> USART10_RX / LPUART1_TX style claims."""
    block = Device()
    dev: Optional[str] = None
    direction: Optional[str] = None
    for line, guards in src.scan():
        m = re.search(r"\.identifier\s*=\s*SERIAL_PORT_((?:US|U|LPU)ART\d+)", line)
        if m:
            block.close()
            dev, direction = m.group(1), None
            continue
        m = re.match(r"\.af\s*=\s*(\w+)", line)
        if m and dev:
            block.shared_af(af(m.group(1)))
            direction = None
            continue
        if ".rxPins" in line:
            direction = "RX"
        elif ".txPins" in line:
            direction = "TX"
        elif re.match(r"\.\w+\s*=", line) and "Pins" not in line:
            direction = None
        if not (dev and direction) or not guards_hold(guards, defs)[0]:
            continue
        for pin, token in re.findall(
                r"DEFIO_TAG_E\(\s*(P[A-K]\d{1,2})\s*\)(?:\s*,\s*(\w+))?", line):
            block.add(PinEntry("uart", f"{dev}_{direction}", pin,
                               af(token) if token else None,
                               src.path.name, src.lineno()))
    return block.close()


def fw_i2c(src: Source, defs: Set[str], af: AfToken) -> List[PinEntry]:
    """`bus_i2c_ll_init.c` / `bus_i2c_stm32f4xx.c` -> I2C4_SCL style claims."""
    block = Device()
    dev: Optional[str] = None
    role: Optional[str] = None
    for line, guards in src.scan():
        m = re.search(r"\.device\s*=\s*I2CDEV_(\d+)", line)
        if m:
            block.close()
            dev, role = f"I2C{m.group(1)}", None
            continue
        if ".sclPins" in line:
            role = "SCL"
        elif ".sdaPins" in line:
            role = "SDA"
        if not (dev and role) or not guards_hold(guards, defs)[0]:
            continue
        for pin, token in re.findall(
                r"I2CPINDEF\(\s*(P[A-K]\d{1,2})\s*(?:,\s*(\w+))?\s*\)", line):
            block.add(PinEntry("i2c", f"{dev}_{role}", pin,
                               af(token) if token else None,
                               src.path.name, src.lineno()))
    return block.close()


def fw_spi(src: Source, defs: Set[str], af: AfToken) -> List[PinEntry]:
    """
    `bus_spi_pinconfig.c` -> SPI1_SCK style claims.

    Betaflight's own vocabulary is SDI/SDO, the table's is MISO/MOSI, and the
    datasheet's is MISO/MOSI - so the table spelling passes straight through.
    """
    ROLES = {"sckPins": "SCK", "misoPins": "MISO", "mosiPins": "MOSI"}
    block = Device()
    dev: Optional[str] = None
    role: Optional[str] = None
    for line, guards in src.scan():
        m = re.search(r"\.device\s*=\s*SPIDEV_(\d+)", line)
        if m:
            block.close()
            dev, role = f"SPI{m.group(1)}", None
            continue
        m = re.match(r"\.af\s*=\s*(\w+)", line)
        if m and dev:
            block.shared_af(af(m.group(1)))
            role = None
            continue
        for key, val in ROLES.items():
            if f".{key}" in line:
                role = val
                break
        if not (dev and role) or not guards_hold(guards, defs)[0]:
            continue
        for pin, token in re.findall(
                r"DEFIO_TAG_E\(\s*(P[A-K]\d{1,2})\s*\)(?:\s*,\s*(\w+))?", line):
            block.add(PinEntry("spi", f"{dev}_{role}", pin,
                               af(token) if token else None,
                               src.path.name, src.lineno()))
    return block.close()


def fw_timer(src: Source, defs: Set[str]) -> List[PinEntry]:
    """
    `timer_def.h` -> TIM2_CH1 style claims.

    `DEF_TIM_AF__PA0__TCH_TIM2_CH1  D(1, 2)` is the firmware's whole statement
    about which AF puts TIM2_CH1 on PA0; `fullTimerHardware[]` only chooses
    among them. Auditing the AF map therefore covers every timer option a
    target could ever reference.
    """
    out: List[PinEntry] = []
    for line, guards in src.scan():
        m = re.match(
            r"#\s*define\s+DEF_TIM_AF__(P[A-K]\d{1,2})__TCH_(TIM\d+)_(CH\d+N?)\s+"
            r"D\(\s*(\d+)\s*,\s*\d+\s*\)", line)
        if not m or not guards_hold(guards, defs)[0]:
            continue
        pin, tim, ch, afn = m.groups()
        out.append(PinEntry("timer", f"{tim}_{ch}", pin, int(afn),
                            src.path.name, src.lineno()))
    return out


def fw_timer_f4(src: Source, defs: Set[str], af: AfToken) -> List[PinEntry]:
    """
    `timer_stm32f4xx.c` -> TIM2_CH1 style claims, for F4 only.

    F4 predates the per-pin AF map: `DEF_TIM_AF` there expands to one
    `GPIO_AF_TIMn` per timer, so the pin list lives in `fullTimerHardware[]`
    and the AF comes from the peripheral. Same claim, stated in two places.
    """
    out: List[PinEntry] = []
    inside = False
    for line, guards in src.scan():
        if "fullTimerHardware[" in line:
            inside = True
            continue
        if inside and line.startswith("};"):
            break
        m = re.search(r"DEF_TIM\(\s*(TIM\d+)\s*,\s*(CH\d+N?)\s*,\s*(P[A-K]\d{1,2})\s*,", line)
        if not (inside and m) or not guards_hold(guards, defs)[0]:
            continue
        tim, ch, pin = m.groups()
        out.append(PinEntry("timer", f"{tim}_{ch}", pin, af(f"GPIO_AF_{tim}"),
                            src.path.name, src.lineno()))
    return out


def fw_max_uart_pins(fw: Path, defs: Set[str]) -> Optional[int]:
    """UARTHARDWARE_MAX_PINS for this family - the ceiling a fix has to fit in."""
    path = fw / "src/platform/STM32/include/platform/platform.h"
    if not path.exists():
        return None
    for line, guards in GuardScanner(path.read_text(errors="replace")):
        m = re.match(r"#\s*define\s+UARTHARDWARE_MAX_PINS\s+(\d+)", line)
        if m and guards_hold(guards, defs)[0]:
            return int(m.group(1))
    return None


def fw_ports(target_h: Path) -> Set[str]:
    """Ports a target enables, from its `TARGET_IO_PORTx` defines."""
    if not target_h.exists():
        return set()
    ports = set()
    for line, guards in GuardScanner(target_h.read_text(errors="replace")):
        m = re.match(r"#\s*define\s+TARGET_IO_PORT([A-K])\b", line)
        if m and guards_hold(guards, set())[0]:
            ports.add(m.group(1))
    return ports


# --------------------------------------------------------------------------- #
# Naming: firmware signal -> what the datasheet calls it
# --------------------------------------------------------------------------- #

def datasheet_names(signal: str) -> List[str]:
    """
    Spellings the datasheet may use for a firmware signal.

    ST is inconsistent about USARTn vs UARTn between the reference manual and
    the AF table (`UART9_TX` but `USART10_TX`), and Betaflight follows whichever
    the header uses. Accepting both is not a loosening: the instance number is
    what identifies the peripheral.
    """
    names = [signal]
    if signal.startswith("USART"):
        names.append("UART" + signal[len("USART"):])
    elif signal.startswith("UART"):
        names.append("USART" + signal[len("UART"):])
    return names


# Signals whose absence from firmware is worth reporting. Anything else in the
# datasheet (TIMx_ETR, SPIx_RDY, I2Cx_SMBA) has no Betaflight table to be
# missing from, so listing it would be noise rather than a gap.
MISSING_INTEREST = re.compile(
    r"^(?:LP)?(?:US)?ART\d+_(?:TX|RX)$|^I2C\d+_(?:SCL|SDA)$"
    r"|^SPI\d+_(?:SCK|MISO|MOSI)$|^TIM\d+_CH\d+N?$")


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Finding:
    kind: str
    peripheral: str
    signal: str
    pin: str
    fw_af: Optional[int]
    ds_af: Optional[int]
    where: str
    note: str = ""


WAIVER_FILE = Path(__file__).parent / "data" / "af-waivers.json"


@dataclass(frozen=True)
class Waiver:
    datasheet: str
    peripheral: str
    signal: str
    pin: str
    reason: str

    def matches(self, f: "Finding", datasheet_stem: str) -> bool:
        return (datasheet_stem.lower().startswith(self.datasheet.lower())
                and f.peripheral.lower() == self.peripheral.lower()
                and f.signal.upper() == self.signal.upper()
                and f.pin.upper() == self.pin.upper())


def load_waivers(path: Path = WAIVER_FILE) -> List[Waiver]:
    """
    Known divergences that must not fail the run.

    One firmware table serves a family; a datasheet describes one part. Where
    two parts genuinely differ, no single table satisfies both and the audit is
    permanently red on one of them - and a check that can never go green stops
    being read. A waiver silences one exact finding, never a category.
    """
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    out = []
    for w in doc.get("waivers", []):
        missing = [k for k in ("datasheet", "peripheral", "signal", "pin", "reason")
                   if not w.get(k)]
        if missing:
            raise SystemExit(f"{path}: waiver missing {', '.join(missing)}: {w}")
        out.append(Waiver(w["datasheet"], w["peripheral"], w["signal"],
                          w["pin"], w["reason"]))
    return out


@dataclass
class Audit:
    findings: List[Finding] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    corroborated: int = 0
    exact: int = 0
    af_unknown: int = 0
    off_package: Set[str] = field(default_factory=set)

    @property
    def failures(self) -> List[Finding]:
        return [f for f in self.findings if f.kind in FAILING]


def audit(entries: Sequence[PinEntry], table: AfTable, ports: Set[str],
          max_uart_pins: Optional[int]) -> Audit:
    """Every firmware claim against the datasheet, then the reverse direction."""
    result = Audit()
    index = table.index()
    where = lambda e: f"{e.file}:{e.line}" if e.line else e.file  # noqa: E731

    # A pin on a port no target enables is not evidence of anything: the
    # firmware table is shared by the whole family and the datasheet covers
    # packages this family never ships.
    live = [e for e in entries if e.pin[1] in ports]

    # A datasheet describes one part. Its AF table has no row for a pin the
    # package does not bond out, which is not the firmware saying anything
    # false - the family's other parts have it. Reporting those as defects
    # buried the real findings on the H743VI (LQFP100) run.
    result.off_package = {e.pin for e in live if e.pin not in table.pins}
    live = [e for e in live if e.pin in table.pins]

    # A firmware table is shared by a whole family; a datasheet describes one
    # part. When a peripheral instance appears nowhere in the AF table, this
    # part simply does not have it - I2C4 on an F722, UART9 and USART10 on an
    # H743, USART7 on a C562. Reporting one "pin not in datasheet" per pin then
    # buries the real findings under noise that can never be fixed, because the
    # entries are right for the family's other parts. Say it once, and do not
    # fail the run for it.
    # The unit is simply everything before the first underscore. A regex like
    # [A-Z]+\d* cannot parse I2C4 or I2S2, where the digit sits mid-name, and
    # silently concludes the part has no I2C at all.
    known_units = {f.split("_")[0] for funcs in table.pins.values()
                   for f in funcs if "_" in f}
    absent, kept = set(), []
    for e in live:
        unit = e.signal.split("_")[0]
        siblings = {unit}
        # USARTn and UARTn name the same silicon; only conclude it is absent
        # when neither spelling occurs.
        if unit.startswith("USART"):
            siblings.add("UART" + unit[len("USART"):])
        elif unit.startswith("UART"):
            siblings.add("USART" + unit[len("UART"):])
        if siblings & known_units:
            kept.append(e)
        else:
            absent.add(unit)
    if absent:
        for unit in sorted(absent):
            n = sum(1 for e in live if e.signal.split("_")[0] == unit)
            result.findings.append(Finding(
                ABSENT_PERIPHERAL, "", unit, "", None, None, "",
                f"{n} pin(s) skipped: this part has no {unit}; the entries "
                "belong to other members of the family"))
    live = kept

    for e in sorted(live, key=lambda e: (e.peripheral, e.signal, e.pin)):
        result.checked[e.peripheral] += 1
        hit = table.af(e.pin, datasheet_names(e.signal))
        if hit is None:
            offers = ", ".join(
                f"{f} (AF{a})" for f, a in sorted(table.pins.get(e.pin, {}).items(),
                                                  key=lambda kv: kv[1])) or "nothing"
            result.findings.append(Finding(
                NOT_IN_DATASHEET, e.peripheral, e.signal, e.pin, e.af, None,
                where(e), f"datasheet offers {offers} on {e.pin}"))
            continue
        result.corroborated += 1
        _, ds_af = hit
        if e.af is None:
            # Pin confirmed, AF unverifiable: some tables (F7 I2C) never state
            # one. Counting this as exact would inflate the confidence figure.
            result.af_unknown += 1
            continue
        if e.af != ds_af:
            result.findings.append(Finding(
                WRONG_AF, e.peripheral, e.signal, e.pin, e.af, ds_af, where(e)))
        else:
            result.exact += 1

    # Reverse direction: options the silicon has and the firmware does not.
    # Only for signals the firmware already knows about - a device it does not
    # support at all is a feature request, not a table gap.
    known: Dict[str, Set[str]] = defaultdict(set)
    periph_of: Dict[str, str] = {}
    for e in live:
        known[e.signal].add(e.pin)
        periph_of[e.signal] = e.peripheral

    for signal, fw_pins in sorted(known.items()):
        if not MISSING_INTEREST.match(signal):
            continue
        ds_pins: Dict[str, int] = {}
        for name in datasheet_names(signal):
            for pin, af in index.get(name, {}).items():
                if pin[1] in ports:
                    ds_pins.setdefault(pin, af)
        note = ""
        if (periph_of[signal] == "uart" and max_uart_pins
                and len(fw_pins) >= max_uart_pins):
            note = f"array full at UARTHARDWARE_MAX_PINS={max_uart_pins}"
        for pin in sorted(ds_pins.keys() - fw_pins):
            result.findings.append(Finding(
                MISSING_PIN, periph_of[signal], signal, pin, None, ds_pins[pin],
                "", note))
    return result


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

HEADINGS = {
    WRONG_AF: "WRONG AF NUMBER      firmware has the pin, the AF value disagrees",
    ABSENT_PERIPHERAL: "PERIPHERAL NOT ON THIS PART  shared family table, not a defect",
    NOT_IN_DATASHEET: "PIN NOT IN DATASHEET  firmware offers a function the "
                      "silicon does not have there",
    MISSING_PIN: "MISSING PIN          the datasheet has an option the firmware "
                 "lacks (informational)",
}


def report(result: Audit, table: AfTable, meta: Dict[str, str], out=sys.stdout) -> None:
    p = lambda *a: print(*a, file=out)  # noqa: E731 - a report is not a program

    p("afaudit  datasheet vs firmware alternate-function audit\n")
    p(f"  datasheet   {meta['datasheet']}")
    p(f"              {meta['part'] or 'unknown part'} - {table.pages} AF-table "
      f"pages, {len(table.pins)} pins, {table.cells} (function, AF) cells")
    p(f"  firmware    {meta['firmware']}")
    p(f"              {meta['rev']}")
    p(f"  family      {meta['family']}   ports {meta['ports']}"
      + (f"   UARTHARDWARE_MAX_PINS {meta['max_uart_pins']}"
         if meta.get("max_uart_pins") else ""))
    p(f"  targets     {meta['targets']}  ({meta['scope']})")
    if meta.get("excluded"):
        # One #ifdef STM32H7 block serves seven targets, so a row can be right
        # for a sibling this datasheet never mentions. Say so rather than let
        # the reader assume every finding below is a defect.
        p(f"  caveat      these tables are shared with {meta['excluded']}, not "
          f"audited\n              here - a 'pin not in datasheet' finding may "
          f"belong to one of them")
    checked = "  ".join(f"{k.upper()} {v}" for k, v in sorted(result.checked.items()))
    p(f"  checked     {checked or 'nothing'}  (pin, AF) pairs")
    if result.off_package:
        pins = ", ".join(sorted(result.off_package, key=lambda s: (s[1], int(s[2:]))))
        p(f"  off-package {len(result.off_package)} firmware pin(s) have no row in "
          f"this part's AF table\n              and were not judged: {pins}")

    if meta.get("silent"):
        # An unreadable table shape reports zero pairs, which looks identical to
        # a clean bill of health. It is not one.
        p(f"  NOT AUDITED {meta['silent']} - no pin table could be read for this "
          f"family;\n              those peripherals are unchecked, not clean")

    if meta.get("unresolved"):
        p(f"  unresolved  AF token(s) with no number behind them, so those pins "
          f"were\n              checked for existence only: {meta['unresolved']}")

    total = sum(result.checked.values())
    if total:
        # Honest confidence: a geometry parser that missed a column looks
        # exactly like a firmware full of phantom pins, so say which is likelier.
        pct = 100.0 * result.corroborated / total
        p(f"  extraction  {result.corroborated}/{total} firmware pairs found in "
          f"the AF table ({pct:.1f}%), {result.exact} with a matching AF number")
        if result.af_unknown:
            p(f"              {result.af_unknown} of those state no AF number at "
              f"all and were checked\n              for pin existence only")
        if pct < 90.0:
            p("              WARNING: a low corroboration rate usually means the "
              "table\n              layout was not parsed, not that the firmware "
              "is this wrong.\n              Treat 'pin not in datasheet' below as "
              "unproven.")
    p("")

    for kind in (WRONG_AF, NOT_IN_DATASHEET, ABSENT_PERIPHERAL, MISSING_PIN):
        rows = [f for f in result.findings if f.kind == kind]
        p(f"{HEADINGS[kind]}  -  {len(rows)}")
        if not rows:
            p("  none\n")
            continue
        for f in sorted(rows, key=lambda f: (f.peripheral, f.signal, f.pin)):
            if kind == WRONG_AF:
                p(f"  {f.signal:<14} {f.pin:<5} firmware AF{f.fw_af:<3} "
                  f"datasheet AF{f.ds_af:<3}  {f.where}")
            elif kind == NOT_IN_DATASHEET:
                shown = f"AF{f.fw_af}" if f.fw_af is not None else "AF unstated"
                p(f"  {f.signal:<14} {f.pin:<5} firmware {shown}  {f.where}")
                p(f"  {'':<14} {'':<5} {f.note}")
            else:
                extra = f"   [{f.note}]" if f.note else ""
                p(f"  {f.signal:<14} {f.pin:<5} datasheet AF{f.ds_af}{extra}")
        p("")

    bad = len(result.failures)
    p(f"{bad} defect(s), "
      f"{len([f for f in result.findings if f.kind == MISSING_PIN])} missing pin(s)")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def firmware_rev(fw: Path) -> str:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(fw), *args],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    rev, branch, date = (git("rev-parse", "--short=9", "HEAD"),
                         git("rev-parse", "--abbrev-ref", "HEAD"),
                         git("log", "-1", "--format=%cs"))
    return f"{rev or '?'} ({branch or '?'}, {date or '?'})"


def collect(fw: Path, family: str, targets: Dict[str, dict],
            wanted: Sequence[str], af: AfToken) -> List[PinEntry]:
    """
    Firmware claims for every target in the family, merged.

    Parsing once per target rather than once per family matters where a table
    is guarded per MCU (`#if defined(STM32H723xx)`): a single set of defines
    with two MCUs in it would satisfy both arms of an #if/#elif and invent
    entries no build ever sees.
    """
    sources: Dict[Path, Source] = {}

    def source(path: Optional[Path]) -> Optional[Source]:
        if path is None or not path.exists():
            return None
        if path not in sources:
            sources[path] = Source(path)
        return sources[path]

    seen: Set[PinEntry] = set()
    merged: List[PinEntry] = []
    for info in targets.values():
        defs = target_defs(info)
        found: List[PinEntry] = []
        if "uart" in wanted:
            s = source(_platform_file(fw, family, "serial_uart_stm32{lower}.c"))
            if s:
                found += fw_uart(s, defs, af)
        if "i2c" in wanted:
            rel = ("src/platform/STM32/bus_i2c_stm32f4xx.c" if family == "STM32F4"
                   else "src/platform/STM32/bus_i2c_ll_init.c")
            s = source(fw / rel)
            if s:
                found += fw_i2c(s, defs, af)
        if "spi" in wanted:
            s = source(fw / "src/platform/common/stm32/bus_spi_pinconfig.c")
            if s:
                found += fw_spi(s, defs, af)
        if "timer" in wanted and family == "STM32F4":
            s = source(_platform_file(fw, family, "timer_stm32{lower}.c"))
            if s:
                found += fw_timer_f4(s, defs, af)
        elif "timer" in wanted:
            s = source(fw / "src/platform/STM32/timer_def.h")
            if s:
                found += fw_timer(s, defs)
        for e in found:
            key = PinEntry(e.peripheral, e.signal, e.pin, e.af, e.file, 0)
            if key not in seen:
                seen.add(key)
                merged.append(e)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasheet", type=Path, required=True, help="ST datasheet PDF")
    ap.add_argument("--firmware", type=Path, required=True, help="Betaflight source tree")
    ap.add_argument("--family", help="STM32H5 etc; detected from the datasheet if omitted")
    ap.add_argument("--target", action="append",
                    help="restrict to this target dir (repeatable); default is "
                         "every target the datasheet names")
    ap.add_argument("--peripheral", default=",".join(PERIPHERALS),
                    help=f"comma-separated subset of {','.join(PERIPHERALS)}")
    ap.add_argument("--dump-af", type=Path,
                    help="write the extracted pin -> {function: AF} map as JSON")
    ap.add_argument("--json", type=Path, help="write findings as JSON")
    ap.add_argument("--no-waivers", action="store_true",
                    help="ignore data/af-waivers.json and report every finding")
    args = ap.parse_args()

    if not args.datasheet.exists():
        print(f"No such datasheet: {args.datasheet}", file=sys.stderr)
        return 2
    fw = args.firmware
    if not (fw / "src/main/target/common_pre.h").exists():
        print(f"Not a Betaflight tree: {fw}", file=sys.stderr)
        return 2
    wanted = [w.strip().lower() for w in args.peripheral.split(",") if w.strip()]
    unknown = set(wanted) - set(PERIPHERALS)
    if unknown:
        print(f"Unknown peripheral(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    pages = pdf_pages(args.datasheet)
    devices, detected = detect_devices(pages)
    family = args.family or detected
    if not family:
        print("Could not detect the MCU family from the datasheet; pass --family",
              file=sys.stderr)
        return 2

    table = extract_af_table(pages)
    if not table.pins:
        print(f"No alternate-function table found in {args.datasheet.name}. "
              f"This family's table layout is not parsed.", file=sys.stderr)
        return 2
    if args.dump_af:
        args.dump_af.write_text(json.dumps(table.pins, indent=1) + "\n")

    targets = {n: i for n, i in parse_targets(fw).items() if i["family"] == family}
    if not targets:
        print(f"No {family} target under {fw}/src/platform", file=sys.stderr)
        return 2
    family_targets = dict(targets)
    scope = "all targets in the family"
    if args.target:
        targets = {n: i for n, i in targets.items() if n in set(args.target)}
        scope = "--target"
        if not targets:
            print(f"No such {family} target: {', '.join(args.target)}", file=sys.stderr)
            return 2
    elif devices:
        # Judging an H723-only table row against an H743 datasheet produces a
        # page of confident nonsense, so audit only what this part covers.
        named = {n: i for n, i in targets.items() if covers(devices, n, i["mcu"])}
        if named:
            targets, scope = named, "named by the datasheet"

    ports: Set[str] = set()
    for name, info in targets.items():
        ports |= fw_ports(fw / "src/platform" / info["platform"] / "target" / name / "target.h")
    if not ports:
        ports = set("ABCDEFGHIJK")

    af = AfToken(fw)
    entries = collect(fw, family, targets, wanted, af)
    max_pins = fw_max_uart_pins(fw, target_defs(next(iter(targets.values()))))
    result = audit(entries, table, ports, max_pins)

    report(result, table, {
        "datasheet": str(args.datasheet),
        "part": " ".join(devices),
        "scope": scope,
        "firmware": str(fw),
        "rev": firmware_rev(fw),
        "family": family,
        "targets": ", ".join(sorted(targets)),
        "ports": "".join(sorted(ports)),
        "max_uart_pins": max_pins,
        "excluded": ", ".join(sorted(set(family_targets) - set(targets))),
        "unresolved": ", ".join(sorted(af.unresolved)),
        "silent": ", ".join(sorted(set(wanted) - set(result.checked))),
    })

    if args.json:
        args.json.write_text(json.dumps({
            "datasheet": str(args.datasheet),
            "devices": devices,
            "family": family,
            "firmware": str(fw),
            "revision": firmware_rev(fw),
            "targets": sorted(targets),
            "ports": sorted(ports),
            "checked": dict(result.checked),
            "off_package": sorted(result.off_package),
            "unresolved_af_tokens": sorted(af.unresolved),
            "corroborated": result.corroborated,
            "exact": result.exact,
            "af_unknown": result.af_unknown,
            "findings": [f.__dict__ for f in result.findings],
        }, indent=1) + "\n")

    # Waivers are applied last, so the report above always shows the raw truth
    # and only the exit status is softened.
    waivers = [] if args.no_waivers else load_waivers()
    stem = args.datasheet.stem
    failures = result.failures
    waived, unwaived = [], []
    for f in failures:
        hit = next((w for w in waivers if w.matches(f, stem)), None)
        (waived if hit else unwaived).append((f, hit))

    # A waiver that no longer matches anything has either been fixed or has
    # rotted. Either way it must be seen: a stale waiver silently hiding a
    # future finding is exactly the failure this mechanism could introduce.
    stale = [w for w in waivers
             if w.datasheet.lower() in stem.lower()
             and not any(w.matches(f, stem) for f in failures)]

    if waived:
        print(f"\nWAIVED  {len(waived)} known divergence(s), see "
              f"{WAIVER_FILE.name}:")
        for f, w in waived:
            print(f"  {f.signal:14} {f.pin:5} {w.reason[:96]}")
    if stale:
        print(f"\nSTALE WAIVER  {len(stale)} waiver(s) matched nothing - firmware "
              "may have changed; remove them or re-verify:")
        for w in stale:
            print(f"  {w.signal:14} {w.pin:5} ({w.datasheet})")

    return 1 if (unwaived or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
