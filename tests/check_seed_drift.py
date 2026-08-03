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
Has Betaflight moved out from under the committed capability data?

`mcu-parser/data/firmware.json` is committed so a clone can convert a board
without a Betaflight checkout. That makes it the one file here that goes stale
on its own - the firmware changes, the snapshot does not, and nothing says so.
Every pin the generator emits is validated against this file, so a stale one
does not fail loudly: it quietly keeps offering a pin that has been removed, or
withholds one that has been added. Both produce a config that looks fine.

So: reseed from a fresh tree and compare the *tables*, ignoring the rev, branch
and timestamp, which change on every commit and mean nothing on their own.

    python3 tests/check_seed_drift.py --firmware <betaflight tree>

Exit 1 on drift, with what moved. Run `seed_firmware.py` to resolve it - and
look at the diff before committing, because a table shrinking is how a wrong
pin gets fixed *and* how a parser regression looks.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "mcu-parser" / "data" / "firmware.json"
SEEDER = REPO / "mcu-parser" / "seed_firmware.py"

# Everything except the provenance, which is expected to differ.
TABLES = ("timers", "dma", "uart", "spi", "i2c", "adc", "limits", "sdio")


def tables_of(data: dict) -> dict:
    return {
        "drivers": data.get("drivers", {}),
        "targets": {
            name: {k: t.get(k) for k in TABLES}
            for name, t in data.get("targets", {}).items()
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", required=True, type=Path,
                    help="Betaflight source tree to reseed from")
    args = ap.parse_args()

    if not COMMITTED.exists():
        print(f"{COMMITTED.relative_to(REPO)} is missing - run seed_firmware.py")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "firmware.json"
        run = subprocess.run(
            [sys.executable, str(SEEDER), "--firmware", str(args.firmware),
             "--out", str(fresh), "--quiet"],
            capture_output=True, text=True,
        )
        if run.returncode != 0:
            print(run.stderr.strip() or "seed_firmware.py failed")
            return 1
        new = json.loads(fresh.read_text())

    old = json.loads(COMMITTED.read_text())
    a, b = tables_of(old), tables_of(new)
    print(f"committed: firmware {old['firmware']['rev']} "
          f"({old['firmware']['branch']}, {old['firmware']['date']})")
    print(f"fresh:     firmware {new['firmware']['rev']} "
          f"({new['firmware']['branch']}, {new['firmware']['date']})")

    if a == b:
        print("\nno drift: the capability tables are identical.")
        return 0

    print("\nDRIFT - the committed capability data no longer matches the firmware:")
    if a["drivers"] != b["drivers"]:
        for cat in sorted(set(a["drivers"]) | set(b["drivers"])):
            before, after = a["drivers"].get(cat, {}), b["drivers"].get(cat, {})
            if before != after:
                print(f"  drivers.{cat}: {len(before)} -> {len(after)} parts")
    for name in sorted(set(a["targets"]) | set(b["targets"])):
        ta, tb = a["targets"].get(name), b["targets"].get(name)
        if ta is None or tb is None:
            print(f"  {name}: {'added' if ta is None else 'removed'}")
            continue
        for key in TABLES:
            if ta.get(key) != tb.get(key):
                sa, sb = ta.get(key) or {}, tb.get(key) or {}
                print(f"  {name}.{key}: {len(sa)} -> {len(sb)} entries")
    print("\nRun:  python3 mcu-parser/seed_firmware.py")
    print("Then read the diff before committing - a table that shrinks is both "
          "how a wrong pin gets fixed and how a parser regression looks.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
