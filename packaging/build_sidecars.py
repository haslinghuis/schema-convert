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
Build the two things the desktop app ships so a vendor installs nothing.

    python3 packaging/build_sidecars.py

Produces, under app/src-tauri/resources/:

    pipeline/schema-convert   the whole converter frozen with PyInstaller,
                              capability data included
    poppler/pdftotext         poppler's extractor, with the libraries it needs
    poppler/lib/*.so          beside it

Why both. The pipeline is Python and the extraction is poppler; a vendor should
need neither, and asking a board designer to install a toolchain to submit a
schematic is how a tool goes unused.

The glibc family is deliberately *not* vendored. It has to match the dynamic
loader that starts the process, so shipping it is the one way to guarantee the
binary will not run. Everything else poppler links is copied in beside it, and
the app points the loader at that directory when it launches the extractor -
no rpath rewriting, so no patchelf in the build.

That does couple the Linux build to the glibc it was built against: a bundle
built here runs on that release and newer, not older. Building in the oldest
container you intend to support is the usual answer, and is what CI should do.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESOURCES = REPO / "app" / "src-tauri" / "resources"

# Shipping any of these breaks the bundle rather than making it portable: they
# are the runtime the loader itself is part of, and must come from the host.
SYSTEM_LIBS = (
    "libc.so", "libm.so", "libpthread.so", "libdl.so", "librt.so",
    "ld-linux", "libresolv.so", "libutil.so", "linux-vdso",
)


def run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, **kw)


def build_python() -> str:
    """
    A private virtualenv with PyInstaller in it.

    Not the system interpreter: distributions mark theirs externally managed
    (PEP 668) and installing into it is both refused and rude. The venv lives
    under build/ and is disposable.
    """
    venv = REPO / "build" / "venv"
    exe = venv / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")
    if not exe.is_file():
        print("  creating build/venv...")
        run(sys.executable, "-m", "venv", str(venv))
    have = subprocess.run([str(exe), "-c", "import PyInstaller"],
                          capture_output=True)
    if have.returncode != 0:
        print("  installing PyInstaller into build/venv...")
        run(str(exe), "-m", "pip", "install", "--quiet", "--upgrade",
            "pip", "pyinstaller")
    return str(exe)


def freeze_pipeline(out: Path) -> Path:
    """The converter as one executable, with its capability data inside."""
    out.mkdir(parents=True, exist_ok=True)
    work = REPO / "build" / "pyinstaller"
    work.mkdir(parents=True, exist_ok=True)

    sep = ";" if os.name == "nt" else ":"
    parser = REPO / "mcu-parser"
    run(
        build_python(), "-m", "PyInstaller",
        "--onefile", "--noconfirm", "--clean",
        "--name", "schema-convert",
        "--distpath", str(out),
        "--workpath", str(work / "work"),
        "--specpath", str(work),
        # The data the generator validates every pin against. Without it the
        # frozen binary starts and then refuses on the first conversion.
        "--add-data", f"{parser / 'data'}{sep}data",
        # genconfig imports its siblings by path at runtime, which the analyser
        # cannot see, so they are named explicitly.
        "--hidden-import", "netmap",
        "--hidden-import", "manufacturers",
        "--paths", str(parser),
        str(parser / "genconfig.py"),
    )
    exe = out / ("schema-convert.exe" if os.name == "nt" else "schema-convert")
    if not exe.is_file():
        raise SystemExit(f"PyInstaller produced no {exe}")
    return exe


def vendor_poppler(out: Path) -> Path:
    """pdftotext plus the libraries it needs, made relocatable."""
    src = shutil.which("pdftotext")
    if not src:
        raise SystemExit("pdftotext not on PATH - install poppler-utils to vendor it")
    lib = out / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    dest = out / "pdftotext"
    shutil.copy2(src, dest)
    dest.chmod(0o755)

    ldd = subprocess.run(["ldd", src], capture_output=True, text=True).stdout
    copied = 0
    for line in ldd.splitlines():
        if "=>" not in line:
            continue
        path = line.split("=>", 1)[1].strip().split(" (")[0].strip()
        if not path or not Path(path).is_file():
            continue
        name = Path(path).name
        if any(s in name for s in SYSTEM_LIBS):
            continue
        shutil.copy2(path, lib / name)
        copied += 1

    # No rpath rewriting, so no patchelf build dependency: the app sets
    # LD_LIBRARY_PATH to this lib/ when it launches the extractor. One less
    # tool to have installed, and it works the same on a machine without it.
    print(f"  poppler: pdftotext + {copied} libraries")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-pipeline", action="store_true")
    ap.add_argument("--skip-poppler", action="store_true")
    args = ap.parse_args()

    if os.name == "nt" or sys.platform == "darwin":
        print("Only the Linux vendoring is implemented here; see the README for "
              "what the other platforms need.", file=sys.stderr)

    if not args.skip_pipeline:
        print("freezing the pipeline...")
        exe = freeze_pipeline(RESOURCES / "pipeline")
        print(f"  {exe.relative_to(REPO)}  "
              f"{exe.stat().st_size / 1_048_576:.1f} MB")
    if not args.skip_poppler:
        print("vendoring poppler...")
        vendor_poppler(RESOURCES / "poppler")

    total = sum(f.stat().st_size for f in RESOURCES.rglob("*") if f.is_file())
    print(f"\nbundle payload: {total / 1_048_576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
