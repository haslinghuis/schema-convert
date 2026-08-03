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
Real vendor schematics, converted end to end.

Two kinds of check, and the difference matters:

  * **Invariants** - things that must be true of any conversion of any board.
    No board data is needed to state them, they survive an intentional
    improvement, and they are what catches a refactor breaking something
    nobody thought to look at.

  * **Goldens** - one-way digests of the netmap and of the generated config.h.
    They notice *any* change, including the ones an invariant cannot describe,
    at the cost of also firing when a change is deliberate. Re-record with
    `python3 tests/update_golden.py`, after reading the diff.

Everything runs against the frozen firmware fixture, never against the local
`mcu-parser/data/firmware.json`, so re-seeding from a moving Betaflight tree
cannot move a golden.

The schematics are confidential and are not in the repository; without them
these tests skip.
"""

import os
import subprocess
import sys
import textwrap
import unittest

import analysis
import support


class BoardTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not support.available_pdfs():
            raise unittest.SkipTest(support.missing_message())
        cls.fixtures = support.board_fixtures()

    def board(self, fixture):
        """The conversion, or a skip when this particular sheet is not here."""
        if fixture["sha256"] not in support.available_pdfs():
            raise unittest.SkipTest(
                f"schematic for '{fixture['id']}' not available locally")
        return support.run_board(fixture)

    def each(self):
        for fixture in self.fixtures:
            yield fixture

    # ---- what the vendor's sheet resolved to --------------------------- #

    def test_agreement_is_exactly_what_was_recorded(self):
        """
        The share of nets whose pin the firmware confirms. 100% on the boards
        that reach it; the known-lower ones must stay exactly where they are.
        A move in either direction is something to look at - upwards usually
        means a firmware table gained a pin, downwards that something broke.
        """
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(list(run.result.score), fixture["agreement"])
                self.assertEqual(run.target, fixture["target"])

    def test_the_symbol_and_its_gutters_are_read_the_same_way(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(len(run.symbol.rows), fixture["symbol_rows"])
                self.assertEqual(round(run.symbol.pitch, 2), fixture["pitch"])
                self.assertEqual(len(run.labels), fixture["net_labels"])
                self.assertEqual(round(run.result.offset, 4), fixture["offset"])

    def test_the_diagnostics_are_the_ones_recorded(self):
        """
        Whether a rejected net is a firmware gap, a schematic error or merely
        uncorroborated decides what a human does about it, so each count is
        pinned separately.
        """
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                got = analysis.metrics(run)
                for key in ("orphan_labels", "nets_on_power_pins", "unmapped_pins",
                            "firmware_gaps", "schematic_errors",
                            "uncorroborated_mismatches", "warnings", "notes"):
                    self.assertEqual(got[key], fixture[key], key)

    # ---- invariant: nothing is dropped in silence ----------------------- #

    def test_no_net_label_is_lost_between_the_gutter_and_the_map(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                self.assertEqual(analysis.label_conservation(self.board(fixture)), [])

    def test_every_mapped_net_is_emitted_or_explained(self):
        """
        A net that reaches neither the config.h nor a warning just looks like an
        incomplete config later, with no clue why. Recorded exceptions are
        listed in the fixture with a reason.
        """
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(analysis.unaccounted_roles(run),
                                 fixture["known_defects"]["unaccounted_roles"],
                                 self.hint(fixture, "unaccounted_roles"))

    # ---- invariant: the file is internally consistent ------------------- #

    def test_every_timer_mapping_names_a_define_the_file_makes(self):
        """TIMER_PIN_MAP referring to a define that is never made will not compile."""
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(analysis.dangling_timer_labels(run),
                                 fixture["known_defects"]["dangling_timer_labels"],
                                 self.hint(fixture, "dangling_timer_labels"))

    def test_pin_defines_are_well_formed_and_unique(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(analysis.malformed_pin_defines(run), [])
                self.assertEqual(analysis.duplicate_pin_defines(run), [])

    def test_a_bus_is_either_complete_or_recorded_as_not(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(analysis.incomplete_spi_buses(run),
                                 fixture["known_defects"]["incomplete_spi_buses"],
                                 self.hint(fixture, "incomplete_spi_buses"))
                self.assertEqual(analysis.instances_without_pins(run),
                                 fixture["known_defects"]["instances_without_pins"],
                                 self.hint(fixture, "instances_without_pins"))

    # ---- invariant: the firmware agrees with every pin we emit ---------- #

    def test_every_emitted_pin_can_do_the_job(self):
        """
        Re-checked against the capability map from the outside, so a bug in
        genconfig's own bookkeeping cannot hide here. Chip selects, LEDs and
        PINIO outputs are any-GPIO and are checked for shape only.
        """
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(analysis.unvalidated_pin_defines(run),
                                 fixture["known_defects"]["unvalidated_pin_defines"],
                                 self.hint(fixture, "unvalidated_pin_defines"))

    def test_timer_occurrence_indices_select_the_intended_channel(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                self.assertEqual(analysis.timer_occurrence_errors(self.board(fixture)),
                                 [])

    # ---- invariant: buses and DMA ------------------------------------- #

    def test_spi_assignment_is_conflict_free(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                self.assertEqual(analysis.spi_conflicts(self.board(fixture)), [])

    def test_dma_options_follow_the_rule_for_the_part(self):
        """
        Distinct on a DMAMUX/GPDMA part, where the option indexes one shared
        channel table; on a fixed-mapping part repeats are correct, and what
        must hold is that the ADC dodges the streams the timers took.

        This is the invariant that caught four motors sharing channel 0 on an
        H5.
        """
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(analysis.dma_conflicts(run), [])
                style, used = analysis.dma_assignments(run)
                if style == "mux" and len(used) > 1:
                    self.assertEqual(len(used), len({o for _, o in used}))

    # ---- the file is a Betaflight config at all ------------------------ #

    def test_the_output_is_a_betaflight_config(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                d = support.defines(run.text)
                self.assertIn("#pragma once", run.text)
                self.assertIn("GNU General", run.text)
                self.assertEqual(d.get("FC_TARGET_MCU"), run.target)
                self.assertEqual(d.get("BOARD_NAME"), support.BOARD_NAME)
                self.assertEqual(d.get("MANUFACTURER_ID"), support.MANUFACTURER_ID)
                # REFERENCE is issued by the Betaflight team and cannot be
                # computed locally, so an unreviewed target must not claim one.
                self.assertNotIn("REFERENCE:", run.text)
                self.assertIn("GYRO_1_ALIGN", d)

    # ---- goldens -------------------------------------------------------- #

    def test_the_recovered_pin_map_is_unchanged(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(support.netmap_digest(run), fixture["netmap_digest"],
                                 self.golden_hint(run, "the recovered pin map"))

    def test_the_generated_config_is_unchanged(self):
        for fixture in self.each():
            with self.subTest(board=fixture["id"]):
                run = self.board(fixture)
                self.assertEqual(support.config_digest(run.text),
                                 fixture["config_digest"],
                                 self.golden_hint(run, "the generated config.h"))
                self.assertEqual(len(support.defines(run.text)),
                                 fixture["config_defines"])
                self.assertEqual(len(support.timer_rows(run.text)),
                                 fixture["timer_rows"])

    # ---- failure messages ---------------------------------------------- #

    def hint(self, fixture, field):
        return (f"{fixture['id']}: this differs from the {field} recorded in "
                f"tests/fixtures/boards.json. A new entry is a regression; an "
                f"entry that has gone means a known defect was fixed and the "
                f"fixture needs updating (python3 tests/update_golden.py).")

    def golden_hint(self, run, what):
        path = support.dump_actual(run)
        return (f"{run.id}: {what} changed. The output that was produced is in "
                f"{path} (gitignored) - diff it against your previous one. "
                f"If the change is intended, re-record with "
                f"python3 tests/update_golden.py.")


class FixtureTests(unittest.TestCase):
    """The fixtures themselves, checkable without any schematic."""

    def test_the_goldens_and_the_frozen_firmware_belong_together(self):
        """
        The goldens are only meaningful against the capability data they were
        recorded with; if the two ever drift apart every digest is noise.
        """
        self.assertEqual(support.boards_recorded_with()["frozen_firmware_rev"],
                         support.frozen_firmware()["firmware"]["rev"])

    def test_no_board_fixture_carries_vendor_material(self):
        """
        The one rule the fixtures exist under: a schematic is confidential, so
        a fixture may hold a hash and a count, never a name, a net or a pin.
        """
        for board in support.board_fixtures():
            with self.subTest(board=board["id"]):
                self.assertRegex(board["sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(board["id"], r"^[a-z0-9-]+$")
                text = " ".join(str(v) for v in board.values())
                self.assertNotRegex(text, r"\bP[A-K]\d{1,2}\b",
                                    "a pin assignment leaked into the fixture")
                self.assertNotRegex(text, r"\.pdf\b",
                                    "a schematic file name leaked into the fixture")

    def test_every_board_records_every_field_the_tests_read(self):
        required = {"id", "sha256", "target", "agreement", "symbol_rows", "pitch",
                    "net_labels", "offset", "netmap_digest", "config_digest",
                    "known_defects"}
        for board in support.board_fixtures():
            with self.subTest(board=board["id"]):
                self.assertLessEqual(required, set(board))
                self.assertEqual(set(board["known_defects"]),
                                 set(analysis.DEFECT_CHECKS))


class DeterminismTests(unittest.TestCase):
    """
    The pipeline has to be deterministic or every golden above is worthless.

    Set iteration is the risk: `sorted()` is used almost everywhere, and where
    it is not, a differing hash seed reorders the result. Run in a subprocess
    because PYTHONHASHSEED can only be set before the interpreter starts.
    """

    SCRIPT = textwrap.dedent("""
        import pathlib, sys
        sys.path.insert(0, sys.argv[1])
        import genconfig
        genconfig.DATA_DIR = pathlib.Path(sys.argv[2])
        cfg, _ = genconfig.build(pdf=pathlib.Path(sys.argv[3]), board='TESTBOARD',
                                 manufacturer='TEST', target=None,
                                 gyro_align='CW0_DEG')
        sys.stdout.write("\\n".join(cfg.lines))
    """)

    def convert_with_seed(self, pdf, seed):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", self.SCRIPT, str(support.MCU_PARSER),
             str(support.frozen_data_dir()), str(pdf)],
            capture_output=True, text=True, env=env, check=True,
        )
        return support.stable_config(out.stdout)

    def test_the_hash_seed_does_not_change_the_output(self):
        pdfs = support.available_pdfs()
        if not pdfs:
            self.skipTest(support.missing_message())
        for sha, pdf in sorted(pdfs.items()):
            with self.subTest(board=sha[:12]):
                first = self.convert_with_seed(pdf, "0")
                self.assertEqual(first, self.convert_with_seed(pdf, "12345"))

    def test_converting_twice_in_one_process_gives_the_same_answer(self):
        fixtures = support.board_fixtures()
        available = [f for f in fixtures if f["sha256"] in support.available_pdfs()]
        if not available:
            self.skipTest(support.missing_message())
        import genconfig
        support.use_frozen_firmware()
        for fixture in available:
            with self.subTest(board=fixture["id"]):
                pdf = support.available_pdfs()[fixture["sha256"]]
                runs = [genconfig.build(pdf=pdf, board="TESTBOARD",
                                        manufacturer="TEST", target=None,
                                        gyro_align="CW0_DEG")[0].lines
                        for _ in range(2)]
                self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
