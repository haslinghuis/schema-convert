#!/usr/bin/env python3
"""
Re-record the golden values in tests/fixtures/boards.json.

    python3 tests/update_golden.py               # refresh every board found
    python3 tests/update_golden.py --add x.pdf --id h5-rev-c --note "..."
    python3 tests/update_golden.py --reseed      # also refresh firmware-frozen.json

Run this only after looking at what changed. It prints every field that moves,
including newly recorded defects, because "the golden is whatever the code does
now" is worth nothing unless a human read the diff first. The generated
config.h itself is written to tests/.actual/<id>/ (gitignored) so it can be
diffed against the previous one.

Nothing board-identifying is written: the fixture holds a sha256 of the PDF,
counts, and digests of the output. See tests/support.py for why.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis  # noqa: E402
import support  # noqa: E402

FROZEN_TARGETS = ("STM32F405", "STM32F722", "STM32G474", "STM32H562")


def refresh(board: dict) -> dict:
    run = support.run_board(board)
    support.dump_actual(run)
    return {"id": board["id"], "sha256": board["sha256"],
            "note": board.get("note", ""), **analysis.metrics(run)}


def diff(old: dict, new: dict, path: str = "") -> list:
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key, "<absent>"), new.get(key, "<absent>")
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff(a, b, f"{path}{key}.")
        elif a != b:
            out.append(f"    {path}{key}: {a} -> {b}")
    return out


def reseed(firmware: Path | None) -> None:
    import seed_firmware
    fw = firmware or support.firmware_tree()
    if fw is None:
        raise SystemExit("no Betaflight tree found; pass --firmware PATH")
    data = seed_firmware.build(fw, quiet=True)
    frozen = json.loads(support.FROZEN_FIRMWARE.read_text())
    frozen.update({
        "schema": data["schema"],
        "generated": data["generated"],
        "firmware": {k: v for k, v in data["firmware"].items() if k != "path"},
        "drivers": data["drivers"],
        "targets": {k: v for k, v in data["targets"].items() if k in FROZEN_TARGETS},
    })
    support.FROZEN_FIRMWARE.write_text(json.dumps(frozen, indent=1) + "\n")
    print(f"reseeded {support.FROZEN_FIRMWARE.name} from {fw} "
          f"({frozen['firmware']['rev']}, {frozen['firmware']['branch']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--add", type=Path, help="register another schematic")
    ap.add_argument("--id", help="fixture id for --add (no vendor or board name)")
    ap.add_argument("--note", default="", help="what makes this board worth testing")
    ap.add_argument("--reseed", action="store_true",
                    help="refresh the frozen firmware capability fixture too")
    ap.add_argument("--firmware", type=Path, help="Betaflight tree for --reseed")
    args = ap.parse_args()

    if args.reseed:
        reseed(args.firmware)

    doc = json.loads(support.BOARDS_FIXTURE.read_text())
    boards = doc["boards"]

    if args.add:
        if not args.id:
            raise SystemExit("--add needs --id")
        digest = hashlib.sha256(args.add.read_bytes()).hexdigest()
        if any(b["sha256"] == digest for b in boards):
            raise SystemExit("that schematic is already recorded")
        boards.append({"id": args.id, "sha256": digest, "note": args.note})
        support._pdfs = None

    changed = 0
    out = []
    for board in boards:
        if board["sha256"] not in support.available_pdfs():
            print(f"  {board['id']}: schematic not available, keeping recorded values")
            out.append(board)
            continue
        new = refresh(board)
        lines = diff(board, new)
        if lines:
            changed += 1
            print(f"  {board['id']}:")
            print("\n".join(lines))
        else:
            print(f"  {board['id']}: unchanged")
        out.append(new)

    doc["boards"] = out
    doc["recorded_with"] = {
        "frozen_firmware_rev": support.frozen_firmware()["firmware"]["rev"],
        "frozen_firmware_branch": support.frozen_firmware()["firmware"]["branch"],
        "tool_rev": support.git_rev(support.REPO),
    }
    support.BOARDS_FIXTURE.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"\n{changed} board(s) changed; wrote {support.BOARDS_FIXTURE}")
    print("actual output for inspection: tests/.actual/<id>/ (gitignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
