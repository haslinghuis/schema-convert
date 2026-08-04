# Findings

Every defect this tool has had, what it cost, and how it was found. Kept
separate from [`ROADMAP.md`](ROADMAP.md), which is only what is still open —
these two share one numbering, so a `§1.12` reference resolves here and a `§3.4`
resolves there.

This is not a changelog. Each entry is here because the *way* it was found is
worth repeating, and because several of them were invisible to anyone reading
the code.

## The shapes that keep recurring

Stated once, so the entries below can be evidence rather than repetition.

1. **Silence is the failure mode.** Almost none of these announced themselves.
   Input was dropped while every diagnostic stayed clean — and agreement stayed
   at 100%, because it measures what was read against the firmware, not what was
   on the sheet. A board reading 64 of its 98 pins reported nothing wrong.
   *(§1.1, §1.5, §1.9, §1.17, §1.20)*

2. **A rule describes one spelling — the one on the boards you had.** `TX4` but
   not `UART4_TX`. `MOTOR1` but not `MOTOR_1`. `I2C` but not `IIC`. A separator
   that is a dash or an underscore but never a dot. None of it was findable by
   reading the code; what finds it is counting what came out *unclassified*
   across the whole corpus, which takes five minutes and should be run whenever
   a submission arrives. *(§1.8, §1.11, §1.15, §1.16, §1.18, §1.19)*

3. **Wherever a rule matches an indexed name, the index may carry a separator —
   in any position.** `GYRO_1_CS`, `GYRO-CS2`, `GYRO_CS_1`, `SPI_MISO_2`.
   Written down in §1.16 and *then* found to be unapplied in two rules, by
   re-reading it rather than by another board arriving. *(§1.16, §1.19)*

4. **Two gates in series must share a vocabulary.** `NET_VOCAB` decides what is
   collected as a label; `classify` decides what a collected label means.
   Teaching the second about a spelling the first has never heard of changes
   nothing at all. A test now asserts they agree. *(§1.18)*

5. **Geometry proposes, firmware validates — and a green build proves almost
   nothing.** A config compiled at 23% flash and would never have run, because
   the constants were self-consistent and did not match the hardware.
   *(§1.2, §1.7)*

6. **Measure the whole corpus before and after, every time.** Several changes
   that were obviously right lost more than they gained: a wider label gutter
   took one board from 100% to 67%, and an adjacency guard cost another 20 pins.
   Aggregate movement is the only honest test. *(§1.13, §1.14)*

7. **Refuse rather than guess.** A wrong answer is worse than a gap, because
   nothing downstream can tell. Where the evidence is not decisive the tool says
   so and names what it did find. *(§3.2, §3.3b, §1.14)*

---

## 1. Defects

### 1.1 Multi-page schematics flattened into one coordinate space — FIXED

*Resolved. Extraction is now page-aware; the record below is kept because the
failure mode is instructive and the second bug it exposed is worth remembering.*

`netmap.extract_words()` used to regex every `<word>` element out of the
`pdftotext -bbox-layout` output without tracking which `<page>` it came from.
Every page of an A-series sheet shares the same coordinate range, so words from
page 1 and page 4 collided:

```
pages: 5
  page 1: 275 words,   0 pin-name tokens, y range 29-561
  page 2: 214 words,   0 pin-name tokens, y range 29-561
  page 3: 429 words,   0 pin-name tokens, y range 29-561
  page 4: 514 words,  36 pin-name tokens, y range 29-561
  page 5: 508 words,   0 pin-name tokens, y range 29-561
```

One of the boards processed is a 5-page schematic. It still scored 18/18,
because the MCU symbol dominates its own column and the firmware check rejects
nonsense — but that was luck, not design. Words from four unrelated pages were
competing to be its net-label column, and unexplained unclassified nets on that
board were the visible symptom.

**Fixed by:** parsing per `<page>`, running symbol detection on each, ranking by
distinct GPIO pins, and locking net labels to the symbol's own sheet. A split
MCU merges only on strong evidence (disjoint pin sets, comparable size, matching
pitch); a rival page that fails that test is reported, never silently dropped.
`--page N` overrides, and the header now reads `31 pins on page 4 of 5`.

Result on the 5-page board: the offset moved from +2.99pt to +0.60pt — matching
every other board's convention, which is strong evidence the old fit was being
dragged by foreign text. Three unclassified nets (`HOLD`, `COU42`, `VTX`) turned
out to be labels from other sheets, each of which had been *stealing a pin from a
legitimate page-4 net*. The three single-page boards are byte-identical.

#### The second bug this exposed: phantom symbol edge

Isolating one page revealed a latent defect that had been masked. On a
**single-column** symbol, right-aligned names of unequal length share an `x1` but
not an `x0`, so the x0 clustering produced a phantom second "edge" holding just
the short names about 8pt from the real one. The overlap guard only discarded the
duplicate when the two clusters were *identical*.

That board's "50 pins" were really **31 pins with 19 counted twice**, and the
phantom left edge was pairing against whatever text happened to sit to its left
(`PIC1301 → PB3`, `P → PB4`). The guard now removes items the larger edge already
claimed.

Worth noting how this hid: the generated `config.h` was byte-identical before and
after, because none of the bogus nets matched a `ROLE_RULES` entry and so never
reached the output. A silent internal corruption that happened not to surface —
exactly the class of thing §4.1's invariant tests exist to catch.

### 1.2 `SYSTEM_HSE_MHZ` was never emitted — FIXED, and it was worse than described

Used by **41%** of boards. This entry originally said "wrong for F4 and G4",
reasoning from the comment in `src/main/config/config.c`:

```c
.hseMhz = SYSTEM_HSE_MHZ,  // Only used for F4 and G4 targets
```

**That comment describes only one of two paths, and taking it at face value was
wrong.** The define has a second, entirely separate compile-time effect:

- **Runtime path** — `config.c` seeds `systemConfig()->hseMhz`, which `fc/init.c`
  feeds to `systemClockSetHSEValue()`. Gated by `PLATFORM_TRAIT_CONFIG_HSE`,
  defined only for F4/G4. This is what the comment means.
- **Compile-time path** — `mk/config.mk` preprocesses the value out of `config.h`
  and exports it as `-DHSE_VALUE`, which the platform clock init consumes
  directly. On H5: `#if HSE_VALUE == 8000000 … #elif == 25000000 … #else #error`,
  selecting the PLL M/N ratios. H7, C5 and N6 have equivalents.

The trap is `Makefile:184`:

```make
HSE_VALUE ?= 8000000
```

**Omitting the define is not "no HSE" — it silently compiles as 8 MHz.** So an H5
board with a 25 MHz crystal builds perfectly and asks PLL1 for M=2/N=125: a
12.5 MHz PFD outside the VCIRANGE band the code declares, and a 1.56 GHz VCO
instead of 500 MHz. It would not run.

A config generated by this tool before the fix had exactly that defect. It built
clean at 23.42% flash — because 8 MHz is a *valid* branch of that `#if`. **The
successful build proved nothing**, which is the lesson worth keeping: a green
build says the constants were self-consistent, not that they match the hardware.

Affected families are therefore `{F4, G4, H5, H7, C5, N6}`. F7 really is
cosmetic — `system_stm32f7xx.c` hardcodes `PLL_M 8` and `HSE_VALUE` only feeds
clock reporting — so dropping it there was correct.

**Fixed by** associating a crystal with the MCU two ways: a shared `OSC*` net
label between the crystal and the MCU's OSC pins (which resolves a G4 board whose
crystal is 154 pt away), falling back to proximity only when decisive — within
`30 × symbol pitch`, and at least 3× nearer than any rival. Boards carry several
crystals: the H562 sheets have both a 25 MHz MCU crystal and a 27 MHz OSD clock,
and picking the wrong one is as bad as picking none. Ferrite-bead impedance specs
(`/200R@100MHZ`) are excluded. Values outside the corpus set are emitted with a
warning; an unassociable crystal emits nothing and warns rather than guessing.
`--hse-mhz N` overrides.

Verified against hand-written references: G4 → 8 (matches `SDMODELG473`), F4 → 8
(matches `JHEF405PRO`), H5 → 25 (matches `NUCLEOH563ZI`), F7 → unchanged.

#### Round two: it was still picking the wrong crystal, not just missing one

Running the corpus put 41 boards in the "no crystal found" column, and looking
at them found two ways of being *wrong* rather than silent.

**Distance was measured across sheets.** Pages share a coordinate space (§1.1),
so a crystal on page 2 and OSC pins on page 1 produce a number that means
nothing. One H562 was emitting `SYSTEM_HSE_MHZ 27` from an OSD crystal drawn on
another sheet — and `system_stm32h5xx.c` accepts only 8 or 25, so that config
would have stopped at its own `#error`. A G4 was emitting 26 from an RF module's
crystal on page 4 where its hand-written config says 8.

**Frequency was never weighed, only proximity.** 27 MHz is the MAX7456's part
and 40 MHz an RF module's. Across the 619 hand-written configs neither is the
`SYSTEM_HSE_MHZ` of a single board — they are 8 MHz on 245, then 16, 48, 25 and
24. So neither may be claimed on proximity alone. A shared OSC net label is the
sheet stating the connection outright and still wins for any frequency, but the
result is flagged; two boards reach 27 MHz that way.

**The window was 30 × the symbol's row pitch**, which is not a sheet scale.
Pitch runs from 1.4pt to 18pt across this corpus, so the window ranged from 43pt
to 540pt — rejecting an 8 MHz crystal 214pt from the OSC pins on a fine-pitch
sheet while accepting almost anything on a coarse one. The symbol's own height
and width track the drawing scale instead.

44 → 46 boards emit it: four gained, two withdrawn because they were wrong. The
58 that still omit are 19 F7 (deliberate — the PLL input is hardcoded), 21
sheets that print no frequency anywhere, 9 carrying only OSD or RF crystals, 5
whose symbol never names its OSC pins, and 4 with the crystal on another sheet.
Each says which, and points at `--hse-mhz`.

The lesson is the same one as §1.7: **a rule that only counts evidence *for* a
candidate will pick a bad one when nothing better is near.** Proximity said
"this is the closest crystal"; nothing asked whether it could be an HSE at all.

### 1.3 Fixed-mapping timer DMA options are never de-duplicated — ADDRESSED

On DMAMUX parts every timer gets a distinct `dmaopt` (§ below). On **fixed-mapping**
parts (F4, F7) nothing checks the timers against each other. One F7 board has
MOTOR1/2/3 on `TIM8_CH1/CH2/CH3` all at `dmaopt 0`, which resolves to
`DMA2_S2_C0` for all three — three motors on one stream.

Latent, because `DSHOT_BITBANG_ON` is always emitted and bitbang does not use the
timer's own DMA; the hand-written configs in the config repo do the same thing.
It becomes real the moment a board wants `DSHOT_DMAR_OFF` without bitbang. The
ADC allocation already dodges streams the timers claimed — the timers just do not
dodge each other.

Found by the regression suite, which asserted the *designed* property rather than
the behaviour of the day.

**Fixed by** giving each timer row a stream nobody else holds, and saying why in
the report rather than silently renumbering:

```
MOTOR2_PIN takes DMA option 1 (DMA2_S3_C7): option 0 is DMA2_S2_C0,
and DMA2_S2 is taken by MOTOR1_PIN
```

That F7 board now lands on DMA2_S2 / S3 / S4 / S7 instead of three motors on
DMA2_S2. This diverges from the hand-written configs, which all use option 0 —
inert while `DSHOT_BITBANG_ON` is emitted, correct the moment it is not.

Burst DShot is *detected* but deliberately not enabled: all four motors being on
one timer is the case for it, yet nothing here can check that timer's update-DMA
stream is free, so it says so and leaves the choice to a human.

### 1.4 `seed_firmware._eval` mishandles arithmetic guards — FIXED

The comment says arithmetic guards such as `TARGET_FLASH_SIZE > 512` are treated
as "no obstacle", and the fallback that would do so only fires when `eval`
*raises*. It does not raise: the identifier resolves to `False`, so
`False > 512` evaluates to `False` and silently gates the line off.

**Fixed** by making a macro name answer only the question the harvest can
answer. In a boolean context it still reports whether the macro is defined, which
is what `#ifdef`-style guards ask; a comparison or arithmetic use asks for its
*value*, which is never harvested, so it yields an unknown that the guard solver
tries both ways rather than resolving to `False` by accident. The test that
pinned the old behaviour was updated in the same change.

### 1.5 Only one box of a split MCU symbol was read — FIXED

A large package is routinely plotted as **several boxes on one sheet**, one per
port group. Detection returned a single box per page, so the rest of the
package was dropped — and dropped invisibly: every pin it did read was correct
and agreement stayed at 100%. One H743 board read **43 of its 100 pins** and
reported nothing wrong. §1.1 had fixed the same failure *across* sheets; this
was the same thing within one.

Four causes, and only the first is the one you would guess:

- Only the strongest aligned column per page was kept. Boxes are now peeled off
  one at a time and offered to the existing split-symbol test.
- **Altium stamps an invisible annotation token beside everything it draws** —
  `PIU1014` (pin 14 of U10), `COU8`, `NLTX2` — placed closer than the gap that
  separates the pieces of a split pin name. Absorbing one turned `PC0` into
  `PC0PIU108`, which no longer reads as a pin. This is what actually hid the
  right-hand column on most of these sheets, and it had already been seen
  without being recognised: the `PIC1301 → PB3` pairing in §1.1 is the same
  token.
- Labels were matched to a part through a dict **keyed by page**, so two boxes
  on one sheet collapsed to whichever was stored last. Ownership is now
  geometric.
- A box only has a gutter on a side it has pin names on. Searching the empty
  side reached across the gap and collected the neighbouring box's labels.

Also: ST's own dash qualifier is now accepted in a pin name (`PA0-WKUP`,
`PC14-OSC32_IN`, and `PB8-BOOT0`, which is where G4 keeps BOOT0), and a part
number with a wildcarded digit (`STM32F7X2RXT`) resolves when exactly one
seeded target fits.

### 1.6 The label gutter is not always one column — FIXED

Net labels were assumed to form one aligned column per side. Where a sheet
draws each label against its own wire the gutter is several near-parallel
columns a few points apart, and only the strongest was kept. One board lost its
**entire I2C2 bus** that way while reporting nothing amiss.

Neighbouring columns are now taken too, but only the words in them that read
like net names on their own — a BGA sheet fills that same space with ball
coordinates (`E10`, `B10`), and those would otherwise bind to whatever row they
sit level with. The strongest column is still trusted wholesale, having already
proved itself as a column.

### 1.7 A firmware contradiction did not count against an offset — FIXED

Widening the gutter made one board *worse*, which exposed an older bug.

Offsets were scored on agreements only; a pin the firmware says **cannot** do
the job counted for nothing. So two fits with equal agreement were separated on
raw coverage, and the fit that paired more labels won even when its extra pairs
were contradictions. That board chose an offset three quarters of a row out,
sliding `SWDIO` onto its neighbour's pin and eight nets onto supply rows.

Contradictions now rank directly, then supply-row landings — below
contradictions, since a sheet can legitimately run a net past a supply pin, but
no schematic wires `FLASH_CS` to `VBAT`. The board reads 19/19 where it read
16/16 before.

The general lesson: **the scorer only counted evidence for, never evidence
against.** Anywhere a fit is chosen by search, check that both are counted.

### 1.8 Half the UART spellings were unclassified — FIXED

Counting what came out of the generator as "no config.h role" across the corpus
made this one impossible to miss: `UART4_TX`/`UART4_RX` unclassified on **21
boards**, `UART1` on 14, `UART3` on 14, `UART2` and `UART5` on 13 each.

Only one of the two orderings was matched. `TX4` and `UART-TX4` put the index
last and were recognised; `UART4_TX` and `USART3_RX` put it first and were not.
Both are ordinary. **Two thirds of the corpus generated a config with no serial
ports in it at all** — while netmap had validated every one of those nets
against the firmware tables and reported them fine. The two halves of the tool
disagreed about what a UART net looks like and nothing compared them.

Smaller cases of the same shape: I2C nets that carry no bus number (`SCL`,
`SDA1` — the bus comes from the pins, so the number was never needed), `LED2`
having no rule at all, `LED-1`/`LED-2` spelled with a dash, and `BEEPER_PIN`
losing its role to a suffix.

### 1.9 Only two of a symbol's four edges were read — FIXED

A submitted F405 came back with 29 of its 50 pins. Symbol detection looked for
pin names sharing an *x* — the columns down the left and right of a box — and
nothing else. A four-sided QFP symbol also runs names across its top and bottom,
and those were invisible. **20 of the 125 corpus boards draw one**, and on the
largest 41 of its 82 pins are on horizontal edges; several were already filed
under §3.3 as "no SPI bus resolved", because the bus nets were on the two sides
nobody read.

An edge is the same search on a different coordinate, so the fix was to stop
assuming which. A row carries where it sits *along its own edge*; `ALONG` says
which axis that is; the gutter, the ownership test and the pairing all ask
before measuring.

Three things this turned up, each of which had been latent:

- **A pin name is never the continuation of another pin name.** That sheet sets
  its names 1.15pt apart horizontally — closer than the gap that rejoins one
  split name — so the assembler fused `PA4+PA5+…+PB2` into a single token and
  dropped nine pins. Supply names end a name too: `PB8`+`BOOT0` became
  `PB8BOOT0` and took the board's I2C SCL with it.
- **The offset must be swept per axis.** A label sits above the horizontal wire
  of a left-hand pin and beside the vertical wire of a top one - different
  distances between different anchors. One offset for both put a bottom edge two
  pins out, and it still read 100%, because the pins it landed on could do the
  job.
- **An axis has to earn its bindings.** Names and labels on horizontal edges are
  drawn rotated, and on one board that alignment cannot be confirmed at all —
  the best any offset reaches is 2 of its 3 checkable nets. A contradicted axis
  now keeps its rows, so those pins are still reported as unconnected, and loses
  its links.

Also: ST writes more than one dash qualifier (`PC13-TAMPER-RTC`,
`PC14-OSC32-IN`) where the pattern allowed one, and a trailing `_PIN` is
Betaflight's own define name copied onto the sheet — 91 nets across the corpus
end that way and 87 classify once it is stripped.

### 1.10 The row pitch was measured across both columns — FIXED

Pitch is the smallest gap between adjacent names, and it was taken over *all*
rows. The two columns of a symbol are interleaved along the same axis: a left
row and the right row opposite it sit a fraction apart while the real step is to
the next pair. One board's columns are each a clean 7.2pt apart and their union
alternates 0.72 and 6.48, so its pitch came out **ten times too small**.

It survived for as long as it did because nothing had ever shifted the minimum.
Recognising two more pin names did, and the board went from 100% agreement to
binding nothing at all — every tolerance downstream is scaled by pitch.

Two lessons worth more than the fix. **A constant tuned against a broken value
is broken too**: the merge test's adjacency reach of six pitches was harmless
while the pitch was under-measured and far too generous once it was right, and
correcting one without the other ballooned a symbol to 198 rows. And **a number
that has never moved is not the same as a number that is right** — this one had
been wrong on that board since the day it was written.

---

### 1.11 A pin name can be written last, and a net can be named after its part — FIXED

Two more submissions, two more spellings, and between them the clearest
statement of what this class of bug costs.

**The pin name at the end.** One H743's symbol names its left column pin-first
(`PC14/OSC32_IN`, as everything else does) and its right column the other way
round — `USART3_RX/PD9`, `SPI2_SCK/PB13`, `TIM3_CH3(M7)/PB0`, and
`TIM4_CH3(M11)PD14` with no separator at all. `PIN_RE` anchors at the start, so
half the package was invisible: 62 pins of ~97, with 20 net labels binding to
nothing. Matching a pin name at the *tail* as well takes it to 97 pins at 27/27
agreement with one label unbound. It also reads through `SPI1_MISO/IPA6`, which
is a typo for PA6.

Deliberately used only where a pin name is being *recognised*, never where
`PIN_RE` excludes a word from being a net label — a label that merely ends in
something pin-shaped is still a label.

**The net named after the part.** The same sheet names every device net after
the part on the other end of it, with a dot: `ICM42688.CS`, `AT7456.MOSI`,
`W25Q128.CS`, `BMP280.SCK`. The dot was not a separator and the part numbers
are not the family words the rules match on, so none of it classified. Both are
normalised now — the dot to an underscore, and a leading part number back to
the family the rules already know — and only where a separator follows, so a
bare `ICM42688` stays a part marking for `detect_parts` to read.

That settled something the tests had given up on. `MPU6000-CS` and
`ICM42688-CS` were asserted to classify as *nothing*, with a comment explaining
why: a part number is not a device index, and reading the digits would invent a
sixth or fourth IMU. Folding the part number to its family recognises the
device and yields no index at all — the right answer rather than the safe one.
The invariant those cases were protecting is now checked directly instead of
being sidestepped.

Over the corpus these two took total defines **4586 → 5109**, buses resolved
196 → 237, chip-select pins 160 → 182 and SPI instances 144 → 160, over 29
boards with none losing anything.

### 1.12 A pin name can carry ST's dual-pad suffix — FIXED

An H7 board came in "missing SPI 2 defines". It was missing one *row*: the
left edge had a two-pitch gap where a name should be, and the name in it was
`PC2_C/SPI2_MISO`.

`PC2_C` is ST's own spelling. On the H7 parts that carry an analog switch,
`PA0`/`PA1`/`PC2`/`PC3` each have a second pad, named with an underscore and a
`C`. `PIN_RE` allowed ST's *dash* qualifiers (`PC13-TAMPER-RTC`, §1.11) but not
this one, so the row was not a row, the label bound to nothing, and SPI2 came
out with SCK and SDO and no SDI — which is refused, because a partial bus does
not compile. One character of spelling cost the bus, the OSD on it, and the ADC
instance, since those same pads are ADC3's inputs.

Written out as `(?:_C)?` rather than allowing any underscore tail, because
`PIN_RE` also decides what is *not* a net label and a net called `PC13_LED`
must stay one.

Corpus: 21 boards, all H7 as expected. Unbound labels **179 → 140**, nets
checked 1920 → 1950 with agreement holding, defines 5109 → 5158. Ten boards
gained a complete SPI2; four more gained `ADC_INSTANCE`/`ADC3_DMA_OPT`, because
VBAT and current sit on those pads and ADC3 is the only ADC that reaches them.
Verified against a hand-written config for one of them: `SPI2` `PB13`/`PC2`/`PC3`
and the ADC pins match exactly.

### 1.13 A name can sit just outside its own column — FIXED

Same board class, different cause, found by asking why §1.12's sibling boards
still refused a bus. On one F4 the name `PA6/SPI1_MISO` is reported by
`pdftotext` at x0 169.1 while the rest of its column is at 167.6. The edge
cluster is 1pt wide, so it was not on the edge, and its pin did not exist:
SPI1 lost its SDI, the gyro lost its bus, and `GYRO_1_SPI_INSTANCE` went with
it. The only symptom was one net label that matched no row.

Widening the tolerance is not available — this corpus draws real, distinct
columns 3.5pt apart, and merging them would be far worse than the bug. What
separates the two cases is the *run*: an edge is names at a regular pitch, so a
gap of two pitches is a row that must exist and does not. Only such a gap is
filled, and only from a name less than one character's width off the edge, with
the character width taken from the names themselves rather than a constant.
A genuine neighbouring column has no hole to fall into.

Two boards, three pins, and with them two complete SPI1 buses, a restored
`GYRO_1_SPI_INSTANCE` and a UART pin; nothing else in the corpus moved.

Both are now golden fixtures — `h7-dual-pad` and `f4-column-drift` — because
the check that should have caught the second one already existed
(`instances_without_pins`) and passed on all five boards under test while six
instances dangled in the corpus. See §3.8.

### 1.14 A net label need not be in a column — FIXED

The first board to arrive with a **vendor-written config beside it**, which is
the only ground truth this project has had. 81 defines to compare against.

The generator agreed on 54 and missed 25. Almost all of the 25 came from one
cause: this sheet draws each net name against its own wire, so how far it sits
from the symbol is set by whatever components share that row. A row reads

```
3V3_MCU   R36   10k   GYRO_1_EXTI   D8   PD0
```

— a 10k pull-up, with the real net between the resistor and the pin. The next
row has a series resistor instead and its net sits where `3V3_MCU` is here. No
three labels line up, so the `len(col) >= 3` column test discarded every one of
them, and the *supply* feeding the pull-up — which is in a column, because every
pull-up on the sheet shares it — bound to the pin instead. `GYRO_1_EXTI` and
`MAX7456_SPI_CS` became `3V3_MCU`; `LED0`, `ADC_VBAT` and `MOTOR6` vanished into
"unconnected pins", which reads like a board that does not use those pins.

Two changes, both bounded:

- **Labels outside any column are collected**, but only between the symbol and
  the strongest column — which is what establishes where this sheet's labels
  live — and only if they match `NET_VOCAB`. That filter is what keeps out ball
  coordinates (`D8`, `H8`, `F3`), designators (`R36`, `C55`) and values (`10k`,
  `100R`, `1%`); it is the same one the neighbouring-column pass already trusts.
- **One row carries one net.** Where two labels are level with a pin, the nearer
  wins, because the other is on the far side of a component. Distance is rounded
  to the point and alignment breaks the tie: below a point this is extraction
  noise, and comparing raw floats let a stray `N` take a row from the `I2C1_SCL`
  beside it by one thousandth of a point.

Corpus: **+57 defines**, 63 gained against 6 lost, warnings down 6. Four boards
gained a complete SPI bus, four the ADC3 current sense. Four of the six losses
are corrections — before this, three boards emitted a pin under two different
roles (`FLASH_CS_PIN` *and* `SPI3_SDO_PIN` on one pin; `GYRO_1_CS_PIN` *and*
`ADC_VBAT_PIN` on another), which cannot be built. That count is now zero.

On the board itself, exact agreement with the vendor went **54 → 66 of 81**.

**The case that was still wrong - now FIXED, and not by ranking.** One sheet
prints a pin's own ADC channel (`ADC1_8`) between the symbol and the wire,
nearer than the net; `RSSI` sat behind it and lost the row. Two orderings were
measured and both were wrong: alignment-first fixes that board and costs three
F7 boards a whole SPI3 bus, 16 defines against 6.

The discriminator is not distance at all. `ADC1_8` says "ADC1, channel 8", and
the firmware tables give `PC5` as `devices: '12', channel: '8'` - the label only
restates what the pin already is, so it is an annotation and cannot be the net.
`_restates_the_pin` checks exactly that against the capability map and demotes
such a label below any real net, whatever the distances are. Checked against the
map rather than by shape, because a board is free to name a net `ADC1_8` and
wire it somewhere else entirely.

`ADC_RSSI_PIN` and `ADC1_DMA_OPT` return on that board - it had no ADC defines
at all without this - and **0 of 5217 defines move** across the other 104.

That fix came out of §3.7, which set out to settle this with the drawn wires and
could not: see there for why, and for the vendor typo the attempt uncovered.

### 1.15 Two more spellings, from the same board

`SDCARD_SPI_CS` and `CLKIN`, both read correctly and both classified as nothing.
The first wanted the `_SPI` infix that the `FLASH`, `OSD` and `BARO` patterns
already allow; the second is a bare `CLKIN` where the pattern demanded a
`GYRO`/`IMU` prefix. The MCU's own clock input is not a rival for that spelling —
it arrives on `OSC_IN`, which the symbol names as a system pin and which never
reaches classification.

§1.8–§1.11 again, and worth noting how it was found: not by reading code, but by
having a config to compare against.

#### What the vendor config settled, and what it did not

Two differences turned out to be neither side's defect, and both took tracing to
firmware rather than reasoning about it:

- **`ADC_INSTANCE ADC3`**, which the vendor omits. Firmware's `adcTagMap` has
  `PC2`/`PC3` as `ADC_DEVICES_3` on H743, and `ADC_INSTANCE` defaults to `ADC1`,
  so the vendor's config looks broken. It is not: `adc_stm32h7xx.c` falls back to
  any *activated* device that can reach the pin, and their `ADC3_DMA_OPT` is what
  activates it. Both work; ours reaches ADC3 directly instead of via the fallback.
- **`ADC3_DMA_OPT` 9 against their 10.** On a DMAMUX part the option is an index
  into one shared channel table, so any free channel is as good as another.

Still genuinely missing, and derivable in principle: `DEFAULT_VOLTAGE_METER_SCALE`.
The divider *is* on the sheet — `R33 150k` to `BAT`, `R34 10k` to `GND`, giving
exactly the vendor's 160 — but it is drawn with the top resistor horizontal and
the bottom one vertical, where §3.2's reader looks for two resistors on one
vertical leg either side of the node. A layout it does not know, not a bug.

### 1.16 MOTOR_1 is not MOTOR1 — FIXED

Found while building §4.7's manual placement, on a board that seemed to need it:
`MOTOR_1(PC9)`, `MOTOR_2(PC8)` … `MOTOR_8(PB9)`, `SERVO_1(PA9)`, `SERVO_2(PA10)`,
`SERVO_3(PA8)` all came out as "nets with no config.h role". Eleven pins, on a
board reporting 100% agreement on what was left.

The motor and servo patterns did not allow a separator before the index, which
every other rule has allowed since §1.11 put it into `GYRO_1_CS`. Corpus: **+40
defines on 7 boards, none lost** — four servos on three H7 boards, four more
motors on two others, eight motors and three servos and a whole timer map on the
F4.

It is the fifth time this exact defect has appeared, so it is worth stating as a
rule rather than a story: **when a pattern matches an indexed name, the index may
be preceded by a separator.** That is now true of every rule in the table.

### 1.17 A symbol split into columns side by side — FIXED

An H743 read 64 of its 98 pins and reported nothing missing beyond a vague note
about a second symbol. The MCU is drawn as two boxes on one sheet — ports A–D in
one, port E in the other, 45pt apart, spanning the same rows. `_is_split_half`
admits a second box on size (a third of the first, and this one is 18 pins
against 64, just under) or on adjacency (`pitch * 2`, and 45pt is far outside
it). Neither route let it in, so half an LQFP100 was dropped: 39 pins reported
as unconnected on a board that has them.

A third route: **drawn beside it, spanning the same extent.** Safe because of
what the function already requires — the two share no pin at all, and any two
symbols of the same MCU both carry PA0, so a disjoint pin set on the same page,
on the same grid, over the same rows is a port group and not a second part.

The board went 64 → 98 pins; `g4-multipage` went 32 → 48 rows and 18/18 → 21/21.

### 1.18 The vocabulary that collects and the one that classifies had drifted

The largest single defect of the session, and structural rather than a spelling.

`NET_VOCAB` decides what is *collected* as a net label; `classify` decides what
a collected label *means*. They are two gates in series, and only the second was
being maintained. Teaching `classify` about `PWMn` and `ESCn_SIGNAL` changed
nothing at all on the boards that use them — the labels were dropped one step
earlier, by a vocabulary that had never heard of either, and reported as
nothing.

Auditing the two against each other found five more that had drifted apart long
ago: `UART3_TX`, `USART6_RX`, `SPI_SDO2`, `SDIO_CMD`, and a bare `Mn`.

There is now a test that asserts every spelling `classify` knows also passes
`NET_VOCAB`, with the list of spellings written out. It has one deliberate
exception, and the exception is the interesting part: a bare **`Mn` is
excluded**. `classify` reads `M1` as a motor and sheets do label them that way,
but a BGA plot's ball coordinates run `A1..M12`, and admitting `Mn` dropped a
whole row of them into one board's gutter, where they took the rows its real net
labels wanted and cost it SPI1 and SPI4 entirely. Ball coordinates sitting
exactly where a net label would is the thing this vocabulary exists to exclude,
so the collision is decided in favour of the many boards over the one.

The same audit produced a ranking worth keeping. Counting unclassified nets over
the whole corpus, by how many boards use each spelling:

| boards | nets | spelling | verdict |
|---|---|---|---|
| 13 | 13 | `OSC_IN`/`OSC_OUT`, `USB_DM`/`DP` | correctly unclassified — no config.h role |
| 9 | 25 | `SPIn_NSS` | a chip select needing device attribution — §3.3, not spelling |
| 5 | 26 | `ESCn_SIGNAL` | **fixed** → motor |
| 5 | 18 | `PWMn` | **fixed** → motor |
| 5 | 10 | `GYRO-EXTIn` | **fixed** — index on either side, as the CS rule already allowed |
| 4 | 19 | `TXDn`/`RXDn` | **fixed** → uart |
| 5 | 7 | `PIOn` | **fixed** → pinio |
| 8 | 8 | `BUZZ`, `BEEP` | **fixed** → beeper |
| 6 | 6 | `CAM` | left alone — could be the control line or the video signal |

Corpus over the whole group: **+285 defines on 30 boards**, and a vocabulary
tie-break in the bargain — a label the vocabulary recognises now beats one it
does not before distance is consulted, because the trusted column can carry a
fragment the extractor made (`2C4` out of `I2C4`) that happens to sit nearer the
pin than the `SCL` beside it.

Run the ranking whenever a submission arrives. It is five minutes and it is the
only thing that finds this class.

### 1.19 The rule stated in §1.16 was not applied to trailing indices

§1.16 ended by stating the pattern as a rule: *when a pattern matches an indexed
name, the index may be preceded by a separator.* Checking the rules against it
found two that only honour it in the leading position.

`GYRO_CS2` classified and `GYRO_CS_1` did not - the trailing `(\d)?` allowed no
separator before it. Same in the gyro SPI rule, so `GYRO_MISO_2` was nothing
while `GYRO_MISO2` was fine. And the separator form of the SPI bus rule accepted
`SCK`, `SCLK`, `MISO`, `MOSI`, `SDI`, `SDO` but not `CLK`, which the *no*-separator
form beside it has always accepted - so `SPI4_CLK_PIN` was unreadable and cost
two boards their whole SPI4.

Corpus: **+16 defines**, incomplete buses 19 → 17, and one correction worth
noting - a board that had `USE_ACC_MPU6000` now has `USE_ACC_SPI_MPU6000`,
because recognising the chip select is what says the part is on SPI.

Small, but the way it was found is the point: not from a board, from re-reading
a rule this file already contained and checking the code against it. Worth doing
whenever a rule gets written down here.

### 1.20 An annotation fused onto a pin name splits the symbol's edge — FIXED

Chasing the 396 unbound net labels. Ranking boards by raw orphan count is the
wrong question - what matters is orphans that *classify to a real role*, since
those are nets that were understood and then lost. That is 164 over 31 boards,
and the top two were one sheet at **100% agreement while 19 real nets bound to
nothing**.

The cause is one word shape. Altium writes a hidden designator-and-ball token
beside every pin, and poppler returns it *inside* the word when the two are
drawn without a gap:

```
PH1-OSC_OUTPIU10D1     PC15-OSC32_OUTPIU10B1     PH0-OSC_INPIU10C1
```

`drop_annotations()` removes these where they stand alone, and the name still
parses when fused - so nothing looked wrong. But an edge is found by clustering
on a shared coordinate, and on a right-hand column the shared coordinate is x1.
The fused names end 10pt further right than the clean ones, so they form a rival
cluster and the edge is read as two. That board kept 16 of its right column's
rows, and the 19 labels wanting the missing ones had nowhere to bind.

`_unfuse_annotation()` trims the token and narrows the box in proportion to how
much of the text the name is. Proportional is an approximation on a
proportional font, but the error is a fraction of a character against a 10pt
displacement.

That board: **66 rows → 100, 20 nets checked → 34, all agreeing, no orphans.**
Corpus **+87 defines on 5 boards**, orphaned labels 54 → 51.

One board is smaller afterwards and it is worth saying why. It went 63 rows →
100 and 19 nets checked → 39, so its old 100% was agreement on half of what it
had; six of the newly-visible nets put SPI1 and SPI3 on pins the firmware tables
do not support, and those are refused. It emits less and asserts nothing false,
which is the trade this project keeps choosing.

#### The pattern behind §1.8 through §1.11

Four entries now, and they are all the same defect: **a rule that describes one
spelling of a thing, written from the boards that happened to be on hand.**
`TX4` but not `UART4_TX`. `SPI3_SCK` but not `SCK3` or `SPI_SCK2` or `SPI1CLK`.
`GYRO1-CS` but not `GYRO_1_CS`. Pin names at the head but not the tail. `I2C`
but not `IIC`. A separator that is a dash or an underscore but never a dot.

None of it was findable by reading the code, and each one was silent: the nets
went out as "no config.h role" while every diagnostic the tool prints stayed
clean. What finds them is counting what came out the far end unclassified,
across every board available — which is a five-minute check and should be run
whenever a new submission arrives, before anything else is investigated.

---

## 2. Coverage — what the sheets carry and the tool did not emit

### 2.2 Dual-gyro boards — DONE, and now proven on hardware sheets

Three corpus boards emit a complete `GYRO_2_*` set (`SPI_INSTANCE`, `CS_PIN`,
`EXTI_PIN`, `ALIGN`, and `CLKIN_PIN` on two of them). This was the roadmap's
single highest-value unknown and it is now closed.

One of the three is a **shipping board whose hand-written config is in the
config repo**, which makes it the strongest verification available anywhere in
this project — the same board, converted independently by a human. Of the
defines both files carry, **52 agree exactly and 3 differ**:

| | hand-written | generated | verdict |
|---|---|---|---|
| `ADC1_DMA_OPT` | 8 | 10 | both free channels; benign |
| `BARO_I2C_INSTANCE` | `I2CDEV_2` | `I2CDEV_1` | generator emits only one I2C bus — see §2.4 |
| `PINIO1_PIN` | *BEC switch* | *camera switch* | generator emits only one PINIO — see §2.5 |

`GYRO_1_*` matches exactly. `GYRO_2_*` is emitted by the generator and **absent
from the hand-written config**, though the sheet carries a full second bus,
chip-select and interrupt for it. Either the shipped config is missing a gyro
the board has, or this sheet is a later revision than the config was written
from. Worth resolving with the vendor; it is not a generator defect.

The refusal path is exercised too. A different dual-gyro board gives its second
IMU only a chip-select, and the tool declines to emit `GYRO_2_*` at all —
correctly, since `GYRO_2_CS_PIN` alone would raise `GYRO_COUNT` to 2 with no bus
behind it — and says exactly which three defines a human must add.

### 2.3 Still unproven after 168 schematics — and SDIO is now DONE

`RX_PPM_PIN`, `ESCSERIAL_PIN` and burst DShot are implemented and unit-tested,
and **not one board in the corpus emits any of them**. That is a stronger
statement than "no board exercises them" and it retires them as priorities:
both are legacy, and the corpus says they are gone.

**`USE_SDCARD` was the wrong shape, and is now fixed.** 22 boards mention a
card and the ones that wire it use the MCU's **SDMMC controller**, not a SPI
bus — so the SPI-only implementation gave none of them a card and the nets came
out as "no config.h role". Six spellings occur and none was matched:
`SDMMC1-CK`, `SDMMC2_D3`, `SDIO_CMD`, `SDIO-D2`, `SD_SDIO_CK`, `SD_CLK`. The
digit is the *controller* (`SDMMC2` → `SDIO_DEVICE SDIODEV_2`), not a line
number.

The interesting part was deciding where it may be emitted at all. `pg/sdio.c`
registers the pin config behind `#if ENABLE_SDIO_PIN_CONFIG`, which
`common_post.h` defaults to **0** — and only a target's own `target.h` turns it
on, so it is per *target*, not per family: H743 sets it, H750 does not, and both
are STM32H7. On a target that leaves it off, `SDIO_CK_PIN` and the rest compile
fine and are never read. That is the §1 case exactly, so the seeder now records
`pin_config` and whether the family has an sdio driver, and the generator
declines with the reason instead of emitting inert defines.

`USE_SDCARD_SDIO` is deliberately *not* emitted — every `target.h` with an SDMMC
controller defines it inside `#ifdef USE_SDCARD`, and the hand-written SDIO
configs set neither. `SDIO_DEVICE` and `SDIO_USE_4BIT` are, because `pg/sdio.c`
defaults them to `SDIOINVALID` and `false`: omitting them selects no controller
and runs the card one-bit.

Verified against a second board with a hand-written config, and every SDIO
define matches it exactly — CK, CMD, D0–D3, DEVICE, 4BIT, the detect pin and its
inversion. That board now agrees on **73 of 93** defines overall.

Seven boards emit a complete SDIO block where none did; three more are refused
because their CMD or D0 net is not drawn, and say so.

### 2.4 Second I2C bus — DONE

`genconfig` kept a single `i2c` dict of `{scl, sda}`, so a board with I2C1 *and*
I2C2 lost whichever was read first and the survivor wore the other's name. It
was not rare, it was structurally impossible: **no board in the corpus emitted
more than one I2C bus.** The visible symptom was the note *"net names say I2C2
but the pins are I2C1"*, on 29 boards.

Fixed by keying the collected nets on the index in their name, so each bus is
its own group — the same shape as `spi_named`. The *instance* still comes from
the pins through the firmware map; the index only says which nets belong
together, and `infer_i2c_bus` still reports it when the two disagree.

That left the second half: **which bus each device is on**. An I2C part has no
chip select, so the trick that settles SPI (§3.3) does not apply — there is no
per-device net to follow. The part itself is the anchor instead: a baro marked
`DPS310` is drawn with its SCL and SDA beside it. `i2c_bus_for()` reads those,
with the same discipline as the SPI tracer — nearest bus must be 3× nearer than
the next, MCU-side labels excluded, and it declines rather than guesses.

| | before | after |
|---|---|---|
| I2C SCL/SDA pins emitted | 138 | **202** |
| boards emitting two I2C buses | 0 | **27** |
| boards showing the name/pin collision | 29 | **0** |

17 baro/mag parts are traced to a bus by their own nets and 3 refused as
ambiguous. 27 boards gain defines, none lose any, and all four golden fixtures
are byte-identical.

On the one board with a hand-written config to check against, the whole I2C
block now matches it exactly — including `BARO_I2C_INSTANCE I2CDEV_2`, which
was `_1` before, off a `DPS310` traced 23pt from I2C2's nets against 423pt to
the next. That board is now down to **a single difference** across 62 agreeing
defines: `ADC1_DMA_OPT` 8 against 10, two equally free DMAMUX channels.

### 2.5 Two PINIOs — DONE

Boards routinely have two switched rails (a BEC/VTX switch and a camera
switch). Both are emitted now; 19 corpus boards carry a `PINIO2_PIN`, and on
the one board with a hand-written config to compare, both pins match it.

Two things had to change. A board writing `BEC-SWITCH` had that net dropped
entirely — the rule matched a trailing `_SW`/`_EN` but not the word spelled out
— so only its *camera* PINIO was emitted. And the polarity is not derivable at
all; see §3.2, which records why 129 is emitted as a *default* rather than read
from the sheet, and why `drivers/pinio.h`'s cap of 4 is now enforced.

---

---

## 3. Capability — what needed analysis the tool could not yet do

### 3.2 Circuit-level analysis — the VBAT divider is DONE

Everything used to be net-label and component matching. The highest-value piece
of circuit reading is now implemented, and it did not need a general netlist
either — the same "find the net away from the MCU and look at what is drawn
around it" that solved §3.3.

**`DEFAULT_VOLTAGE_METER_SCALE` — done.** The scale is not a free parameter.
`voltage.c` computes

```
volts = adc * vbatscale * Vref / 10 / (0xFFF * vbatresdivval)
```

which with the shipped `vbatresdivval` of 10 puts full scale at
`0.33 * vbatscale` volts, so for a divider of ratio *r*

```
vbatscale = 10 * (Rtop + Rbottom) / Rbottom
```

exactly. `100K/10K` gives 110 — the firmware default, which is why so many
boards need no define and why any other divider silently misreports the battery.
Read from the code, not from the comment beside it, which describes only the
10k:1k case.

Read **structurally**, not by taking the two nearest values: the ADC net sits at
the midpoint of one vertical leg with a resistor either side. Requiring that
shared leg is what separates the divider from everything else nearby — on two
boards a third resistor from a neighbouring circuit sat 34pt the other side of
the node, and on one the two nearest values would have given 223 instead of 110.
Orientation is anchored by a supply above or ground below (either alone: one
board draws ground as a bare symbol with no text), and `vbatscale` is a
`uint8_t`, so anything over 255 is refused.

Verified against hand-written configs, and the ones that matter are the
non-default boards the roadmap insisted on: `20K/1K` → **210** against a config
that says 210, and `150K/10K` → **160** against a config that says 160. A third
computes 110 from `100K/10K` against a config that leaves it unset. No board
gets a scale that disagrees with its config.

11 boards now carry a computed scale — 8 at 110, 2 at 210, 1 at 160. Widening
the sense-net vocabulary at the same time (`VBAT-ADC1`, `VBATT-ADC`,
`BATT_VOLTAGE`, `BAT1_V`) took `ADC_VBAT_PIN` from 48 boards to 63.

**The limit is the sheets, not the method.** Of the boards with a VBAT node, 23
give the resistor *designators* with no values anywhere — one names the divider
only as an annotated "20：1" — so there is nothing to read. That is worth
stating plainly: this will never reach every board, and where the values are
absent it says so rather than falling back on 110.

- **`BEEPER_INVERTED`** — corroborated, not decided. 554 of the 582 corpus
  configs that drive a beeper set it, so it stays the default; on 10 boards the
  sheet shows the transistor and the note now names it. Its absence is
  deliberately not treated as evidence the other way — a sheet that does not
  draw a transistor has not shown there is none, and a wrong flip is a beeper
  that never sounds.
- **PINIO polarity — not derivable, and the old rule was wrong.** This entry
  used to say the real answer is whether the regulator enable is held up by its
  own divider. That understates it: a PINIO is just a pin the user can toggle,
  vendors assign the indices however they like, shipped configs use both 129 and
  1 across every category of rail, and at least one board inverts in *hardware*
  as well. The net name carries no information about any of it.

  The generator used to infer 129 from the name mentioning VTX and 1 otherwise,
  which was wrong in the common case — of the corpus configs whose PINIO is a
  BEC or power rail, 93% use 129 and that rule gave them 1.

  A value must still be emitted: `drivers/pinio.c` switches only on the mode
  bit, so omitting `PINIOn_CONFIG` never configures the pin as an output and the
  box does nothing. So 129 is emitted on the basis of *how the two fail*, not of
  what the sheet says — `pinioInit` drives an inverted pin high at boot and a
  plain one low, so a switched rail comes up powered with 129 and dead with 1,
  and dead reads as broken hardware rather than an inverted switch. It is
  flagged as a default rather than a reading, on every PINIO.

  Separately, a board writing `BEC-SWITCH` had that net dropped entirely, so
  only one of its two PINIOs was emitted — the camera one. Both are emitted now
  and both match the hand-written config.
- **`DEFAULT_CURRENT_METER_SCALE`** — genuinely ESC-dependent when the FC just
  filters a sense line, but a board with an on-board shunt could be computed.

### 3.3b Chip selects named after the bus, not the device — FIXED

The largest single block left in §3.3, and the measurements are worth keeping
because the obvious implementation does not survive them.

Fifteen boards name every chip select after its **bus**: `SPI1_NSS`, `SPI2_CS`,
`SPI4_SS`. 41 such nets. The bus is stated outright; what is missing is *which
device* the select belongs to. This is the exact inverse of the CS-only case
above, where the device is known and the bus has to be traced - and the two are
complementary, so the same boards often have both.

The cost is high and concentrated. The clearest example has four SPI
buses fully emitted, four devices detected (ICM42688P, DPS310, W25N01G,
MAX7456), and **not one `_CS_PIN` or `_SPI_INSTANCE`** - the config declares
drivers it has no way to reach. Roughly eight defines per board.

**The evidence is there.** Each of these nets appears twice, once at the MCU and
once at the peripheral, and the far end is unmistakable to a reader:

| net | what is drawn beside its far end |
|---|---|
| `SPI1_NSS` | `IMU1_INT` 14pt, `IMU1` 32pt |
| `SPI2_NSS` | `IMU2_INT` 14pt, `IMU2` 32pt |
| `SPI3_NSS` | `SDIN`, `SCLK`, `CLKOUT`, `DGND` - a MAX7456's pin names |
| `SPI4_NSS` | `W25N01GVZEIG` 56pt, `DO(DQ1)`, `WP#(DQ2)`, `HOLD#(DQ3)` |

**What was tried, and what it scored.** Nearest *part marking* to the far end,
using the `PartHit` positions `i2c_bus_for()` already records:

| approach | decisive |
|---|---|
| nearest part, `runner < near * 3` as elsewhere | 13 of 41 |
| ...with acc/gyro collapsed and I2C-only parts excluded | 14 of 41 |
| global assignment - one chip has one select - within 300pt | **30 of 41** |

The global framing is clearly right: these are not 41 independent
nearest-neighbour queries, they are an assignment between the selects and the
chips, which is what stops two parts 20pt apart from reading as a tie.

**It is still not shippable, and that is the point.** "Within 300pt" is not
"correct", and there is no ground truth for these boards to check attribution
against. The prototype already misfires visibly - on one board it assigned a
select to a baro 350pt away while a gyro sat at 328pt, because the candidate
list keeps the first hit per category rather than the nearest. A wrong
attribution here is worse than none: it emits `GYRO_1_SPI_INSTANCE SPI3` with
full confidence when SPI3 is the flash, and nothing downstream can tell.

**What it actually needs** is the signal the table above shows and the prototype
ignored: the *peripheral's own pin names*. `SDIN`/`SCLK`/`CLKOUT`/`VSYNC`/`LOS`
is a MAX7456 and nothing else; `DI(DQ0)`/`DO(DQ1)`/`WP#`/`HOLD#` is a SPI flash;
`INT1`/`INT2`/`FSYNC` is an IMU. That is a real, small, learnable vocabulary -
the same shape as `NET_VOCAB` - and it identifies the chip directly instead of
inferring it from how close a marking happens to be drawn. It also degrades
honestly: a far end with none of those names beside it is simply undecided.

#### Done, and the vocabulary was the whole difference

`identify_bus_cs()` reads the far end for the chip's own pin names. Two
independent tokens are required and a tie decides nothing, so a net with nothing
distinctive beside it comes back **undecided** and says so, naming what it did
find. The part marking still counts, but as one token among several rather than
as the case.

The reach is 80pt - the size of a symbol, not of the sheet - which is what the
proximity attempt lacked and why it reached a chip 1717pt away.

| | |
|---|---|
| identified | 24 of 41 |
| undecided, and honest about it | 17 |
| misattributed | 0 |
| corpus | **+59 defines on 12 boards, none lost**; one more board to 100% |

The validation board comes out 4/4 with the gyro indices right -
`SPI1`→gyro 1, `SPI2`→gyro 2, `SPI3`→OSD, `SPI4`→flash - and now emits the four
`_CS_PIN`/`_SPI_INSTANCE` pairs it had none of, plus `GYRO_2_EXTI_PIN` once the
second IMU exists. A second sheet from the same vendor, with a different
arrangement, resolves differently and correctly - which is the check that it is
reading the sheet and not the family.

The gyro index came free: a board that labels `IMU1_INT` and `IMU2_INT` beside
two selects has said which is which.

Still open here: 17 nets whose far end carries nothing identifying, and the
`SPIn_CS` boards whose select appears only once on the sheet - there is no far
end to read. Those now name the pin and suggest `--set`, which is §4.7's job.

### 3.6 Unreadable documents are now named, and one class was invisible — DONE

A submission arrived that cannot be converted at all, and the tool blamed the
geometry: first "could not detect FC_TARGET_MCU", then, once told the target,
"no aligned pin-name column". Neither is the problem.

Its fonts are **Type 3 with a custom encoding, no embedded font file and no
ToUnicode map**, and the `/Differences` array names the glyphs `/0 /1 /2 …
/141` — sequential numbers carrying no character information at all, with the
shapes drawn by `CharProcs`. Every glyph is a drawing procedure. `pdftotext`
recovers the internal byte codes instead of the letters, so *Interface Type*
comes back as `RNLC@S?ICÿTFCA`. 1904 words over seven pages, not one of them a
pin name. Only OCR could read it, and that is a dependency this repo will not
take.

The lesson is the reclassification. Naming the two non-parse failures — no text
layer at all, and a text layer that carries no characters — split the 64
schematics that yield nothing into:

| | |
|---|---|
| never name their MCU (read fine with `--target`, §3.5) | 32 |
| scans, no text layer | 16 |
| **text present, fonts unmappable** | **12** |
| text fine, symbol genuinely not found | 3 |
| `pdftotext` crash | 1 |

**Those 12 were already in the corpus and had been counted as geometry
failures.** A whole class of defect was invisible because the diagnostic
described the last thing that failed rather than the first. That is the same
shape as §1.5 and §1.8 — the tool knew, and did not say.

A handful of corpus files are also not schematics at all — wiring diagrams, a
datasheet, calibration notes, one PID paper. They fall into the "never names
its MCU" bucket, which is harmless but not precise.

---

## 4. Engineering

### 4.1 No test suite — DONE

Every change was once verified by regenerating boards and eyeballing diffs. That
caught two real regressions, and it did not scale.

**Fix:** golden-file tests — commit the expected `config.h` for a few boards
(they are gitignored here, so a fixture directory outside the repo or sanitised
fixtures) plus the netmap JSON, and assert byte-equality. The pipeline is
deterministic, which makes this straightforward. Also worth asserting the
invariants that already exist informally: agreement is 100%, no net is dropped
silently, every emitted pin is firmware-validated.

### 4.2 `MANUFACTURER_ID` is not validated — FIXED

The ID must appear in the config repo's `Manufacturers.md`. This was checked by
hand for both boards converted so far. The file is a parseable markdown table —
validate against it and fail loudly on an unregistered ID.

### 4.3 Firmware array limits are invisible — FIXED

`UARTHARDWARE_MAX_PINS` is 5 on H5 and 4 on F4/F7. A pin sweep hit that ceiling
and the generator has no idea the limit exists — it would happily reference a
pin the firmware table has no room for. Seed the limits and check against them.

### 4.4 `genconfig` does not surface which page it read — FIXED

`netmap` now reports `31 pins on page 4 of 5` and exposes `sym.page`,
`sym.pages`, `sym.split`, `sym.ignored_pages` and `describe_pages()`, but
`genconfig` calls `find_symbol()` itself and prints its own header. On a
multi-page submission the generated config gives no indication which sheet was
read, and a rival symbol on another page is never mentioned. Small wiring job.

### 4.6 Provenance is weak — FIXED

The schematic sha256 is recorded, but nothing ties a generated config to the
firmware revision it was validated against. A config generated against a patched
tree looks identical to one generated against release firmware — which already
mattered once, when a board depended on an unmerged pin-table fix. Record the
seeder's firmware rev in the generated header.

---

### 4.7 Nothing could be placed by hand — FIXED

Some nets cannot be followed however good the reader gets: the page holding them
was not supplied, or they are drawn in a way nothing here reads. Until now the
only options were to edit the generated file, which loses everything derived from
that pin — the timer map, the DMA allocation, the bitbang decision — or to wait
for the reader to improve.

`--set NAME=PIN` places one, and the report names what is absent so there is
something to answer:

```
WARN: not produced from this sheet: MOTOR1_PIN, ..., ADC_VBAT_PIN.
      If the board has them, supply each with --set NAME=PIN
```

Three decisions worth recording:

- **It goes through `classify`, not a table of its own.** A define name is the
  net name the reader already knows with `_PIN` on the end, so `--set` supports
  every spelling the sheet does and cannot drift away from it.
- **It is checked against the firmware map exactly as a read net is,** and
  refused if the pin cannot do the job. Being told a pin by hand is not a reason
  to emit one the build will not honour — and since every STM32 family the
  seeder harvests has been audited against its ST datasheet, the tables are not
  the doubtful party: a refusal means the pin is wrong, and it says so plainly
  rather than hedging about firmware PRs.
- **It is a placement, not a text edit.** The value enters as though the sheet
  had carried it, so everything downstream follows: supplying four motors on a
  board with none produced the timer mapping, resolved a DMA collision between
  two of them and explained it, and decided `DEFAULT_DSHOT_BITBANG`.

Both the hand-placed value and anything it displaced are reported. A config that
mixes what was read with what was asserted, and does not say which is which, is
the "looks finished" failure the README warns about.

The desktop app offers the same thing as a box per function, driven by
`meta.absent` and `meta.placed` rather than by scraping the warning text — the
list shrinks as functions are filled in, a refused pin is shown against the box
it came from, and anything not on the curated list can be added by name.

