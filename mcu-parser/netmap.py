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
netmap.py - Recover the MCU pin <-> net wiring from a schematic PDF.

Altium plots the MCU as a rectangle with pin names inside one edge, pin numbers
just outside it, and net labels further out on the wires. All three are separate
text runs; only their geometry ties them together. This module reconstructs that
association.

Two details make it work:

  * Text extraction uses `pdftotext -bbox-layout`, not pdfplumber. On these
    vector schematics pdfplumber returns none of the MCU pin-name strings at all
    and mangles adjacent labels; poppler returns every one cleanly.

  * The vertical offset between a net label and its pin name is not assumed. Net
    labels sit above the wire while pin names are centred on it, and the gap
    scales with the sheet's font size. So candidate offsets are scored against
    the firmware capability map (see seed_firmware.py) and the best-agreeing one
    wins. A net called TX4 has to land on a pin that really has a UART4 TX
    function; an off-by-one-row error scores near zero and cannot survive.

Usage:  python netmap.py <schematic.pdf> [--target STM32F722]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# A pin name, optionally carrying ST's own dash qualifier and then an AF list:
#   PB2   PA0-WKUP   PC14-OSC32_IN   PC6/TIM3_CH1/TIM8_CH1/USART6_TX
# The qualifier names a second *fixed* function of the pin (wakeup, oscillator),
# not an alternate function, so it is dropped rather than parsed as one.
PIN_RE = re.compile(r"^(P[A-K]\d{1,2})(?:-[A-Z0-9_]+)?(?:/(.*))?$")

# Supply and system pins. They are not routable, but they occupy rows in the
# symbol, so knowing where they are is what lets the tool say "this net landed on
# VCAP" instead of quietly discarding it.
POWER_PIN_RE = re.compile(
    r"^(VCAP\w*|VSS\w*|VDD\w*|VBAT|VREF\+?|VLX\w*|NRST|RST|BOOT0?|PDR_ON)"
    r"(?:/.*)?$", re.IGNORECASE,
)
DATA_FILE = Path(__file__).parent / "data" / "firmware.json"


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 1

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


PAGE_RE = re.compile(r"<page\b[^>]*>(.*?)</page>", re.S)
WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>',
    re.S,
)


def _words_in(xml: str, page: int) -> List[Word]:
    words: List[Word] = []
    for m in WORD_RE.finditer(xml):
        x0, y0, x1, y1, text = m.groups()
        text = re.sub(r"&amp;", "&", text).strip()
        if text:
            words.append(Word(text, float(x0), float(y0), float(x1), float(y1), page))
    return words


def extract_words(pdf: Path) -> List[Word]:
    """
    Every word in the document, each tagged with the sheet it was drawn on.

    Sheets of one plot share a coordinate space - page 1 and page 4 of an A3 set
    both run y 29-561 - so a flattened word list puts unrelated sheets on the
    same rows. Anything downstream that reasons about geometry has to stay
    inside a page, or it will pair a net label with a pin drawn elsewhere.
    """
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found - install poppler-utils")
    out = subprocess.run(
        ["pdftotext", "-bbox-layout", str(pdf), "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    pages = PAGE_RE.findall(out)
    if not pages:
        # No <page> markup at all: treat the document as a single sheet rather
        # than losing every word.
        return _words_in(out, 1)
    return [w for n, body in enumerate(pages, 1) for w in _words_in(body, n)]


def page_count(words: Sequence[Word]) -> int:
    return max((w.page for w in words), default=1)


# --------------------------------------------------------------------------- #
# Symbol detection
# --------------------------------------------------------------------------- #

@dataclass
class PinRow:
    pin: str                 # PA0, or VCAP for a supply row
    y: float                 # y0 of the pin-name text
    side: str                # 'L' or 'R'
    afs: List[str] = field(default_factory=list)   # from the symbol, if present
    gpio: bool = True        # False for supply/system rows


@dataclass
class SymbolPart:
    """One page's worth of symbol. Edges and the row band are per page, because
    coordinates only mean anything relative to the sheet they were drawn on."""
    page: int
    rows: List[PinRow]
    left_edge: float
    right_edge: float

    @property
    def y_min(self) -> float:
        return min(r.y for r in self.rows)

    @property
    def y_max(self) -> float:
        return max(r.y for r in self.rows)

    @property
    def pins(self) -> set[str]:
        return {r.pin for r in self.rows if r.gpio}


@dataclass
class Symbol:
    parts: List[SymbolPart]
    pitch: float
    page_count: int = 1
    # Pages carrying a rival symbol that was not merged - reported, never used.
    ignored_pages: List[int] = field(default_factory=list)

    @property
    def rows(self) -> List[PinRow]:
        return [r for p in self.parts for r in p.rows]

    @property
    def page(self) -> int:
        return self.parts[0].page

    @property
    def pages(self) -> List[int]:
        """The distinct sheets the symbol occupies, in order."""
        return sorted({p.page for p in self.parts})

    @property
    def split(self) -> bool:
        """Drawn as more than one box, wherever those boxes are."""
        return len(self.parts) > 1

    @property
    def split_across_pages(self) -> bool:
        """Drawn on more than one sheet, as opposed to several boxes on one."""
        return len(self.pages) > 1

    @property
    def left_edge(self) -> float:
        return self.parts[0].left_edge

    @property
    def right_edge(self) -> float:
        return self.parts[0].right_edge

    @property
    def y_min(self) -> float:
        return min(r.y for r in self.rows)

    @property
    def y_max(self) -> float:
        return max(r.y for r in self.rows)

    @property
    def has_af_lists(self) -> bool:
        return sum(1 for r in self.rows if r.afs) >= len(self.rows) / 2


def cluster(items: Sequence[Tuple[float, object]], tol: float) -> List[List[object]]:
    """
    Group items by a 1-D coordinate, splitting only where the gap exceeds `tol`.

    Fixed-width buckets are not good enough here: Altium's right-aligned pin
    names jitter by a few tenths of a point, and a bucket boundary falling inside
    that jitter silently splits one symbol edge into two, losing pins.
    """
    if not items:
        return []
    ordered = sorted(items, key=lambda t: t[0])
    groups: List[List[object]] = [[ordered[0][1]]]
    last = ordered[0][0]
    for coord, payload in ordered[1:]:
        if coord - last > tol:
            groups.append([])
        groups[-1].append(payload)
        last = coord
    return groups


# Altium stamps an invisible annotation token beside everything it draws:
# PIU1014 (pin 14 of U10), COU8 (component U8), NLTX2 (net label TX2). They are
# placed within a hair of the object they annotate - closer than the gap that
# separates the pieces of a split pin name - so absorbing one destroys the name
# it is attached to: 'PC0' + 'PIU108' becomes 'PC0PIU108', which no longer reads
# as a pin, and the row is dropped with it.
#
# The designator letters are spelled out rather than [A-Z]{1,3}, which would
# also swallow real alternate functions - G4 pin names carry COMP1_OUT.
ANNOT_RE = re.compile(
    r"^(?:PI|CO)(?:C|R|L|D|Q|U|Y|J|P|X|FB|TP|SW|RN|BT|VR)\d{1,6}$"
    r"|^NL[A-Z0-9_]*$",
)


def assemble_pin_names(words: Sequence[Word], gap: float = 2.5) -> List[Word]:
    """
    Rejoin pin-name strings that the extractor split into several words.

    Long alternate-function lists come back in pieces:
        'PC6/TIM3_CH1/TIM8_CH1/'  +  'USART6_TX'
    That matters for more than tidiness. Right-hand pin names are right-aligned
    on the symbol edge, but the piece holding the PXn token ends early, so it
    misses the edge cluster and the whole row - pin and net - is silently lost.
    Stitching the pieces back together first restores the true row extent.

    An annotation token ends the name rather than joining it; see ANNOT_RE.
    """
    out: List[Word] = []
    for w in words:
        if not (PIN_RE.match(w.text) or POWER_PIN_RE.match(w.text)):
            continue
        text, x1 = w.text, w.x1
        # Absorb words butting up against the right-hand end, repeatedly, so a
        # name broken into three or more pieces is reassembled too.
        while True:
            nxt = [v for v in words
                   if abs(v.y0 - w.y0) < 0.6 and -0.1 <= v.x0 - x1 < gap]
            if not nxt:
                break
            v = min(nxt, key=lambda v: v.x0)
            if ANNOT_RE.match(v.text):
                break
            text, x1 = text + v.text, v.x1
        out.append(Word(text, w.x0, w.y0, x1, w.y1, w.page))
    return out


Tagged = Tuple[Word, str, List[str], bool]


def _tag_pins(words: Sequence[Word]) -> List[Tagged]:
    """Every word that reads as a pin name or a supply pin, with its AF list."""
    tagged: List[Tagged] = []
    for w in assemble_pin_names(words):
        m = PIN_RE.match(w.text)
        if m:
            pin, rest = m.group(1), m.group(2)
            afs = [a for a in (rest.split("/") if rest else []) if a]
            tagged.append((w, pin, afs, True))
            continue
        p = POWER_PIN_RE.match(w.text)
        if p:
            tagged.append((w, p.group(1).upper(), [], False))
    return tagged


def _find_part(words: Sequence[Word], page: int, min_pins: int
               ) -> Optional[Tuple[SymbolPart, float]]:
    """
    The strongest symbol on one sheet, as (part, row pitch), or None if this
    sheet has none. Returns rather than raises: most pages carry no MCU at all.
    """
    got = _part_from_tagged(_tag_pins(words), page, min_pins)
    return (got[0], got[1]) if got else None


def _find_parts(words: Sequence[Word], page: int, min_pins: int,
                limit: int = 6) -> List[Tuple[SymbolPart, float]]:
    """
    Every distinct symbol box on one sheet, strongest first.

    A large MCU is routinely drawn as several boxes on the *same* sheet - one
    per port group, often with a separate supply box. Each is its own edge
    alignment, so keeping only the strongest silently drops the others: the
    pins that were read stay correct and agreement stays at 100%, which is
    precisely what makes the loss invisible. One H743 board here read 43 of its
    100 pins that way and reported no problem at all.

    So peel boxes off one at a time and let the caller decide which of them are
    the same MCU - the merge test is the same one used for a symbol split
    across sheets, and it is what rejects a second MCU or a repeated symbol.
    """
    tagged = _tag_pins(words)
    out: List[Tuple[SymbolPart, float]] = []
    for _ in range(limit):
        got = _part_from_tagged(tagged, page, min_pins)
        if not got:
            break
        part, pitch, used = got
        out.append((part, pitch))
        tagged = [t for t in tagged if id(t) not in used]
    return out


def _part_from_tagged(tagged: Sequence[Tagged], page: int, min_pins: int
                      ) -> Optional[Tuple[SymbolPart, float, set]]:
    """
    The strongest aligned column pair in `tagged`, plus the items it consumed
    so a caller can look for the next box in what is left over.
    """
    left = max(cluster([(t[0].x0, t) for t in tagged], tol=1.0), key=len, default=[])
    right = max(cluster([(t[0].x1, t) for t in tagged], tol=1.0), key=len, default=[])
    # A pin name belongs to whichever edge claimed it; drop the overlap so a
    # single-column symbol is not counted twice. Names of unequal length share
    # an x1 but not an x0, so such a symbol also throws up a partial second
    # "edge" holding just its shortest names - which sits a character's width
    # from the real one and would pair that half of the rows against whatever
    # text happens to lie to its left.
    if {id(t) for t in left} == {id(t) for t in right}:
        right = []
    elif len(right) >= len(left):
        claimed = {id(t) for t in right}
        left = [t for t in left if id(t) not in claimed]
    else:
        claimed = {id(t) for t in left}
        right = [t for t in right if id(t) not in claimed]
    if len(left) + len(right) < min_pins:
        return None

    rows: List[PinRow] = []
    for w, pin, afs, gpio in left:
        rows.append(PinRow(pin, w.y0, "L", afs, gpio))
    for w, pin, afs, gpio in right:
        rows.append(PinRow(pin, w.y0, "R", afs, gpio))
    rows.sort(key=lambda r: r.y)

    left_edge = min(w.x0 for w, *_ in left) if left else min(w.x0 for w, *_ in right)
    right_edge = max(w.x1 for w, *_ in right) if right else max(w.x1 for w, *_ in left)

    # Row pitch = the most common gap between adjacent distinct rows.
    ys = sorted({round(r.y, 2) for r in rows})
    gaps = [round(b - a, 2) for a, b in zip(ys, ys[1:]) if 0.5 < b - a < 20]
    pitch = min(gaps) if gaps else 3.66
    used = {id(t) for t in left} | {id(t) for t in right}
    return SymbolPart(page, rows, left_edge, right_edge), pitch, used


def _is_split_half(accepted: Sequence[SymbolPart], pitch: float,
                   part: SymbolPart, part_pitch: float) -> bool:
    """
    Does this second sheet hold the other half of the same MCU symbol?

    Large designs do split one MCU across sheets, and taking only the bigger
    half would drop real pins with no sign that anything was lost. But a sheet
    that merely repeats the symbol, or carries a second MCU, would be merged
    into nonsense - so the bar is deliberately high. A genuine half shares no
    pin with what is already accepted (the two halves partition the package),
    is drawn on the same grid, and is substantial in its own right.
    """
    mine = set().union(*(p.pins for p in accepted))
    theirs = part.pins
    if len(theirs) < 12 or len(theirs) * 3 < len(mine):
        return False
    if mine & theirs:
        return False
    return abs(part_pitch - pitch) <= pitch * 0.1


# Words any flight-controller sheet has in quantity, whatever the vendor's
# naming style. Used only to tell "this text means nothing" from "this text
# means something the tool did not expect".
SCHEMATIC_VOCAB = re.compile(
    r"^(?:P[A-K]\d{1,2}|GND|VCC|VDD|VSS|VBAT|3V3|5V|NRST|BOOT0?|"
    r"(?:SPI|I2C|USART|UART|TIM|ADC|DMA)\d|MOTOR\d?|SCL|SDA|SCK|MISO|MOSI)",
    re.I,
)


def describe_unreadable(words: Sequence[Word]) -> Optional[str]:
    """
    Why this document yielded nothing, when the answer is not about geometry.

    Two cases look identical from here and neither is a parse failure, so
    reporting them as one sends the reader after the wrong problem:

      * A scan. No text layer at all, and nothing short of OCR will help.
      * A text layer that carries no characters. Some CAD exporters write every
        glyph as a Type 3 drawing procedure named /0, /1, /2 ... with no
        ToUnicode map, so the extractor recovers the byte codes rather than the
        letters and 'Interface Type' comes back as 'RNLC@S?ICÿTFCA'. There is
        plenty of text, and none of it means anything.

    Both are recognised by the same test: a real sheet carries this vocabulary
    in quantity. Returns None when the text is fine and the failure really is
    the symbol.
    """
    if len(words) < 25:
        return ("This PDF has no text layer - it is a scan or an image export, "
                "so there is nothing to read. Ask the vendor for the PDF their "
                "CAD tool produced rather than a printed or photographed copy.")
    known = sum(1 for w in words if SCHEMATIC_VOCAB.match(w.text))
    if known >= 5:
        return None
    return (f"This PDF's text carries no characters: {len(words)} words were "
            f"extracted and {known} of them read as anything a schematic "
            "contains - no pin names, no GND, no bus names. The fonts are drawn "
            "glyphs with no character mapping (pdffonts will show Type 3, "
            "Custom encoding, uni=no), so the extractor recovers the internal "
            "codes instead of the letters. Nothing here can read it. Ask the "
            "vendor to re-export with embedded standard fonts, or for the "
            "source schematic.")


def find_symbol(words: Sequence[Word], min_pins: int = 8,
                page: Optional[int] = None) -> Symbol:
    """
    Locate the MCU symbol as the largest set of pin-name words sharing an edge
    alignment. Left-side names are left-aligned (common x0), right-side names are
    right-aligned (common x1).

    Detection runs per page and the strongest symbol wins, because sheets share
    a coordinate space: assembled from a flattened word list, a symbol happily
    takes rows - and later, net labels - from an unrelated sheet, and the
    firmware check only rejects the subset of those that name a function.
    """
    by_page: Dict[int, List[Word]] = defaultdict(list)
    for w in words:
        by_page[w.page].append(w)
    npages = page_count(words)
    if page is not None:
        by_page = {page: by_page.get(page, [])}

    found = [got for p, ws in sorted(by_page.items())
             for got in _find_parts(ws, p, min_pins)]
    if not found:
        raise SystemExit(describe_unreadable(words)
                         or "Could not find an MCU symbol - no aligned pin-name "
                            "column")

    # Strength is distinct GPIO pins, not rows: a single-column symbol whose
    # names cluster on both alignments would otherwise count itself twice.
    found.sort(key=lambda f: (-len(f[0].pins), -len(f[0].rows), f[0].page))
    (best, pitch), rivals = found[0], found[1:]

    parts, pitches, ignored = [best], [pitch], []
    for part, part_pitch in rivals:
        if _is_split_half(parts, pitch, part, part_pitch):
            parts.append(part)
            pitches.append(part_pitch)
        elif len(part.pins) >= min_pins and part.page not in ignored:
            ignored.append(part.page)
    parts.sort(key=lambda p: (p.page, -len(p.rows)))
    return Symbol(parts, min(pitches), npages, sorted(ignored))


# Gutter text that is not a net label: component designators (C50, R21, U3),
# package/value strings (04-0.1uF/16V/X5R, 04-10K/5%), and part numbers.
JUNK_RE = re.compile(
    # Reference designators. Spelled out rather than [A-Z]{1,3}\d+, which would
    # also swallow net names like TX4 and RX1.
    r"^(?:C|R|L|D|Q|U|Y|J|P|X|FB|TP|SW|RN|BT|VR)\d{1,3}$"
    r"|^\d{2}-"                                  # 04-0.1uF/16V/X5R, 08-22uF
    r"|\d\s*(?:uF|nF|pF|UH|MHZ|K|R|V)\b"         # bare values
    r"|^[\d.]+%?$"                               # pin numbers, percentages
    r"|^NL[A-Z0-9_]*$"                           # Altium net-label helper text
    r"|/\d|@",                                   # MS0518/6.8UH, 200R@100MHZ
    re.IGNORECASE,
)

# Power rails: real nets, but they belong to supply pins we never emit. USB data
# and SWD lines are deliberately NOT listed - they are genuine GPIO connections,
# so keeping them makes the "unconnected pins" report honest.
POWER_RE = re.compile(
    r"^(?:GND|VCC|VDD|VDDA|VSS|VSSA|VBAT|VBUS|VIN\+?|VREF\+?|3V3|1V8|"
    r"MCU3V3|DCDC\d*V?|\+?[\d.]+V)$",
    re.IGNORECASE,
)


def find_net_labels(words: Sequence[Word], sym: Symbol) -> List[Word]:
    """
    Text in the gutters either side of the symbol, level with its rows.

    Net labels sit in one aligned column per side, close to the symbol edge;
    everything further out belongs to neighbouring components. Rather than guess
    a distance threshold, cluster the gutter text by its alignment coordinate and
    keep the substantial column nearest the edge.

    Only the symbol's own sheet is searched. Every sheet occupies the same
    coordinates, so words from the others sit inside the row band too and would
    compete - on equal terms - to be the net-label column.
    """
    # A label drawn between two boxes of the same symbol is in the right-hand
    # gutter of one and the left-hand gutter of the other, so it is collected
    # twice. Keep one copy; which row it belongs to is settled by _owner(),
    # which picks the nearer edge.
    seen: set = set()
    out: List[Word] = []
    for part in sym.parts:
        for w in _labels_for_part(words, part, sym.pitch):
            if id(w) not in seen:
                seen.add(id(w))
                out.append(w)
    return out


def _labels_for_part(words: Sequence[Word], part: SymbolPart,
                     pitch: float) -> List[Word]:
    pad = pitch * 2
    lo, hi = part.y_min - pad, part.y_max + pad

    # Only a side this part actually has pin names on can have a net-label
    # gutter. A tall MCU is often drawn as two separate boxes on one sheet, and
    # each is found as a one-sided part; searching the empty side would reach
    # across the gap and collect the *other* box's labels, binding every net
    # twice and leaving one box's rows with nothing.
    sides: Dict[str, List[Word]] = {r.side: [] for r in part.rows}
    for w in words:
        if w.page != part.page:
            continue
        if not (lo <= w.y0 <= hi) or PIN_RE.match(w.text):
            continue
        if JUNK_RE.search(w.text) or POWER_RE.match(w.text):
            continue
        if w.x1 < part.left_edge and "L" in sides:
            sides["L"].append(w)
        elif w.x0 > part.right_edge and "R" in sides:
            sides["R"].append(w)

    out: List[Word] = []
    for side, cands in sides.items():
        cols = []
        # Labels may be aligned on either edge of their own text, so try both.
        for anchor in ("x0", "x1"):
            groups = cluster([(getattr(w, anchor), w) for w in cands], tol=1.0)
            for col in (c for c in groups if len(c) >= 3):
                # Score by whether the column reads like net names, not merely
                # by how near it is. A BGA sheet puts its ball coordinates (F8,
                # H9, A3) exactly where a net label would sit, and proximity
                # alone happily picks those.
                hits = sum(1 for w in col if NET_VOCAB.search(w.text))
                edge = (max(w.x1 for w in col) if side == "L"
                        else -min(w.x0 for w in col))
                cols.append((hits, edge, col))
        if not cols:
            continue
        cols.sort(key=lambda c: (c[0], c[1]), reverse=True)
        best_hits, best_edge, _ = cols[0]
        if best_hits == 0:
            continue           # nothing in the gutter resembles a net name

        # Not every sheet lines its labels up. Where each is drawn against its
        # own wire the gutter is several near-parallel columns a few points
        # apart, and keeping only the strongest drops the rest - one board lost
        # its whole I2C2 bus that way while reporting nothing amiss.
        #
        # So take the neighbouring columns too, but only the words in them that
        # read like net names on their own. The strongest column is trusted
        # wholesale because it has already proved itself as a column; the rest
        # have not, and a BGA sheet fills that same space with ball coordinates
        # (E10, B10) which would otherwise bind to whatever row they sit level
        # with.
        band = pitch * 3
        keep: Dict[int, Word] = {}
        for i, (hits, edge, col) in enumerate(cols):
            if not hits or best_edge - edge > band:
                continue
            for w in col:
                if i == 0 or NET_VOCAB.search(w.text):
                    keep[id(w)] = w
        out.extend(keep.values())
    return out


# --------------------------------------------------------------------------- #
# Net-name semantics: what function must a pin support?
# --------------------------------------------------------------------------- #

# The vocabulary flight-controller schematics use for nets that end up in a
# config.h. Used to tell a net-label column apart from a column of package ball
# coordinates or component values.
NET_VOCAB = re.compile(
    r"MOTOR|SERVO|GYRO|IMU|MPU|ICM|OSD|MAX7456|AT7456|FLASH|BARO|SDCARD|"
    r"\bTX\d|\bRX\d|I2C\d|SCL|SDA|SCK|MISO|MOSI|\bCS\b|CS_|_CS|EXTI|CLKIN|CLOCK|"
    r"LED|BEEP|BUZZ|CAM|VTX|USER\d|PINIO|ADC|BATT|CURR|RSSI|SWDIO|SWCLK|BOOT|OTG",
    re.IGNORECASE,
)

# (regex on the upper-cased net name) -> (kind, detail)
NET_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:.*[-_])?I2C(\d)[-_]SCL$"), "i2c_scl"),
    (re.compile(r"^(?:.*[-_])?I2C(\d)[-_]SDA$"), "i2c_sda"),
    (re.compile(r"^(?:.*[-_])?T(?:X)(\d)(?:[-_]?R)?$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?R(?:X)(\d)(?:[-_]?R)?$"), "uart_rx"),
    (re.compile(r"^(?:.*[-_])?U(?:ART)?(\d)[-_]TX$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?U(?:ART)?(\d)[-_]RX$"), "uart_rx"),
    (re.compile(r"^.*[-_]SCK$|^.*[-_]SCLK$"), "spi_sck"),
    # MISO/MOSI name a direction from somebody's point of view, and vendors
    # disagree about whose - one sheet puts SPI1_MOSI on the MCU's SDI pin and
    # SPI1_MISO on its SDO pin. So a data line is only checked for being a data
    # line; which one it is comes from the firmware map, in genconfig.
    (re.compile(r"^.*[-_](?:MISO|MOSI|SDI|SDO(?:UT)?)$"), "spi_data"),
    (re.compile(r"^MOTOR(\d+)$|^M(\d)$"), "timer"),
    (re.compile(r"^.*LED[-_]?STRIP.*$"), "timer"),
    (re.compile(r"^.*(?:CLOCK|CLKIN)$"), "timer"),
    (re.compile(r"^ADC[-_].*$|^.*[-_](?:BATT|VBAT|CURR|CURRENT|RSSI)$"), "adc"),
]


def net_requirement(net: str) -> Optional[Tuple[str, Optional[str]]]:
    """('uart_tx', '4') for TX4; None when the net constrains nothing."""
    n = net.upper()
    for rx, kind in NET_RULES:
        m = rx.match(n)
        if m:
            idx = next((g for g in m.groups() if g), None)
            return kind, idx
    return None


def afs_support(afs: Sequence[str], kind: str, idx: Optional[str]) -> Optional[bool]:
    """
    Second opinion from the symbol's own alternate-function list.

    Betaflight's tables list the pins it has implemented, which is not always
    everything the silicon offers. When the firmware rejects a net, knowing
    whether the symbol independently claims that function separates "Betaflight
    needs a new pin option" from "this schematic is wrong" - and only the first
    is worth raising as a firmware change.

    Returns None when the symbol carries no AF list to consult.
    """
    if not afs:
        return None
    toks = [re.sub(r"[-_]", "", a).upper() for a in afs]

    def has(*pats: str) -> bool:
        return any(re.fullmatch(p, t) for t in toks for p in pats)

    if kind == "timer":
        return has(r"TIM\d+CH\d+N?")
    if kind == "adc":
        return has(r"ADC\d*INP?\d+")
    if kind in ("i2c_scl", "i2c_sda"):
        role = kind.split("_")[1].upper()
        return has(rf"I2C{idx or r'\d'}{role}")
    if kind in ("uart_tx", "uart_rx"):
        d = kind.split("_")[1].upper()
        return has(rf"U?S?ART{idx or r'\d+'}{d}", rf"LPUART{idx or r'\d+'}{d}")
    if kind == "spi_sck":
        return has(r"SPI\d+SCK", r"I2S\d+CK")
    if kind == "spi_data":
        return has(r"SPI\d+MISO", r"SPI\d+MOSI", r"SPI\d+SD[IO]")
    return None


def pin_supports(caps: dict, pin: str, kind: str, idx: Optional[str]) -> bool:
    """Does the firmware capability map say this pin can do this?"""
    if kind == "timer":
        return bool(caps["timers"].get(pin))
    if kind == "adc":
        return pin in caps["adc"]
    if kind.startswith("i2c"):
        role = kind.split("_")[1]
        return any(e["role"] == role and (not idx or e["dev"] == f"I2C{idx}")
                   for e in caps["i2c"].get(pin, []))
    if kind.startswith("uart"):
        want = kind.split("_")[1]
        return any(e["dir"] == want and (not idx or e["dev"] == f"UART{idx}")
                   for e in caps["uart"].get(pin, []))
    if kind == "spi_sck":
        return any(e["role"] == "sck" for e in caps["spi"].get(pin, []))
    if kind == "spi_data":
        return any(e["role"] in ("sdi", "sdo") for e in caps["spi"].get(pin, []))
    return True


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

@dataclass
class Link:
    net: str
    pin: str
    side: str
    checked: bool          # did the net name imply a testable requirement?
    ok: bool               # ...and was it satisfied?
    afs: List[str] = field(default_factory=list)
    gpio: bool = True      # False when the net landed on a supply/system pin
    symbol_ok: Optional[bool] = None   # does the symbol's own AF list agree?


@dataclass
class Result:
    links: List[Link]
    offset: float
    score: Tuple[int, int]         # (satisfied, checkable)
    symbol: Symbol
    unmapped: List[str]
    orphans: List[str] = field(default_factory=list)   # labels that matched no row

    @property
    def agreement(self) -> float:
        return self.score[0] / self.score[1] if self.score[1] else 0.0

    @property
    def on_power_pin(self) -> List[Link]:
        """
        Nets on a supply/system row that do not belong there. A BOOT net on the
        BOOT0 pin or VBAT on VBAT is exactly right; it is only worth reporting
        when the net is something else entirely.
        """
        out = []
        for l in self.links:
            if l.gpio:
                continue
            net = re.sub(r"[^A-Z0-9]", "", l.net.upper())
            pin = re.sub(r"[^A-Z0-9]", "", l.pin.upper())
            if net.startswith(pin) or pin.startswith(net):
                continue
            out.append(l)
        return out


def _owner(sym: Symbol, w: Word) -> Optional[Tuple[SymbolPart, str]]:
    """
    The part whose gutter this label sits in, and which side of it.

    Keyed on geometry rather than on the page, because two parts of one symbol
    are routinely drawn side by side on the *same* sheet. A page lookup silently
    keeps whichever of them was stored last, and every label then resolves
    against that one box - so the other box's nets pair with nothing and its
    pins are all reported unconnected.
    """
    best: Optional[Tuple[SymbolPart, str, float]] = None
    for p in sym.parts:
        if p.page != w.page:
            continue
        for side in ("L", "R"):
            if not any(r.side == side for r in p.rows):
                continue
            if side == "L" and w.x1 < p.left_edge:
                d = p.left_edge - w.x1
            elif side == "R" and w.x0 > p.right_edge:
                d = w.x0 - p.right_edge
            else:
                continue
            if best is None or d < best[2]:
                best = (p, side, d)
    return (best[0], best[1]) if best else None


def _pair(sym: Symbol, labels: Sequence[Word], offset: float
          ) -> Tuple[List[Tuple[Word, PinRow]], List[Word]]:
    pairs, orphans = [], []
    for w in labels:
        owner = _owner(sym, w)
        if owner is None:
            orphans.append(w)
            continue
        part, side = owner
        cands = [r for r in part.rows if r.side == side]
        best = min(cands, key=lambda r: abs(r.y - (w.y0 + offset)))
        if abs(best.y - (w.y0 + offset)) <= sym.pitch / 2:
            pairs.append((w, best))
        else:
            orphans.append(w)
    return pairs, orphans


def resolve(sym: Symbol, labels: Sequence[Word], caps: dict) -> Result:
    """
    Try candidate label->row offsets and keep the one the firmware agrees with
    most. Offsets are swept in fine steps across +/- 1.5 rows, which covers the
    'label above the wire, name centred on it' convention at any font size.

    Labels that pair with nothing are carried out as `orphans` rather than
    dropped. A silently discarded net just looks like an incomplete config later,
    with no clue why - and the usual cause is worth seeing: a net wired to a pin
    the symbol names as a supply, which is a schematic problem, not a parse one.

    One offset is swept for the whole symbol, including a split one: halves of
    the same symbol come off the same Altium template, so they share a font size
    and a grid, and scoring them together is what keeps the winning offset from
    being chosen on half the evidence.
    """
    step = sym.pitch / 12
    candidates = [i * step for i in range(-18, 19)]

    best: Optional[Result] = None
    best_key: Optional[Tuple] = None
    for off in candidates:
        pairs, orphans = _pair(sym, labels, off)
        sat = chk = 0
        onpower = 0
        links: List[Link] = []
        for w, row in pairs:
            if not row.gpio:
                onpower += 1
            req = net_requirement(w.text)
            ok, checked, sym_ok = True, False, None
            if req and row.gpio:
                checked = True
                ok = pin_supports(caps, row.pin, req[0], req[1])
                if not ok:
                    sym_ok = afs_support(row.afs, req[0], req[1])
                chk += 1
                sat += int(ok)
            links.append(Link(w.text, row.pin, row.side, checked, ok, row.afs,
                              row.gpio, sym_ok))
        # Prefer agreement, then the absence of evidence against, then coverage;
        # a tie goes to the smaller shift.
        #
        # A pin the firmware says cannot do the job counts *against* the offset.
        # Only agreements used to count, so two fits with equal agreement were
        # separated on raw coverage - and the one that paired more labels won
        # even when the extra pairs were contradictions. One board picked a fit
        # three quarters of a row out that way, sliding SWDIO onto its
        # neighbour's pin and several nets onto supply rows.
        #
        # Landing on a supply row is the same kind of evidence: no schematic
        # wires FLASH_CS to VBAT or OTG_FS_DP to VSS. It ranks below a firmware
        # contradiction because a sheet can legitimately run a net past one.
        key = (sat, -(chk - sat), -onpower, len(pairs), -abs(off))
        if best_key is None or key > best_key:
            best_key = key
            mapped = {l.pin for l in links}
            unmapped = [r.pin for r in sym.rows
                        if r.gpio and r.pin not in mapped]
            best = Result(links, off, (sat, chk), sym, sorted(set(unmapped)),
                          [w.text for w in orphans])
    assert best is not None
    return best


def load_caps(target: str, data_file: Path = DATA_FILE) -> Tuple[dict, dict]:
    if not data_file.exists():
        raise SystemExit(
            f"{data_file} missing - run:  python mcu-parser/seed_firmware.py"
        )
    data = json.loads(data_file.read_text())
    if target not in data["targets"]:
        known = ", ".join(sorted(data["targets"]))
        raise SystemExit(f"Unknown target '{target}'. Known: {known}")
    return data["targets"][target], data


def detect_target(words: Sequence[Word], data: dict) -> Optional[str]:
    """
    Match a part number on the sheet to an FC_TARGET_MCU name.

    Betaflight names a target after one representative part, so silicon it also
    supports will not match by name: an STM32G473 board builds the STM32G474
    target. Try the exact name first, then fall back to the only target in the
    same family.
    """
    blob = " ".join(w.text.upper() for w in words)
    names = {n.upper(): n for n in data["targets"]}
    for m in re.finditer(r"STM32([FGHCN]\d)([\dX]{2})", blob):
        family, digits = m.group(1), m.group(2)
        exact = names.get(f"STM32{family}{digits}")
        if exact:
            return exact
        siblings = sorted(n for u, n in names.items() if u.startswith(f"STM32{family}"))
        # A sheet may name the part with an X standing in for a digit -
        # STM32F7X2RXT covers the whole F7x2 line. Accept it only when exactly
        # one seeded target fits, so a genuine ambiguity still asks for --target.
        if "X" in digits:
            pat = re.compile("STM32" + family + digits.replace("X", r"\d"))
            fits = [n for n in siblings if pat.fullmatch(n.upper())]
            if len(fits) == 1:
                return fits[0]
            continue
        if len(siblings) == 1:
            return siblings[0]
    return None


def describe_pages(sym: Symbol) -> str:
    """' on page 4 of 5' - empty for a single-sheet plot, where it says nothing."""
    if sym.page_count < 2:
        return ""
    where = "+".join(str(p) for p in sym.pages)
    if sym.split_across_pages:
        return f" on pages {where} of {sym.page_count} (merged)"
    return f" on page {where} of {sym.page_count}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--target", help="FC_TARGET_MCU (auto-detected if omitted)")
    ap.add_argument("--page", type=int,
                    help="sheet holding the MCU (auto-detected if omitted)")
    ap.add_argument("--json", type=Path, help="write the net map as JSON")
    args = ap.parse_args()

    words = extract_words(args.pdf)
    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {"targets": {}}
    target = args.target or detect_target(words, data)
    if not target:
        raise SystemExit(describe_unreadable(words)
                         or "Could not detect FC_TARGET_MCU: no part number on "
                            "the sheet matches a seeded target. Many sheets "
                            "simply never name the MCU - pass --target")
    caps, data = load_caps(target)

    sym = find_symbol(words, page=args.page)
    labels = find_net_labels(words, sym)
    res = resolve(sym, labels, caps)

    print(f"target {target}  ({caps['mcu']}, {caps['family']})")
    print(f"symbol: {len(sym.rows)} pins{describe_pages(sym)}, "
          f"pitch {sym.pitch:.2f}pt, "
          f"AF lists {'present' if sym.has_af_lists else 'absent'}")
    if sym.ignored_pages:
        print(f"WARN: page(s) {', '.join(str(p) for p in sym.ignored_pages)} also "
              f"carry an MCU symbol; only page {sym.page} was used - if that is "
              f"the wrong sheet, pass --page")
    print(f"offset {res.offset:+.2f}pt  agreement {res.score[0]}/{res.score[1]} "
          f"({res.agreement:.0%})\n")
    for l in sorted(res.links, key=lambda l: (l.side, l.pin)):
        mark = "" if not l.checked else ("ok" if l.ok else "MISMATCH")
        if not l.gpio:
            mark = "NOT A GPIO"
        print(f"  {l.side}  {l.net:16} {l.pin:5} {mark}")
    if res.unmapped:
        print(f"\nunconnected: {' '.join(res.unmapped)}")
    for l in res.on_power_pin:
        print(f"WARN: {l.net} is wired to {l.pin}, which the symbol names as a "
              f"supply/system pin - check the schematic")
    if res.orphans:
        print(f"WARN: {len(res.orphans)} net label(s) matched no pin row: "
              f"{', '.join(res.orphans)}")

    if args.json:
        args.json.write_text(json.dumps({
            "target": target,
            "pages": sym.pages,
            "page_count": sym.page_count,
            "offset": res.offset,
            "agreement": res.agreement,
            "links": [vars(l) for l in res.links],
            "unmapped": res.unmapped,
        }, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
