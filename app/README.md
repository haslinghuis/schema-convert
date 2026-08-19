# Target Generator

A desktop front end for the converter: pick a schematic, get a Betaflight
`config.h` and a report of everything the pipeline would not decide for you.

```bash
cd app
npm install
npm run tauri:dev      # development
npm run tauri:build    # installable bundle
```

## Why a desktop app

Schematics are confidential descriptions of unreleased hardware. A hosted
upload page would put vendor IP on someone else's server; here the PDF is read
from disk by a local process and never leaves the machine. That constraint
picked the architecture, not taste.

## Why it calls Python instead of reimplementing it

The extraction *is* the product. It is validated against a 168-schematic corpus
and checked against four hand-written configs, and it already rejected one PDF
library for mangling these Altium plots. A TypeScript port would be a second
implementation to keep correct, on a third PDF engine, with none of that
evidence behind it — so the Rust side runs `genconfig.py --json` and passes the
report through without reshaping it.

That last part matters. Every refusal the pipeline makes — an SPI bus it will
not place a device on, a crystal it will not attribute, a polarity no schematic
states — travels as a warning. Dropping any of them would turn a config that
says what it does not know into one that looks finished.

## The report is a file too

**Save report.md** writes the whole report out: the numbers the conversion
rested on, every warning grouped by what has to be done about it, anything
supplied by hand, the reasoning, the pin map, and the config itself.

It exists because a conversion gets handed on — to a reviewer, to the vendor, to
whoever picks the board up in six months — and until it did, the only thing that
travelled was `config.h`, which is precisely the half that cannot say what it
does not know. Nothing in the file is summarised into a count, and the grouping
is shared with the on-screen panel rather than reimplemented, so the saved file
and the window cannot disagree about what was left undecided.

## What a vendor needs installed

Nothing. An installed build ships the whole converter frozen into one
executable and poppler's extractor with the libraries it needs, so it runs on a
machine with no Python and no poppler at all — verified by running the
installed pipeline under `env -i`, with no `PATH`.

Build the payload before bundling:

```bash
python3 packaging/build_sidecars.py   # ~36 MB: frozen pipeline + poppler
npm run tauri:build
```

A **source checkout** has neither and falls back to the system Python and
whatever poppler is on `PATH`, which is what development wants. The window says
which of the two is missing rather than failing vaguely.

### Per platform

The Linux vendoring is implemented and tested. It copies `pdftotext` and every
library it links except the glibc family — those must come from the host,
because they have to match the dynamic loader that starts the process. That
couples a Linux bundle to the glibc it was built against: it runs on that
release and newer, not older, so CI should build in the oldest container you
intend to support.

macOS and Windows need the same two pieces and are not wired up here. macOS
wants the `otool -L` walk and `DYLD_LIBRARY_PATH` in place of `ldd`; Windows
wants a self-contained poppler build, of which there are prebuilt ones, and no
library juggling at all.

## What it does not do

It does not replace review. A config can build cleanly and still not match the
hardware — the roadmap's §1.2 is a board that compiled at 23% flash and would
never have run. Read the agreement score and every warning before shipping a
target.
