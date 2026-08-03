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
The invariant checks themselves.

A check that cannot fail is worse than no check at all, and the board tests only
ever exercise the passing side of these - every real board is expected to be
clean. So each detector is also shown a broken config and made to find the
break.

The configs here are hand-written fragments for a made-up MCU. No schematic is
involved.
"""

import unittest
from types import SimpleNamespace

import analysis
from netmap import Link
from test_units import fake_caps


def fake_run(text, *, links=(), orphans=(), labels=None, timers=(),
             warnings=(), notes=(), style="fixed"):
    """A stand-in for support.BoardRun, carrying only what analysis reads."""
    links = list(links)
    result = SimpleNamespace(links=links, orphans=list(orphans),
                             score=(len(links), len(links)), offset=0.0,
                             unmapped=[], on_power_pin=[])
    return SimpleNamespace(
        id="fake", text=text, caps=fake_caps(style), result=result,
        labels=list(labels) if labels is not None else links + list(orphans),
        symbol=SimpleNamespace(rows=[], pitch=4.0),
        meta={"timers": [dict(t) for t in timers]},
        cfg=SimpleNamespace(warnings=list(warnings), notes=list(notes)),
        diagnostics=" ".join(list(warnings) + list(notes)),
    )


def link(net, pin, ok=True, checked=True, gpio=True):
    return Link(net, pin, "L", checked, ok, [], gpio, None)


class LabelConservationTests(unittest.TestCase):
    def test_clean(self):
        run = fake_run("", links=[link("TX1", "PA9")], orphans=["STRAY"])
        self.assertEqual(analysis.label_conservation(run), [])

    def test_a_label_that_became_neither_a_link_nor_an_orphan_is_caught(self):
        run = fake_run("", links=[link("TX1", "PA9")], labels=["a", "b", "c"])
        self.assertNotEqual(analysis.label_conservation(run), [])


class AccountingTests(unittest.TestCase):
    CONFIG = "#define UART1_TX_PIN        PA9\n"

    def test_an_emitted_net_is_accounted_for(self):
        run = fake_run(self.CONFIG, links=[link("TX1", "PA9")])
        self.assertEqual(analysis.unaccounted_roles(run), [])

    def test_a_net_left_out_but_explained_is_accounted_for(self):
        run = fake_run(self.CONFIG, links=[link("TX1", "PA9"), link("RX1", "PB1")],
                       warnings=["RX1 is on PB1, which the firmware does not "
                                 "support for that function - omitted"])
        self.assertEqual(analysis.unaccounted_roles(run), [])

    def test_a_net_left_out_in_silence_is_caught(self):
        run = fake_run(self.CONFIG, links=[link("TX1", "PA9"), link("RX1", "PB1")])
        self.assertEqual(analysis.unaccounted_roles(run), ["uart_rx"])

    def test_deliberately_ignored_nets_stay_quiet(self):
        run = fake_run(self.CONFIG, links=[link("FC-SWDIO", "PA13"),
                                           link("BOOT", "PB8")])
        self.assertEqual(analysis.unaccounted_roles(run), [])


class SelfConsistencyTests(unittest.TestCase):
    def test_a_timer_mapping_without_its_define_is_caught(self):
        text = ("#define MOTOR1_PIN          PC6\n"
                "#define TIMER_PIN_MAPPING \\\n"
                "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 2, 0) \\\n"
                "    TIMER_PIN_MAP( 1, LED_STRIP_PIN, 1, 0)\n")
        self.assertEqual(analysis.dangling_timer_labels(fake_run(text)),
                         ["LED_STRIP_PIN"])

    def test_a_complete_mapping_is_clean(self):
        text = ("#define MOTOR1_PIN          PC6\n"
                "#define TIMER_PIN_MAPPING \\\n"
                "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 2, 0)\n")
        self.assertEqual(analysis.dangling_timer_labels(fake_run(text)), [])

    def test_a_define_swallowing_the_next_one_is_not_mistaken_for_a_value(self):
        """
        A valueless define followed by a blank line used to swallow the define
        after it, which hid that one from every check here.
        """
        text = "#define BEEPER_INVERTED\n\n#define MOTOR1_PIN          PC6\n"
        self.assertEqual(analysis.malformed_pin_defines(fake_run(text)), [])
        self.assertIn("MOTOR1_PIN", analysis.defines(text))

    def test_a_pin_value_that_is_not_a_pin_is_caught(self):
        text = "#define MOTOR1_PIN          NONE\n"
        self.assertEqual(analysis.malformed_pin_defines(fake_run(text)),
                         ["MOTOR1_PIN"])

    def test_two_defines_claiming_one_pin_are_caught(self):
        text = "#define MOTOR1_PIN          PC6\n#define LED_STRIP_PIN       PC6\n"
        self.assertEqual(analysis.duplicate_pin_defines(fake_run(text)),
                         ["LED_STRIP_PIN+MOTOR1_PIN"])

    def test_a_half_wired_bus_is_caught(self):
        text = "#define SPI1_SCK_PIN        PA5\n#define SPI1_SDI_PIN        PA6\n"
        self.assertEqual(analysis.incomplete_spi_buses(fake_run(text)), ["SPI1"])

    def test_a_device_pointed_at_a_bus_with_no_pins_is_caught(self):
        text = "#define GYRO_1_SPI_INSTANCE          SPI1\n"
        self.assertEqual(analysis.instances_without_pins(fake_run(text)),
                         ["GYRO_1_SPI_INSTANCE"])


class FirmwareValidationTests(unittest.TestCase):
    def test_a_uart_pin_the_firmware_does_not_have_is_caught(self):
        text = "#define UART1_TX_PIN        PA10\n"      # PA10 is UART1 rx
        self.assertEqual(analysis.unvalidated_pin_defines(fake_run(text)),
                         ["UART1_TX_PIN"])

    def test_a_correct_uart_pin_passes(self):
        text = "#define UART1_TX_PIN        PA9\n"
        self.assertEqual(analysis.unvalidated_pin_defines(fake_run(text)), [])

    def test_miso_and_mosi_swapped_is_caught(self):
        text = ("#define SPI1_SCK_PIN        PA5\n"
                "#define SPI1_SDI_PIN        PA7\n"
                "#define SPI1_SDO_PIN        PA6\n")
        self.assertEqual(analysis.unvalidated_pin_defines(fake_run(text)),
                         ["SPI1_SDI_PIN", "SPI1_SDO_PIN"])

    def test_an_adc_pin_the_chosen_instance_cannot_read_is_caught(self):
        text = ("#define ADC_VBAT_PIN        PC3\n"
                "#define ADC_INSTANCE                 ADC1\n")   # PC3 is ADC3 only
        self.assertEqual(analysis.unvalidated_pin_defines(fake_run(text)),
                         ["ADC_VBAT_PIN"])

    def test_an_occurrence_past_the_end_of_the_firmware_list_is_caught(self):
        text = ("#define MOTOR1_PIN          PC6\n"
                "#define TIMER_PIN_MAPPING \\\n"
                "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 4, 0)\n")
        run = fake_run(text)
        self.assertEqual(analysis.unvalidated_pin_defines(run), ["MOTOR1_PIN"])
        self.assertTrue(analysis.timer_occurrence_errors(run))

    def test_an_occurrence_selecting_a_different_channel_is_caught(self):
        text = ("#define MOTOR1_PIN          PC6\n"
                "#define TIMER_PIN_MAPPING \\\n"
                "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 1, 0)\n")
        run = fake_run(text, timers=[{"label": "MOTOR1_PIN", "pin": "PC6",
                                      "channel": "TIM8_CH1"}])
        # Occurrence 1 on PC6 is TIM3_CH1, not the TIM8_CH1 that was chosen.
        self.assertTrue(analysis.timer_occurrence_errors(run))


class SpiConflictTests(unittest.TestCase):
    TEXT = ("#define SPI1_SCK_PIN        PA5\n"
            "#define SPI1_SDI_PIN        PA6\n"
            "#define SPI1_SDO_PIN        PA7\n"
            "#define GYRO_1_SPI_INSTANCE          SPI1\n"
            "#define FLASH_SPI_INSTANCE           SPI1\n")

    def test_two_devices_on_the_same_wires_are_fine(self):
        links = [link("GYRO-SCK", "PA5"), link("GYRO-MISO", "PA6"),
                 link("GYRO-MOSI", "PA7"), link("FLASH-SCK", "PA5"),
                 link("FLASH-MISO", "PA6"), link("FLASH-MOSI", "PA7")]
        self.assertEqual(analysis.spi_conflicts(fake_run(self.TEXT, links=links)), [])

    def test_two_devices_with_different_wires_on_one_instance_are_caught(self):
        links = [link("GYRO-SCK", "PA5"), link("GYRO-MISO", "PA6"),
                 link("GYRO-MOSI", "PA7"), link("FLASH-SCK", "PB3"),
                 link("FLASH-MISO", "PB4"), link("FLASH-MOSI", "PB5")]
        found = analysis.spi_conflicts(fake_run(self.TEXT, links=links))
        self.assertTrue(any("different data pins" in f for f in found), found)

    def test_a_device_on_a_bus_its_pins_cannot_reach_is_caught(self):
        text = ("#define SPI2_SCK_PIN        PA5\n"
                "#define GYRO_1_SPI_INSTANCE          SPI2\n")
        links = [link("GYRO-SCK", "PA5"), link("GYRO-MISO", "PA6"),
                 link("GYRO-MOSI", "PA7")]
        found = analysis.spi_conflicts(fake_run(text, links=links))
        self.assertTrue(any("no SPI2 sck function" in f for f in found), found)


class DmaTests(unittest.TestCase):
    MAP = ("#define MOTOR1_PIN          PC6\n"
           "#define MOTOR2_PIN          PC7\n"
           "#define TIMER_PIN_MAPPING \\\n"
           "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 2, {a}) \\\n"
           "    TIMER_PIN_MAP( 1, MOTOR2_PIN, 2, {b})\n")

    def test_on_a_dmamux_part_two_users_may_not_share_an_option(self):
        run = fake_run(self.MAP.format(a=0, b=0), style="mux")
        self.assertTrue(analysis.dma_conflicts(run))
        run = fake_run(self.MAP.format(a=0, b=1), style="mux")
        self.assertEqual(analysis.dma_conflicts(run), [])

    def test_on_a_dmamux_part_an_option_past_the_channel_table_is_caught(self):
        run = fake_run(self.MAP.format(a=0, b=99), style="mux")
        self.assertTrue(any("beyond" in f for f in analysis.dma_conflicts(run)))

    def test_on_a_fixed_mapping_part_repeats_are_correct(self):
        """Option 0 on two timer channels is two different streams there."""
        run = fake_run(self.MAP.format(a=0, b=0), style="fixed")
        self.assertEqual(analysis.dma_conflicts(run), [])

    def test_an_adc_landing_on_a_stream_a_timer_holds_is_caught(self):
        text = ("#define MOTOR1_PIN          PA8\n"
                "#define TIMER_PIN_MAPPING \\\n"
                "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 2, 0)\n"
                "#define ADC1_DMA_OPT                 0\n")
        # PA8 occurrence 2 is TIM4_CH1, whose option 0 is DMA1_S0 - and ADC1's
        # option 0 is the same stream.
        found = analysis.dma_conflicts(fake_run(text, style="fixed"))
        self.assertTrue(any("already used by" in f for f in found), found)

    def test_an_adc_that_dodges_the_timer_streams_is_clean(self):
        text = ("#define MOTOR1_PIN          PA8\n"
                "#define TIMER_PIN_MAPPING \\\n"
                "    TIMER_PIN_MAP( 0, MOTOR1_PIN, 2, 0)\n"
                "#define ADC1_DMA_OPT                 1\n")
        self.assertEqual(analysis.dma_conflicts(fake_run(text, style="fixed")), [])

    def test_an_option_that_does_not_exist_at_all_is_caught(self):
        text = "#define ADC1_DMA_OPT                 7\n"
        found = analysis.dma_conflicts(fake_run(text, style="fixed"))
        self.assertTrue(any("not a valid option" in f for f in found), found)


if __name__ == "__main__":
    unittest.main()
