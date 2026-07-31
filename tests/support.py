"""
Shared plumbing for the regression suite.

Confidentiality shapes everything here. Vendor schematics and the config.h files
generated from them are unreleased hardware and are gitignored; a test fixture
may not contain a net name, a pin assignment, a part number or even a file name
lifted from one. So the suite is built from three kinds of fixture, none of
which carries board data:

  * `fixtures/firmware-frozen.json` - a frozen subset of `seed_firmware.py`
    output. Public Betaflight pin tables, committed so the golden tests are
    hermetic: re-seeding from a moving firmware tree must not move the goldens.

  * `fixtures/boards.json` - per board, a sha256 of the PDF (which identifies
    the input without revealing anything about it), counts, and one-way digests
    of the netmap and the generated config.h. A digest cannot be inverted, so
    committing it publishes nothing, yet any change to the output fails the
    test. When one does fail, the actual output is written to `tests/.actual/`
    (gitignored) so the developer - who has the schematic - can diff it.

  * synthetic MCUs and synthetic schematic geometry, built in the test files
    themselves. These carry no board data at all and are what actually pins
    down the algorithms.

Boards are discovered by hashing every PDF in a few search directories and
matching against the recorded sha256. Nothing to configure, and a schematic that
has been revised no longer matches, so the suite skips rather than silently
grading a different board.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
MCU_PARSER = REPO / "mcu-parser"
FIXTURES = TESTS / "fixtures"
FROZEN_FIRMWARE = FIXTURES / "firmware-frozen.json"
BOARDS_FIXTURE = FIXTURES / "boards.json"
ACTUAL_DIR = TESTS / ".actual"

# Where to look for the vendor schematics. Anything in SCHEMA_CONVERT_PDF_DIRS
# (os.pathsep separated) is searched first.
PDF_DIRS_ENV = "SCHEMA_CONVERT_PDF_DIRS"
MAX_PDF_BYTES = 32 * 1024 * 1024

# Betaflight tree for the seed_firmware.py tests. Auto-detected when unset.
FIRMWARE_ENV = "SCHEMA_CONVERT_FIRMWARE"

sys.path.insert(0, str(MCU_PARSER))

import genconfig  # noqa: E402
import netmap  # noqa: E402
import seed_firmware  # noqa: E402


# --------------------------------------------------------------------------- #
# Frozen firmware data
# --------------------------------------------------------------------------- #

_frozen: Optional[dict] = None
_frozen_dir: Optional[tempfile.TemporaryDirectory] = None


def frozen_firmware() -> dict:
    """The committed capability data, parsed once."""
    global _frozen
    if _frozen is None:
        _frozen = json.loads(FROZEN_FIRMWARE.read_text())
    return _frozen


def use_frozen_firmware() -> None:
    """
    Point genconfig at the frozen data instead of the developer's local
    `mcu-parser/data/firmware.json`.

    `build()` reads DATA_DIR at call time, so rebinding the module attribute is
    enough. ALIAS_FILE is bound at import from the real DATA_DIR and is left
    alone on purpose: aliases.json is committed source, and a change to it
    should move the goldens.
    """
    global _frozen_dir
    if _frozen_dir is not None:
        return
    assert hasattr(genconfig, "DATA_DIR"), "genconfig.DATA_DIR is gone - update this shim"
    _frozen_dir = tempfile.TemporaryDirectory(prefix="schema-convert-frozen-")
    path = Path(_frozen_dir.name) / "firmware.json"
    path.write_text(FROZEN_FIRMWARE.read_text())
    genconfig.DATA_DIR = Path(_frozen_dir.name)
    netmap.DATA_FILE = path


def frozen_data_dir() -> Path:
    """The directory genconfig is reading its capability data from."""
    use_frozen_firmware()
    assert _frozen_dir is not None
    return Path(_frozen_dir.name)


# --------------------------------------------------------------------------- #
# Board discovery
# --------------------------------------------------------------------------- #

def board_fixtures() -> List[dict]:
    return json.loads(BOARDS_FIXTURE.read_text())["boards"]


def boards_recorded_with() -> dict:
    return json.loads(BOARDS_FIXTURE.read_text())["recorded_with"]


def search_dirs() -> List[Path]:
    """
    Where to look for the schematics.

    SCHEMA_CONVERT_PDF_DIRS replaces the defaults rather than adding to them, so
    pointing it at an empty directory reproduces exactly what a developer
    without the schematics sees - which is the only way to check that the suite
    skips cleanly instead of failing.
    """
    env = os.environ.get(PDF_DIRS_ENV)
    if env is not None:
        dirs = [Path(p) for p in env.split(os.pathsep) if p]
    else:
        dirs = [REPO, TESTS / "pdfs", Path.home() / "Downloads"]
    out, seen = [], set()
    for d in dirs:
        if d.is_dir() and d not in seen:
            seen.add(d)
            out.append(d)
    return out


_pdfs: Optional[Dict[str, Path]] = None


def available_pdfs() -> Dict[str, Path]:
    """sha256 -> path, for every schematic the fixtures know about."""
    global _pdfs
    if _pdfs is not None:
        return _pdfs
    wanted = {b["sha256"] for b in board_fixtures()}
    found: Dict[str, Path] = {}
    for d in search_dirs():
        for pdf in sorted(d.iterdir()):
            if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
                continue
            if pdf.stat().st_size > MAX_PDF_BYTES:
                continue
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            if digest in wanted:
                found.setdefault(digest, pdf)
        if len(found) == len(wanted):
            break
    _pdfs = found
    return found


def missing_message() -> str:
    return (
        "no fixture schematics found. They are confidential vendor material and "
        "are not in the repository; put them in the repo root, in tests/pdfs/, "
        f"or set {PDF_DIRS_ENV} to a directory holding them. Searched: "
        + ", ".join(str(d) for d in search_dirs())
    )


def require_any_board() -> None:
    if not available_pdfs():
        raise unittest.SkipTest(missing_message())


# --------------------------------------------------------------------------- #
# Running the pipeline
# --------------------------------------------------------------------------- #

# Board name and manufacturer are constants so that nothing derived from the
# vendor's own naming reaches a committed digest.
BOARD_NAME = "TESTBOARD"
MANUFACTURER_ID = "TEST"
GYRO_ALIGN = "CW0_DEG"

# Lines whose content is a timestamp, the schematic's file name or its hash.
# Dropped before digesting so a golden tracks the conversion, not the clock -
# and so no vendor file name is baked into a committed value.
VOLATILE_LINE = re.compile(r"^\s*(?:Generated from|Schematic sha256:|Converted:|DATE:)")


@dataclass
class BoardRun:
    fixture: dict
    pdf: Path
    target: str
    caps: dict
    words: list
    symbol: object
    labels: list
    result: object
    cfg: object
    meta: dict
    text: str

    @property
    def id(self) -> str:
        return self.fixture["id"]

    @property
    def diagnostics(self) -> str:
        return " ".join(self.cfg.warnings + self.cfg.notes)


_runs: Dict[str, BoardRun] = {}


def run_board(fixture: dict) -> BoardRun:
    """Convert one board, once per process."""
    bid = fixture["id"]
    if bid in _runs:
        return _runs[bid]
    pdf = available_pdfs().get(fixture["sha256"])
    if pdf is None:
        raise unittest.SkipTest(f"schematic for board '{bid}' not available locally")
    use_frozen_firmware()
    fw = frozen_firmware()

    words = netmap.extract_words(pdf)
    target = netmap.detect_target(words, fw)
    if target is None:
        raise AssertionError(f"{bid}: no FC_TARGET_MCU detected")
    caps = fw["targets"][target]
    symbol = netmap.find_symbol(words)
    labels = netmap.find_net_labels(words, symbol)
    result = netmap.resolve(symbol, labels, caps)

    cfg, meta = genconfig.build(pdf=pdf, board=BOARD_NAME, manufacturer=MANUFACTURER_ID,
                                target=None, gyro_align=GYRO_ALIGN)
    text = "\n".join(cfg.lines).rstrip() + "\n"
    _runs[bid] = BoardRun(fixture, pdf, target, caps, words, symbol, labels,
                          result, cfg, meta, text)
    return _runs[bid]


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #

def stable_config(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not VOLATILE_LINE.match(l)) + "\n"


def config_digest(text: str) -> str:
    return hashlib.sha256(stable_config(text).encode()).hexdigest()


def netmap_json(run: BoardRun) -> str:
    """
    A canonical rendering of the recovered pin map, for digesting.

    Kept separate from the config digest on purpose: when both move, the change
    is in the geometry; when only the config digest moves, it is in generation.
    """
    payload = {
        "target": run.target,
        "offset": round(run.result.offset, 6),
        "pitch": round(run.symbol.pitch, 6),
        "rows": sorted((r.pin, r.side, round(r.y, 3), tuple(r.afs), r.gpio)
                       for r in run.symbol.rows),
        "links": sorted((l.net, l.pin, l.side, l.checked, l.ok, l.gpio, l.symbol_ok)
                        for l in run.result.links),
        "unmapped": sorted(run.result.unmapped),
        "orphans": sorted(run.result.orphans),
    }
    return json.dumps(payload, sort_keys=True, default=str)


def netmap_digest(run: BoardRun) -> str:
    return hashlib.sha256(netmap_json(run).encode()).hexdigest()


def dump_actual(run: BoardRun) -> Path:
    """
    Write the actual output next to the failure, so a mismatch can be diffed.

    Under tests/.actual/, which tests/.gitignore excludes - and the config is
    additionally named config.h, which the repository .gitignore excludes too.
    """
    d = ACTUAL_DIR / run.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.h").write_text(run.text)
    (d / "netmap.json").write_text(json.dumps(json.loads(netmap_json(run)), indent=1) + "\n")
    return d


# --------------------------------------------------------------------------- #
# Reading the generated config.h back
# --------------------------------------------------------------------------- #

DEFINE_RE = re.compile(r"^#define (\w+)[ \t]+(\S.*?)\s*$", re.M)
TIMER_MAP_RE = re.compile(r"TIMER_PIN_MAP\(\s*(\d+),\s*(\w+),\s*(\d+),\s*(-?\d+)\)")
PIN_VALUE_RE = re.compile(r"^P[A-K](?:[0-9]|1[0-5])$")


def defines(text: str) -> Dict[str, str]:
    """
    Every `#define NAME VALUE` in the file.

    The separator is `[ \\t]+` rather than `\\s+` deliberately: `\\s` spans
    newlines, so a valueless define followed by a blank line swallows the next
    define and silently hides it from the check.
    """
    return dict(DEFINE_RE.findall(text))


def timer_rows(text: str):
    """[(index, label, occurrence, dmaopt), ...] from TIMER_PIN_MAPPING."""
    return [(int(i), label, int(occ), int(opt))
            for i, label, occ, opt in TIMER_MAP_RE.findall(text)]


# --------------------------------------------------------------------------- #
# seed_firmware.py against a real tree
# --------------------------------------------------------------------------- #

_seeded: Optional[dict] = None
_seed_failure: Optional[str] = None


def firmware_tree() -> Optional[Path]:
    env = os.environ.get(FIRMWARE_ENV)
    if env:
        p = Path(env)
        return p if (p / "src/main/target/common_pre.h").exists() else None
    return seed_firmware.find_firmware()


def seeded_firmware() -> dict:
    """
    Run seed_firmware.py once per process and reuse the result.

    Read-only with respect to the Betaflight tree, and it never touches
    mcu-parser/data/firmware.json.
    """
    global _seeded, _seed_failure
    if _seed_failure:
        raise unittest.SkipTest(_seed_failure)
    if _seeded is None:
        fw = firmware_tree()
        if fw is None:
            _seed_failure = (
                "no Betaflight source tree found; set "
                f"{FIRMWARE_ENV} to one to exercise seed_firmware.py"
            )
            raise unittest.SkipTest(_seed_failure)
        _seeded = seed_firmware.build(fw, quiet=True)
    return _seeded


def git_rev(path: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "--short=9", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""
