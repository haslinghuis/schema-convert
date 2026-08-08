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
seed_firmware.py - the harvest of Betaflight's own hardware tables.

Everything downstream trusts this file completely: it is what decides whether a
net may be emitted at all. A silently truncated table does not look like a bug,
it looks like a board with fewer pins - so the checks here are about shape and
reachability rather than about specific values, which move whenever the firmware
does.

Two sources are exercised. The frozen fixture always is, so these run anywhere.
A live Betaflight tree is used as well when one can be found, which is what
catches the firmware moving under the parser.
"""

import re
import unittest

import support

import seed_firmware

PIN = re.compile(r"^P[A-K](?:[0-9]|1[0-5])$")
# AT32 spells its timers TMR, and the seeder keeps each vendor's own
# spelling rather than normalising - the name only labels the channel,
# and what a config.h carries is the occurrence index.
TIMER_CHANNEL = re.compile(r"^(?:TIM|TMR)\d+_CH\d+N?$")
DMA_OPTION = re.compile(r"^DMA\d_S\d+_C\d+$")
UART_DEV = re.compile(r"^(?:LP)?UART\d+$")


# --------------------------------------------------------------------------- #
# Preprocessor guard handling
# --------------------------------------------------------------------------- #

class GuardScannerTests(unittest.TestCase):
    """
    Which lines a given MCU can reach. Getting this wrong does not fail loudly:
    it drops a whole peripheral table and the board simply comes out with fewer
    pins than it has.
    """

    def scan(self, text):
        return {line: guards for line, guards in seed_firmware.GuardScanner(text)
                if line and not line.startswith("#")}

    def test_else_branches_carry_the_negation_of_the_ones_above(self):
        got = self.scan(
            "#if defined(STM32F4)\n"
            "a;\n"
            "#elif defined(STM32H7)\n"
            "b;\n"
            "#else\n"
            "c;\n"
            "#endif\n"
        )
        self.assertEqual(got["a;"], ["defined(STM32F4)"])
        self.assertEqual(got["b;"], ["!(defined(STM32F4))", "defined(STM32H7)"])
        self.assertEqual(got["c;"], ["!(defined(STM32F4))", "!(defined(STM32H7))"])

    def test_nesting(self):
        got = self.scan("#ifdef USE_SPI\n#ifndef USE_SOFT\nx;\n#endif\n#endif\n")
        self.assertEqual(got["x;"], ["defined(USE_SPI)", "!defined(USE_SOFT)"])

    def test_a_guard_inside_a_comment_is_not_a_guard(self):
        got = self.scan("/* #if defined(STM32H7) */\nx;\n// #if defined(STM32F4)\ny;\n")
        self.assertEqual(got["x;"], [])
        self.assertEqual(got["y;"], [])


class GuardSatisfiabilityTests(unittest.TestCase):
    """
    seed_firmware.guards_hold.

    The MCU macros are fixed facts, but the USE_* macros are the board author's
    choice, so the question is whether *any* build of this MCU reaches the line.
    Treating every USE_* as defined makes the I2C table unreachable and silently
    drops every I2C pin on every target - which is exactly the bug this models.
    """

    F7 = {"STM32F722xx", "STM32F7", "STM32"}

    def test_the_i2c_tables_guard_is_satisfiable(self):
        guard = ["defined(USE_I2C) && !defined(USE_SOFT_I2C) && !defined(USE_I3C_AS_I2C)"]
        holds, notable = seed_firmware.guards_hold(guard, self.F7)
        self.assertTrue(holds)
        self.assertEqual(notable, [])

    def test_another_mcu_family_is_not_reachable(self):
        self.assertFalse(seed_firmware.guards_hold(["defined(STM32H7)"], self.F7)[0])
        self.assertTrue(seed_firmware.guards_hold(["defined(STM32F7)"], self.F7)[0])

    def test_a_contradiction_is_not_reachable(self):
        self.assertFalse(seed_firmware.guards_hold(
            ["defined(USE_SPI)", "!defined(USE_SPI)"], self.F7)[0])

    def test_a_non_default_switch_is_kept_but_flagged(self):
        holds, notable = seed_firmware.guards_hold(
            ["defined(USE_EXTENDED_SPI_DEVICE)"], self.F7)
        self.assertTrue(holds)
        self.assertEqual(notable, ["USE_EXTENDED_SPI_DEVICE"])

    def test_a_guard_that_does_not_translate_is_no_obstacle(self):
        """
        A guard the translator cannot turn into Python must not be read as
        "unreachable" - that would drop real pins for a parsing failure.
        """
        self.assertTrue(seed_firmware.guards_hold(["defined(A) ? 1 : 0"], self.F7)[0])

    def test_a_test_of_a_macros_value_is_free_not_false(self):
        """
        Only macro *names* are harvested, so `TARGET_FLASH_SIZE > 512` cannot be
        decided either way and both branches of it must stay reachable.

        This used to assert the opposite, because the name resolved to False and
        `False > 512` is False - a value test silently gated its line off, and
        the suite pinned that rather than the behaviour _eval documents. No
        table parsed today carries such a guard, so this is a latent contract,
        deliberately changed.
        """
        self.assertTrue(seed_firmware.guards_hold(
            ["TARGET_FLASH_SIZE > 512"], self.F7)[0])
        self.assertTrue(seed_firmware.guards_hold(
            ["!(TARGET_FLASH_SIZE > 512)"], self.F7)[0])
        self.assertTrue(seed_firmware.guards_hold(
            ["MCU_FLASH_SIZE == 1024"], self.F7)[0])

    def test_an_undecidable_value_test_does_not_erase_the_family_evidence(self):
        """
        The cheap fix - "anything with a comparison is reachable" - would hand
        every other MCU's pins to this one. The family conjunct still decides,
        whichever side of the && it sits on.
        """
        for guard in ("defined(STM32H7) && TARGET_FLASH_SIZE > 512",
                      "TARGET_FLASH_SIZE > 512 && defined(STM32H7)"):
            self.assertFalse(seed_firmware.guards_hold([guard], self.F7)[0], guard)
        self.assertTrue(seed_firmware.guards_hold(
            ["TARGET_FLASH_SIZE > 512 && defined(STM32F7)"], self.F7)[0])
        # ...and one value test cannot be both true and false at once.
        self.assertFalse(seed_firmware.guards_hold(
            ["TARGET_FLASH_SIZE > 512", "!(TARGET_FLASH_SIZE > 512)"], self.F7)[0])


class DefineValueTests(unittest.TestCase):
    """
    seed_firmware._define_value - the firmware's fixed array ceilings.

    These bound what a config may reference: a UART pin past
    UARTHARDWARE_MAX_PINS is not in the hardware table at all. Reading the wrong
    branch of the per-family #if chain would hand a family someone else's
    ceiling, which is the one failure worth testing in isolation.
    """

    CHAIN = (
        "#if defined(STM32F4)\n"
        "#define UARTHARDWARE_MAX_PINS 4\n"
        "#elif defined(STM32H7)\n"
        "#define UARTHARDWARE_MAX_PINS 6\n"
        "#elif defined(STM32H5) || defined(STM32C5)\n"
        "#define UARTHARDWARE_MAX_PINS 5\n"
        "#endif\n"
    )

    def value(self, text, defs, macro="UARTHARDWARE_MAX_PINS"):
        return seed_firmware._define_value(text, macro, defs)

    def test_each_family_gets_its_own_branch(self):
        self.assertEqual(self.value(self.CHAIN, {"STM32F4"}), 4)
        self.assertEqual(self.value(self.CHAIN, {"STM32H7"}), 6)
        self.assertEqual(self.value(self.CHAIN, {"STM32H5"}), 5)
        self.assertEqual(self.value(self.CHAIN, {"STM32C5"}), 5)

    def test_a_family_the_chain_does_not_name_gets_none_not_a_default(self):
        self.assertIsNone(self.value(self.CHAIN, {"STM32G4"}))

    def test_an_unguarded_define_applies_to_every_family(self):
        self.assertEqual(
            self.value("#define I2C_PIN_SEL_MAX 8\n", {"STM32G4"}, "I2C_PIN_SEL_MAX"), 8)

    def test_a_value_that_is_not_an_integer_is_not_guessed_at(self):
        self.assertIsNone(self.value("#define UARTHARDWARE_MAX_PINS SOME_OTHER\n",
                                     {"STM32F4"}))

    def test_a_longer_macro_name_is_not_mistaken_for_this_one(self):
        self.assertIsNone(self.value("#define UARTHARDWARE_MAX_PINS_X 9\n",
                                     {"STM32F4"}))


# --------------------------------------------------------------------------- #
# The harvested data
# --------------------------------------------------------------------------- #

class CapabilityDataMixin:
    """Shape checks applied to whichever capability map is under test."""

    def check_target(self, name, caps):
        self.assertTrue(caps["family"].startswith(("STM32", "AT32")), name)
        self.assertTrue(caps["mcu"], name)

        for table in ("timers", "uart", "spi", "i2c", "adc"):
            self.assertTrue(caps[table], f"{name}: {table} table is empty")
            for pin in caps[table]:
                self.assertRegex(pin, PIN, f"{name}: {table} has pin {pin!r}")

        for pin, channels in caps["timers"].items():
            self.assertEqual(len(set(channels)), len(channels),
                             f"{name}: {pin} lists a timer channel twice, which "
                             "shifts every TIMER_PIN_MAP occurrence after it")
            for channel in channels:
                self.assertRegex(channel, TIMER_CHANNEL, f"{name}: {pin}")

        for pin, entries in caps["uart"].items():
            for e in entries:
                self.assertRegex(e["dev"], UART_DEV, f"{name}: {pin}")
                self.assertIn(e["dir"], ("tx", "rx"), f"{name}: {pin}")
        for pin, entries in caps["spi"].items():
            for e in entries:
                self.assertRegex(e["dev"], r"^SPI\d+$", f"{name}: {pin}")
                self.assertIn(e["role"], ("sck", "sdi", "sdo"), f"{name}: {pin}")
        for pin, entries in caps["i2c"].items():
            for e in entries:
                self.assertRegex(e["dev"], r"^I2C\d+$", f"{name}: {pin}")
                self.assertIn(e["role"], ("scl", "sda"), f"{name}: {pin}")
        for pin, entry in caps["adc"].items():
            self.assertRegex(entry["devices"], r"^\d+$", f"{name}: {pin}")
            self.assertTrue(entry["channel"], f"{name}: {pin}")

        for table in ("uart", "spi", "i2c"):
            for pin, entries in caps[table].items():
                keys = [tuple(sorted(e.items())) for e in entries]
                self.assertEqual(len(set(keys)), len(keys),
                                 f"{name}: {table} lists {pin} twice")

        self.check_dma(name, caps)

    def check_dma(self, name, caps):
        dma = caps["dma"]
        self.assertIn(dma["style"], ("mux", "fixed"), name)
        if dma["style"] == "mux":
            # One shared channel table: the option is an index into it, so its
            # length is the only thing that bounds a valid option. Reading zero
            # there is what let every peripheral be given the same channel.
            self.assertGreater(dma["mux_options"], 0,
                               f"{name}: DMAMUX part with no channel table")
            self.assertEqual(dma["timer"], {}, name)
            self.assertEqual(dma["peripheral"], {}, name)
        else:
            self.assertTrue(dma["timer"], f"{name}: fixed mapping with no timer DMA")
            self.assertTrue(dma["peripheral"], name)
            for key, options in dma["timer"].items():
                self.assertRegex(key, TIMER_CHANNEL, name)
                for opt in options:
                    self.assertRegex(opt, DMA_OPTION, f"{name}: {key}")
            for key, options in dma["peripheral"].items():
                self.assertRegex(key, r"^(?:ADC|SPI|UART|TIMUP)\w*\d\w*$", name)
                for opt in options:
                    self.assertRegex(opt, DMA_OPTION, f"{name}: {key}")


class FrozenDataTests(CapabilityDataMixin, unittest.TestCase):
    """The committed fixture, which the golden tests are generated against."""

    def setUp(self):
        self.data = support.frozen_firmware()

    def test_every_target_is_well_formed(self):
        for name, caps in sorted(self.data["targets"].items()):
            with self.subTest(target=name):
                self.check_target(name, caps)

    def test_the_families_the_boards_need_are_present(self):
        """
        Derived from the boards rather than listed here. A board whose target is
        missing from the frozen map does not skip - detect_target finds nothing
        and run_board raises - so the two files have to be kept in step, and a
        second hand-written copy of the list is one more thing to forget.
        """
        need = sorted({b["target"] for b in support.board_fixtures() if b.get("target")})
        have = sorted(self.data["targets"])
        self.assertEqual([t for t in need if t not in have], [],
                         f"frozen firmware has {have}; the golden boards need {need}. "
                         "Add the target to FROZEN_TARGETS in tests/update_golden.py "
                         "and re-run it with --reseed")

    def test_both_dma_styles_are_represented(self):
        styles = {c["dma"]["style"] for c in self.data["targets"].values()}
        self.assertEqual(styles, {"mux", "fixed"},
                         "the fixture must keep one of each DMA style, or the "
                         "numbering rules are only half tested")

    def test_the_driver_catalogue_names_parts_not_feature_switches(self):
        drivers = self.data["drivers"]
        self.assertIn("MPU6000", drivers["gyro"])
        self.assertIn("ICM42688P", drivers["gyro"])
        self.assertIn("MAX7456", drivers["osd"])
        self.assertIn("W25Q128FV", drivers["flash"])
        for category, parts in drivers.items():
            for part, buses in parts.items():
                self.assertNotIn(part, seed_firmware.DRIVER_REJECT, category)
                for bus, define in buses.items():
                    self.assertIn(bus, ("spi", "i2c", "any"), part)
                    self.assertTrue(define.startswith("USE_"), define)

    def test_every_alias_points_at_a_real_driver(self):
        """
        aliases.json is the one hand-maintained file, so an entry can rot into a
        name no driver has - and a silkscreen marking would then map to nothing.
        """
        import json
        aliases = json.loads((support.MCU_PARSER / "data" / "aliases.json").read_text())
        for category, entries in aliases.items():
            if category.startswith("_"):
                continue
            self.assertIn(category, self.data["drivers"], category)
            for marking, driver in entries.items():
                self.assertIn(driver, self.data["drivers"][category],
                              f"aliases.json {category}.{marking} -> {driver}")


class LiveSeedTests(CapabilityDataMixin, unittest.TestCase):
    """
    The seeder run against a real Betaflight tree. Skipped when there is none.

    This is the test that fails when the firmware moves the tables - a renamed
    target, a restructured file, a guard that stops being satisfiable.
    """

    @classmethod
    def setUpClass(cls):
        cls.data = support.seeded_firmware()

    def test_every_target_is_well_formed(self):
        for name, caps in sorted(self.data["targets"].items()):
            with self.subTest(target=name):
                self.check_target(name, caps)

    def test_all_the_stm32_families_are_harvested(self):
        families = {c["family"] for c in self.data["targets"].values()}
        self.assertGreaterEqual(families,
                                {"STM32F4", "STM32F7", "STM32G4", "STM32H5", "STM32H7"})

    def test_the_target_names_are_the_ones_the_build_system_uses(self):
        """
        FC_TARGET_MCU has to name a target directory the build system knows.
        A stale name is the failure mode that bites: PR #15470 renamed
        STM32F7X2 to STM32F722, and a config naming the old one no longer
        builds.
        """
        tree = support.firmware_tree()
        # Every platform the seeder harvests, not only STM32: an AT32 target
        # directory is named the same way and is just as capable of being
        # renamed out from under a config.
        on_disk = {mk.parent.name
                   for mk in tree.glob("src/platform/*/target/*/target.mk")}
        self.assertTrue(on_disk, "no target directories found")
        self.assertEqual(set(self.data["targets"]) & on_disk, set(self.data["targets"]))
        for name in self.data["targets"]:
            # STM32F722, and AT32F435G - the AT32 name carries the flash-size
            # letter after the three digits, which is what selects the target.
            self.assertRegex(
                name, r"^(?:STM32[A-Z]\d[A-Z0-9]{2}|AT32[A-Z]\d{3}[A-Z])$")

    def test_the_array_limits_are_harvested_per_family(self):
        """
        Read straight out of the per-family #if chain in
        src/platform/STM32/include/platform/platform.h. If the firmware moves a
        family to a different ceiling this fails, which is the point - the value
        is only useful if it is the build's own.
        """
        expected = {"STM32F405": 4, "STM32F722": 4, "STM32G474": 3,
                    "STM32H562": 5, "STM32H743": 6}
        for target, pins in expected.items():
            if target not in self.data["targets"]:
                continue
            with self.subTest(target=target):
                self.assertEqual(
                    self.data["targets"][target]["limits"]["UARTHARDWARE_MAX_PINS"],
                    pins)

    def test_every_target_carries_a_plausible_set_of_limits(self):
        for name, caps in sorted(self.data["targets"].items()):
            limits = caps["limits"]
            self.assertEqual(sorted(limits),
                             sorted(m for m, _ in seed_firmware.LIMIT_SOURCES), name)
            for macro, value in limits.items():
                with self.subTest(target=name, macro=macro):
                    self.assertIsNotNone(value, f"{name}: {macro} not found")
                    self.assertIsInstance(value, int)
                    self.assertGreater(value, 0)

    # Ports whose pin list is longer than the array declared to hold it. Recorded
    # exactly, so a new one fails and so does fixing one of these.
    #
    # STM32N6 sets UARTHARDWARE_MAX_PINS to 5, but serial_uart_stm32n6xx.c gives
    # UART7 six .txPins. Betaflight builds with -Werror, so an N6 config that
    # defines USE_UART7 does not compile at all - excess elements in an array
    # initializer. Read out of the firmware, not inferred from this parser.
    UART_TABLE_OVERFLOWS = {("STM32N657", "UART7", "tx"): 6}

    def test_the_harvested_tables_stay_inside_those_limits(self):
        """
        The limits are only worth recording if they agree with the tables next
        to them. More UART pins for one port than the array holds would mean
        either the parser is reading rows the build cannot reach, or the
        firmware table has outgrown its array.
        """
        overflows = {}
        for name, caps in sorted(self.data["targets"].items()):
            limits = caps["limits"]
            with self.subTest(target=name):
                counts = {}
                for pin, entries in caps["uart"].items():
                    for e in entries:
                        counts[(e["dev"], e["dir"])] = counts.get((e["dev"], e["dir"]), 0) + 1
                for (dev, direction), n in counts.items():
                    if n > limits["UARTHARDWARE_MAX_PINS"]:
                        overflows[(name, dev, direction)] = n

                counts = {}
                for pin, entries in caps["i2c"].items():
                    for e in entries:
                        counts[(e["dev"], e["role"])] = counts.get((e["dev"], e["role"]), 0) + 1
                self.assertLessEqual(max(counts.values(), default=0),
                                     limits["I2C_PIN_SEL_MAX"], name)

                dma = caps["dma"]
                if dma["style"] == "mux":
                    self.assertLessEqual(dma["mux_options"],
                                         limits["MAX_PERIPHERAL_DMA_OPTIONS"], name)
                else:
                    for key, options in dma["timer"].items():
                        self.assertLessEqual(len(options),
                                             limits["MAX_TIMER_DMA_OPTIONS"],
                                             f"{name}: {key}")
                    for key, options in dma["peripheral"].items():
                        self.assertLessEqual(len(options),
                                             limits["MAX_PERIPHERAL_DMA_OPTIONS"],
                                             f"{name}: {key}")

        self.assertEqual(overflows, self.UART_TABLE_OVERFLOWS,
                         "a UART pin list has outgrown UARTHARDWARE_MAX_PINS, or a "
                         "recorded overflow has been fixed upstream")

    def test_the_revision_is_recorded(self):
        rev = self.data["firmware"]
        self.assertTrue(rev["rev"], "no git revision recorded - provenance is lost")
        self.assertTrue(rev["date"])

    def test_the_frozen_fixture_still_describes_these_targets(self):
        """
        Not an equality check: the fixture is deliberately pinned to one
        revision. What must stay true is that the targets it holds still exist.
        """
        for name in support.frozen_firmware()["targets"]:
            self.assertIn(name, self.data["targets"],
                          f"{name} has disappeared from the firmware; the golden "
                          "fixtures need regenerating")


if __name__ == "__main__":
    unittest.main()
