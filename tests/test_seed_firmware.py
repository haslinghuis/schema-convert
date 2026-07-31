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
TIMER_CHANNEL = re.compile(r"^TIM\d+_CH\d+N?$")
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

        Note the limit: this only holds for expressions that fail to evaluate.
        A comparison against an unknown macro (`TARGET_FLASH_SIZE > 512`)
        evaluates cleanly to False and does gate the line, which is not what
        _eval's comment says it intends. No table parsed today uses one.
        """
        self.assertTrue(seed_firmware.guards_hold(["defined(A) ? 1 : 0"], self.F7)[0])
        self.assertFalse(seed_firmware.guards_hold(["TARGET_FLASH_SIZE > 512"],
                                                   self.F7)[0])


# --------------------------------------------------------------------------- #
# The harvested data
# --------------------------------------------------------------------------- #

class CapabilityDataMixin:
    """Shape checks applied to whichever capability map is under test."""

    def check_target(self, name, caps):
        self.assertTrue(caps["family"].startswith("STM32"), name)
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
        self.assertEqual(sorted(self.data["targets"]),
                         ["STM32F405", "STM32F722", "STM32G474", "STM32H562"])

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
        on_disk = {mk.parent.name
                   for mk in (tree / "src/platform/STM32/target").glob("*/target.mk")}
        self.assertTrue(on_disk, "no STM32 target directories found")
        self.assertEqual(set(self.data["targets"]) & on_disk, set(self.data["targets"]))
        for name in self.data["targets"]:
            self.assertRegex(name, r"^STM32[A-Z]\d[A-Z0-9]{2}$")

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
