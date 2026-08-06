#!/usr/bin/env python3
#
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
Refuse to bundle a frozen pipeline that is older than the pipeline.

`tauri build` copies app/src-tauri/resources/pipeline/schema-convert into the
bundle; it never rebuilds it. So a bundle can ship a converter from any earlier
revision, and nothing about that looks wrong - the app starts, converts, and
behaves like the revision it was frozen from. The only symptom is the desktop
app disagreeing with the repository, which is indistinguishable from a bug in
the conversion.

This is the §1.1 shape: the failure is silent. Run from tauri.conf.json's
beforeBuildCommand so it cannot be forgotten.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_sidecars import REPO, RESOURCES, SOURCE_STAMP, stamp_sources  # noqa: E402


def main() -> int:
    exe = RESOURCES / "pipeline" / "schema-convert"
    if not exe.is_file():
        exe = RESOURCES / "pipeline" / "schema-convert.exe"
    stamp = RESOURCES / "pipeline" / SOURCE_STAMP

    if not exe.is_file():
        print("No frozen pipeline to bundle.\n"
              "  python3 packaging/build_sidecars.py", file=sys.stderr)
        return 1

    want = stamp_sources()
    have = stamp.read_text().strip() if stamp.is_file() else None

    if have is None:
        print(f"{exe.relative_to(REPO)} was built before it recorded what went "
              f"into it, so it cannot be shown to match the pipeline.\n"
              f"  python3 packaging/build_sidecars.py", file=sys.stderr)
        return 1

    if have != want:
        print(f"{exe.relative_to(REPO)} was frozen from different sources than "
              f"the ones in the tree.\n"
              f"  frozen from  {have[:12]}\n"
              f"  tree is      {want[:12]}\n"
              f"Bundling it would ship a converter that behaves like an older "
              f"revision, with nothing to show for it.\n"
              f"  python3 packaging/build_sidecars.py", file=sys.stderr)
        return 1

    print(f"frozen pipeline matches the tree ({want[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
