# Working on schema-convert

Converts flight-controller schematic PDFs into Betaflight `config.h` targets.
Read `README.md` for what it does, `ROADMAP.md` for what is still open, and
`FINDINGS.md` for every defect it has had - its opening section lists the seven
shapes they keep taking, which is the fastest way to avoid repeating one.

Every rule below is here because it was violated and cost something. None of it
is general advice.

---

## 1. Firmware is the single source of truth

The generator may only emit what the build actually honours. A pin Betaflight's
tables do not list cannot be used, whatever the silicon supports and whatever the
schematic says.

This gets violated in disguise. Two real cases:

- **A vendor label overrode the table.** SPI roles were taken from the net name,
  so a sheet labelling the wire on the MCU's SDI pin `SPI1_MOSI` produced a bus
  with two SDIs and no SDO — which does not compile, because firmware has no
  default for `SPIn_SDO_PIN`. The pin decides; the label is a hint about *which
  bus*, not which line.
- **Datasheet data nearly became a runtime input.** It must not. If a datasheet
  says a pin works and firmware disagrees, the output is a *firmware PR*, not a
  config that the build cannot honour for months.

So: `datasheet ──audit──> firmware ──seed──> generator`. One way. `afaudit.py`
verifies firmware; it never feeds the generator.

## 2. A successful build proves almost nothing

`SYSTEM_HSE_MHZ` was omitted from a board with a 25 MHz crystal. `Makefile` has
`HSE_VALUE ?= 8000000`, so it compiled cleanly at 23.42% flash — and would not
have run, because the PLL was configured for an 8 MHz input.

A green build says the constants were self-consistent. It does not say they match
the hardware. Check emitted values against the datasheet, the schematic, or a
hand-written config for a comparable board.

## 3. Build every board in the corpus, not the one you are delivering

A generated config referenced `GYRO_1_CLKIN_PIN` in `TIMER_PIN_MAPPING` without
defining it, and emitted half a SPI bus. It failed to compile for an entire
working session because only the boards being handed over were ever built; the
others were checked with `netmap` alone.

```bash
python3 -m unittest discover tests     # then build each board
```

## 4. Fix the cause, not the symptom

Refusing to emit an incomplete SPI bus stops the broken output and leaves the bug
in place. The cause was §1. When a check starts rejecting something, ask why the
input reached that state before adding the check.

## 5. Comments and vendor symbols are secondary sources

- `config.c` says `SYSTEM_HSE_MHZ` is *"Only used for F4 and G4 targets"*. True of
  the runtime path, and it misses `mk/config.mk` exporting `-DHSE_VALUE` to the
  clock init on H5/H7/C5/N6. Trace to the code that consumes the value.
- A schematic symbol's AF list claimed `UART7_TX` on a pin whose datasheet AF row
  is empty. Symbols corroborate; they do not prove.
- Betaflight's own tables contain copy-paste errors between MCU families — that
  is what `afaudit.py` exists to find.

## 6. Nothing manufacturer-derived is committed

Vendor schematics are confidential unreleased hardware, and the configs generated
from them describe the same boards. `.gitignore` covers `*.pdf`, `configs/`,
`config.h` and `out/`.

`data/firmware.json` **is** committed, which reverses the earlier rule. Without
it a clone converts nothing, which rules out handing this to anyone who does not
already keep a firmware checkout. Every byte of it is harvested from Betaflight's
own GPL sources, so there is nothing confidential in it — but note *why* it was
nearly a leak anyway: it used to record the path of the tree it came from, and
one tree on this machine is named after the vendor submission it was cut for.
The seeder now records rev, branch and date and deliberately not the path.

That is the general shape to watch for. The risk is rarely the data; it is the
provenance stapled to it — a checkout path, a filename, a directory name.

Board and manufacturer names are scrubbed from docs too — examples use
`EXAMPLEH562` / `CUST`. Before pushing:

```bash
git diff --cached | grep -icE "<vendor>|<board>|/home/"
```

Test fixtures identify a board by the sha256 of its PDF and record output as
counts and one-way digests, never as pin assignments.

---

## Running it

```bash
python3 mcu-parser/seed_firmware.py                  # needs a Betaflight tree
python3 mcu-parser/netmap.py board.pdf               # pin map only
python3 mcu-parser/genconfig.py board.pdf --board NAME --manufacturer ID -o out/
python3 mcu-parser/afaudit.py --datasheet ds.pdf --firmware <tree>
python3 -m unittest discover tests
python3 tests/update_golden.py                       # after an intended change
```

Python 3.12 stdlib only, plus `pdftotext` from poppler. Do not add dependencies.

Betaflight trees live under `../betaflight/*/betaflight` — independent clones, not
worktrees. `seed_firmware.py` prefers the newest one on `master`; pass
`--firmware` to pin it. Some clones carry uncommitted work: never commit, stash,
revert or switch branches in a tree you did not create.

Datasheets are in `../manufacturers/datasheets/`. Every STM32 family the seeder
harvests now has one: F4 (`stm32f405-407.pdf`, DS8626), F7, G4, H5, H7, C5, N6.

F4 was the blocking gap and is closed. Its audit now comes back **0 defects**:
`TIM1_CH1N` on `PA11` was the one finding, it merged as #15510, and the loop
closed the way it was meant to — datasheet → audit → firmware PR → reseed.
#15512 (N657 timer options) and #15513 (H5/H7 `PF8`/`PF9`) came from the same
pass. The remaining families without a datasheet are AT32 and APM32, and those
are not harvested at all yet (ROADMAP §3.4), so nothing can be audited against
them.

A full pass after the reseed comes back **0 defects on every family**:

| family | defects | informational `MISSING PIN` |
|---|---|---|
| F4 `stm32f405-407` | 0 | 2 |
| F7 `stm32f722ic` | 0 | 2 |
| G4 `stm32g474cb` | 0 | 0, plus 1 waived in `af-waivers.json` |
| H5 `stm32h563ri` | 0 | 7 |
| H7 `stm32h743vi` | 0 | 9 |
| C5 `stm32c562ce` | 0 | 38 |
| N6 `stm32n657a0` | 0 | 11 |

**Do not file the `MISSING PIN` entries without checking reachability first.**
They are datasheet options the firmware lacks, which is only worth a PR if a
board can use them, and the ones checked cannot be:

- F4/F7 `TIM13_CH1`/`TIM14_CH1` on `PF8`/`PF9` — `PF` pins exist only on
  144-pin-and-up packages. Of 619 shipped targets exactly two use any `PF` pin,
  and both are H-series where the table is already right.
- H7's nine (`I2C2/3/4` on `PH*`, `SPI2_SCK` on `PI1`, …) — **0** shipped
  targets want those pin/function pairs and **0** corpus boards are blocked by
  them.

So the audit's job here is done: it found three real defects, they merged, and
what is left is coverage nobody can reach. Re-run it after the tables move, not
on a schedule.

**Check the tree is on upstream before seeding.** `seed_firmware.py` picks the
newest clone on `master`, and those clones track the *fork*, not
`betaflight/betaflight`. When the fork is behind, `git pull` reports "already up
to date" while sitting several commits short of upstream — so a reseed appears
to succeed and silently produces the older tables. It has happened once, eight
commits' worth, including the three pin-table fixes.

Both are in sync as of `c18421eb5`, and `tests/check_seed_drift.py --firmware
<tree>` answers the question in seconds. Run it before trusting a reseed; if the
fork has drifted again, `gh repo sync haslinghuis/betaflight --branch master`
then `git pull --ff-only`.

Only the tree under `../betaflight/master/` is safe to fast-forward. The others
carry uncommitted work on feature branches — `test-h5-i2c3` had ten dirty files
throughout this — and must not be touched.

---

## Delegating to subagents

What worked, and why:

- **One file per agent.** Ownership stated explicitly in the brief. Agents do not
  run `git add -A` and do not commit — integration is the caller's job.
- **Falsifiable acceptance criteria.** Not "make it work" but: *on this tree it
  must report zero findings; on that one it must find exactly these three. If it
  does not reproduce them, the tool is wrong — fix it, do not rationalise.*
  A self-graded agent grades itself generously.
- **State the baseline numbers** so a regression is visible. Check them first —
  a stale baseline in a brief wastes the agent's time arguing with reality.
- **Invite disagreement.** One brief asserted a family did not need a define;
  the agent proved otherwise from the source and was right to override. Say
  "confirm from source, not assumption" and mean it.

Verify what comes back. Agent reports have been both right against my
instructions and wrong in detail; re-run the acceptance test and spot-check the
central claim before acting on it or repeating it.
