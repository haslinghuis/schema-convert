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
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

# A pin name, optionally carrying ST's own dash qualifier and then an AF list:
#   PB2   PA0-WKUP   PC14-OSC32_IN   PC13-TAMPER-RTC
#   PC6/TIM3_CH1/TIM8_CH1/USART6_TX
# The qualifier names a second *fixed* function of the pin (wakeup, oscillator,
# tamper), not an alternate function, so it is dropped rather than parsed as
# one. There can be more than one of them - PC13-TAMPER-RTC is ST's own name -
# and allowing only a single segment lost that pin, and with it the net beside
# it, on a board whose symbol spells its pins out in full.
#   ...and PC2_C, ST's name for the second pad of a dual-pad pin on the H7
# parts that carry an analog switch (PA0_C, PA1_C, PC2_C, PC3_C). It is the
# same GPIO and Betaflight names it PC2, so the suffix is dropped like any
# other qualifier - but it is spelled with an underscore, which the dash form
# above does not cover. Written out rather than allowing any underscore tail,
# because PIN_RE also decides what is *not* a net label and a net called
# PC13_LED should stay one.
#   ...and the rest of the underscore-joined qualifiers, which are the same
# fixed functions the dash form already carries - PC14_OSC32_IN, PH0_OSC_IN,
# PA0_WKUP - just punctuated the other way. They are written out one by one for
# the reason the paragraph above gives, and the corpus says that caution is
# load-bearing rather than theoretical: one board labels its *nets* PA13_SWDIO,
# PC0_MOTOR1, PB10_UART3_TX, PA4_CS_FLASH. Allowing any underscore tail would
# read all 44 of them as pin names, which would take that board's entire net
# map with it. So only the oscillator and wakeup qualifiers are admitted, and
# `_C` keeps its place at the head of the list.
#
# The cost of not having them was one board's crystal: PH0_OSC_IN / PH1_OSC_OUT
# did not parse, so the sheet had no PH0 or PH1 row, and PC14_OSC32_IN /
# PC15_OSC32_OUT went the same way - taking with them the two net labels drawn
# beside them, which on that board were the CAN controller's RX and standby
# lines.
#   ...and the parenthesised form, which is the other way ST's own names are
# copied onto a symbol: PA13(JTMS/SWDIO)-DEBUG_JTMS-SWDIO, PA15(JTDI),
# PH0-OSC_IN(PH0)-RCC_OSC_IN, PA0-WKUP(PA0), and PA11(PA9) where the bracket
# holds a remap note rather than a function. None of it parsed, so those pins
# were not rows at all - 77 words over 14 boards of this corpus, and the pins
# went missing silently: absent from the map *and* from the unconnected list, so
# nothing said the symbol had been read short. On the board this was found on it
# cost PA13/PA14 (orphaning the SWCLK and SWDIO labels beside them) and PH0/PH1,
# which is why the crystal could not be tied to the MCU and SYSTEM_HSE_MHZ had
# to be given by hand.
#
# The closing bracket is optional because poppler splits the long form at the
# slash inside it and returns 'PA14(JTCK' as a word of its own.
#
# What the bracket holds is deliberately *not* read as an alternate function:
# only the slash-separated tail is, and group(2) still captures exactly that. A
# fixed second function and a remap note are not AFs, and treating them as AFs
# would put this symbol's word against the firmware tables in afs_support().
PIN_RE = re.compile(
    r"^(P[A-K]\d{1,2})"
    r"(?:_(?:C|OSC(?:32)?_(?:IN|OUT)|WKUP\d?|WAKE_UP|BOOT0))?"
    r"(?:\([^)]*\)?)?(?:-/?[A-Z0-9_()]+)*(?:/(.*))?$")

# The same name written the other way round: function first, pin last. One
# vendor's symbol does this down its right-hand column while the left column
# reads pin-first, so half its pins were invisible:
#
#   USART3_RX/PD9   SPI2_SCK/PB13   TIM3_CH3(M7)/PB0   TIM4_CH3(M11)PD14
#
# The separator is optional because one of those has none, and the leading part
# is taken as the AF list. Used only where a pin name is being *recognised* -
# not where PIN_RE excludes a word from being a net label, since a label that
# merely ends in something pin-shaped is still a label.
PIN_TAIL_RE = re.compile(r"^(?P<afs>.+?)[/_-]?(?P<pin>P[A-K]\d{1,2})$")

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

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


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


# Set by a caller that ships its own poppler rather than relying on one being
# installed - the desktop app bundles it so a vendor needs nothing on PATH.
# Kept as an environment variable rather than an argument because it has to
# reach here through the CLI, and every caller in between would otherwise have
# to know about a detail none of them care about.
PDFTOTEXT_ENV = "SCHEMA_CONVERT_PDFTOTEXT"


def pdftotext() -> str:
    """The poppler binary to extract with: a bundled one if told, else PATH."""
    override = os.environ.get(PDFTOTEXT_ENV)
    if override:
        if not Path(override).is_file():
            raise SystemExit(f"{PDFTOTEXT_ENV} points at {override}, which is "
                             "not a file")
        return override
    found = shutil.which("pdftotext")
    if not found:
        raise SystemExit("pdftotext not found - install poppler-utils")
    return found


def extract_words(pdf: Path) -> List[Word]:
    """
    Every word in the document, each tagged with the sheet it was drawn on.

    Sheets of one plot share a coordinate space - page 1 and page 4 of an A3 set
    both run y 29-561 - so a flattened word list puts unrelated sheets on the
    same rows. Anything downstream that reasons about geometry has to stay
    inside a page, or it will pair a net label with a pin drawn elsewhere.
    """
    out = subprocess.run(
        [pdftotext(), "-bbox-layout", str(pdf), "-"],
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
    pos: float               # where along its own edge the name sits
    side: str                # 'L', 'R', 'T' or 'B'
    afs: List[str] = field(default_factory=list)   # from the symbol, if present
    gpio: bool = True        # False for supply/system rows


# Which coordinate runs *along* an edge. A left or right edge stacks its pin
# names down the page, so a name's place on it is its y and its net label sits
# out to the side; a top or bottom edge spreads them across, so the place is x
# and the label sits above or below. Everything that pairs a label to a pin has
# to ask which of the two it is dealing with - a four-sided QFP symbol has both
# at once, and reading it with the vertical assumption baked in loses every pin
# on two of its four sides.
ALONG = {"L": "y", "R": "y", "T": "x", "B": "x"}


def _along(side: str, w: "Word") -> float:
    """Where this word sits along an edge of that orientation."""
    return w.y0 if ALONG[side] == "y" else w.x0


@dataclass
class SymbolPart:
    """One page's worth of symbol. Edges and the row band are per page, because
    coordinates only mean anything relative to the sheet they were drawn on."""
    page: int
    rows: List[PinRow]
    left_edge: float
    right_edge: float
    top_edge: float = 0.0
    bottom_edge: float = 0.0

    @property
    def sides(self) -> set[str]:
        return {r.side for r in self.rows}

    def band(self, side: str) -> Tuple[float, float]:
        """How far this edge's names run along it, as (low, high)."""
        on = [r.pos for r in self.rows if r.side == side] or [r.pos for r in self.rows]
        return min(on), max(on)

    @property
    def y_min(self) -> float:
        return min(r.pos for r in self.rows if ALONG[r.side] == "y")

    @property
    def y_max(self) -> float:
        return max(r.pos for r in self.rows if ALONG[r.side] == "y")

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
        return min(p.y_min for p in self.parts if any(
            ALONG[r.side] == "y" for r in p.rows))

    @property
    def y_max(self) -> float:
        return max(p.y_max for p in self.parts if any(
            ALONG[r.side] == "y" for r in p.rows))

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
#
# A BGA numbers its pins by ball coordinate rather than sequentially, so the
# same token comes out as PIU40M11 - pin M11 of U40 - and the trailing \d{1,6}
# form does not match it, because of the letter in the middle. That is not a
# cosmetic miss. assemble_pin_names() stops at an annotation; one it does not
# recognise it *absorbs*, which moves the name's right edge out to where the
# annotation ends. _unfuse_annotation() then trims the text back and rescales
# the box by character count, and on a proportional font with a long name that
# estimate lands up to 5pt short - enough to break the x1 edge cluster in two.
# A UFBGA176 board split its right-hand column that way: 13 of its rows formed
# a rival edge, were glued to the supply box as a second part, and every net
# level with them - five motors, the LED strip, the ADC nets, the gyro
# interrupt - then paired with nothing while the board still reported 96%
# agreement on what was left.
#
# Recognising the form here means the name is never fused, so its own exact box
# survives and nothing has to be estimated. The shape is the one
# FUSED_ANNOT_RE already trusts for the fused case, and it is held to a U
# designator: only an IC is drawn with ball coordinates.
ANNOT_RE = re.compile(
    r"^(?:PI|CO)(?:C|R|L|D|Q|U|Y|J|P|X|FB|TP|SW|RN|BT|VR)\d{1,6}$"
    r"|^(?:PI|CO)U\d{1,4}[A-Z]\d{1,3}$"
    r"|^NL[A-Z0-9_]*$",
)


# The same exporter also stamps a token for every *net*: NL for a net label, PO
# for a port, CO/PI for the component and pin it lands on. Unlike the designator
# form above these carry the net's own name, with the separators replaced by 0 -
# RX8_1 becomes RX801, GYRO1-CS becomes GYRO10CS - so they read like plausible
# net names and were being collected as labels. On one board that put four
# labels on a single pin row and pushed GYRO_CS onto the wrong one.
#
# They cannot be recognised by prefix alone: PINIO1 is a real net that starts
# with PI. What identifies them is that the remainder names a net drawn
# elsewhere on the same sheet, which is exactly what an annotation is.
ANNOT_PREFIXES = ("NL", "PO", "CO", "PI")


def _annot_norm(text: str) -> str:
    """A name as its annotation token would spell it: no separators at all."""
    return re.sub(r"[^A-Z0-9]", "", text.upper()).replace("0", "")


def drop_annotations(labels: Sequence[Word], words: Sequence[Word]) -> List[Word]:
    """Discard the annotation copy of a net that is also drawn as itself."""
    by_page: Dict[int, set] = defaultdict(set)
    for w in words:
        by_page[w.page].add(_annot_norm(w.text))
    out: List[Word] = []
    for w in labels:
        t = w.text.upper()
        if any(len(t) > len(p) and t.startswith(p)
               and _annot_keys(t[len(p):]) & by_page[w.page]
               for p in ANNOT_PREFIXES):
            continue
        out.append(w)
    return out


def _annot_keys(rest: str) -> set:
    """
    What net this annotation could be naming.

    The pin form carries the pin number as well as the net - PITX301 is TX3 on
    pin 01 - so the trailing digits are tried both ways rather than assumed to
    be part of the name.
    """
    # Trim before normalising, not after: the separator-as-0 encoding would
    # otherwise fold TX301 (TX3 on pin 01) to TX31 and lose the boundary.
    cands = {rest} | {rest[:-n] for n in (1, 2, 3) if len(rest) > n}
    return {_annot_norm(c) for c in cands} - {""}


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
        if not (PIN_RE.match(w.text) or POWER_PIN_RE.match(w.text)
                or PIN_TAIL_RE.match(w.text)):
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
            if (ANNOT_RE.match(v.text) or PIN_RE.match(v.text)
                    or POWER_PIN_RE.match(v.text)):
                # A pin name is never the continuation of another pin name -
                # and a supply or system name is a name in the row too, so
                # BOOT0 and NRST end one exactly as PB5 does. Missing that
                # fused 'PB8' and 'BOOT0' into PB8BOOT0 on one sheet, which
                # took PB8 with it and left the board's I2C SCL unbindable.
                # Some sheets set their pin names in a horizontal row about a
                # point apart - closer than the gap that separates the pieces
                # of one split name - and without this the whole row fuses into
                # 'PA4PA5PA6PA7PC4PC5PB0PB1PB2', which is not a pin, so every
                # one of them is lost. One F405 board came out with 29 pins of
                # its 50 that way.
                break
            text, x1 = text + v.text, v.x1
        out.append(Word(text, w.x0, w.y0, x1, w.y1, w.page))
    return out


Tagged = Tuple[Word, str, List[str], bool]


FUSED_ANNOT_RE = re.compile(
    r"^(?P<name>.*?)(?P<annot>(?:PI|CO)U\d{1,4}[A-Z]\d{1,3})$")


def _unfuse_annotation(w: Word) -> Word:
    """
    Trim an annotation token that was drawn hard against a pin name.

    Altium writes a hidden designator-and-ball token beside every pin, and
    poppler returns it as part of the word when there is no gap:
    `PH1-OSC_OUTPIU10D1`, `PC15-OSC32_OUTPIU10B1`. drop_annotations() removes
    these where they stand alone; fused, the name still parses but the word's
    box runs to where the *annotation* ends.

    That is what breaks the symbol. An edge is found by clustering on a shared
    coordinate, and on a right-hand column the shared coordinate is x1 - so the
    fused names land 10pt out and form a rival cluster. One H7 read 16 of its
    right column's rows for this reason and orphaned 19 net labels that had
    nowhere to bind, while reporting 100% agreement on what was left.

    The box is narrowed in proportion to how much of the text the name is. That
    is an approximation - these are proportional fonts - but the error is a
    fraction of a character against a 10pt displacement.
    """
    m = FUSED_ANNOT_RE.match(w.text)
    if not m or not m.group("name"):
        return w
    name = m.group("name")
    share = len(name) / len(w.text)
    return Word(name, w.x0, w.y0, w.x0 + (w.x1 - w.x0) * share, w.y1, w.page)


def _tag_pins(words: Sequence[Word]) -> List[Tagged]:
    """Every word that reads as a pin name or a supply pin, with its AF list."""
    tagged: List[Tagged] = []
    for w in map(_unfuse_annotation, assemble_pin_names(words)):
        m = PIN_RE.match(w.text)
        if m:
            pin, rest = m.group(1), m.group(2)
            afs = [a for a in (rest.split("/") if rest else []) if a]
            tagged.append((w, pin, afs, True))
            continue
        p = POWER_PIN_RE.match(w.text)
        if p:
            tagged.append((w, p.group(1).upper(), [], False))
            continue
        m = PIN_TAIL_RE.match(w.text)
        if m:
            afs = [a for a in m.group("afs").split("/") if a]
            tagged.append((w, m.group("pin"), afs, True))
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
    # Top or bottom is judged against the box found so far, not against every
    # pin-shaped word on the sheet: other components carry pin names too, and on
    # one board that stretched the reference to twice the symbol's height and
    # put its bottom edge above the midpoint - so it came back labelled T, and
    # its labels were then looked for on the wrong side.
    frame: Optional[Tuple[float, float, float, float]] = None
    out: List[Tuple[SymbolPart, float]] = []
    for _ in range(limit):
        got = _part_from_tagged(tagged, page, min_pins, frame)
        if not got:
            break
        part, pitch, used = got
        frame = (part.left_edge, part.top_edge, part.right_edge, part.bottom_edge) \
            if frame is None else (
                min(frame[0], part.left_edge), min(frame[1], part.top_edge),
                max(frame[2], part.right_edge), max(frame[3], part.bottom_edge))
        out.append((part, pitch))
        tagged = [t for t in tagged if id(t) not in used]
    return out


def _fill_edge_holes(edge: List[Tagged], pool: Sequence[Tagged],
                     across, along) -> List[Tagged]:
    """
    Adopt names the edge's coordinate cluster missed but its pitch demands.

    The cluster tolerance is 1pt, and pdftotext's bounding boxes are not that
    exact: one board reports a name 1.5pt right of the column it is plainly in,
    which dropped the pin, and with it the SPI bus that pin carried and the
    device sitting on that bus - reported only as one net label that matched no
    row.

    Widening the tolerance is the wrong fix, because real columns come that
    close: one sheet here draws two of them 3.5pt apart. What separates the two
    cases is the run. An edge is names at a regular pitch, so a gap of two
    pitches is a row that must exist and does not - and only such a gap is
    filled, from names less than a character's width off the edge. A genuine
    neighbouring column is never absorbed: it has no hole to fall into.
    """
    if len(edge) < 3:
        return edge
    at = sorted(along(t[0]) for t in edge)
    gaps = [b - a for a, b in zip(at, at[1:]) if b - a > 0.5]
    if not gaps:
        return edge
    pitch = min(gaps)
    edge_at = median([across(t[0]) for t in edge])
    # A character's width, from the names themselves rather than a constant -
    # these plots are drawn at whatever scale the vendor chose.
    tol = 0.6 * median([(t[0].x1 - t[0].x0) / max(len(t[0].text), 1) for t in edge])
    taken = {id(t) for t in edge}
    spare = [t for t in pool
             if id(t) not in taken and abs(across(t[0]) - edge_at) <= tol]
    if not spare:
        return edge
    out = list(edge)
    for a, b in zip(at, at[1:]):
        for k in range(1, round((b - a) / pitch)):
            slot = a + k * pitch
            fits = [t for t in spare if abs(along(t[0]) - slot) <= pitch / 3
                    and id(t) not in taken]
            if not fits:
                continue
            best = min(fits, key=lambda t: abs(along(t[0]) - slot))
            taken.add(id(best))
            out.append(best)
    return out


def _part_from_tagged(tagged: Sequence[Tagged], page: int, min_pins: int,
                      frame: Optional[Tuple[float, float, float, float]] = None
                      ) -> Optional[Tuple[SymbolPart, float, set]]:
    """
    The strongest edge in `tagged`, plus the items it consumed so a caller can
    look for the next one in what is left over.

    An edge is a set of pin names sharing an alignment. Which alignment says
    which edge it is: names down a left edge share an x0 and names across a top
    edge share a y0, so all four orientations are the same search on a different
    coordinate. Only the vertical two were looked for, which on a four-sided QFP
    symbol reads half the package and reports nothing missing.

    Left and right are still returned together when they are the two columns of
    one box, because that is what a two-column symbol is and splitting it would
    lose the pairing between them.
    """
    if not tagged:
        return None
    # Top or bottom is decided against the whole symbol's extent, not against
    # whatever is left on this pass. Edges are peeled off one at a time, so by
    # the time the last one is reached it is the only thing left and would
    # always look like the top of itself - which is how a bottom edge came back
    # labelled T, with its labels then searched for above it.
    box = frame or (min(t[0].x0 for t in tagged), min(t[0].y0 for t in tagged),
                    max(t[0].x1 for t in tagged), max(t[0].y1 for t in tagged))

    def biggest(coord) -> List[Tagged]:
        return max(cluster([(coord(t[0]), t) for t in tagged], tol=1.0),
                   key=len, default=[])

    left = biggest(lambda w: w.x0)
    right = biggest(lambda w: w.x1)
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

    # Only after the two columns have divided the overlap between them, so a
    # hole in one is never filled with a name the other already owns.
    spare = [t for t in tagged
             if id(t) not in {id(x) for x in left} | {id(x) for x in right}]
    left = _fill_edge_holes(left, spare, lambda w: w.x0, lambda w: w.y0)
    spare = [t for t in spare if id(t) not in {id(x) for x in left}]
    right = _fill_edge_holes(right, spare, lambda w: w.x1, lambda w: w.y0)

    # A rotated name is aligned at one end and grows from the other, exactly as
    # a column of horizontal ones is: a top edge shares y0 and runs downward, a
    # bottom edge shares y1 and runs up. So the same argument the two columns
    # get above applies here, and clustering on y0 alone reads a bottom edge as
    # "the names that happen to be the same length". One F7 kept the eleven
    # 3-character names of its bottom edge and dropped PB10, PB11 and VCAP_1,
    # orphaning the three labels that wanted them - with nothing else wrong,
    # because a shorter edge is still a valid edge.
    by_y0, by_y1 = biggest(lambda w: w.y0), biggest(lambda w: w.y1)
    horizontal, h_coord = ((by_y1, lambda w: w.y1) if len(by_y1) > len(by_y0)
                           else (by_y0, lambda w: w.y0))
    # A horizontal edge only wins when it is clearly the stronger reading: the
    # two columns of an ordinary symbol also share a handful of y values where
    # rows happen to line up, and mistaking that for a top edge would tear a
    # working two-column symbol apart.
    if len(horizontal) > len(left) + len(right):
        # Filled only once this edge has won, so that adopting into it cannot
        # be what tips the comparison above.
        horizontal = _fill_edge_holes(
            horizontal, [t for t in tagged if id(t) not in {id(x) for x in horizontal}],
            h_coord, lambda w: w.x0)
        mid_y = (box[1] + box[3]) / 2
        side = "T" if sum(t[0].y0 for t in horizontal) / len(horizontal) < mid_y else "B"
        rows = [PinRow(pin, w.x0, side, afs, gpio)
                for w, pin, afs, gpio in horizontal]
        if len(rows) < min_pins:
            return None
        rows.sort(key=lambda r: r.pos)
        part = SymbolPart(
            page, rows,
            left_edge=min(w.x0 for w, *_ in horizontal),
            right_edge=max(w.x1 for w, *_ in horizontal),
            top_edge=min(w.y0 for w, *_ in horizontal),
            bottom_edge=max(w.y1 for w, *_ in horizontal),
        )
        return part, _pitch(rows), {id(t) for t in horizontal}

    if len(left) + len(right) < min_pins:
        return None

    rows = [PinRow(pin, w.y0, "L", afs, gpio) for w, pin, afs, gpio in left]
    rows += [PinRow(pin, w.y0, "R", afs, gpio) for w, pin, afs, gpio in right]
    rows.sort(key=lambda r: r.pos)
    rows = _drop_double_struck(rows)

    both = left + right
    part = SymbolPart(
        page, rows,
        left_edge=min(w.x0 for w, *_ in left) if left else min(w.x0 for w, *_ in right),
        right_edge=max(w.x1 for w, *_ in right) if right else max(w.x1 for w, *_ in left),
        top_edge=min(w.y0 for w, *_ in both),
        bottom_edge=max(w.y1 for w, *_ in both),
    )
    return part, _pitch(rows), {id(t) for t in both}


def _drop_double_struck(rows: List[PinRow]) -> List[PinRow]:
    """
    Collapse a pin name the sheet draws twice, a fraction of a point apart.

    Some exporters emboldened text by drawing it twice with a tiny offset, so
    the same name arrives as two words 0.3-0.9pt apart. Each becomes a row, the
    median gap between adjacent rows halves, and `_pitch` - which everything
    downstream is scaled by - comes back at a tenth of the truth. On one H743
    that made three of the symbol's four columns look like they had a different
    pitch from the fourth, so `_is_split_half` refused them and the board was
    read from a quarter of its own pins.

    Only an identical name at practically the same place is collapsed. A symbol
    legitimately repeats VSS and VDD many times, and those are rows apart, not
    a fraction of one.
    """
    out: List[PinRow] = []
    for r in rows:
        if any(o.pin == r.pin and o.side == r.side and abs(o.pos - r.pos) <= 1.5
               for o in out):
            continue
        out.append(r)
    return out


def _pitch(rows: Sequence[PinRow]) -> float:
    """
    The row pitch: the smallest gap between adjacent names *on one side*.

    Per side, because the two columns of a symbol are interleaved along the
    same axis - a left row and the right row opposite it sit a fraction apart
    while the real step is to the next pair. One board's columns are each a
    clean 7.2pt apart and their union is 0.72 and 6.48 alternating, so measured
    together its pitch came out ten times too small. Everything downstream is
    scaled by it, and that board bound nothing at all.
    """
    gaps: List[float] = []
    for side in {r.side for r in rows}:
        at = sorted({round(r.pos, 2) for r in rows if r.side == side})
        gaps += [round(b - a, 2) for a, b in zip(at, at[1:]) if 0.5 < b - a < 20]
    return min(gaps) if gaps else 3.66


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
    if not theirs or mine & theirs:
        return False
    if abs(part_pitch - pitch) > pitch * 0.1:
        return False

    # Substantial in its own right - the original test, and the only one
    # available across sheets, where there is no geometry to appeal to.
    if len(theirs) >= 12 and len(theirs) * 3 >= len(mine):
        return True

    # Or touching what has already been accepted. The four edges of a QFP
    # symbol meet at its corners while a second MCU elsewhere on the sheet does
    # not, so a small edge can be admitted on position instead of size: the
    # size test alone dropped a real 11-pin bottom edge for being smaller than a
    # third of the rest of its own symbol. It is an *additional* way in, not a
    # replacement - two columns of one symbol are routinely drawn far enough
    # apart to fail an adjacency test, and requiring it lost 20 pins on a board
    # that had been read correctly for weeks.
    # The edges of one symbol meet at its corners, so this is a small number of
    # rows, not a large one. It was six, which is harmless while the pitch is
    # under-measured and far too generous once it is right.
    reach = pitch * 2
    if any(
        part.left_edge <= p.right_edge + reach
        and p.left_edge <= part.right_edge + reach
        and part.top_edge <= p.bottom_edge + reach
        and p.top_edge <= part.bottom_edge + reach
        for p in accepted if p.page == part.page):
        return True

    # Or drawn beside it, spanning the same rows. A symbol split by port group
    # is two columns side by side running the full height of both - here 45pt
    # apart, which is far outside any adjacency reach, and 18 pins against 64,
    # which is just under the third the size test wants. Neither route admitted
    # it and half an LQFP100 was dropped: 39 pins reported as unconnected on a
    # board that had them.
    #
    # Safe because of what is already required above: the two share no pin at
    # all. Any two symbols of the same MCU both carry PA0, so a disjoint pin set
    # on the same page, on the same grid, spanning the same extent is a port
    # group of one part and not a second one.
    def span(p: SymbolPart) -> Tuple[float, float]:
        vertical = any(ALONG[r.side] == "y" for r in p.rows)
        return ((p.top_edge, p.bottom_edge) if vertical
                else (p.left_edge, p.right_edge))

    lo, hi = span(part)
    for p in accepted:
        if p.page != part.page:
            continue
        plo, phi = span(p)
        overlap = min(hi, phi) - max(lo, plo)
        if overlap > 0 and overlap >= 0.5 * min(hi - lo, phi - plo):
            return True
    return False


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
        # "No aligned pin-name column" reads as a parse failure and sends a
        # reader looking for one. Three documents in the corpus are datasheets
        # and specifications rather than schematics - 1, 0 and 0 pin names in
        # the whole file - and there is nothing there to parse. Counting first
        # is what tells the two apart: a schematic this cannot read has pin
        # names it failed to line up; a datasheet has none at all.
        pins = sum(1 for w in words if PIN_RE.match(w.text))
        raise SystemExit(
            describe_unreadable(words)
            or ((("No MCU symbol here, and no pin names at all in the whole "
                  "document" if pins == 0 else
                  f"No MCU symbol here, and only {pins} pin name"
                  f"{'' if pins == 1 else 's'} in the whole document")
                 + " - this reads as a datasheet or a specification rather "
                   "than a schematic. Nothing here is a parse failure to "
                   "chase; check it is the right file") if pins < 8 else
                f"Could not find an MCU symbol: {pins} pin names are on the "
                "sheet but none of them line up into a column, which is what "
                "this reads a symbol from"))

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


def _flip_one_sided(sym: "Symbol") -> Optional["Symbol"]:
    """
    The same symbol with its one-column parts pointed the other way, or None.

    Which side a column of pin names is *of* is decided nowhere: the two
    clusters are found by shared x0 and shared x1, and where a part has only
    one column both find the same names, so the side it ends up with is an
    artefact of which cluster won. It usually does not matter, because the
    gutter is then searched on the one side that has anything in it.

    It matters when a symbol is drawn as two columns far enough apart to be
    read as two parts. One BGA H7 sheet has its PE names at x=456 with their
    nets at 424, and its PA names at 496 with theirs at 522 - facing *outward*
    - and both parts were assigned the inward side. Every net paired against
    the other column's rows and the board read at 40% agreement with 29
    orphans.

    Nearness cannot decide it: an F405 sheet has the same two-part shape with
    the columns facing *each other*, so the nearest net-like text to each is
    the other's gutter. Choosing by distance fixed the H7 and broke that board
    from 100% to nothing.

    So this only proposes. The caller resolves both and keeps whichever the
    firmware map agrees with more, which is the same arbiter that picks the
    row offset - and the reason a wrong guess here cannot survive.
    """
    flippable = [p for p in sym.parts if p.sides in ({"L"}, {"R"})]
    if not flippable:
        return None
    parts = []
    for p in sym.parts:
        if p.sides in ({"L"}, {"R"}):
            other = "R" if p.sides == {"L"} else "L"
            rows = [PinRow(r.pin, r.pos, other, r.afs, r.gpio) for r in p.rows]
            parts.append(SymbolPart(p.page, rows, p.left_edge, p.right_edge,
                                    p.top_edge, p.bottom_edge))
        else:
            parts.append(p)
    return Symbol(parts, sym.pitch, sym.page_count, sym.ignored_pages)


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


def find_net_labels(words: Sequence[Word], sym: Symbol,
                    caps: Optional[dict] = None) -> List[Word]:
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
        for w in _labels_for_part(words, part, sym.pitch, caps):
            if id(w) not in seen:
                seen.add(id(w))
                out.append(w)
    return drop_annotations(out, words)


# How far past the label zone the corroborated pass of _labels_for_part() will
# look, in row pitches. Measured over 154 schematics against a baseline of 2827
# checked nets, 2788 agreeing, 39 disagreeing and 393 orphans:
#
#   reach   checked  agreeing  disagreeing  orphans  boards worse
#     10      2875     2836        39         389        0
#     12      2893     2854        39         385        0
#     16      2910     2871        39         387        0
#     20      2911     2872        39         388        0
#     30      2916     2877        39         392        0
#   none      2926     2886        40         397        1
#
# The striking part is how flat that is, and it is worth saying why: the reach
# is *not* what makes this safe. The firmware corroboration is, together with
# the two guards beside it - a row already claimed is never taken, and a name
# already bound inside the zone is never re-adopted from outside. With those in
# place every bounded reach behaves identically on the disagreement count, and
# even removing the bound costs only one board.
#
# So this is a backstop rather than a tuned constant, and 16 is chosen for
# sitting in the middle of a band where nothing changes. An earlier version
# without those guards had a real cliff here; if the guards are ever weakened,
# re-measure before trusting this number.
OUT_OF_ZONE_REACH = 16


def _label_zone(cols: Sequence[Tuple[int, float, List[Word]]],
                pitch: float) -> float:
    """
    How far out from the symbol this sheet's label columns run, as a `near`.

    The zone used to be a single radius around the strongest column, and the
    radius was `pitch * 3` - a horizontal distance taken from the *row* pitch,
    which is a vertical quantity and has nothing to do with how far apart a
    sheet stacks its label columns. On a UFBGA176 board that put a column of
    eight net names - the ADC nets, both remaining chip selects, the beeper, the
    gyro interrupt, the camera control - 15.83pt from the strongest one, against
    a radius of 15.81pt. The whole column was discarded by two hundredths of a
    point, and nothing said so.

    A radius is the wrong shape anyway. Where each name is drawn against its own
    wire the gutter is a *run* of near-parallel columns, and what ends the run is
    the gap to the next component - whose own labels are often the same nets seen
    at the far end, and must not be adopted, because they would bind to whatever
    row they happen to sit level with. So walk outward from the strongest column
    through label-bearing columns only and stop at the first real break. On that
    board the run is 343.9 / 356.6 / 372.4 and the break to the microSD socket's
    own labels at 454.9 is 48pt wide, so the socket stays out while the third
    column comes in.

    Columns *nearer* than the strongest are still kept unconditionally, as
    before; only the outward reach is decided here.

    `pitch * 6` is what the 153-schematic corpus chose. Measured against the
    behaviour before this function existed, it binds 102 more nets across 22
    boards for one extra mismatch and no board losing a correct one. Tolerances
    derived from the label widths instead all did worse, and `pitch * 7` is
    where it turns: nets start falling again and orphans jump by a fifth as
    columns begin displacing each other.
    """
    best_edge = cols[0][1]
    tol = pitch * 6
    zone = best_edge
    for edge in sorted({e for h, e, _ in cols if h}, reverse=True):
        if edge >= zone:
            continue
        if zone - edge > tol:
            break
        zone = edge
    return zone


def _labels_for_part(words: Sequence[Word], part: SymbolPart, pitch: float,
                     caps: Optional[dict] = None) -> List[Word]:
    pad = pitch * 2

    # Only a side this part actually has pin names on can have a net-label
    # gutter. A tall MCU is often drawn as two separate boxes on one sheet, and
    # each is found as a one-sided part; searching the empty side would reach
    # across the gap and collect the *other* box's labels, binding every net
    # twice and leaving one box's rows with nothing.
    #
    # Which way "out" is depends on the edge. A left edge's labels lie further
    # left at a matching y; a top edge's lie further up at a matching x.
    sides: Dict[str, List[Word]] = {r.side: [] for r in part.rows}
    for w in words:
        if w.page != part.page:
            continue
        if PIN_RE.match(w.text) or JUNK_RE.search(w.text) or POWER_RE.match(w.text):
            continue
        for side in sides:
            lo, hi = part.band(side)
            if not (lo - pad <= _along(side, w) <= hi + pad):
                continue
            outside = (
                (side == "L" and w.x1 < part.left_edge)
                or (side == "R" and w.x0 > part.right_edge)
                or (side == "T" and w.y1 < part.top_edge)
                or (side == "B" and w.y0 > part.bottom_edge)
            )
            if outside:
                sides[side].append(w)
                break

    out: List[Word] = []
    for side, cands in sides.items():
        cols = []
        # Nearness to the symbol, whichever way the gutter lies. Signed so that
        # larger is always closer, on all four sides.
        near = {
            "L": lambda c: max(w.x1 for w in c),
            "R": lambda c: -min(w.x0 for w in c),
            "T": lambda c: max(w.y1 for w in c),
            "B": lambda c: -min(w.y0 for w in c),
        }[side]
        # Labels may be aligned on either edge of their own text, so try both.
        # Across a left or right edge that is x; across a top or bottom one, y.
        anchors = ("x0", "x1") if ALONG[side] == "y" else ("y0", "y1")
        for anchor in anchors:
            groups = cluster([(getattr(w, anchor), w) for w in cands], tol=1.0)
            for col in (c for c in groups if len(c) >= 3):
                # Score by whether the column reads like net names, not merely
                # by how near it is. A BGA sheet puts its ball coordinates (F8,
                # H9, A3) exactly where a net label would sit, and proximity
                # alone happily picks those.
                hits = sum(1 for w in col if NET_VOCAB.search(w.text))
                cols.append((hits, near(col), col))
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
        # So take the neighbouring columns too, out to where the run of them
        # ends - see _label_zone() - but only the words in them that read like
        # net names on their own. The strongest column is trusted wholesale
        # because it has already proved itself as a column; the rest have not,
        # and a BGA sheet fills that same space with ball coordinates (E10, B10)
        # which would otherwise bind to whatever row they sit level with.
        zone_edge = _label_zone(cols, pitch)
        keep: Dict[int, Word] = {}
        for i, (hits, edge, col) in enumerate(cols):
            if not hits or edge < zone_edge:
                continue
            for w in col:
                if i == 0 or NET_VOCAB.search(w.text):
                    keep[id(w)] = w

        # Labels that are in no column at all. Some sheets draw each net name
        # against its own wire, so how far it sits from the symbol is decided by
        # whatever components share that row - a pull-up on one, a series
        # resistor on the next - and no three of them line up. Every one is then
        # discarded by the column test, and silently: the rows come back
        # unconnected, which reads like a sheet that simply does not use those
        # pins. One board lost its OSD chip select, its gyro interrupt, LED0,
        # ADC_VBAT and a motor that way, and still reported 100% agreement on
        # what was left.
        #
        # Bounded by the same zone the columns are, which is what establishes
        # where this sheet's labels live: between it and the symbol is the label
        # zone by construction, and nothing further out is taken on its own.
        # NET_VOCAB is what keeps ball coordinates and component values out -
        # the same filter the neighbouring-column pass above already relies on.
        for w in cands:
            if id(w) not in keep and NET_VOCAB.search(w.text) \
                    and near([w]) > zone_edge:
                keep[id(w)] = w

        # A label further out than the zone, admitted only where the firmware
        # map vouches for it.
        #
        # The paragraph above says how far a lone label sits is decided by the
        # components sharing its row, and a row carrying more of them puts its
        # label outside any column. Bounding that by the columns is circular -
        # but simply lifting the bound is catastrophic, and measurably so: on
        # the 154-schematic corpus it adopts far-end names wholesale, 47 boards
        # get worse, orphans go 393 -> 1104 and mismatches 39 -> 163. Distance
        # is doing real work out there and cannot just be dropped.
        #
        # So replace it with evidence rather than with a bigger number. Take the
        # label only if it *claims* something checkable - net_requirement() -
        # and the row it would land on can actually do that, per the same
        # firmware tables everything else here is judged against. A stray name
        # from another component has to clear a bar it has no reason to clear:
        # landing on a pin that supports exactly the function its own name
        # states. That is the §1 rule used as a filter instead of as a check.
        #
        # One board's ADC_CURR is 59pt outside a gutter whose tolerance is 32,
        # with the net's own 12pF filter caps drawn in the gap. It came back as
        # an unconnected pin and a missing function; PC2 is an ADC pin sitting
        # between the two ADC nets that did bind.
        if caps:
            # Every row, not only the routable ones. The check has to be about
            # the row this label will actually pair with, and _pair() does not
            # skip supply rows - so vetting against the nearest *GPIO* row while
            # the label lands on a VCAP beside it corroborates the wrong thing.
            # One H743 adopted ADC_RSSI onto VCAP2 that way.
            rows = list(part.rows)
            for w in cands:
                if id(w) in keep or not NET_VOCAB.search(w.text):
                    continue
                req = net_requirement(w.text)
                if not req or not requirement_answerable(caps, req[0]):
                    continue
                # A name already spoken for inside the zone is not admitted
                # again from outside it - that is §1.24's echo, and on a sheet
                # carrying two MCU symbols it is the *other* symbol's copy. One
                # H743 draws both, and reading the second set put a duplicate
                # MOTOR1..4 and an ADC_RSSI onto the rows they happened to sit
                # level with.
                if any(v.text.upper() == w.text.upper() for v in keep.values()):
                    continue
                pos = w.yc if ALONG[part.rows[0].side] == "y" else w.xc
                row = min(rows, key=lambda r: abs(r.pos - pos), default=None)
                if row is None or abs(row.pos - pos) > pitch:
                    continue
                if zone_edge - near([w]) > pitch * OUT_OF_ZONE_REACH:
                    continue
                # Never displace a label that is already in the zone: the row
                # this one would land on has to be free. Without it four boards
                # gain a contradiction apiece from a name that had a better
                # claimant.
                if any(abs((v.yc if ALONG[part.rows[0].side] == "y" else v.xc)
                           - row.pos) <= pitch / 2 for v in keep.values()):
                    continue
                if row.gpio and pin_supports(caps, row.pin, req[0], req[1]):
                    keep[id(w)] = w
        out.extend(keep.values())
    return out


# --------------------------------------------------------------------------- #
# Net-name semantics: what function must a pin support?
# --------------------------------------------------------------------------- #

# The vocabulary flight-controller schematics use for nets that end up in a
# config.h. Used to tell a net-label column apart from a column of package ball
# coordinates or component values.
# Deliberately NOT here: a bare Mn. `classify` reads M1 as a motor and some
# sheets do label them that way, but a BGA plot's ball coordinates run A1..M12,
# and admitting Mn put a whole row of them in the gutter of one board - which
# then took the rows its real net labels wanted, costing that board its SPI1 and
# SPI4 buses. Ball coordinates sitting exactly where a net label would is the
# thing this vocabulary exists to exclude, so the collision is decided in favour
# of the many boards rather than the one.
NET_VOCAB = re.compile(
    r"MOTOR|SERVO|PWM\d|ESC\d|GYRO|IMU|MPU|ICM|OSD|MAX7456|AT7456|"
    r"FLASH|BARO|SDCARD|SDIO|SDMMC|\bTX\d|\bRX\d|TXD\d|RXD\d|U(?:S?ART)?\d[-_]"
    r"[TR]X|I2C\d|SCL|SDA|SCK|SCLK|SPI\d[-_]?(?:CLK|MISO|MOSI|SDI|SDO)|"
    r"MISO|MOSI|\bSDI\b|SDI\d|\bSDO\b|"
    r"SDO\d|\bCS\b|"
    r"CS_|_CS|_NSS|_SS\b|EXTI|CLKIN|CLOCK|LED|BEEP|BUZZ|CAM|VTX|USER\d|PINIO|"
    r"PIO\d|ADC|"
    r"BATT|CURR|VBAT|RSSI|SWDIO|SWCLK|BOOT|OTG|"
    # classify() places USB_DETECT_PIN and SDCARD_DETECT_PIN from
    # ^USB[-_]?DETECT$ / ^VBUS[-_]?DETECT$ and ^SD(?:CARD)?[-_]?DET(?:ECT)?$,
    # and this gate knew neither spelling - only SDCARD/SDIO/SDMMC, which
    # SD_DET does not contain. Both nets were then dropped out of every column
    # but the strongest, so a board that draws them one column further out lost
    # its card-detect and its USB-detect while reporting nothing missing.
    r"DETECT|SD[-_]?DET\b",
    re.IGNORECASE,
)

# (regex on the upper-cased net name) -> (kind, detail)
NET_RULES: List[Tuple[re.Pattern, str]] = [
    # Every spelling on this side of the gate has a twin in classify(), and the
    # test that says so is what found most of them: IIC for I2C, the S in
    # USART, the D in TXD, the index after the role rather than before it. A
    # net whose requirement is unknown is emitted unchecked *and* contributes
    # nothing to the row-offset fit, which is the expensive half.
    (re.compile(r"^(?:.*[-_])?I{1,2}2?C(\d)?[-_]SCL$"), "i2c_scl"),
    (re.compile(r"^(?:.*[-_])?I{1,2}2?C(\d)?[-_]SDA$"), "i2c_sda"),
    (re.compile(r"^(?:.*[-_])?T(?:X)(\d)(?:[-_]?R)?$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?R(?:X)(\d)(?:[-_]?R)?$"), "uart_rx"),
    (re.compile(r"^(?:.*[-_])?TXD(\d)$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?RXD(\d)$"), "uart_rx"),
    (re.compile(r"^(?:.*[-_])?U(?:S?ART)?(\d)[-_]TX$"), "uart_tx"),
    (re.compile(r"^(?:.*[-_])?U(?:S?ART)?(\d)[-_]RX$"), "uart_rx"),
    # The index sits on either side of the role, exactly as it does for the
    # chip selects and the UARTs: SPI1_SCK and SPI_SCK_1 are one board's two
    # ways of writing the same thing, and classify() has read both since the
    # bus rules were widened. This gate had only the first, and that costs more
    # than a dropped net - `resolve` scores candidate row offsets on precisely
    # the nets this function calls checkable, so a spelling missing here starves
    # the scorer. One H743 sheet writes every bus line as SPI_MISO_1, left 5
    # checkable nets out of 43 bound, and the offset it settled on was a whole
    # row out: SWDIO on PA12, USB_N on PA10, SPI_SCK_1 on PA4.
    (re.compile(r"^.*[-_](?:SCK|SCLK)(?:[-_]?\d)?$|^(?:SCK|SCLK)\d$"), "spi_sck"),
    # CLK only after something that names an SPI bus or an SPI device. Bare
    # `.*_CLK` swallows SW_CLK (the debug port), SD_CLK (the SDMMC card clock,
    # which the sdio rules read as CK) and GYRO_CLK (the gyro's 32kHz clock
    # *input*, a timer) - and this gate deciding those are SPI clocks while
    # classify() calls them something else is the drift this whole comment
    # block exists to prevent.
    (re.compile(r"^(?:.*[-_])?(?:SPI\d?|FLASH|OSD|MAX7456|AT7456|BARO)[-_]?CLK"
                r"(?:[-_]?\d)?$"), "spi_sck"),
    # MISO/MOSI name a direction from somebody's point of view, and vendors
    # disagree about whose - one sheet puts SPI1_MOSI on the MCU's SDI pin and
    # SPI1_MISO on its SDO pin. So a data line is only checked for being a data
    # line; which one it is comes from the firmware map, in genconfig.
    (re.compile(r"^.*[-_](?:MISO|MOSI|SDI|SDO(?:UT)?)(?:[-_]?\d)?$"
                r"|^SPI\d[-_]?(?:MISO|MOSI|SDI|SDO)$"
                # A chip's own data pins, which vendors copy onto the net. Only
                # after a device prefix: a bare DO or SI is anybody's.
                r"|^(?:FLASH|OSD|MAX7456|AT7456|BARO|SDCARD|GYRO|IMU|MPU|ICM)"
                r"[-_]?\d?[-_](?:DO|DI|DOUT|DIN)$"), "spi_data"),
    (re.compile(r"^MOTOR(\d+)$|^M(\d)$"), "timer"),
    (re.compile(r"^.*LED[-_]?STRIP.*$"), "timer"),
    (re.compile(r"^.*(?:CLOCK|CLKIN)$"), "timer"),
    # The same spellings genconfig.classify reads as a gyro clock input. Both
    # gates have to know a name or the pair is useless: a role with no
    # requirement is emitted unchecked, and a requirement with no role checks a
    # pin nothing will ever use.
    (re.compile(r"^(?:GYRO|IMU|MPU|ICM)[-_]?\d?[-_]?CLK$"
                r"|^CLK[-_]?(?:GYRO|IMU|MPU|ICM)[-_]?\d?$"), "timer"),
    (re.compile(r"^ADC[-_].*$|^.*[-_](?:BATT|VBAT|CURR|CURRENT|RSSI)$"), "adc"),
    # CAN, spelled FDCAN on every ST sheet because that is what ST calls the
    # peripheral. The index is optional: a part with one instance is drawn as
    # FDCAN_TX, not FDCAN1_TX, and that is CAN1.
    #
    # Only TX and RX are gated here. The transceiver's standby line is a plain
    # GPIO - canPinConfigure() takes any pin for it - so it constrains nothing
    # and a rule claiming otherwise would reject boards for no reason. It still
    # has a role in classify(); this is the half that has a firmware table
    # behind it.
    #
    # CAN_H and CAN_L cannot match, and must not: they are the differential
    # pair on the far side of the transceiver and never reach the MCU.
    (re.compile(r"^(?:.*[-_])?(?:FD)?CAN(\d)?[-_]?TX$"), "can_tx"),
    (re.compile(r"^(?:.*[-_])?(?:FD)?CAN(\d)?[-_]?RX$"), "can_rx"),
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


def requirement_answerable(caps: dict, kind: str) -> bool:
    """
    Can this capability data speak to `kind` at all?

    Distinct from "does the pin support it". A table that is simply absent -
    `can` is missing from anything seeded before CAN was harvested, and from
    every target whose firmware builds no CAN driver - cannot answer either way,
    and the honest response is to leave the net unchecked.

    Answering True instead would be worse than cosmetic. `resolve` scores
    candidate row offsets on the checkable nets, so a requirement that is
    satisfied at *every* offset is not evidence: it cannot separate a good fit
    from a bad one and only dilutes the ones that can.
    """
    if kind.startswith("can"):
        return bool(caps.get("can"))
    return True


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
    if kind in ("can_tx", "can_rx"):
        d = kind.split("_")[1].upper()
        return has(rf"FDCAN{idx or r'\d*'}{d}", rf"CAN{idx or r'\d*'}{d}")
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
    if kind.startswith("can"):
        # An absent table is handled by requirement_answerable() before this is
        # reached, so a caller that gets here has one. Kept as a guard because
        # this function is called directly elsewhere, and False would read as
        # "the firmware rejects this pin" rather than "there is nothing to ask".
        if not caps.get("can"):
            return True
        want = kind.split("_")[1]
        return any(e["dir"] == want and (not idx or e["dev"] == f"CAN{idx}")
                   for e in caps["can"].get(pin, []))
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
        for side in ("L", "R", "T", "B"):
            if not any(r.side == side for r in p.rows):
                continue
            if side == "L" and w.x1 < p.left_edge:
                d = p.left_edge - w.x1
            elif side == "R" and w.x0 > p.right_edge:
                d = w.x0 - p.right_edge
            elif side == "T" and w.y1 < p.top_edge:
                d = p.top_edge - w.y1
            elif side == "B" and w.y0 > p.bottom_edge:
                d = w.y0 - p.bottom_edge
            else:
                continue
            if best is None or d < best[2]:
                best = (p, side, d)
    return (best[0], best[1]) if best else None


# ADC1_8, ADC12_IN8, ADC3_INP0 - a peripheral instance and a channel number.
OWN_ADC_RE = re.compile(r"^ADC(\d+)[-_](?:IN|INP)?(\d{1,2})$")


def _restates_the_pin(text: str, row: PinRow, caps: dict) -> bool:
    """
    Is this label only naming what the pin already is?

    Sheets print a pin's own peripheral channel beside it - `ADC1_8` next to
    PC5, which the firmware tables give as ADC1/ADC2 channel 8. That is an
    annotation, not a net, but it is drawn nearer the symbol than the net label
    behind it, so nearest-wins reads it as the net and the real one loses the
    row. It cost a board its ADC_RSSI_PIN.

    Checked against the capability map rather than by shape, so a net that
    merely looks like this - a board is free to name a net ADC1_8 and wire it
    somewhere else entirely - is only demoted where the pin really does have
    that exact device and channel.
    """
    m = OWN_ADC_RE.match(text.upper())
    if not m:
        return False
    entry = (caps.get("adc") or {}).get(row.pin)
    if not entry:
        return False
    return (m.group(2) == str(entry.get("channel"))
            and any(d in str(entry.get("devices", "")) for d in m.group(1)))


def _pair(sym: Symbol, labels: Sequence[Word], offset: float, caps: dict
          ) -> Tuple[List[Tuple[Word, PinRow]], List[Word]]:
    pairs, orphans = [], []
    claimed: Dict[int, Tuple[tuple, Tuple[Word, PinRow]]] = {}
    for w in labels:
        owner = _owner(sym, w)
        if owner is None:
            orphans.append(w)
            continue
        part, side = owner
        cands = [r for r in part.rows if r.side == side]
        at = _along(side, w) + offset
        best = min(cands, key=lambda r: abs(r.pos - at))
        if abs(best.pos - at) > sym.pitch / 2:
            orphans.append(w)
            continue
        # One row carries one net. Where a line has a pull-up on it, the supply
        # name and the net name are both drawn in that row and both are level
        # with the pin; the nearer one is what the wire into the pin carries,
        # because the other is on the far side of a component. Taking whichever
        # came first put 3V3_MCU on a gyro interrupt and on an OSD chip select.
        gap = {"L": part.left_edge - w.x1, "R": w.x0 - part.right_edge,
               "T": part.top_edge - w.y1, "B": w.y0 - part.bottom_edge}[side]
        # A label that only restates the pin's own capability loses to any real
        # net, however close it is drawn - that is not a distance question.
        # Then distance decides, and alignment breaks the tie. Distance to the
        # whole point, because below that it is extraction noise rather than
        # draughtsmanship: raw floats let a stray "N" take a row from the
        # I2C1_SCL beside it by a thousandth of a point, on a row it was not
        # even level with.
        # Then a name the vocabulary recognises beats one it does not, before
        # distance is consulted at all. The strongest column is trusted
        # wholesale, so it can carry a fragment the extractor made - "2C4" out
        # of I2C4 - and that fragment happened to sit nearer the pin than the
        # SCL beside it. Being a word this domain uses is better evidence of
        # being the net than being a few points closer to it.
        rank = (_restates_the_pin(w.text, best, caps),
                not NET_VOCAB.search(w.text),
                round(gap), abs(best.pos - at))
        prev = claimed.get(id(best))
        loser = w if prev is not None and rank >= prev[0] else \
            (prev[1][0] if prev is not None else None)
        if prev is None or rank < prev[0]:
            claimed[id(best)] = (rank, (w, best))
        # A label drawn twice is one net, not a net that failed to bind. Some
        # sheets carry the name and its net annotation a point apart, and every
        # such row used to pair twice - counting one net as two agreements, and
        # reporting the copy as unmatched once it stopped.
        if loser is not None and loser.text != claimed[id(best)][1][0].text:
            orphans.append(loser)
    pairs = [p for _, p in claimed.values()]
    return pairs, orphans


def read_symbol(words: Sequence[Word], caps: dict,
                page: Optional[int] = None) -> Tuple["Symbol", List[Word], "Result"]:
    """
    Find the symbol, its labels and their pin map - trying both readings of a
    one-column part and keeping the one the firmware agrees with.

    The sides of such a part are not evidence (see `_flip_one_sided`), and the
    labels depend on them, so the two readings are two different sets of nets
    and have to be resolved separately rather than compared as offsets.

    Kept only on a strictly better score. A tie leaves the symbol as found,
    since nothing was learned - and where neither reading checks anything, both
    are equally unsupported and the first is as good as the second.
    """
    sym = find_symbol(words, page=page)
    if sym is None:
        return None, [], None
    labels = find_net_labels(words, sym, caps)
    res = resolve(sym, labels, caps)

    other = _flip_one_sided(sym)
    if other is None:
        return sym, labels, res
    alt_labels = find_net_labels(words, other, caps)
    alt = resolve(other, alt_labels, caps)
    if (alt.score[0], alt.score[0] - alt.score[1]) > (res.score[0],
                                                      res.score[0] - res.score[1]):
        return other, alt_labels, alt
    return sym, labels, res


def resolve(sym: Symbol, labels: Sequence[Word], caps: dict) -> Result:
    """
    Try candidate label->row offsets and keep the one the firmware agrees with
    most. Offsets are swept in fine steps across +/- 1.5 rows, which covers the
    'label above the wire, name centred on it' convention at any font size.

    Labels that pair with nothing are carried out as `orphans` rather than
    dropped. A silently discarded net just looks like an incomplete config later,
    with no clue why - and the usual cause is worth seeing: a net wired to a pin
    the symbol names as a supply, which is a schematic problem, not a parse one.

    One offset is swept per *axis*, not per symbol. Halves of one symbol come off
    the same Altium template and share a font size and a grid, so scoring all of
    a symbol's vertical edges together is what keeps the winning offset from
    being chosen on half the evidence - but a four-sided symbol has two axes,
    and they do not share a shift. A label sits above the horizontal wire of a
    left or right pin, and beside the vertical wire of a top or bottom one;
    those are different distances between different anchors. Sweeping one
    offset for both put a bottom edge two positions out on one board, which
    still read 100% because the pins it landed on could do the job.
    """
    step = sym.pitch / 12
    candidates = [i * step for i in range(-18, 19)]
    axes = {ALONG[r.side] for r in sym.rows}
    if len(axes) > 1:
        # Solve each axis on its own evidence, then put the two halves back
        # together. Each label belongs to exactly one axis, by the side of the
        # part whose gutter it sits in, so the split is clean.
        per_axis = {}
        for axis in sorted(axes):
            mine = [w for w in labels
                    if (o := _owner(sym, w)) and ALONG[o[1]] == axis]
            per_axis[axis] = _resolve_axis(sym, mine, caps, candidates)
        # An axis has to earn its bindings. A four-sided symbol's second axis
        # is read from rotated text - both the pin names and the labels along a
        # top or bottom edge are drawn on their side - and where that alignment
        # cannot be confirmed there is no reason to believe it. One board's
        # bottom edge reaches 2 of its 3 checkable nets at *every* offset, which
        # is not a fit, and binding it anyway put SPI1_SCK two pins out.
        #
        # So a contradicted axis keeps its rows, which is how those pins get
        # reported as unconnected, and loses its links.
        # Contradicted, not merely imperfect. The rule was "satisfies every
        # net it checks", which was right while an axis checked two or three
        # nets and became wrong the moment more spellings became checkable: one
        # G473 sheet mislabels a single UART - USART4_TX drawn on PC11, which
        # is UART4's *RX* - and that one vendor error threw away an axis that
        # agreed on the other eight, taking 19 links and 21 defines with it.
        #
        # One in five may disagree. On the original case - a bottom edge
        # reaching 2 of its 3 nets at every offset, which is not a fit at all -
        # that still allows none, so it is still refused.
        refused = [a for a, r in per_axis.items()
                   if r.score[1] >= 2
                   and r.score[0] < r.score[1] - r.score[1] // 5]
        for axis in refused:
            per_axis[axis] = Result([], per_axis[axis].offset, (0, 0), sym, [],
                                    per_axis[axis].orphans
                                    + [l.net for l in per_axis[axis].links])
        first = per_axis[sorted(axes)[0]]
        links = [l for r in per_axis.values() for l in r.links]
        sat = sum(r.score[0] for r in per_axis.values())
        chk = sum(r.score[1] for r in per_axis.values())
        mapped = {l.pin for l in links}
        return Result(
            links, first.offset, (sat, chk), sym,
            sorted({r.pin for r in sym.rows if r.gpio and r.pin not in mapped}),
            [o for r in per_axis.values() for o in r.orphans],
        )
    return _resolve_axis(sym, labels, caps, candidates)


def _resolve_axis(sym: Symbol, labels: Sequence[Word], caps: dict,
                  candidates: Sequence[float]) -> Result:
    """One axis's worth of the sweep; see resolve() for what is being chosen."""

    best: Optional[Result] = None
    best_key: Optional[Tuple] = None
    for off in candidates:
        pairs, orphans = _pair(sym, labels, off, caps)
        sat = chk = 0
        onpower = 0
        links: List[Link] = []
        for w, row in pairs:
            if not row.gpio:
                onpower += 1
            req = net_requirement(w.text)
            ok, checked, sym_ok = True, False, None
            if req and row.gpio and requirement_answerable(caps, req[0]):
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

    # AT32 names its targets after the flash size, not the package. An
    # AT32F435CGU7 is a 48-pin part with 1024K, an AT32F435RGT7 a 64-pin part
    # with the same 1024K, and Betaflight builds both as AT32F435G - the M
    # target is the 4032K die (ZMT7). So the letter that selects the target is
    # the second one after the family, and the first says only how many of the
    # pins are bonded out, which the sheet settles by what it draws.
    for m in re.finditer(r"AT32F(\d{3})([A-Z])([CGM])", blob):
        line, _package, flash = m.groups()
        exact = names.get(f"AT32F{line}{flash}")
        if exact:
            return exact
    return None


def _fit_against(words: Sequence[Word], data: dict,
                 candidates: Sequence[str]) -> List[Tuple[str, int, int]]:
    """
    How much of the wiring each candidate part accounts for, best first.

    The pin map is recovered the same way for each: the tables differ, so the
    label-to-row fit is scored against different capabilities and the one that
    describes this board agrees with more of it. Candidates that check nothing
    are dropped rather than shown as 0/0, which reads like a failure and is
    only an absence of evidence.
    """
    out: List[Tuple[str, int, int]] = []
    for name in candidates:
        caps = data["targets"].get(name)
        if not caps:
            continue
        try:
            _sym, _labels, res = read_symbol(words, caps)
        except Exception:
            continue
        if res is None or res.score[1] == 0:
            continue
        out.append((name, res.score[0], res.score[1]))
    out.sort(key=lambda r: (-(r[1] / r[2]), -r[2], r[0]))
    return out


def describe_target_miss(words: Sequence[Word], data: dict) -> Optional[str]:
    """
    Why detection failed, when the sheet *does* name a part.

    "Many sheets never name the MCU" is true of 32 boards here and useless on
    the ones that name one and get it wrong. Three sheets from one vendor read
    STM32F743VIH6, which is not a part ST makes - VIH6 is an LQFP100 H743 and
    the F7 line has no 743. Detection is right to refuse it, and saying so, with
    the part that is one character away, is the difference between a dead end
    and a one-field fix.

    Only a single-character substitution is offered. Anything looser starts
    inventing a part the vendor did not write.
    """
    blob = " ".join(w.text.upper() for w in words)
    seen = {m.group(0) for m in re.finditer(r"STM32[FGHCLNUWS]\d[\dX]{2}", blob)}
    if not seen:
        return None
    known = sorted({n.upper() for n in data["targets"]})
    for marking in sorted(seen):
        near = [k for k in known
                if len(k) == len(marking)
                and sum(a != b for a, b in zip(k, marking)) == 1]
        if len(near) == 1:
            return (f"The sheet names {marking}, which is not a part this tool "
                    f"has tables for. {near[0]} differs from it by one "
                    f"character; if that is the intended part, pass --target "
                    f"{near[0]}. Worth telling the vendor either way, since the "
                    "marking on the sheet is then wrong")
        if near:
            # More than one candidate - but the sheet is not the only witness.
            # Each candidate brings its own pin tables, so resolving against
            # each and comparing how much of the wiring it agrees with is
            # exactly the check that decides every other question here. On the
            # F743 sheets it separates them cleanly: H743 agrees with all 22
            # checkable nets and F745 with 20.
            #
            # Still reported rather than chosen. A tie is real - H743 and H750
            # are pin-compatible and fit identically, while differing in flash
            # size - and picking one on the strength of a tie would be the
            # alphabetical order this used to fall back on, dressed up as
            # evidence.
            ranked = _fit_against(words, data, near)
            if ranked:
                shown = ", ".join(f"{k} {s}/{n}" for k, s, n in ranked)
                return (f"The sheet names {marking}, which is not a part this "
                        f"tool has tables for. These differ from it by one "
                        f"character, and this is how much of the wiring each "
                        f"one accounts for: {shown}. Pass --target with the one "
                        "you intend - and tell the vendor, since the marking is "
                        "wrong either way")
            return (f"The sheet names {marking}, which is not a part this tool "
                    f"has tables for. These differ from it by one character: "
                    f"{', '.join(near)}. Nothing on the sheet says which is "
                    "meant, so pass --target with the one you intend - and tell "
                    "the vendor, since the marking is wrong either way")
    return (f"The sheet names {', '.join(sorted(seen))}, which is not a target "
            "this tool has tables for. Pass --target with the part you mean, or "
            "check the marking on the sheet")


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

    sym, labels, res = read_symbol(words, caps, page=args.page)

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
