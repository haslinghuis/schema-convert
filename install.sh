#!/usr/bin/env bash
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

#
# install.sh — check the prerequisites for the schema-convert pipeline.
#
# The pipeline is pure Python standard library; the only real dependency is
# `pdftotext` from poppler, which every tool shells out to for text extraction.
# There is nothing to pip-install.
#
# Usage:
#   ./install.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n'  "$*"; }
err()  { printf '\033[1;31m[x]\033[0m %s\n'  "$*" >&2; }

case "${1:-}" in
  -h|--help) sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) err "Unknown argument: $1 (try --help)"; exit 2 ;;
esac

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  info "python3 found: $(python3 --version)"
  python3 - <<'PY' || { err "Python 3.12 or newer is required"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 12) else 1)
PY
else
  err "python3 not found"
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. pdftotext (poppler) — the one hard dependency
# ---------------------------------------------------------------------------
if command -v pdftotext >/dev/null 2>&1; then
  info "pdftotext found: $(pdftotext -v 2>&1 | head -1)"
else
  err "pdftotext not found — required by every tool here"
  warn "  Debian/Ubuntu:  sudo apt install poppler-utils"
  warn "  Fedora:         sudo dnf install poppler-utils"
  warn "  macOS:          brew install poppler"
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Seed the firmware capability data
# ---------------------------------------------------------------------------
if [[ -f mcu-parser/data/firmware.json ]]; then
  info "firmware data present: $(python3 -c 'import json;d=json.load(open("mcu-parser/data/firmware.json"))["firmware"];print(f"{d[\"rev\"]} ({d[\"branch\"]}, {d[\"date\"]})")')"
  info "refresh it after firmware changes:  python3 mcu-parser/seed_firmware.py"
else
  warn "mcu-parser/data/firmware.json missing — generate it with:"
  warn "  python3 mcu-parser/seed_firmware.py --firmware /path/to/betaflight"
fi

cat <<'EOF'

Setup complete. No virtualenv needed — the tools use only the standard library.

    python3 mcu-parser/seed_firmware.py                   # refresh firmware data
    python3 mcu-parser/netmap.py board.pdf                # inspect the pin map
    python3 mcu-parser/genconfig.py board.pdf \
        --board NAME --manufacturer ID -o out/            # generate a config.h
EOF
