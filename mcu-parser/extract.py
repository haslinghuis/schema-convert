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
MCU Parser - Regex-Based Betaflight Schematic Extraction

Extracts a flight-controller board's components AND its functional resources
(power rails, UARTs, SPI/I2C buses, motor outputs, pads/connectors) from a PDF
schematic, then emits a full board summary plus a Betaflight-target-oriented
view.

Text extraction uses `pdftotext -layout` (poppler), which recovers ~25x more
text from these vector schematics than PyPDF2/pdfplumber.

Usage:  python extract.py <schematic.pdf> [output_basename]
Needs:  poppler-utils  (sudo apt install poppler-utils)
"""

import sys
import re
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from datetime import datetime


# --------------------------------------------------------------------------- #
# Component detection
# --------------------------------------------------------------------------- #

@dataclass
class Component:
    category: str
    part_number: str
    reference_designator: str = ""
    confidence: float = 0.6
    instances: int = 1  # how many times the part was seen in the text


# Ordered so that more specific / longer part families win. Each entry maps a
# category to a list of regexes. IGNORECASE is applied at match time.
PATTERNS: Dict[str, List[str]] = {
    "MCU": [
        r"STM32[FGLH]\d[A-Z0-9]{2,}",     # STM32F405OGY6TR, STM32H743VIT6, ...
        r"AT32F4\d{2}[A-Z0-9]*",          # Arterytek
        r"GD32[FGL]\d+[A-Z0-9]*",         # GigaDevice
        r"APM32F\d+[A-Z0-9]*",            # Geehy
        r"RP2350[A-Z0-9]*",
    ],
    "GYRO": [
        r"ICM[-_]?(?:42688|42605|20602|20689|20948|426\d\d)[A-Z0-9]*",
        r"MPU[-_]?(?:6000|6050|6500|9250)",
        r"BMI[-_]?(?:270|160|088|085)",
        # LSM6DSO / LSM6DSR, and the variants that carry a full-scale digit
        # group: LSM6DSV16X, LSM6DSV320KXTR. The letters-only form missed every
        # one of the latter, so a board fitted with an LSM6DSV320K reported "no
        # gyro part recognised" and emitted no driver at all.
        r"LSM6DS[A-Z]{1,2}(?:\d+[A-Z0-9]*)?",
        r"IIM[-_]?42652",
    ],
    "BARO": [
        r"DPS(?:310|368)",
        r"BMP(?:280|388|390|581)",
        r"SPL06[-_]?001?",
        r"LPS22[A-Z0-9]*",
        r"QMP6988",
    ],
    "MAG": [
        r"QMC5883L?",
        r"HMC5883L?",
        r"IST8310",
        r"LIS3MDL",
    ],
    "FLASH": [
        # Match the whole dual-sourced token so "W25Q128JVSIQ/PY25Q128" is ONE part.
        r"(?:W25Q|PY25Q|GD25Q|EN25Q|MX25L|ZB25VQ)\d+[A-Z0-9/]*",
    ],
    "OSD": [
        r"(?:AT|MAX)7456[A-Z0-9]*",
    ],
    "LDO": [
        r"SPX3819[A-Z0-9\-]*",
        r"ME62\d{2}[A-Z0-9\-]*",
        r"AMS1117[A-Z0-9\-]*",
        r"RT9013[A-Z0-9\-]*",
        r"XC6206[A-Z0-9\-]*",
        r"TLV7\d{3}[A-Z0-9\-]*",
    ],
    "DCDC": [
        r"MP\d{4}[A-Z0-9]*",              # MP9943, MP2451, ...
        r"TPS5\d{4}[A-Z0-9]*",
        r"RT\d{4}[A-Z]",
        r"LM2596[A-Z0-9\-]*",
        r"XL1509[A-Z0-9\-]*",
    ],
    "MUX": [
        r"SN74LVC1G3157[A-Z0-9]*",
        r"74LVC1G\d+[A-Z0-9]*",
    ],
    "TRANSISTOR": [
        r"MMBT\d{4}[A-Z0-9]*",
        r"2N\d{4}",
        r"AO3\d{3}",
    ],
    "DIODE": [
        r"SR\d{3}[A-Z0-9]*",             # SR340 schottky
        r"SS\d{2}[A-Z0-9]*",
        r"1N\d{4}[A-Z0-9]*",
        r"SMCJ\d+[A-Z0-9]*",
    ],
    "CRYSTAL": [
        r"\d+(?:\.\d+)?\s?MHZ",
    ],
}

# Which reference-designator letter each category normally uses.
REF_LETTER = {
    "MCU": "U", "GYRO": "U", "BARO": "U", "MAG": "U", "FLASH": "U",
    "OSD": "U", "LDO": "U", "DCDC": "U", "MUX": "U",
    "TRANSISTOR": "Q", "DIODE": "D", "CRYSTAL": "X",
}


def run_pdftotext(pdf_path: str) -> str:
    """Extract layout-preserving text via poppler's pdftotext."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        sys.exit("Error: pdftotext not found. Install poppler-utils "
                 "(Debian/Ubuntu: sudo apt install poppler-utils).")
    if result.returncode != 0:
        sys.exit(f"Error: pdftotext failed: {result.stderr.strip()}")
    return result.stdout


def find_refdes(line: str, part: str, letter: str) -> str:
    """Find a reference designator (e.g. U7) on the same line as the part.

    The layout puts the refdes just before the part number, so we prefer the
    closest matching designator to the left of the part on that line.
    """
    idx = line.find(part)
    if idx == -1:
        return ""
    before = line[:idx]
    matches = list(re.finditer(rf"\b{letter}(\d{{1,3}})\b", before))
    if matches:
        return f"{letter}{matches[-1].group(1)}"  # nearest to the left
    return ""


def detect_components(text: str) -> List[Component]:
    """Detect and de-duplicate components across the whole schematic text."""
    # keyed by (category, dedup_key) -> Component
    found: Dict[tuple, Component] = {}
    lines = text.splitlines()

    for category, patterns in PATTERNS.items():
        letter = REF_LETTER[category]
        for pattern in patterns:
            rx = re.compile(pattern, re.IGNORECASE)
            for line in lines:
                for m in rx.finditer(line):
                    part = re.sub(r"\s+", "", m.group(0)).upper()
                    refdes = find_refdes(line, m.group(0), letter)

                    # Dedup key: prefer the physical part (refdes). Without a
                    # refdes, collapse by part number so a chip mentioned in
                    # several places counts once.
                    key = (category, refdes or part)
                    conf = 0.9 if refdes else (0.8 if len(pattern) > 12 else 0.6)

                    if key in found:
                        c = found[key]
                        c.instances += 1
                        c.confidence = max(c.confidence, conf)
                        if not c.reference_designator and refdes:
                            c.reference_designator = refdes
                    else:
                        found[key] = Component(
                            category=category,
                            part_number=part,
                            reference_designator=refdes,
                            confidence=conf,
                        )
    return merge_substring_duplicates(list(found.values()))


def merge_substring_duplicates(components: List[Component]) -> List[Component]:
    """Fold a shorter part into a longer one in the same category.

    A bare second mention such as "W25Q128" or "ICM-42688" is the same physical
    chip as the fully-qualified "W25Q128JVSIQ/PY25Q128HA" / "ICM-42688P" — the
    shorter form only survives if it has no refdes of its own (or the same one).
    """
    # Longest part numbers first so the canonical entry is the merge target.
    components = sorted(components, key=lambda c: len(c.part_number), reverse=True)
    kept: List[Component] = []
    for c in components:
        host = next(
            (k for k in kept
             if k.category == c.category
             and c.part_number in k.part_number
             and (not c.reference_designator
                  or c.reference_designator == k.reference_designator)),
            None,
        )
        if host:
            host.instances += c.instances
            host.confidence = max(host.confidence, c.confidence)
        else:
            kept.append(c)
    return kept


# --------------------------------------------------------------------------- #
# Functional resource extraction (buses, motors, pads, connectors)
# --------------------------------------------------------------------------- #

@dataclass
class Resources:
    power_rails: List[str] = field(default_factory=list)
    uarts: List[str] = field(default_factory=list)          # e.g. UART1, UART3
    spi_buses: List[str] = field(default_factory=list)      # e.g. SPI1, SPI2
    i2c: bool = False
    motor_outputs: List[str] = field(default_factory=list)  # M1..M4
    special_nets: List[str] = field(default_factory=list)   # GYRO_CS, FLASH_CS, ...
    io_pads: List[str] = field(default_factory=list)        # SBUS, LED, BUZZER, ...
    connectors: Dict[str, int] = field(default_factory=dict)


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def detect_resources(text: str) -> Resources:
    up = text.upper()

    def all_of(pattern):
        return _uniq(re.findall(pattern, up))

    rails = all_of(r"\b(?:VBAT|VBUS|VUSB|5V|4\.5V|3\.3V[-_A-Z]*|3V3|VCC|1\.8V)\b")

    uarts = sorted(_uniq(re.findall(r"\bUART([1-6])_(?:TX|RX)\b", up)))
    spi = sorted(_uniq(re.findall(r"\bSPI([1-3])_(?:SCK|MOSI|MISO)\b", up)))
    motors = sorted(_uniq(re.findall(r"\bM([1-4])\b", up)))

    special = [n for n in ("GYRO_CS", "GYRO_EXTI", "FLASH_CS", "CAM_CON",
                           "HD-VTX", "BOOT")
               if re.search(rf"\b{re.escape(n)}\b", up)]

    io_labels = ("SBUS", "PPM", "LED", "BUZZER", "RSSI", "CURR", "CAM",
                 "VTX", "TVOUT")
    io = [n for n in io_labels if re.search(rf"\b{n}\b", up)]

    connectors: Dict[str, int] = {}
    for m in re.findall(r"HDR-M-2\.54_1X(\d)", up):
        connectors[f"HDR 1x{m} header"] = connectors.get(f"HDR 1x{m} header", 0) + 1
    for m in re.findall(r"TYPEC-[0-9A-Z\-]+", up):
        connectors["USB Type-C"] = connectors.get("USB Type-C", 0) + 1
    pad_count = len(re.findall(r"\bPAD\b", up))
    if pad_count:
        connectors["Solder pads"] = pad_count

    return Resources(
        power_rails=rails,
        uarts=[f"UART{n}" for n in uarts],
        spi_buses=[f"SPI{n}" for n in spi],
        i2c=bool(re.search(r"\bI2C_(?:SCL|SDA)\b", up)),
        motor_outputs=[f"M{n}" for n in motors],
        special_nets=special,
        io_pads=io,
        connectors=connectors,
    )


# --------------------------------------------------------------------------- #
# Betaflight target-oriented view
# --------------------------------------------------------------------------- #

def betaflight_view(components: List[Component], res: Resources) -> dict:
    def first(cat) -> Optional[Component]:
        cs = [c for c in components if c.category == cat]
        return cs[0] if cs else None

    def mcu_target(part: Optional[str]) -> str:
        if not part:
            return "UNKNOWN"
        m = re.match(r"(STM32[FGLH]\d{3}|AT32F4\d{2}|APM32F\d{3}|GD32[FGL]\d+)", part)
        return m.group(1) if m else part

    mcu = first("MCU")
    gyros = [c.part_number for c in components if c.category == "GYRO"]
    return {
        "mcu": mcu.part_number if mcu else None,
        "target_family": mcu_target(mcu.part_number if mcu else None),
        "gyro": gyros,
        "baro": [c.part_number for c in components if c.category == "BARO"],
        "mag": [c.part_number for c in components if c.category == "MAG"],
        "osd": [c.part_number for c in components if c.category == "OSD"],
        "blackbox_flash": [c.part_number for c in components if c.category == "FLASH"],
        "motor_outputs": len(res.motor_outputs),
        "uarts": res.uarts,
        "spi_buses": res.spi_buses,
        "i2c": res.i2c,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

CONF_BADGE = lambda c: "✅" if c >= 0.85 else "⚠️" if c >= 0.6 else "❓"


def generate_markdown(components, res, bf, pdf_path) -> str:
    L: List[str] = []
    board = Path(pdf_path).stem
    L += [f"# {board} — Schematic Extraction", "",
          f"**Source**: {pdf_path}",
          f"**Extracted**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
          "", "---", ""]

    # Betaflight target overview -------------------------------------------- #
    L += ["## Betaflight Target Overview", "",
          "| Field | Value |", "|-------|-------|",
          f"| MCU | `{bf['mcu'] or '?'}` |",
          f"| Target family | `{bf['target_family']}` |",
          f"| Gyro / IMU | {', '.join(f'`{g}`' for g in bf['gyro']) or '—'} |",
          f"| Barometer | {', '.join(f'`{b}`' for b in bf['baro']) or '—'} |",
          f"| Magnetometer | {', '.join(f'`{m}`' for m in bf['mag']) or '—'} |",
          f"| OSD | {', '.join(f'`{o}`' for o in bf['osd']) or '—'} |",
          f"| Blackbox flash | {', '.join(f'`{f}`' for f in bf['blackbox_flash']) or '—'} |",
          f"| Motor outputs | {bf['motor_outputs']} |",
          f"| UARTs | {', '.join(bf['uarts']) or '—'} |",
          f"| SPI buses | {', '.join(bf['spi_buses']) or '—'} |",
          f"| I2C | {'yes' if bf['i2c'] else 'no'} |",
          "", "---", ""]

    # Components ------------------------------------------------------------- #
    L += ["## Detected Components", ""]
    by_cat: Dict[str, List[Component]] = {}
    for c in components:
        by_cat.setdefault(c.category, []).append(c)

    L += ["| Category | Ref | Part Number | Seen | Confidence |",
          "|----------|-----|-------------|------|------------|"]
    for cat in sorted(by_cat):
        for c in sorted(by_cat[cat], key=lambda x: x.reference_designator or "~"):
            L.append(f"| {cat} | {c.reference_designator or '—'} | `{c.part_number}` "
                     f"| {c.instances}× | {CONF_BADGE(c.confidence)} {c.confidence:.0%} |")
    L += ["", "---", ""]

    # Resources -------------------------------------------------------------- #
    L += ["## Functional Resources", "",
          f"- **Power rails**: {', '.join(f'`{r}`' for r in res.power_rails) or '—'}",
          f"- **Motor outputs**: {', '.join(res.motor_outputs) or '—'}",
          f"- **UARTs**: {', '.join(res.uarts) or '—'}",
          f"- **SPI buses**: {', '.join(res.spi_buses) or '—'}",
          f"- **I2C**: {'present' if res.i2c else 'not detected'}",
          f"- **Key nets**: {', '.join(f'`{n}`' for n in res.special_nets) or '—'}",
          f"- **I/O pads / functions**: {', '.join(res.io_pads) or '—'}",
          ""]
    if res.connectors:
        L += ["### Connectors", "", "| Connector | Count |", "|-----------|-------|"]
        for name, n in res.connectors.items():
            L.append(f"| {name} | {n} |")
        L.append("")

    L += ["---", "",
          "> Part numbers are matched by pattern from layout-extracted text; "
          "reference designators are inferred from same-line proximity. "
          "Exact pin/net mapping is not reconstructed from this layout — "
          "verify against the schematic before using for a firmware target.", ""]
    return "\n".join(L)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        sys.exit(f"Error: File not found: {pdf_path}")

    out_base = Path(sys.argv[2]) if len(sys.argv) > 2 else pdf_path

    print(f"Reading PDF: {pdf_path}")
    text = run_pdftotext(str(pdf_path))
    print(f"Extracted {len(text)} characters of text")

    print("Detecting components and resources...")
    components = detect_components(text)
    resources = detect_resources(text)
    bf = betaflight_view(components, resources)

    markdown = generate_markdown(components, resources, bf, str(pdf_path))
    md_path = out_base.with_suffix(".md")
    md_path.write_text(markdown)
    print(f"Markdown: {md_path}")

    json_path = out_base.with_suffix(".json")
    json_path.write_text(json.dumps({
        "source": str(pdf_path),
        "betaflight": bf,
        "components": [asdict(c) for c in components],
        "resources": asdict(resources),
    }, indent=2))
    print(f"JSON: {json_path}")

    # Console summary
    print("\n=== Summary ===")
    print(f"  MCU: {bf['mcu']} (target {bf['target_family']})")
    print(f"  Gyro: {', '.join(bf['gyro']) or '—'}")
    print(f"  Baro: {', '.join(bf['baro']) or '—'}   OSD: {', '.join(bf['osd']) or '—'}")
    print(f"  Flash: {', '.join(bf['blackbox_flash']) or '—'}")
    print(f"  Motors: {bf['motor_outputs']}   UARTs: {', '.join(bf['uarts']) or '—'}"
          f"   SPI: {', '.join(bf['spi_buses']) or '—'}   I2C: {bf['i2c']}")
    print(f"  {len(components)} distinct components detected")


if __name__ == "__main__":
    main()
