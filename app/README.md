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

## What it needs

- **Python 3.12+** and **poppler** (`pdftotext`) on `PATH`
- The pipeline itself, either alongside the app in a checkout or bundled

The window says which of these is missing rather than failing vaguely, and
shows the firmware revision the capability data was harvested from — every pin
in a generated config is validated against that snapshot, so it is part of the
provenance.

Packaging Python and poppler into the bundle so a vendor needs neither is not
done yet; today the app expects both on the machine.

## What it does not do

It does not replace review. A config can build cleanly and still not match the
hardware — the roadmap's §1.2 is a board that compiled at 23% flash and would
never have run. Read the agreement score and every warning before shipping a
target.
