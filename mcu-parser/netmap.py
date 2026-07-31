#!/usr/bin/env python3
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

PIN_RE = re.compile(r"^(P[A-K]\d{1,2})(?:/(.*))?$")

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

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


def extract_words(pdf: Path) -> List[Word]:
    if not shutil.which("pdftotext"):
        raise SystemExit("pdftotext not found - install poppler-utils")
    out = subprocess.run(
        ["pdftotext", "-bbox-layout", str(pdf), "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    words: List[Word] = []
    for m in re.finditer(
        r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>',
        out, re.S,
    ):
        x0, y0, x1, y1, text = m.groups()
        text = re.sub(r"&amp;", "&", text).strip()
        if text:
            words.append(Word(text, float(x0), float(y0), float(x1), float(y1)))
    return words


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
class Symbol:
    rows: List[PinRow]
    left_edge: float
    right_edge: float
    pitch: float

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


def assemble_pin_names(words: Sequence[Word], gap: float = 2.5) -> List[Word]:
    """
    Rejoin pin-name strings that the extractor split into several words.

    Long alternate-function lists come back in pieces:
        'PC6/TIM3_CH1/TIM8_CH1/'  +  'USART6_TX'
    That matters for more than tidiness. Right-hand pin names are right-aligned
    on the symbol edge, but the piece holding the PXn token ends early, so it
    misses the edge cluster and the whole row - pin and net - is silently lost.
    Stitching the pieces back together first restores the true row extent.
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
            text, x1 = text + v.text, v.x1
        out.append(Word(text, w.x0, w.y0, x1, w.y1))
    return out


def find_symbol(words: Sequence[Word], min_pins: int = 8) -> Symbol:
    """
    Locate the MCU symbol as the largest set of pin-name words sharing an edge
    alignment. Left-side names are left-aligned (common x0), right-side names are
    right-aligned (common x1).
    """
    tagged: List[Tuple[Word, str, List[str], bool]] = []
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

    left = max(cluster([(t[0].x0, t) for t in tagged], tol=1.0), key=len, default=[])
    right = max(cluster([(t[0].x1, t) for t in tagged], tol=1.0), key=len, default=[])
    # A pin name belongs to whichever edge claimed it; drop the overlap so a
    # single-column symbol is not counted twice.
    if {id(t) for t in left} == {id(t) for t in right}:
        right = []
    if len(left) + len(right) < min_pins:
        raise SystemExit("Could not find an MCU symbol - no aligned pin-name column")

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
    return Symbol(rows, left_edge, right_edge, pitch)


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
    """
    pad = sym.pitch * 2
    lo, hi = sym.y_min - pad, sym.y_max + pad

    sides: Dict[str, List[Word]] = {"L": [], "R": []}
    for w in words:
        if not (lo <= w.y0 <= hi) or PIN_RE.match(w.text):
            continue
        if JUNK_RE.search(w.text) or POWER_RE.match(w.text):
            continue
        if w.x1 < sym.left_edge:
            sides["L"].append(w)
        elif w.x0 > sym.right_edge:
            sides["R"].append(w)

    out: List[Word] = []
    for side, cands in sides.items():
        best: List[Word] = []
        best_key = (-1, 0.0)
        # Labels may be aligned on either edge of their own text, so try both.
        for anchor in ("x0", "x1"):
            groups = cluster([(getattr(w, anchor), w) for w in cands], tol=1.0)
            for col in (c for c in groups if len(c) >= 3):
                # Pick the column that actually reads like net names, not merely
                # the nearest one. A BGA sheet puts its ball coordinates (F8, H9,
                # A3) in exactly the place a net label would sit, and proximity
                # alone happily picks those.
                hits = sum(1 for w in col if NET_VOCAB.search(w.text))
                edge = (max(w.x1 for w in col) if side == "L"
                        else -min(w.x0 for w in col))
                if (hits, edge) > best_key:
                    best_key, best = (hits, edge), col
        if best_key[0] == 0:
            best = []      # nothing in the gutter resembles a net name
        out.extend(best)
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
    (re.compile(r"^.*[-_]MISO$|^.*[-_]SDI$|^.*[-_]SDO(?:UT)?$"), "spi_sdi_or_sdo"),
    (re.compile(r"^.*[-_]MOSI$"), "spi_sdo"),
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
    if kind == "spi_sdo":
        return has(r"SPI\d+MOSI", r"SPI\d+SDO")
    if kind == "spi_sdi_or_sdo":
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
    if kind == "spi_sdo":
        return any(e["role"] == "sdo" for e in caps["spi"].get(pin, []))
    if kind == "spi_sdi_or_sdo":
        # A net called *-SDO is the peripheral's output, i.e. the MCU's input.
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


def _pair(sym: Symbol, labels: Sequence[Word], offset: float
          ) -> Tuple[List[Tuple[Word, PinRow]], List[Word]]:
    pairs, orphans = [], []
    for w in labels:
        side = "L" if w.x1 < sym.left_edge else "R"
        cands = [r for r in sym.rows if r.side == side]
        if not cands:
            orphans.append(w)
            continue
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
    """
    step = sym.pitch / 12
    candidates = [i * step for i in range(-18, 19)]

    best: Optional[Result] = None
    for off in candidates:
        pairs, orphans = _pair(sym, labels, off)
        sat = chk = 0
        links: List[Link] = []
        for w, row in pairs:
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
        # Prefer agreement, then coverage; a tie goes to the smaller shift.
        key = (sat, len(pairs), -abs(off))
        if best is None or key > (best.score[0], len(best.links), -abs(best.offset)):
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
    for m in re.finditer(r"STM32([FGHCN]\d)(\d{2})", blob):
        family, digits = m.group(1), m.group(2)
        exact = names.get(f"STM32{family}{digits}")
        if exact:
            return exact
        siblings = sorted(n for u, n in names.items() if u.startswith(f"STM32{family}"))
        if len(siblings) == 1:
            return siblings[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--target", help="FC_TARGET_MCU (auto-detected if omitted)")
    ap.add_argument("--json", type=Path, help="write the net map as JSON")
    args = ap.parse_args()

    words = extract_words(args.pdf)
    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {"targets": {}}
    target = args.target or detect_target(words, data)
    if not target:
        raise SystemExit("Could not detect FC_TARGET_MCU - pass --target")
    caps, data = load_caps(target)

    sym = find_symbol(words)
    labels = find_net_labels(words, sym)
    res = resolve(sym, labels, caps)

    print(f"target {target}  ({caps['mcu']}, {caps['family']})")
    print(f"symbol: {len(sym.rows)} pins, pitch {sym.pitch:.2f}pt, "
          f"AF lists {'present' if sym.has_af_lists else 'absent'}")
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
            "offset": res.offset,
            "agreement": res.agreement,
            "links": [vars(l) for l in res.links],
            "unmapped": res.unmapped,
        }, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
