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
The invariants, derived from a converted board.

Each function returns *what is wrong*, as a list - empty means the invariant
holds. Nothing here raises; the assertions live in the test files and
`update_golden.py` records the same values, so a fixture can never disagree with
what the tests check.

Everything returned is generic vocabulary - define names, roles, bus names.
None of it identifies a board.
"""

import re
from typing import Dict, List

import support
from support import BoardRun, PIN_VALUE_RE, defines, timer_rows

import genconfig

ADC_PIN_DEFINES = ("ADC_VBAT_PIN", "ADC_CURR_PIN", "ADC_RSSI_PIN")


# --------------------------------------------------------------------------- #
# 1. Nothing is dropped in silence
# --------------------------------------------------------------------------- #

def label_conservation(run: BoardRun) -> List[str]:
    """
    Every net label the geometry found is either mapped to a row or orphaned.

    Except a further copy of a name that *is* mapped. Some sheets draw the label
    and its net annotation a point apart, so one row attracts two labels reading
    the same thing; only one can own the row and the other is not a net that
    went missing. Reporting it as an orphan would put a warning about a lost net
    on every board drawn that way.

    Counted per name, not in total: two copies of one name that land on two
    different rows are both real links, and a blanket allowance for "it appears
    twice" would excuse a genuine loss elsewhere. A copy of a name that is
    mapped nowhere is still a loss and still fails.
    """
    def text_of(w) -> str:
        # A real run holds Words; the analysis fixtures stand labels in as
        # Links, or as bare strings.
        return getattr(w, "text", None) or getattr(w, "net", None) or w

    have: Dict[str, int] = {}
    for w in run.labels:
        key = text_of(w)
        have[key] = have.get(key, 0) + 1
    for name in [l.net for l in run.result.links] + list(run.result.orphans):
        key = text_of(name)
        have[key] = have.get(key, 0) - 1

    mapped = {l.net for l in run.result.links}
    lost = sorted(k for k, n in have.items() if n > 0 and k not in mapped)
    if lost:
        return [f"{len(run.labels)} labels but {len(run.result.links)} links + "
                f"{len(run.result.orphans)} orphans; unaccounted: "
                + ", ".join(lost)]
    return []


def unaccounted_roles(run: BoardRun) -> List[str]:
    """
    Roles of nets that reached neither the config.h nor a diagnostic.

    A net is accounted for when its pin is emitted as some define's value, or
    when the net is named in a warning or note explaining why it was left out.
    `ignore` roles (SWD, USB D+/D-, BOOT) are silent by design.
    """
    emitted = set(defines(run.text).values())
    said = run.diagnostics
    out: List[str] = []
    for link in run.result.links:
        role = genconfig.classify(link.net)[0]
        if role == "ignore":
            continue
        if link.pin in emitted or link.net in said:
            continue
        out.append(role or "unclassified")
    return sorted(out)


# --------------------------------------------------------------------------- #
# 2. The emitted file is internally consistent
# --------------------------------------------------------------------------- #

def dangling_timer_labels(run: BoardRun) -> List[str]:
    """TIMER_PIN_MAPPING entries naming a define the file never makes."""
    known = defines(run.text)
    return sorted({label for _, label, _, _ in timer_rows(run.text)
                   if label not in known})


def malformed_pin_defines(run: BoardRun) -> List[str]:
    return sorted(name for name, value in defines(run.text).items()
                  if name.endswith("_PIN") and not PIN_VALUE_RE.match(value))


def duplicate_pin_defines(run: BoardRun) -> List[str]:
    """Two defines claiming the same MCU pin."""
    seen: Dict[str, List[str]] = {}
    for name, value in defines(run.text).items():
        if name.endswith("_PIN") and PIN_VALUE_RE.match(value):
            seen.setdefault(value, []).append(name)
    return sorted("+".join(sorted(names)) for names in seen.values() if len(names) > 1)


def incomplete_spi_buses(run: BoardRun) -> List[str]:
    """A bus with pins emitted but not all three of SCK/SDI/SDO."""
    d = defines(run.text)
    buses: Dict[str, set] = {}
    for name in d:
        m = re.fullmatch(r"SPI(\d)_(SCK|SDI|SDO)_PIN", name)
        if m:
            buses.setdefault(f"SPI{m.group(1)}", set()).add(m.group(2))
    return sorted(bus for bus, roles in buses.items()
                  if roles != {"SCK", "SDI", "SDO"})


def instances_without_pins(run: BoardRun) -> List[str]:
    """A device pointed at an SPI bus whose pins were never emitted."""
    d = defines(run.text)
    out = []
    for name, value in d.items():
        if name.endswith("_SPI_INSTANCE") and re.fullmatch(r"SPI\d", value):
            if f"{value}_SCK_PIN" not in d:
                out.append(name)
    return sorted(out)


# --------------------------------------------------------------------------- #
# 3. Every emitted pin is one the firmware agrees can do the job
# --------------------------------------------------------------------------- #

def unvalidated_pin_defines(run: BoardRun) -> List[str]:
    """
    Defines whose pin the firmware tables do not support for that function.

    Cross-checked against the capability map rather than against genconfig's own
    bookkeeping, so a bug in the latter cannot hide here.

    Only defines whose meaning implies a peripheral function are checkable. A
    chip select, an interrupt input, an LED or a PINIO output is any GPIO and
    the firmware has no table to check it against, so those are checked for
    shape and no further - claiming otherwise would be a test that cannot fail.
    """
    caps, d = run.caps, defines(run.text)
    bad: List[str] = []

    def has(table: str, pin: str, dev: str, key: str, value: str) -> bool:
        return any(e["dev"] == dev and e[key] == value
                   for e in caps[table].get(pin, []))

    for name, pin in sorted(d.items()):
        if not name.endswith("_PIN") or not PIN_VALUE_RE.match(pin):
            continue
        m = re.fullmatch(r"UART(\d+)_(TX|RX)_PIN", name)
        if m and not has("uart", pin, f"UART{m.group(1)}", "dir", m.group(2).lower()):
            bad.append(name)
            continue
        m = re.fullmatch(r"SPI(\d)_(SCK|SDI|SDO)_PIN", name)
        if m and not has("spi", pin, f"SPI{m.group(1)}", "role", m.group(2).lower()):
            bad.append(name)
            continue
        m = re.fullmatch(r"I2C(\d)_(SCL|SDA)_PIN", name)
        if m and not has("i2c", pin, f"I2C{m.group(1)}", "role", m.group(2).lower()):
            bad.append(name)
            continue
        if name in ADC_PIN_DEFINES:
            instance = d.get("ADC_INSTANCE", "ADC1")[len("ADC"):]
            entry = caps["adc"].get(pin)
            if not entry or instance not in entry["devices"]:
                bad.append(name)

    # Anything in the timer map has to have that timer channel on that pin.
    for _, label, occurrence, _ in timer_rows(run.text):
        pin = d.get(label)
        if pin and PIN_VALUE_RE.match(pin):
            channels = caps["timers"].get(pin) or []
            if not 1 <= occurrence <= len(channels):
                bad.append(label)
    return sorted(set(bad))


def timer_occurrence_errors(run: BoardRun) -> List[str]:
    """
    The TIMER_PIN_MAP occurrence index must select the channel genconfig chose.

    It is a 1-based index into the firmware's own per-pin channel list, so an
    off-by-one here silently drives the wrong timer.
    """
    d = defines(run.text)
    chosen = {p["label"]: (p["pin"], p["channel"]) for p in run.meta["timers"]}
    out = []
    for _, label, occurrence, _ in timer_rows(run.text):
        pin = d.get(label)
        if not pin:
            continue
        channels = run.caps["timers"].get(pin) or []
        if not 1 <= occurrence <= len(channels):
            out.append(f"{label}: occurrence {occurrence} outside 1..{len(channels)}")
            continue
        want = chosen.get(label)
        if want and want[1] != channels[occurrence - 1]:
            out.append(f"{label}: occurrence {occurrence} is {channels[occurrence - 1]}, "
                       f"not the chosen {want[1]}")
    return out


def timer_rate_class_clashes(run: BoardRun) -> List[str]:
    """
    TIM units carrying two functions that want different rates of them.

    Derived from the emitted file and the capability map, not from genconfig's
    own bookkeeping, so a bug in the latter cannot hide here: the occurrence in
    each TIMER_PIN_MAP row is resolved back to a channel through the firmware
    table, exactly as timer_occurrence_errors does.

    Recorded per board rather than asserted empty. It is a real hazard - the
    period belongs to the whole unit - but usually a latent one, since DShot
    bitbang leaves the motor timers alone, and some boards have no other pin to
    move to. See ROADMAP 4.8.
    """
    d = defines(run.text)
    units: Dict[str, set] = {}
    for _, label, occurrence, _ in timer_rows(run.text):
        pin = d.get(label)
        channels = run.caps["timers"].get(pin) or [] if pin else []
        if not 1 <= occurrence <= len(channels):
            continue
        got = genconfig._rate_class(label)
        if not got:
            continue
        units.setdefault(channels[occurrence - 1].split("_")[0], set()).add(got[1])
    return sorted(f"{unit}: {'+'.join(sorted(classes))}"
                  for unit, classes in units.items() if len(classes) > 1)


# --------------------------------------------------------------------------- #
# 4. SPI bus assignment is conflict-free
# --------------------------------------------------------------------------- #

def spi_device_pins(run: BoardRun) -> Dict[str, Dict[str, str]]:
    """
    owner -> {role: pin}, rebuilt from the net map the way genconfig groups it.

    Uses genconfig's own classifier, so the check follows changes to the rules
    rather than freezing a copy of them.
    """
    groups: Dict[str, Dict[str, str]] = {}
    for link in run.result.links:
        if not link.gpio or (link.checked and not link.ok):
            continue
        role, _, sub = genconfig.classify(link.net)
        if role and role.endswith("_spi"):
            groups.setdefault(role[:-4], {})[sub or "sck"] = link.pin
        elif role and role.endswith("_cs"):
            groups.setdefault(role[:-3], {})["cs"] = link.pin
    return groups


def spi_conflicts(run: BoardRun) -> List[str]:
    """
    Two devices with different data pins may not share an SPI instance, and a
    device's own pins must all belong to the instance it was given.
    """
    d = defines(run.text)
    groups = spi_device_pins(run)
    out: List[str] = []
    by_instance: Dict[str, List[tuple]] = {}
    for owner, pins in sorted(groups.items()):
        define = genconfig.INSTANCE_DEFINE.get(owner)
        instance = d.get(define) if define else None
        if not instance:
            continue
        data = tuple(sorted((r, p) for r, p in pins.items() if r != "cs"))
        by_instance.setdefault(instance, []).append((owner, data))
        for role, pin in data:
            if not any(e["dev"] == instance and e["role"] == role
                       for e in run.caps["spi"].get(pin, [])):
                out.append(f"{owner} is on {instance} but its {role} pin has no "
                           f"{instance} {role} function")
    for instance, owners in sorted(by_instance.items()):
        shapes = {data for _, data in owners}
        if len(shapes) > 1:
            names = "/".join(sorted(o for o, _ in owners))
            out.append(f"{instance} is shared by {names} with different data pins")
    return out


# --------------------------------------------------------------------------- #
# 5. DMA options
# --------------------------------------------------------------------------- #

def dma_assignments(run: BoardRun):
    """(style, [(label, opt), ...] for every user that asked for a DMA option)."""
    d = defines(run.text)
    used = [(label, opt) for _, label, _, opt in timer_rows(run.text) if opt >= 0]
    for name, value in sorted(d.items()):
        m = re.fullmatch(r"ADC(\d)_DMA_OPT", name)
        if m:
            used.append((name, int(value)))
    return run.caps["dma"]["style"], used


def dma_conflicts(run: BoardRun) -> List[str]:
    """
    On DMAMUX/GPDMA parts the option is an index into one shared channel table,
    so every user needs a distinct number - four motors on option 0 would all
    claim the same channel. On the fixed-mapping parts the number indexes each
    resource's own stream list, so repeats are correct there; what must hold
    instead is that the ADC does not land on a stream a timer already took.
    """
    style, used = dma_assignments(run)
    caps = run.caps
    out: List[str] = []
    if style == "mux":
        pool = caps["dma"]["mux_options"] or 0
        seen: Dict[int, str] = {}
        for label, opt in used:
            if opt in seen:
                out.append(f"{label} and {seen[opt]} share DMA option {opt}")
            seen[opt] = label
            if pool and opt >= pool:
                out.append(f"{label}: DMA option {opt} is beyond the {pool} channels "
                           f"this part has")
        return out

    d = defines(run.text)
    claimed: Dict[str, str] = {}
    for _, label, occurrence, opt in timer_rows(run.text):
        pin = d.get(label)
        channels = caps["timers"].get(pin) if pin else None
        if opt < 0 or not channels or not 1 <= occurrence <= len(channels):
            continue
        spec = genconfig.dma_streams(caps, channels[occurrence - 1], opt)
        if spec:
            claimed[genconfig._stream_of(spec)] = label
    for label, opt in used:
        m = re.fullmatch(r"ADC(\d)_DMA_OPT", label)
        if not m:
            continue
        spec = genconfig.dma_streams(caps, f"ADC{m.group(1)}", opt)
        stream = genconfig._stream_of(spec) if spec else None
        if stream is None:
            out.append(f"{label} = {opt} is not a valid option for ADC{m.group(1)}")
        elif stream in claimed:
            out.append(f"{label} takes {stream}, already used by {claimed[stream]}")
    return out


# --------------------------------------------------------------------------- #
# The recorded set
# --------------------------------------------------------------------------- #

DEFECT_CHECKS = {
    "unaccounted_roles": unaccounted_roles,
    "dangling_timer_labels": dangling_timer_labels,
    "unvalidated_pin_defines": unvalidated_pin_defines,
    "incomplete_spi_buses": incomplete_spi_buses,
    "instances_without_pins": instances_without_pins,
    # Recorded, not asserted empty: on some boards there is no other pin to move
    # the function to, and the hazard is latent under DShot bitbang. What must
    # not happen is a new one appearing unnoticed. ROADMAP 4.8.
    "timer_rate_class_clashes": timer_rate_class_clashes,
}

# Invariants that hold on every board today. These are asserted empty outright -
# a fixture cannot record an exception to them, because any violation is a
# defect nobody has decided to live with.
STRICT_CHECKS = {
    "label_conservation": label_conservation,
    "malformed_pin_defines": malformed_pin_defines,
    "duplicate_pin_defines": duplicate_pin_defines,
    "timer_occurrence_errors": timer_occurrence_errors,
    "spi_conflicts": spi_conflicts,
    "dma_conflicts": dma_conflicts,
}


def metrics(run: BoardRun) -> dict:
    """Exactly the fields boards.json records for a board."""
    res = run.result
    checked = [l for l in res.links if l.checked]
    return {
        "target": run.target,
        "symbol_rows": len(run.symbol.rows),
        "pitch": round(run.symbol.pitch, 2),
        "net_labels": len(run.labels),
        "offset": round(res.offset, 4),
        "agreement": list(res.score),
        "orphan_labels": len(res.orphans),
        "nets_on_power_pins": len(res.on_power_pin),
        "unmapped_pins": len(res.unmapped),
        "firmware_gaps": sum(1 for l in checked if not l.ok and l.symbol_ok is True),
        "schematic_errors": sum(1 for l in checked if not l.ok and l.symbol_ok is False),
        "uncorroborated_mismatches": sum(1 for l in checked
                                         if not l.ok and l.symbol_ok is None),
        "config_defines": len(defines(run.text)),
        "timer_rows": len(timer_rows(run.text)),
        "warnings": len(run.cfg.warnings),
        "notes": len(run.cfg.notes),
        "netmap_digest": support.netmap_digest(run),
        "config_digest": support.config_digest(run.text),
        "known_defects": {name: fn(run) for name, fn in sorted(DEFECT_CHECKS.items())},
    }
