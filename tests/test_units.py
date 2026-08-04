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
The algorithms, on synthetic input.

Everything here runs from a made-up MCU and a made-up sheet, so it needs no
schematic and no Betaflight tree - and it is the part of the suite that survives
a refactor, because it pins down *why* each answer is right rather than
recording that today's answer was some particular string.

The synthetic MCU deliberately reproduces the awkward shapes the real ones have:
a pin trio valid on two SPI instances at once, a complementary timer channel
listed before the plain one, and an ADC whose first DMA option collides with a
timer's.
"""

import json
import re
import unittest

import support  # noqa: F401  (puts mcu-parser on sys.path)

import genconfig
import netmap
from netmap import Word


# --------------------------------------------------------------------------- #
# A synthetic MCU
# --------------------------------------------------------------------------- #

def fake_caps(style: str = "fixed") -> dict:
    caps = {
        "mcu": "FAKE32X100xx",
        "family": "FAKE32X1",
        "platform": "FAKE32",
        "timers": {
            # Every motor pin can do TIM3 or the advanced TIM8.
            "PC6": ["TIM3_CH1", "TIM8_CH1"],
            "PC7": ["TIM3_CH2", "TIM8_CH2"],
            "PC8": ["TIM3_CH3", "TIM8_CH3"],
            "PC9": ["TIM3_CH4", "TIM8_CH4"],
            # Complementary channel listed first, so occurrence 1 is the wrong
            # answer even though it is the obvious one.
            "PA8": ["TIM1_CH1N", "TIM4_CH1"],
            "PB0": ["TIM3_CH3"],
            # Spare capture-capable pins, for the timer inputs (PPM, escserial).
            "PA0": ["TIM2_CH1"],
            "PA1": ["TIM2_CH2"],
        },
        "uart": {
            "PA9": [{"dev": "UART1", "dir": "tx"}],
            "PA10": [{"dev": "UART1", "dir": "rx"}],
            "PB6": [{"dev": "UART1", "dir": "tx"}, {"dev": "UART5", "dir": "tx"}],
        },
        "spi": {
            # PA5/6/7 are SPI1 only; PB3/4/5 are SPI1 *and* SPI3. A greedy
            # per-device solver puts the flash on SPI1 and strands the gyro.
            "PA5": [{"dev": "SPI1", "role": "sck"}],
            "PA6": [{"dev": "SPI1", "role": "sdi"}],
            "PA7": [{"dev": "SPI1", "role": "sdo"}],
            "PB3": [{"dev": "SPI1", "role": "sck"}, {"dev": "SPI3", "role": "sck"}],
            "PB4": [{"dev": "SPI1", "role": "sdi"}, {"dev": "SPI3", "role": "sdi"}],
            "PB5": [{"dev": "SPI1", "role": "sdo"}, {"dev": "SPI3", "role": "sdo"}],
            "PB13": [{"dev": "SPI2", "role": "sck"}],
            "PB14": [{"dev": "SPI2", "role": "sdi"}],
            "PB15": [{"dev": "SPI2", "role": "sdo"}],
        },
        "i2c": {
            "PB8": [{"dev": "I2C1", "role": "scl"}],
            "PB9": [{"dev": "I2C1", "role": "sda"}],
            "PB10": [{"dev": "I2C2", "role": "scl"}],
        },
        "adc": {
            "PC1": {"devices": "123", "channel": "11"},
            "PC2": {"devices": "123", "channel": "12"},
            "PC3": {"devices": "3", "channel": "13"},
        },
        "dma": {
            "style": style,
            "mux_options": 16 if style == "mux" else 0,
            "timer": {} if style == "mux" else {
                "TIM8_CH1": ["DMA2_S2_C0", "DMA2_S2_C7"],
                "TIM8_CH2": ["DMA2_S2_C0", "DMA2_S3_C7"],
                "TIM4_CH1": ["DMA1_S0_C2"],
            },
            "peripheral": {} if style == "mux" else {
                "ADC1": ["DMA1_S0_C2", "DMA2_S4_C0"],
                "ADC2": ["DMA2_S2_C1"],
            },
        },
    }
    return caps


# --------------------------------------------------------------------------- #
# Net name -> meaning
# --------------------------------------------------------------------------- #

class ClassifyTests(unittest.TestCase):
    """genconfig.classify: which config.h role a net name asks for."""

    CASES = [
        ("MOTOR1", "motor", "1", None),
        ("M4", "motor", "4", None),
        ("S3", "motor", "3", None),
        ("SERVO2", "servo", "2", None),
        ("GYRO-CS", "gyro_cs", None, None),
        ("ICM-INT", "gyro_exti", None, None),
        ("GYRO-CLOCK", "gyro_clkin", None, None),
        ("GYRO-MISO", "gyro_spi", None, "sdi"),
        ("GYRO_MOSI", "gyro_spi", None, "sdo"),
        ("GYRO-SCLK", "gyro_spi", None, "sck"),
        # A second IMU. Sheets write the index on either side of the name and
        # both mean GYRO_2_*; index 1 is the first gyro, not a third device.
        ("GYRO2-CS", "gyro2_cs", None, None),
        ("GYRO-CS2", "gyro2_cs", None, None),
        ("IMU2_EXTI", "gyro2_exti", None, None),
        ("GYRO2-SCK", "gyro2_spi", None, "sck"),
        ("GYRO2-MISO", "gyro2_spi", None, "sdi"),
        ("GYRO1-CS", "gyro_cs", None, None),
        ("GYRO3-CS", "gyro3_cs", None, None),
        # Named after the part on the other end of the wire. The device is
        # recognised by family and the part number is *not* a device index -
        # MPU6000-CS is the first gyro's chip select, not the sixth's.
        ("MPU6000-CS", "gyro_cs", None, None),
        ("ICM42688-CS", "gyro_cs", None, None),
        ("ICM42688.SCK", "gyro_spi", None, "sck"),
        ("AT7456.CS", "osd_cs", None, None),
        ("W25Q128.MOSI", "flash_spi", None, "sdo"),
        ("BMP280-SCK", "baro_spi", None, "sck"),
        ("PPM", "rx_ppm", None, None),
        ("RX-PPM", "rx_ppm", None, None),
        ("PPM_IN", "rx_ppm", None, None),
        ("ESCSERIAL", "escserial", None, None),
        ("ESC-SERIAL", "escserial", None, None),
        ("SPI1_MOSI", "spi_bus", "1", "sdo"),
        ("OSD-CS", "osd_cs", None, None),
        ("AT7456-SCK", "osd_spi", None, "sck"),
        ("FLASH-CS", "flash_cs", None, None),
        ("BARO-CS", "baro_cs", None, None),
        ("SDCARD-CS", "sdcard_cs", None, None),
        ("SD-CS", "sdcard_cs", None, None),
        ("SDCARD-SCK", "sdcard_spi", None, "sck"),
        ("SD_MISO", "sdcard_spi", None, "sdi"),
        ("TX4", "uart_tx", "4", None),
        ("DJI-RX2", "uart_rx", "2", None),
        ("RX1-R", "uart_rx", "1", None),
        ("I2C1-SCL", "i2c_scl", "1", None),
        ("I2C2-SDA", "i2c_sda", "2", None),
        ("ADC-BATT", "adc_vbat", None, None),
        ("VBAT_ADC", "adc_vbat", None, None),
        ("ADC-CURR", "adc_curr", None, None),
        ("RSSI", "adc_rssi", None, None),
        ("LED-STATUS", "led0", None, None),
        ("LED1", "led1", None, None),
        ("LED-STRIP", "led_strip", None, None),
        ("WS2812", "led_strip", None, None),
        ("BEEPER-", "beeper", None, None),
        ("CAM-Controll", "camera_control", None, None),
        ("USB-DETECT", "usb_detect", None, None),
        ("VTX-SW", "pinio", None, None),
        # A trailing _SW/_EN is a switched rail, which is a PINIO output - and
        # must not be confused with CAM-Controll, which is a PWM line.
        ("CAM_SW", "pinio", None, None),
        ("BEC_EN", "pinio", None, None),
        ("USER1", "pinio", "1", None),
        ("FC-SWDIO", "ignore", None, None),
        ("BOOT", "ignore", None, None),
        ("OTG+", "ignore", None, None),
        # No rule: reported as a note, never guessed at.
        ("DP", None, None, None),
        ("TYPEC-304A-16L", None, None, None),
    ]

    def test_roles(self):
        for net, role, idx, sub in self.CASES:
            with self.subTest(net=net):
                self.assertEqual(genconfig.classify(net), (role, idx, sub))

    def test_a_part_number_is_never_a_device_index(self):
        # The danger in naming a net after its part: MPU6000 and ICM42688 carry
        # digits, and reading those as the index would invent a sixth or fourth
        # IMU. Every one of these is the *first* gyro.
        for net in ("MPU6000-CS", "ICM42688-CS", "ICM42688.SCK", "BMI270_CS"):
            with self.subTest(net=net):
                role, idx, _ = genconfig.classify(net)
                self.assertTrue(role.startswith("gyro"), role)
                self.assertNotIn("2", role.replace("gyro", "", 1) or "1")
                self.assertIsNone(idx)

    def test_a_bare_part_marking_is_not_a_net(self):
        # detect_parts reads those off the sheet; only a part number with a
        # signal after it names a wire.
        for net in ("ICM42688", "MAX7456", "W25Q128", "BMP280"):
            with self.subTest(net=net):
                self.assertIsNone(genconfig.classify(net)[0])

    def test_motor_and_uart_do_not_collide(self):
        """VTX-SW is a rail switch, not UART SW; S1 is a motor, not a signal."""
        self.assertEqual(genconfig.classify("VTX-SW")[0], "pinio")
        self.assertEqual(genconfig.classify("S1")[0], "motor")

    def test_a_uart_is_recognised_with_the_index_on_either_side(self):
        # TX4 and UART4_TX are both ordinary spellings and appear on comparable
        # numbers of boards. Only the first was recognised, so on two thirds of
        # the corpus the UART nets reached the config as unclassified and the
        # generated file had no serial ports at all.
        for net, want in (("TX4", ("uart_tx", "4")), ("RX1", ("uart_rx", "1")),
                          ("UART-TX4", ("uart_tx", "4")),
                          ("UART4_TX", ("uart_tx", "4")),
                          ("UART7_RX", ("uart_rx", "7")),
                          ("USART3_RX", ("uart_rx", "3")),
                          ("USART2_TX", ("uart_tx", "2"))):
            with self.subTest(net=net):
                role, idx, _ = genconfig.classify(net)
                self.assertEqual((role, idx), want)

    def test_betaflights_own_define_names_are_accepted_as_net_names(self):
        # Vendors copy the config.h names onto their nets, which puts the index
        # between separators. The older patterns allowed GYRO1-CS but not
        # GYRO_1_CS, so boards written that way lost every device they named.
        for net, want in (("GYRO_1_CS", "gyro_cs"), ("GYRO_2_CS", "gyro2_cs"),
                          ("GYRO_1_EXTI", "gyro_exti"),
                          ("GYRO_1_CLKIN", "gyro_clkin"),
                          ("MAX7456_SPI_CS", "osd_cs"),
                          ("FLASH_SPI_CS", "flash_cs"),
                          ("BARO_SPI_CS", "baro_cs")):
            with self.subTest(net=net):
                self.assertEqual(genconfig.classify(net)[0], want)

    def test_a_bus_net_with_no_separator_at_all(self):
        # SPI1CLK / SPI1MISO. Three boards write it this way and resolved no
        # SPI bus at all, which left every device on them unplaceable.
        for net, want in (("SPI1CLK", ("1", "sck")), ("SPI1MISO", ("1", "sdi")),
                          ("SPI2MOSI", ("2", "sdo"))):
            with self.subTest(net=net):
                role, idx, sub = genconfig.classify(net)
                self.assertEqual((role, idx, sub), ("spi_bus", *want))

    def test_the_bus_digit_can_stand_for_the_whole_name(self):
        for net, want in (("1-SCK", ("1", "sck")), ("2-MISO", ("2", "sdi")),
                          ("3-MOSI", ("3", "sdo"))):
            with self.subTest(net=net):
                role, idx, sub = genconfig.classify(net)
                self.assertEqual((role, idx, sub), ("spi_bus", *want))

    def test_clk_means_the_spi_clock_unless_it_is_the_card(self):
        # The SDMMC clock line is CK; everywhere else CLK is SCK. One
        # normalisation for both would have put an SD card's clock on a bus.
        self.assertEqual(genconfig.classify("SPI1CLK")[2], "sck")
        self.assertEqual(genconfig.classify("SD_CLK")[2], "ck")
        self.assertEqual(genconfig.classify("SDIO_CK")[2], "ck")

    def test_a_net_naming_both_bus_and_device_keeps_the_device(self):
        # SPI3-FLASH_SCK is the most informative spelling a sheet can use, and
        # matched neither the bus rule nor the device rule. The bus number in it
        # is redundant - the instance comes from the pins through the firmware
        # map - so what matters is that the device is recovered.
        for net, want in (("SPI3-FLASH_SCK", ("flash_spi", "sck")),
                          ("SPI2_OSD_SCK", ("osd_spi", "sck")),
                          ("SPI1-ICM1_MOSI", ("gyro_spi", "sdo")),
                          ("SPI4-ICM2_SCK", ("gyro2_spi", "sck")),
                          ("SPI2-BARO_MISO", ("baro_spi", "sdi"))):
            with self.subTest(net=net):
                role, _, sub = genconfig.classify(net)
                self.assertEqual((role, sub), want)

    def test_a_bus_net_is_recognised_with_the_index_on_either_side(self):
        for net, want in (("SPI3_SCK", ("3", "sck")), ("SCK3", ("3", "sck")),
                          ("MISO3", ("3", "sdi")), ("MOSI3", ("3", "sdo")),
                          ("SPI1_MISO", ("1", "sdi"))):
            with self.subTest(net=net):
                role, idx, sub = genconfig.classify(net)
                self.assertEqual(role, "spi_bus")
                self.assertEqual((idx, sub), want)

    def test_an_i2c_net_need_not_carry_its_bus_number(self):
        # The bus is settled by the pins, not the name, so a bare SCL/SDA is
        # as usable as I2C1-SCL.
        for net, want in (("SCL", ("i2c_scl", None)), ("SDA", ("i2c_sda", None)),
                          ("SCL1", ("i2c_scl", "1")), ("SDA2", ("i2c_sda", "2")),
                          ("I2C_SCL", ("i2c_scl", None)),
                          ("I2C1-SCL", ("i2c_scl", "1")),
                          ("I2C2_SDA", ("i2c_sda", "2"))):
            with self.subTest(net=net):
                role, idx, _ = genconfig.classify(net)
                self.assertEqual((role, idx), want)

    def test_leds_are_numbered_as_the_sheet_numbers_them(self):
        for net, want in (("LED0", "led0"), ("LED1", "led1"), ("LED2", "led2"),
                          ("LED-1", "led1"), ("LED-2", "led2"),
                          ("LED-STATUS", "led0"), ("LED-STRIP", "led_strip")):
            with self.subTest(net=net):
                self.assertEqual(genconfig.classify(net)[0], want)

    def test_an_sd_card_on_the_sdmmc_controller_is_recognised(self):
        # Every card-carrying board in the corpus wires it this way rather than
        # over SPI, and none of these spellings was matched before. The digit is
        # the controller - SDMMC2 means SDIO_DEVICE SDIODEV_2 - not a line
        # number, which D0..D3 carry separately.
        for net, want in (("SDMMC1-CK", ("1", "ck")), ("SDMMC1-CMD", ("1", "cmd")),
                          ("SDMMC1-D0", ("1", "d0")), ("SDMMC2_D3", ("2", "d3")),
                          ("SDIO_CK", (None, "ck")), ("SDIO-D2", (None, "d2")),
                          ("SD_SDIO_CMD", (None, "cmd")), ("SD_CLK", (None, "ck")),
                          ("SD_CMD", (None, "cmd")), ("SD_D0", (None, "d0"))):
            with self.subTest(net=net):
                role, idx, sub = genconfig.classify(net)
                self.assertEqual((role, idx, sub), ("sdio", *want))

    def test_the_spi_wired_card_still_classifies_as_spi(self):
        # SD-MISO is a card on a SPI bus, not an SDMMC line.
        self.assertEqual(genconfig.classify("SD-MISO")[0], "sdcard_spi")
        self.assertEqual(genconfig.classify("SDCARD_SCK")[0], "sdcard_spi")
        self.assertEqual(genconfig.classify("SDCARD-CS")[0], "sdcard_cs")

    def test_a_card_detect_switch_is_its_own_role(self):
        for net in ("SD_DETECT", "SD_DET", "SDCARD-DETECT", "SDDET"):
            with self.subTest(net=net):
                self.assertEqual(genconfig.classify(net)[0], "sdcard_detect")
        # and is not confused with the USB one
        self.assertEqual(genconfig.classify("USB_DETECT")[0], "usb_detect")

    def test_a_switched_rail_is_a_pinio_however_it_is_spelled(self):
        # One board writes BEC-SWITCH, which the abbreviated rule missed - so
        # only one of its two PINIOs was emitted, and it was the other one.
        for net in ("CAM_SW", "BEC-SWITCH", "VTX_EN", "BEC_ENABLE", "VTX-PWR",
                    "USER1", "PINIO2"):
            with self.subTest(net=net):
                self.assertEqual(genconfig.classify(net)[0], "pinio")

    def test_a_camera_control_line_is_not_a_pinio(self):
        # CAM-Controll is a PWM line to the camera's OSD, not a switched rail.
        self.assertEqual(genconfig.classify("CAM-CONTROL")[0], "camera_control")

    def test_a_beeper_keeps_its_role_with_a_PIN_suffix(self):
        for net in ("BEEPER", "BUZZER", "BEEPER_PIN", "BUZZER-"):
            with self.subTest(net=net):
                self.assertEqual(genconfig.classify(net)[0], "beeper")


class NetRequirementTests(unittest.TestCase):
    """netmap.net_requirement: what a net name says the pin must be able to do."""

    CASES = [
        ("TX4", ("uart_tx", "4")),
        ("DJI_RX2", ("uart_rx", "2")),
        ("UART3-TX", ("uart_tx", "3")),
        ("I2C1-SCL", ("i2c_scl", "1")),
        ("GYRO-SCK", ("spi_sck", None)),
        ("OSD-MISO", ("spi_data", None)),
        ("FLASH-MOSI", ("spi_data", None)),
        ("MOTOR3", ("timer", "3")),
        ("LED-STRIP", ("timer", None)),
        ("GYRO-CLOCK", ("timer", None)),
        ("ADC-BATT", ("adc", None)),
        # Nets that constrain nothing: a chip select or an LED can be any GPIO,
        # so they must not be scored as agreement.
        ("GYRO-CS", None),
        ("LED0", None),
        ("BEEPER-", None),
    ]

    def test_requirements(self):
        for net, want in self.CASES:
            with self.subTest(net=net):
                self.assertEqual(netmap.net_requirement(net), want)


class HandPlacedFunctionTests(unittest.TestCase):
    """
    genconfig._hand_placed: --set NAME=PIN, for what a sheet does not give.

    Routed through `classify` rather than a table of its own, so the accepted
    vocabulary is exactly the one the reader already understands and cannot
    drift from it. Validated against the firmware map exactly as a read net is:
    being told a pin by hand is not a reason to emit one the build will not
    honour.
    """

    def setUp(self):
        self.caps = fake_caps()

    def place(self, **kw):
        return genconfig._hand_placed(kw, self.caps)

    def test_a_function_is_named_as_the_sheet_would(self):
        links, keys = self.place(MOTOR6="PC6")
        self.assertEqual([(l.net, l.pin) for l in links], [("MOTOR6", "PC6")])
        self.assertIn(("motor", "6", None), keys)

    def test_the_config_h_spelling_is_accepted_too(self):
        # _PIN belongs to config.h, but it is the obvious thing to type.
        links, _ = self.place(MOTOR6_PIN="PC6")
        self.assertEqual([(l.net, l.pin) for l in links], [("MOTOR6", "PC6")])

    def test_the_name_is_case_and_space_insensitive(self):
        links, _ = self.place(**{" uart1_tx_pin ": " pa9 "})
        self.assertEqual([(l.net, l.pin) for l in links], [("UART1_TX", "PA9")])

    def test_a_pin_the_firmware_rejects_is_refused(self):
        # PA9 is UART1 TX; it is not UART5's.
        with self.assertRaises(SystemExit) as e:
            self.place(UART5_TX_PIN="PA9")
        self.assertIn("cannot do", str(e.exception))

    def test_an_any_gpio_role_needs_no_capability(self):
        # LEDs and chip selects have no requirement to check, so any pin goes.
        links, _ = self.place(LED0_PIN="PB0")
        self.assertEqual(links[0].pin, "PB0")

    def test_a_name_with_no_role_is_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.place(WIBBLE_PIN="PA9")
        self.assertIn("not a function this tool knows", str(e.exception))

    def test_a_value_that_is_not_a_pin_is_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.place(MOTOR1_PIN="17")
        self.assertIn("is not a pin name", str(e.exception))

    def test_nothing_supplied_places_nothing(self):
        self.assertEqual(self.place(), ([], set()))


class OwnCapabilityLabelTests(unittest.TestCase):
    """
    netmap._restates_the_pin. Sheets print a pin's own ADC channel beside it,
    nearer the symbol than the net label behind it, so the nearest-wins rule
    read the annotation as the net and the real one lost the row - one board's
    ADC_RSSI_PIN. It is settled against the capability map, not by shape: a
    board is free to *name* a net ADC1_8 and wire it somewhere else.
    """

    caps = {"adc": {"PC5": {"devices": "12", "channel": "8"},
                    "PC2": {"devices": "3", "channel": "0"}}}

    def row(self, pin):
        return netmap.PinRow(pin, 0.0, "R", [], True)

    def test_the_pins_own_channel_is_an_annotation(self):
        for text in ("ADC1_8", "ADC2_8", "ADC12_IN8", "adc1_inp8"):
            with self.subTest(text=text):
                self.assertTrue(
                    netmap._restates_the_pin(text, self.row("PC5"), self.caps))

    def test_another_pins_channel_is_not(self):
        # Same spelling, wrong pin: PC2 is ADC3 channel 0, so on that row the
        # label is saying something the pin is not, and it is a net name.
        self.assertFalse(
            netmap._restates_the_pin("ADC1_8", self.row("PC2"), self.caps))

    def test_the_wrong_channel_on_the_right_device_is_not(self):
        self.assertFalse(
            netmap._restates_the_pin("ADC1_7", self.row("PC5"), self.caps))

    def test_the_wrong_device_on_the_right_channel_is_not(self):
        self.assertFalse(
            netmap._restates_the_pin("ADC3_8", self.row("PC5"), self.caps))

    def test_a_pin_with_no_adc_entry(self):
        self.assertFalse(
            netmap._restates_the_pin("ADC1_8", self.row("PE4"), self.caps))

    def test_ordinary_net_names_are_untouched(self):
        for text in ("RSSI", "ADC_VBAT", "ADC_CURR", "ADC_RSSI", "MOTOR1"):
            with self.subTest(text=text):
                self.assertFalse(
                    netmap._restates_the_pin(text, self.row("PC5"), self.caps))


class PinSupportTests(unittest.TestCase):
    """netmap.pin_supports: the firmware's answer for a pin."""

    def setUp(self):
        self.caps = fake_caps()

    def test_uart_direction_and_instance(self):
        self.assertTrue(netmap.pin_supports(self.caps, "PA9", "uart_tx", "1"))
        self.assertFalse(netmap.pin_supports(self.caps, "PA9", "uart_rx", "1"))
        self.assertFalse(netmap.pin_supports(self.caps, "PA9", "uart_tx", "2"))
        self.assertTrue(netmap.pin_supports(self.caps, "PB6", "uart_tx", "5"))

    def test_spi_roles(self):
        # SCK is unambiguous and still checked strictly.
        self.assertTrue(netmap.pin_supports(self.caps, "PA5", "spi_sck", None))
        self.assertFalse(netmap.pin_supports(self.caps, "PA6", "spi_sck", None))
        # A data line is only checked for being a data line. MISO/MOSI state a
        # direction from somebody's point of view and vendors disagree about
        # whose, so which line it actually is comes from the firmware map in
        # genconfig, not from the label.
        self.assertTrue(netmap.pin_supports(self.caps, "PA6", "spi_data", None))
        self.assertTrue(netmap.pin_supports(self.caps, "PA7", "spi_data", None))
        self.assertFalse(netmap.pin_supports(self.caps, "PA5", "spi_data", None))

    def test_unknown_pin_is_rejected(self):
        self.assertFalse(netmap.pin_supports(self.caps, "PZ9", "uart_tx", "1"))
        self.assertFalse(netmap.pin_supports(self.caps, "PC5", "timer", None))

    def test_a_net_with_no_requirement_is_never_rejected(self):
        self.assertTrue(netmap.pin_supports(self.caps, "PZ9", "gpio", None))


class SymbolAfTests(unittest.TestCase):
    """netmap.afs_support: the schematic symbol's own second opinion."""

    AFS = ["TIM2_CH1", "TIM5_CH1", "UART4_TX", "ADC123_IN0"]

    def test_symbol_backs_the_function(self):
        self.assertIs(netmap.afs_support(self.AFS, "timer", None), True)
        self.assertIs(netmap.afs_support(self.AFS, "adc", None), True)
        self.assertIs(netmap.afs_support(self.AFS, "uart_tx", "4"), True)

    def test_symbol_contradicts_the_function(self):
        self.assertIs(netmap.afs_support(self.AFS, "uart_tx", "1"), False)
        self.assertIs(netmap.afs_support(self.AFS, "uart_rx", "4"), False)
        self.assertIs(netmap.afs_support(self.AFS, "spi_sck", None), False)

    def test_no_af_list_means_no_opinion(self):
        """None, not False - the difference decides whether a firmware PR is due."""
        self.assertIsNone(netmap.afs_support([], "uart_tx", "4"))

    def test_separator_spelling_is_irrelevant(self):
        self.assertIs(netmap.afs_support(["USART6-TX"], "uart_tx", "6"), True)
        self.assertIs(netmap.afs_support(["LPUART1_RX"], "uart_rx", "1"), True)


# --------------------------------------------------------------------------- #
# Bus, timer and DMA inference
# --------------------------------------------------------------------------- #

class SpiSolverTests(unittest.TestCase):
    """
    genconfig.assign_spi_buses.

    The reason this is solved globally rather than per device: PB3/4/5 are valid
    SPI1 pins as well as SPI3, so a flash chip there looks like SPI1 in
    isolation and takes the bus away from a gyro on PA5/6/7 that has nowhere
    else to go.
    """

    def setUp(self):
        self.caps = fake_caps()

    def test_contended_bus_goes_to_the_device_that_has_no_alternative(self):
        groups = {
            "gyro": {"sck": "PA5", "sdi": "PA6", "sdo": "PA7", "cs": "PA4"},
            "flash": {"sck": "PB3", "sdi": "PB4", "sdo": "PB5", "cs": "PA15"},
        }
        assigned, _ = genconfig.assign_spi_buses(self.caps, groups)
        self.assertEqual(assigned, {"gyro": "SPI1", "flash": "SPI3"})

    def test_devices_on_the_same_wires_share_one_instance(self):
        groups = {
            "osd": {"sck": "PB13", "sdi": "PB14", "sdo": "PB15", "cs": "PB12"},
            "baro": {"sck": "PB13", "sdi": "PB14", "sdo": "PB15", "cs": "PB10"},
        }
        assigned, _ = genconfig.assign_spi_buses(self.caps, groups)
        self.assertEqual(assigned, {"osd": "SPI2", "baro": "SPI2"})

    def test_a_cs_only_device_is_not_placed_on_a_bus(self):
        groups = {"sdcard": {"cs": "PB12"}}
        assigned, _ = genconfig.assign_spi_buses(self.caps, groups)
        self.assertEqual(assigned, {})

    def test_an_impossible_layout_is_reported_not_silently_solved(self):
        """Two devices needing SPI1 on different pins cannot both be right."""
        groups = {
            "gyro": {"sck": "PA5", "sdi": "PA6", "sdo": "PA7"},
            "osd": {"sck": "PA5", "sdi": "PA6", "sdo": "PA7", "cs": "PB12"},
        }
        # Same data pins, so this one is legal - they share SPI1.
        assigned, notes = genconfig.assign_spi_buses(self.caps, groups)
        self.assertEqual(set(assigned.values()), {"SPI1"})

        groups["osd"] = {"sck": "PA5", "sdi": "PA6", "sdo": "PB5"}
        assigned, notes = genconfig.assign_spi_buses(self.caps, groups)
        self.assertTrue(any("no conflict-free SPI assignment" in n for n in notes),
                        notes)

    def test_a_pin_with_no_spi_function_is_reported(self):
        groups = {"gyro": {"sck": "PA5", "sdi": "PA6", "sdo": "PC13"}}
        _, notes = genconfig.assign_spi_buses(self.caps, groups)
        self.assertTrue(any("PC13" in n or "distinct role" in n for n in notes), notes)


class I2cDeviceBusTests(unittest.TestCase):
    """
    genconfig.i2c_bus_for: which I2C bus a part sits on.

    An I2C device has no chip select, so the trick that settles SPI does not
    apply - there is no per-device net to follow. The part itself is the anchor:
    a baro is drawn with its SCL and SDA beside it, and on a two-bus board those
    say which one.
    """

    def part(self, marking="DPS310", x=100.0, y=500.0, page=1):
        return genconfig.PartHit(marking, marking, True, x, y, page)

    def sheet(self, entries):
        return [Word(t, x, y, x + 24, y + 3, pg) for t, x, y, pg in entries]

    def test_a_part_among_one_buss_nets_is_traced_to_it(self):
        words = self.sheet([("I2C2_SCL", 105, 505, 1), ("I2C2_SDA", 105, 512, 1),
                            ("I2C1_SCL", 600, 505, 1), ("I2C1_SDA", 600, 512, 1)])
        dev, why = genconfig.i2c_bus_for(self.part(), words, [], ["I2C1", "I2C2"])
        self.assertEqual(dev, "I2C2")
        self.assertIn("I2C2", why)

    def test_one_bus_needs_no_decision(self):
        words = self.sheet([("I2C1_SCL", 105, 505, 1)])
        dev, why = genconfig.i2c_bus_for(self.part(), words, [], ["I2C1"])
        self.assertIsNone(dev)
        self.assertIsNone(why)

    def test_a_part_between_two_buses_is_refused(self):
        words = self.sheet([("I2C1_SCL", 105, 505, 1), ("I2C2_SCL", 108, 508, 1)])
        dev, why = genconfig.i2c_bus_for(self.part(), words, [], ["I2C1", "I2C2"])
        self.assertIsNone(dev)
        self.assertIn("sits between two I2C buses", why)

    def test_the_mcu_side_labels_are_not_the_evidence(self):
        words = self.sheet([("I2C2_SCL", 105, 505, 1), ("I2C2_SDA", 105, 512, 1)])
        dev, _ = genconfig.i2c_bus_for(self.part(), words, words, ["I2C1", "I2C2"])
        self.assertIsNone(dev)

    def test_a_bus_on_another_sheet_is_not_a_neighbour(self):
        words = self.sheet([("I2C1_SCL", 105, 505, 2), ("I2C2_SCL", 400, 505, 1),
                            ("I2C2_SDA", 400, 512, 1)])
        dev, _ = genconfig.i2c_bus_for(self.part(), words, [], ["I2C1", "I2C2"])
        self.assertEqual(dev, "I2C2")

    def test_the_index_is_read_on_either_side(self):
        # SCL2/SDA2 is the same bus as I2C2_SCL, as it is for the UARTs.
        words = self.sheet([("SCL2", 105, 505, 1), ("SDA2", 105, 512, 1),
                            ("SCL1", 600, 505, 1)])
        dev, _ = genconfig.i2c_bus_for(self.part(), words, [], ["I2C1", "I2C2"])
        self.assertEqual(dev, "I2C2")


class McuCrystalTests(unittest.TestCase):
    """
    genconfig.find_mcu_crystal: which crystal on the sheet clocks the MCU.

    Getting this wrong is not cosmetic. Omitting SYSTEM_HSE_MHZ is not "no
    HSE" - the Makefile defaults HSE_VALUE to 8 MHz - and naming the wrong
    crystal is worse: one board was emitting 27 MHz off the OSD's part, which
    STM32H5's clock setup rejects outright with an #error.
    """

    def sym(self, page=1, pages=1, pitch=4.0, top=100.0, bottom=330.0):
        rows = [netmap.PinRow("PA5", top, "L"), netmap.PinRow("PB3", bottom, "L")]
        part = netmap.SymbolPart(page, rows, 600.0, 700.0)
        return netmap.Symbol([part], pitch, pages)

    def sheet(self, xtals, page=1):
        """OSC pins at (600,150) and whatever crystals are asked for."""
        words = [Word("PH0/OSC_IN", 600, 150, 640, 153, page),
                 Word("PH1/OSC_OUT", 600, 160, 640, 163, page)]
        for text, x, y, pg in xtals:
            words.append(Word(text, x, y, x + 30, y + 3, pg))
        return words

    def find(self, words, sym=None):
        return genconfig.find_mcu_crystal(words, sym or self.sym(), fake_caps())

    def test_a_crystal_beside_the_osc_pins_is_taken(self):
        c, why = self.find(self.sheet([("8MHz", 650, 155, 1)]))
        self.assertEqual(c.mhz, 8)

    def test_the_osd_crystal_is_not_taken_on_proximity(self):
        # 27 MHz is the MAX7456's and is the HSE of no board in the config repo.
        # Drawn right at the OSC pins it would otherwise win outright.
        c, why = self.find(self.sheet([("27MHz", 650, 155, 1)]))
        self.assertIsNone(c)
        self.assertIn("27 MHz is the OSD's", " ".join(why))

    def test_an_osc_net_label_still_wins_for_any_frequency(self):
        # Direct evidence beats the prior: if the sheet ties that crystal to the
        # MCU's OSC net by name, it is the HSE whatever its frequency.
        words = self.sheet([("27MHz", 300, 400, 1)])
        words += [Word("OSC_IN", 620, 152, 650, 155, 1),
                  Word("OSCI", 300, 405, 330, 408, 1)]
        c, _ = self.find(words)
        self.assertIsNotNone(c)
        self.assertEqual(c.mhz, 27)

    def test_a_crystal_on_another_sheet_is_not_measured_by_distance(self):
        # Sheets share a coordinate space, so a cross-page gap is meaningless.
        # This is what put a 27 MHz part 145pt from an H5's OSC pins.
        c, why = self.find(self.sheet([("25MHz", 650, 155, 2)]))
        self.assertIsNone(c)
        self.assertIn("another sheet", " ".join(why))

    def test_the_window_follows_the_symbol_not_the_row_pitch(self):
        # Row pitch runs from 1.4pt to 18pt across the corpus, so a pitch-based
        # window was 43pt on one sheet and 540pt on another. This crystal is
        # 200pt out on a fine-pitch sheet and is genuinely the MCU's.
        sym = self.sym(pitch=1.43, top=100.0, bottom=330.0)
        c, _ = self.find(self.sheet([("8MHz", 600, 355, 1)]), sym)
        self.assertIsNotNone(c)
        self.assertEqual(c.mhz, 8)

    def test_two_equally_close_candidates_are_refused(self):
        c, why = self.find(self.sheet([("8MHz", 650, 155, 1), ("25MHz", 655, 158, 1)]))
        self.assertIsNone(c)
        self.assertIn("equally close", " ".join(why))

    def test_a_crystal_far_beyond_the_symbol_is_refused(self):
        c, why = self.find(self.sheet([("8MHz", 600, 1400, 1)]))
        self.assertIsNone(c)
        self.assertIn("symbol's own size", " ".join(why))


class BeeperDriverTests(unittest.TestCase):
    """
    genconfig.beeper_driver: the transistor on the beeper net, if drawn.

    Corroboration only. 554 of the 582 corpus configs that drive a beeper set
    BEEPER_INVERTED, so it is emitted either way; a sheet that does not show a
    transistor has not shown that there is none.
    """

    def test_a_transistor_on_the_net_is_reported(self):
        words = [Word("BEEPER", 100, 300, 130, 302),
                 Word("Q1", 110, 292, 118, 294)]
        self.assertEqual(genconfig.beeper_driver(words, [], "BEEPER", 5.0), "Q1")

    def test_a_distant_transistor_is_not_on_this_net(self):
        words = [Word("BEEPER", 100, 300, 130, 302),
                 Word("Q1", 100, 900, 108, 902)]
        self.assertIsNone(genconfig.beeper_driver(words, [], "BEEPER", 5.0))

    def test_the_mcu_side_label_is_not_the_evidence(self):
        words = [Word("BEEPER", 100, 300, 130, 302),
                 Word("Q1", 110, 292, 118, 294)]
        self.assertIsNone(genconfig.beeper_driver(words, words, "BEEPER", 5.0))


class ResistorValueTests(unittest.TestCase):
    """genconfig.resistor_ohms: the spellings a resistor value arrives in."""

    def test_values(self):
        for text, ohms in (("100K", 100e3), ("10k", 10e3), ("1M", 1e6),
                           ("100R", 100.0), ("13.7K", 13.7e3), ("100kΩ", 100e3),
                           ("4K7", 4700.0), ("0R", 0.0)):
            with self.subTest(text=text):
                self.assertEqual(genconfig.resistor_ohms(text), ohms)

    def test_the_suffix_can_carry_the_decimal_point(self):
        # 4K7 is 4.7k, not 4k. Reading it as 4k turns a 22:1 divider into 26:1.
        self.assertEqual(genconfig.resistor_ohms("4K7"),
                         genconfig.resistor_ohms("4.7K"))

    def test_non_values(self):
        for text in ("R25", "0402", "GND", "ADC_BATT", "C29", "10"):
            with self.subTest(text=text):
                self.assertIsNone(genconfig.resistor_ohms(text))


class VbatDividerTests(unittest.TestCase):
    """
    genconfig.read_vbat_divider: DEFAULT_VOLTAGE_METER_SCALE off the sheet.

    voltage.c makes the scale exactly 10 * (Rtop + Rbottom) / Rbottom, so the
    divider on the schematic *is* the define. The firmware default of 110 is
    right only for an 11:1 divider; on any other board leaving it produces a
    silently wrong battery voltage.
    """

    PITCH = 5.0

    def sheet(self, top="100K", bottom="10K", *, leg=200.0, supply="VBAT",
              gnd=True, extra=()):
        """A divider drawn as one vertical leg through the ADC node."""
        y = 400.0
        words = [Word("ADC_BATT", leg - 40, y, leg - 10, y + 2)]
        if supply:
            words.append(Word(supply, leg, y - 44, leg + 16, y - 42))
        words += [Word("R18", leg, y - 22, leg + 10, y - 20),
                  Word(top, leg, y - 16, leg + 16, y - 14),
                  Word("R25", leg, y + 16, leg + 10, y + 18),
                  Word(bottom, leg, y + 22, leg + 16, y + 24)]
        if gnd:
            words.append(Word("GND", leg, y + 40, leg + 12, y + 42))
        return words + list(extra)

    def read(self, words):
        return genconfig.read_vbat_divider(words, [], "ADC_BATT", self.PITCH)

    def test_the_default_divider_gives_the_firmware_default(self):
        self.assertEqual(self.read(self.sheet("100K", "10K"))[0], 110)

    def test_a_divider_that_is_not_the_default_is_what_matters(self):
        # These are the boards the firmware default is wrong for.
        self.assertEqual(self.read(self.sheet("20K", "1K"))[0], 210)
        self.assertEqual(self.read(self.sheet("150K", "10K"))[0], 160)

    def test_either_anchor_alone_is_enough(self):
        # One board labels its supply and draws ground as a bare symbol.
        self.assertEqual(self.read(self.sheet(gnd=False))[0], 110)
        self.assertEqual(self.read(self.sheet(supply=None))[0], 110)

    def test_with_no_anchor_at_all_it_declines(self):
        scale, why = self.read(self.sheet(supply=None, gnd=False))
        self.assertIsNone(scale)
        self.assertIn("no resistor pair", why)

    def test_a_resistor_from_a_neighbouring_circuit_is_not_joined_in(self):
        # A third resistor within reach but on its own leg. Taking "one above,
        # one below" without requiring a shared leg found two above and either
        # gave up or picked the wrong pair.
        stray = [Word("R53", 130.0, 380.0, 140.0, 382.0),
                 Word("47K", 130.0, 386.0, 146.0, 388.0)]
        self.assertEqual(self.read(self.sheet(extra=stray))[0], 110)

    def test_an_upside_down_reading_is_not_emitted(self):
        # 10K over 100K is 1.1:1 - scale 11, below anything a battery sense
        # uses, so it is refused rather than emitted.
        scale, why = self.read(self.sheet("10K", "100K"))
        self.assertIsNone(scale)
        self.assertIn("not a usable scale", why)

    def test_a_scale_beyond_the_firmware_field_is_refused(self):
        # vbatscale is a uint8_t in voltage.h.
        scale, _ = self.read(self.sheet("1M", "1K"))
        self.assertIsNone(scale)

    def test_designators_without_values_decline_with_a_reason(self):
        words = [Word("ADC_BATT", 160.0, 400.0, 190.0, 402.0),
                 Word("VBAT", 200.0, 356.0, 216.0, 358.0),
                 Word("R18", 200.0, 378.0, 210.0, 380.0),
                 Word("R25", 200.0, 416.0, 210.0, 418.0),
                 Word("GND", 200.0, 440.0, 212.0, 442.0)]
        scale, why = self.read(words)
        self.assertIsNone(scale)
        self.assertIn("designators without the values", why)

    def test_a_net_drawn_only_at_the_mcu_says_so(self):
        words = self.sheet()
        scale, why = genconfig.read_vbat_divider(words, words, "ADC_BATT", self.PITCH)
        self.assertIsNone(scale)
        self.assertIn("not drawn anywhere but the MCU", why)


class TraceCsBusTests(unittest.TestCase):
    """
    genconfig.trace_cs_bus: which bus a chip-select-only device joins.

    A sheet that names its buses generically (SPI2-SCK, not OSD-SCK) says at the
    MCU only that some buses exist and some device has a chip select. Which go
    together is drawn at the device, where the labels appear a second time.
    """

    BUSES = {"SPI1": {"sck": "PA5", "sdi": "PA6", "sdo": "PA7"},
             "SPI2": {"sck": "PB13", "sdi": "PB14", "sdo": "PB15"}}
    PITCH = 4.0

    def device(self, x, y, bus, cs=None):
        """One part's worth of labels: its three bus lines, optionally a CS."""
        out = [Word(f"{bus}-SCK", x, y, x + 20, y + 2),
               Word(f"{bus}-MISO", x, y + 8, x + 20, y + 10),
               Word(f"{bus}-MOSI", x, y + 16, x + 20, y + 18)]
        if cs:
            out.append(Word(cs, x, y - 8, x + 20, y - 6))
        return out

    def test_a_cs_among_one_buss_lines_is_traced_to_it(self):
        words = self.device(100, 500, "SPI1", cs="GYRO_CS") + self.device(600, 500, "SPI2")
        bus, note = genconfig.trace_cs_bus(words, [], "GYRO_CS", self.BUSES, self.PITCH)
        self.assertEqual(bus, "SPI1")
        self.assertIn("SPI1", note)

    def test_a_second_bus_just_as_close_is_refused(self):
        # Two parts drawn side by side: nothing distinguishes them, and a wrong
        # bus is worse than none.
        words = (self.device(100, 500, "SPI1", cs="GYRO_CS")
                 + self.device(118, 500, "SPI2"))
        bus, note = genconfig.trace_cs_bus(words, [], "GYRO_CS", self.BUSES, self.PITCH)
        self.assertIsNone(bus)
        self.assertIn("cannot be told", note)

    def test_a_part_with_its_pins_spread_around_it_still_traces(self):
        # A flash chip is drawn as a large symbol: its CS and one data line sit
        # together while the other two come off the far side. They are still all
        # nearer than anything on another bus, which is the thing that decides
        # it - requiring them to cluster would leave this board to a reviewer.
        words = [Word("FLASH_CS", 100, 500, 130, 502),
                 Word("SPI1-MISO", 100, 505, 130, 507),
                 Word("SPI1-SCK", 100, 630, 130, 632),
                 Word("SPI1-MOSI", 100, 631, 130, 633),
                 Word("SPI2-SCK", 100, 800, 130, 802),
                 Word("SPI2-MISO", 100, 805, 130, 807),
                 Word("SPI2-MOSI", 100, 810, 130, 812)]
        bus, note = genconfig.trace_cs_bus(words, [], "FLASH_CS", self.BUSES,
                                           self.PITCH)
        self.assertEqual(bus, "SPI1")

    def test_a_spread_out_part_is_refused_when_a_rival_is_interleaved(self):
        # Same shape, but the other bus's lines fall between this one's. Nothing
        # then says which part the far labels belong to.
        words = [Word("FLASH_CS", 100, 500, 130, 502),
                 Word("SPI1-MISO", 100, 505, 130, 507),
                 Word("SPI1-SCK", 100, 700, 130, 702),
                 Word("SPI2-SCK", 100, 600, 130, 602),
                 Word("SPI2-MISO", 100, 605, 130, 607)]
        bus, note = genconfig.trace_cs_bus(words, [], "FLASH_CS", self.BUSES,
                                           self.PITCH)
        self.assertIsNone(bus)
        self.assertIn("not clear enough", note)

    def test_a_lone_line_is_not_a_bus_grouping(self):
        words = [Word("SPI1-SCK", 100, 500, 120, 502),
                 Word("GYRO_CS", 100, 492, 120, 494)]
        bus, note = genconfig.trace_cs_bus(words, [], "GYRO_CS", self.BUSES, self.PITCH)
        self.assertIsNone(bus)
        self.assertIn("not clear enough", note)

    def test_the_mcu_side_labels_are_not_the_evidence(self):
        # The same names appear in the MCU's own gutter. Reading those would
        # just measure how the pin rows happen to be ordered on the symbol.
        mcu = self.device(100, 500, "SPI1", cs="GYRO_CS")
        bus, note = genconfig.trace_cs_bus(mcu, mcu, "GYRO_CS", self.BUSES, self.PITCH)
        self.assertIsNone(bus)
        self.assertIsNone(note)

    def test_a_bus_the_mcu_never_named_is_not_a_candidate(self):
        words = self.device(100, 500, "SPI4", cs="GYRO_CS")
        bus, _ = genconfig.trace_cs_bus(words, [], "GYRO_CS", self.BUSES, self.PITCH)
        self.assertIsNone(bus)

    def test_labels_on_another_sheet_are_not_neighbours(self):
        words = (self.device(100, 500, "SPI1", cs="GYRO_CS")
                 + [Word("SPI2-SCK", 101, 500, 121, 502, 2)])
        bus, _ = genconfig.trace_cs_bus(words, [], "GYRO_CS", self.BUSES, self.PITCH)
        self.assertEqual(bus, "SPI1")


class I2cTests(unittest.TestCase):
    def setUp(self):
        self.caps = fake_caps()

    def test_bus_comes_from_the_pins_not_the_net_name(self):
        dev, notes = genconfig.infer_i2c_bus(self.caps, "PB8", "PB9", "1")
        self.assertEqual(dev, "I2C1")
        self.assertEqual(notes, [])

    def test_a_net_name_disagreeing_with_the_pins_is_reported(self):
        dev, notes = genconfig.infer_i2c_bus(self.caps, "PB8", "PB9", "2")
        self.assertEqual(dev, "I2C1")
        self.assertTrue(any("I2C2" in n for n in notes), notes)

    def test_pins_on_different_buses_yield_nothing(self):
        dev, notes = genconfig.infer_i2c_bus(self.caps, "PB10", "PB9", None)
        self.assertIsNone(dev)
        self.assertTrue(any("not on the same I2C bus" in n for n in notes), notes)


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.caps = fake_caps()

    def test_occurrence_is_the_1_based_index_into_the_firmware_list(self):
        self.assertEqual(genconfig.pick_timer(self.caps, "PC6", None, True),
                         (2, "TIM8_CH1", "inferred"))

    def test_a_schematic_annotation_wins(self):
        self.assertEqual(genconfig.pick_timer(self.caps, "PC6", "TIM3_CH1", True),
                         (1, "TIM3_CH1", "schematic"))

    def test_complementary_channels_are_avoided(self):
        self.assertEqual(genconfig.pick_timer(self.caps, "PA8", None, False),
                         (2, "TIM4_CH1", "inferred"))

    def test_a_pin_with_no_timer_returns_nothing(self):
        self.assertIsNone(genconfig.pick_timer(self.caps, "PA5", None, True))

    def test_motors_are_put_on_one_shared_advanced_timer(self):
        """All four on one timer is what keeps burst DShot available."""
        motors = {1: "PC6", 2: "PC7", 3: "PC8", 4: "PC9"}
        plan = genconfig.motor_timer_plan(self.caps, motors, {})
        self.assertEqual({pin: got[1] for pin, got in plan.items()},
                         {"PC6": "TIM8_CH1", "PC7": "TIM8_CH2",
                          "PC8": "TIM8_CH3", "PC9": "TIM8_CH4"})
        self.assertEqual({got[0] for got in plan.values()}, {2})

    def test_an_annotated_motor_overrides_the_shared_choice(self):
        motors = {1: "PC6", 2: "PC7"}
        plan = genconfig.motor_timer_plan(self.caps, motors, {"MOTOR1": "TIM3_CH1"})
        self.assertEqual(plan["PC6"], (1, "TIM3_CH1", "schematic"))
        self.assertEqual(plan["PC7"][1], "TIM8_CH2")


class AdcTests(unittest.TestCase):
    def test_fixed_mapping_dodges_a_stream_a_timer_already_holds(self):
        caps = fake_caps("fixed")
        dev, opt, notes = genconfig.choose_adc(caps, ["PC1", "PC2"], {"DMA1_S0"})
        self.assertEqual((dev, opt), ("ADC1", 1))
        self.assertEqual(notes, [])

    def test_fixed_mapping_takes_the_first_option_when_nothing_collides(self):
        caps = fake_caps("fixed")
        self.assertEqual(genconfig.choose_adc(caps, ["PC1"], set())[:2], ("ADC1", 0))

    def test_dmamux_continues_the_shared_numbering(self):
        """On a DMAMUX part the option indexes one shared table, so the ADC has
        to carry on past whatever the timers took."""
        caps = fake_caps("mux")
        self.assertEqual(genconfig.choose_adc(caps, ["PC1"], set(), mux_next=5)[:2],
                         ("ADC1", 5))

    def test_dmamux_runs_out_of_channels(self):
        caps = fake_caps("mux")
        dev, opt, notes = genconfig.choose_adc(caps, ["PC1"], set(), mux_next=16)
        self.assertIsNone(opt)
        self.assertTrue(any("no DMA channel left" in n for n in notes), notes)

    def test_an_instance_must_cover_every_adc_pin(self):
        caps = fake_caps("fixed")
        dev, opt, notes = genconfig.choose_adc(caps, ["PC1", "PC3"], set())
        self.assertEqual(dev, "ADC3")
        dev, opt, notes = genconfig.choose_adc(caps, ["PC1", "PB0"], set())
        self.assertTrue(any("not an ADC-capable pin" in n for n in notes), notes)

    def test_the_contended_resource_is_the_stream_not_the_channel(self):
        self.assertEqual(genconfig._stream_of("DMA2_S4_C7"), "DMA2_S4")


class DshotBurstTests(unittest.TestCase):
    """
    genconfig._note_dshot_burst.

    Burst DShot is reported, never chosen. `DEFAULT_DSHOT_BURST` is used by 51%
    of the corpus and the obvious rule - four motors on one timer, therefore
    DSHOT_DMAR_ON - does not survive the firmware: burst runs off TIMx_UP, whose
    DMA comes from the `upopt` argument of DEF_TIM(), which every STM32 timer
    table passes as 0. On a DMAMUX part that is one shared channel for every
    timer, and `TIMUPn_DMA_OPT` - the define that would move it - reaches only
    cli.c and the X32 platform, never the STM32 DShot driver.
    """

    def note(self, caps, motors):
        cfg = genconfig.Config()
        plan = genconfig.motor_timer_plan(caps, motors, {})
        genconfig._note_dshot_burst(cfg, caps, plan, "FAKE32X100")
        return " ".join(cfg.notes)

    def test_a_shared_timer_is_reported_but_never_emitted(self):
        for style in ("fixed", "mux"):
            with self.subTest(style=style):
                caps = fake_caps(style)
                note = self.note(caps, {1: "PC6", 2: "PC7", 3: "PC8", 4: "PC9"})
                self.assertIn("TIM8", note)
                self.assertIn("DSHOT_BURST", note.replace("DEFAULT_DSHOT_BURST",
                                                          "DSHOT_BURST"))

    def test_the_dmamux_reason_is_given_where_it_applies(self):
        mux = self.note(fake_caps("mux"), {1: "PC6", 2: "PC7", 3: "PC8", 4: "PC9"})
        self.assertIn("TIMUPn_DMA_OPT", mux)
        fixed = self.note(fake_caps("fixed"), {1: "PC6", 2: "PC7", 3: "PC8", 4: "PC9"})
        self.assertNotIn("TIMUPn_DMA_OPT", fixed)

    def test_motors_spread_over_two_timers_say_nothing(self):
        # PA8's only plain channel is TIM4_CH1, so this cannot be one timer.
        caps = fake_caps()
        self.assertEqual(self.note(caps, {1: "PC6", 2: "PA8"}), "")

    def test_a_single_motor_is_not_a_burst_candidate(self):
        self.assertEqual(self.note(fake_caps(), {1: "PC6"}), "")


# --------------------------------------------------------------------------- #
# Reading the sheet
# --------------------------------------------------------------------------- #

def w(text, x0, y0, width=8.0, height=3.0, page=1):
    return Word(text, x0, y0, x0 + width, y0 + height, page)


class ConnectorRoleTests(unittest.TestCase):
    """
    genconfig.read_connector_roles: what each UART is for, read off the header
    silkscreen. These are defaults for the user, so a wrong one is cosmetic -
    but a silently wrong one is not.
    """

    def test_a_named_header_names_its_uart(self):
        words = [w("J5", 200, 50, 6), w("GPS", 208, 50),
                 w("TX3", 205, 56), w("RX3", 205, 60)]
        roles, notes = genconfig.read_connector_roles(words)
        self.assertEqual(roles, {"GPS_UART": "3"})
        self.assertTrue(any("GPS_UART = UART3" in n for n in notes), notes)

    def test_a_full_duplex_port_beats_one_that_appears_one_way(self):
        """A DJI header carries its MSP link both ways plus an SBUS output."""
        words = [w("J4", 200, 50, 6), w("DJI", 208, 50),
                 w("TX2", 205, 56), w("RX2", 205, 60), w("RX6", 205, 64)]
        roles, _ = genconfig.read_connector_roles(words)
        self.assertEqual(roles, {"MSP_UART": "2"})

    def test_an_unlabelled_header_claims_nothing(self):
        words = [w("J9", 200, 50, 6), w("TX3", 205, 56), w("RX3", 205, 60)]
        self.assertEqual(genconfig.read_connector_roles(words)[0], {})

    def test_a_uart_far_from_every_header_is_not_attributed(self):
        words = [w("J5", 200, 50, 6), w("GPS", 208, 50),
                 w("TX3", 205, 400), w("RX3", 205, 404)]
        self.assertEqual(genconfig.read_connector_roles(words)[0], {})


class TimerHintTests(unittest.TestCase):
    def test_an_annotated_channel_is_read_off_the_sheet(self):
        words = [w("MOTOR1-TIM8", 100, 50, 20), w("CH1", 121, 50)]
        self.assertEqual(genconfig.read_timer_hints(words), {"MOTOR1": "TIM8_CH1"})

    def test_a_timer_without_a_channel_is_not_a_hint(self):
        words = [w("MOTOR1-TIM8", 100, 50, 20), w("100nF", 121, 50)]
        self.assertEqual(genconfig.read_timer_hints(words), {})


class PartDetectionTests(unittest.TestCase):
    """
    genconfig.detect_parts, against the real driver catalogue and aliases.

    Vendors fit alternates on one sheet so a PCB can be built several ways, and
    the firmware is expected to carry drivers for all of them.
    """

    def setUp(self):
        self.drivers = support.frozen_firmware()["drivers"]
        self.aliases = json.loads((support.MCU_PARSER / "data" / "aliases.json").read_text())

    def detect(self, *tokens):
        return genconfig.detect_parts([w(t, 10, 10 + i * 4) for i, t in enumerate(tokens)],
                                      self.drivers, self.aliases)

    def test_a_silkscreen_marking_maps_to_a_driver(self):
        found = self.detect("MPU-6000")
        self.assertEqual([h.driver for h in found["gyro"]], ["MPU6000"])
        self.assertTrue(found["gyro"][0].fitted)

    def test_an_alias_covers_a_marking_betaflight_spells_differently(self):
        found = self.detect("AT7456E", "W25Q128JVEIQ", "ICM42688")
        self.assertEqual([h.driver for h in found["osd"]], ["MAX7456"])
        self.assertEqual([h.driver for h in found["flash"]], ["W25Q128FV"])
        self.assertIn("ICM42688P", [h.driver for h in found["gyro"]])

    def test_a_not_fitted_alternate_is_found_but_marked(self):
        found = self.detect("MPU-6000", "ICM42688(NC)")
        fitted = {h.driver: h.fitted for h in found["gyro"]}
        self.assertEqual(fitted, {"MPU6000": True, "ICM42688P": False})
        # Fitted parts first, so the driver chosen for the bus is deterministic.
        self.assertTrue(found["gyro"][0].fitted)

    def test_detection_is_order_independent(self):
        a = self.detect("MPU-6000", "ICM42688(NC)", "AT7456E")
        b = self.detect("AT7456E", "ICM42688(NC)", "MPU-6000")
        self.assertEqual({k: [h.driver for h in v] for k, v in a.items()},
                         {k: [h.driver for h in v] for k, v in b.items()})


class TargetDetectionTests(unittest.TestCase):
    """
    netmap.detect_target: Betaflight names a target after one representative
    part, so an STM32G473 board has to find the STM32G474 target.
    """

    def test_an_exact_part_number_wins(self):
        data = {"targets": {"STM32F722": {}, "STM32F745": {}}}
        self.assertEqual(netmap.detect_target([w("STM32F722RET6", 0, 0)], data),
                         "STM32F722")

    def test_a_sibling_part_falls_back_to_the_only_target_in_its_family(self):
        data = {"targets": {"STM32G474": {}, "STM32F722": {}}}
        self.assertEqual(netmap.detect_target([w("STM32G473VET6", 0, 0)], data),
                         "STM32G474")

    def test_an_ambiguous_family_is_not_guessed(self):
        data = {"targets": {"STM32F722": {}, "STM32F745": {}}}
        self.assertIsNone(netmap.detect_target([w("STM32F767ZIT6", 0, 0)], data))

    def test_no_part_number_at_all(self):
        self.assertIsNone(netmap.detect_target([w("R21", 0, 0)], {"targets": {}}))

    def test_a_wildcard_digit_resolves_when_only_one_target_fits(self):
        # Vendors label the symbol with an X standing in for a digit, so one
        # sheet covers a whole line. STM32F7X2 can only be the F722 here.
        data = {"targets": {"STM32F722": {}, "STM32F745": {}}}
        self.assertEqual(netmap.detect_target([w("STM32F7X2RXT", 0, 0)], data),
                         "STM32F722")

    def test_an_ambiguous_wildcard_asks_rather_than_guesses(self):
        data = {"targets": {"STM32F722": {}, "STM32F732": {}}}
        self.assertIsNone(netmap.detect_target([w("STM32F7X2", 0, 0)], data))


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

class ClusterTests(unittest.TestCase):
    """
    netmap.cluster splits only where the gap exceeds the tolerance. Fixed-width
    buckets are not good enough: Altium's right-aligned pin names jitter by
    tenths of a point, and a boundary falling inside that jitter splits one
    symbol edge in two and loses pins.
    """

    def test_jitter_within_tolerance_stays_one_group(self):
        items = [(10.0, "a"), (10.4, "b"), (10.8, "c")]
        self.assertEqual(netmap.cluster(items, tol=1.0), [["a", "b", "c"]])

    def test_a_real_gap_splits(self):
        items = [(10.0, "a"), (10.4, "b"), (40.0, "c")]
        self.assertEqual(netmap.cluster(items, tol=1.0), [["a", "b"], ["c"]])

    def test_empty(self):
        self.assertEqual(netmap.cluster([], tol=1.0), [])


class EdgeHoleTests(unittest.TestCase):
    """
    netmap._fill_edge_holes. The edge cluster is 1pt wide and pdftotext's boxes
    are not that exact, so a name can sit just outside the column it belongs to
    and take its pin - and the SPI bus on that pin - with it. Widening the
    tolerance is not available: real columns come within 3.5pt of each other on
    these sheets. The run is what tells them apart, so only a hole is filled.
    """

    # 3.4pt per character, which is what these plots measure at; the tolerance
    # is derived from the names' own width, so it has to be proportional.
    @staticmethod
    def tagged(name, x0, y0):
        return (Word(name, x0, y0, x0 + len(name) * 3.4, y0 + 3), name, [], True)

    def column(self, x0=100.0, skip=()):
        return [self.tagged(f"PA{i}", x0, 100.0 + 8 * i)
                for i in range(6) if i not in skip]

    def test_a_name_nudged_off_the_column_is_taken_back(self):
        edge = self.column(skip=(3,))
        stray = self.tagged("PA3", 101.5, 124.0)
        out = netmap._fill_edge_holes(edge, [stray],
                                      lambda w: w.x0, lambda w: w.y0)
        self.assertIn(stray, out)

    def test_a_neighbouring_column_is_not_absorbed(self):
        # 3.5pt away, which is closer than some sheets put two real columns,
        # and it lines up with the hole. It is still a different column.
        edge = self.column(skip=(3,))
        other = self.tagged("PB3", 103.5, 124.0)
        out = netmap._fill_edge_holes(edge, [other],
                                      lambda w: w.x0, lambda w: w.y0)
        self.assertNotIn(other, out)

    def test_nothing_is_adopted_where_the_run_has_no_hole(self):
        # The safety property that makes this cheap: a complete edge cannot
        # gain rows, however close something sits to it.
        edge = self.column()
        stray = self.tagged("PA9", 100.4, 104.0)
        out = netmap._fill_edge_holes(edge, [stray],
                                      lambda w: w.x0, lambda w: w.y0)
        self.assertEqual(len(out), len(edge))

    def test_one_name_fills_one_slot(self):
        edge = self.column(skip=(2, 3))
        strays = [self.tagged("PA2", 101.2, 116.0), self.tagged("PA3", 101.2, 124.0)]
        out = netmap._fill_edge_holes(edge, strays,
                                      lambda w: w.x0, lambda w: w.y0)
        self.assertEqual(len(out), len(edge) + 2)
        self.assertEqual(len({id(t) for t in out}), len(out))


class AssemblePinNameTests(unittest.TestCase):
    """
    netmap.assemble_pin_names. A long AF list comes back from the extractor in
    pieces; the piece holding the PXn token then ends early and misses the
    right-hand edge cluster, which used to drop the whole row - pin and net
    together.
    """

    def test_pieces_are_rejoined_and_the_row_regains_its_true_extent(self):
        words = [Word("PC6/TIM3_CH1/TIM8_CH1/", 10, 100, 40, 103),
                 Word("USART6_TX", 40.5, 100, 55, 103)]
        got = netmap.assemble_pin_names(words)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "PC6/TIM3_CH1/TIM8_CH1/USART6_TX")
        self.assertEqual(got[0].x1, 55)

    def test_a_distant_word_is_not_absorbed(self):
        words = [Word("PC6", 10, 100, 20, 103),
                 Word("100nF", 40, 100, 55, 103)]
        self.assertEqual([g.text for g in netmap.assemble_pin_names(words)], ["PC6"])

    def test_only_pin_shaped_words_start_a_name(self):
        words = [Word("R21", 10, 100, 20, 103)]
        self.assertEqual(netmap.assemble_pin_names(words), [])

    def test_an_altium_annotation_token_is_not_absorbed(self):
        # Altium stamps PIU1014 / COU8 / NLTX2 beside everything it draws, and
        # places them closer than the gap between the pieces of a split name.
        # Absorbing one leaves 'PC0PIU108', which no longer reads as a pin, so
        # the row and its net were dropped with no sign anything was lost.
        for annot in ("PIU108", "PIC202", "COU8", "NLTX2"):
            with self.subTest(annot=annot):
                words = [Word("PC0", 147.3, 86.5, 152.3, 89.5),
                         Word(annot, 154.0, 86.5, 166.0, 89.5)]
                got = netmap.assemble_pin_names(words)
                self.assertEqual([g.text for g in got], ["PC0"])

    def test_a_real_alternate_function_starting_CO_is_still_absorbed(self):
        # COMP1_OUT is a genuine G4 alternate function; the annotation guard
        # spells out the designator letters rather than matching CO[A-Z]+.
        words = [Word("PA1/", 10, 100, 20, 103),
                 Word("COMP1_OUT", 20.5, 100, 40, 103)]
        got = netmap.assemble_pin_names(words)
        self.assertEqual([g.text for g in got], ["PA1/COMP1_OUT"])


class PinNameFormTests(unittest.TestCase):
    """netmap.PIN_RE: the spellings a pin name arrives in."""

    def test_a_bare_pin(self):
        self.assertEqual(netmap.PIN_RE.match("PB2").group(1), "PB2")

    def test_sts_own_dash_qualifier_is_part_of_the_name(self):
        # ST writes the pin's second fixed function after a dash. Rejecting the
        # whole token loses the row: PB8-BOOT0 is where G4 keeps BOOT0.
        for text, pin in (("PA0-WKUP", "PA0"), ("PC14-OSC32_IN", "PC14"),
                          ("PH0-OSC_IN", "PH0"), ("PB8-BOOT0", "PB8")):
            with self.subTest(text=text):
                m = netmap.PIN_RE.match(text)
                self.assertIsNotNone(m)
                self.assertEqual(m.group(1), pin)

    def test_a_qualifier_is_not_read_as_an_alternate_function(self):
        # It names a fixed function, not an AF, so it must not become evidence
        # that the pin supports something.
        self.assertIsNone(netmap.PIN_RE.match("PA0-WKUP").group(2))

    def test_an_af_list_still_parses(self):
        m = netmap.PIN_RE.match("PC6/TIM3_CH1/USART6_TX")
        self.assertEqual((m.group(1), m.group(2)), ("PC6", "TIM3_CH1/USART6_TX"))

    def test_sts_dual_pad_suffix_is_the_same_gpio(self):
        # H7 parts with the analog switch give the second pad its own name.
        # Betaflight knows one PC2, so the suffix comes off - and it has to be
        # recognised at all, because SPI2's SDI is on it on ten boards here.
        for text, pin in (("PA0_C", "PA0"), ("PA1_C", "PA1"), ("PC2_C", "PC2"),
                          ("PC3_C/ADC12_INP1", "PC3")):
            with self.subTest(text=text):
                m = netmap.PIN_RE.match(text)
                self.assertIsNotNone(m)
                self.assertEqual(m.group(1), pin)

    def test_an_underscore_tail_that_is_not_that_suffix_is_a_net_name(self):
        # PIN_RE also decides what is *not* a net label, so accepting any
        # underscore tail would swallow nets named after the pin they run to.
        for text in ("PC13_LED", "PB5_BEEPER", "PA4_CS"):
            with self.subTest(text=text):
                self.assertIsNone(netmap.PIN_RE.match(text))


class AnnotationTokenTests(unittest.TestCase):
    """
    netmap.drop_annotations: the exporter's copy of a net, not the net.

    Altium stamps a token for every net it draws - NL for a net label, PO for a
    port, CO/PI for the component and pin it lands on - carrying the net's own
    name with the separators replaced by 0, so RX8_1 becomes CORX801. They read
    like plausible net names and were being collected as labels; on one board
    that put four of them on a single pin row.

    They cannot be spotted by prefix alone: PINIO1 is a real net starting with
    PI. What identifies one is that its remainder names a net drawn elsewhere
    on the same sheet.
    """

    def run_(self, texts, page=1):
        ws = [Word(t, 10.0 + 8 * i, 100.0, 30.0 + 8 * i, 103.0, page)
              for i, t in enumerate(texts)]
        kept = {w.text for w in netmap.drop_annotations(ws, ws)}
        return kept, {t for t in texts if t not in kept}

    def test_the_annotation_copy_is_dropped(self):
        _, gone = self.run_(["TX3", "COTX3", "NLTX3", "GYRO1-CS", "POGYRO10CS"])
        self.assertEqual(gone, {"COTX3", "NLTX3", "POGYRO10CS"})

    def test_the_separator_is_encoded_as_a_zero(self):
        # RX8_1 -> RX801, and the underscore is not recoverable any other way.
        _, gone = self.run_(["RX8_1", "CORX801"])
        self.assertEqual(gone, {"CORX801"})

    def test_the_pin_form_carries_a_pin_number_too(self):
        # PITX301 is TX3 on pin 01, so the trailing digits are tried both ways.
        _, gone = self.run_(["TX3", "PITX301", "BEEPER+", "PIBEEPER001"])
        self.assertEqual(gone, {"PITX301", "PIBEEPER001"})

    def test_a_real_net_that_starts_with_a_prefix_survives(self):
        kept, gone = self.run_(["PINIO1", "PINIO2", "POWER", "COMP1_OUT", "LED0"])
        self.assertEqual(gone, set())

    def test_a_net_is_not_its_own_annotation_across_sheets(self):
        # The remainder has to name a net on the *same* sheet.
        ws = [Word("TX3", 10, 100, 30, 103, 1), Word("COTX3", 10, 200, 30, 203, 2)]
        self.assertEqual(len(netmap.drop_annotations(ws, ws)), 2)


class UnreadableDocumentTests(unittest.TestCase):
    """
    netmap.describe_unreadable: why nothing came out, when it is not geometry.

    Reporting "no aligned pin-name column" for a document whose text carries no
    characters sends the reader to look at the symbol, which is not the problem
    and cannot be made into one.
    """

    def words(self, texts):
        return [Word(t, 10.0 + 8 * i, 100.0, 30.0 + 8 * i, 103.0)
                for i, t in enumerate(texts)]

    def test_a_readable_sheet_gets_no_special_message(self):
        # The caller then falls back to its own wording, which is accurate.
        ws = self.words(["PA5", "PA6", "GND", "SPI1", "MOTOR1", "VBAT"] * 6)
        self.assertIsNone(netmap.describe_unreadable(ws))

    def test_a_scan_is_named_as_one(self):
        self.assertIn("no text layer",
                      netmap.describe_unreadable(self.words(["1", "2"])))

    def test_text_that_carries_no_characters_is_named_as_that(self):
        # What a Type 3 font with /0 /1 /2 glyph names and no ToUnicode gives
        # back: plenty of words, none of them meaning anything.
        soup = [">?@AB?@C", "RNLC@S?IC", "TFCA", "QGAHKC", "ECFGH@IC"] * 30
        why = netmap.describe_unreadable(self.words(soup))
        self.assertIn("carries no characters", why)
        self.assertIn("Type 3", why)

    def test_a_handful_of_real_tokens_is_enough_to_trust_the_text(self):
        # An unusual sheet is not an unreadable one; the threshold is low so a
        # board the tool merely does not understand is not mislabelled.
        ws = self.words(["@@@", "###", "%%%"] * 30
                        + ["PA5", "PB3", "GND", "VCC", "SPI2"])
        self.assertIsNone(netmap.describe_unreadable(ws))


class SyntheticSheetTests(unittest.TestCase):
    """
    The whole geometry path on a made-up Altium sheet: pin names in two aligned
    columns, net labels in the gutters, drawn half a row above the wire.

    This is where the central claim is tested - that the label-to-row offset is
    recovered from the firmware map rather than assumed - without needing a
    vendor schematic to do it.
    """

    PITCH = 4.0
    LABEL_RISE = 0.3          # net labels sit slightly above the pin name
    LEFT_EDGE = 10.0
    RIGHT_EDGE = 200.0

    # Pin names carry alternate-function lists, as many real symbols do, which
    # also means their text widths differ - left names share an x0, right names
    # share an x1, and neither side accidentally shares the other's alignment.
    #
    # The order is chosen so that shifting the mapping by one row makes almost
    # every net land on a pin that cannot do its job.
    LEFT = [("PA5/SPI1_SCK", "GYRO-SCK"),
            ("PA6/SPI1_MISO", "GYRO-MISO"),
            ("PA7/SPI1_MOSI", "GYRO-MOSI"),
            ("PB8/I2C1_SCL/TIM4_CH3", "I2C1-SCL"),
            ("PB9/I2C1_SDA/TIM4_CH4", "I2C1-SDA")]
    RIGHT = [("PC6/TIM3_CH1/TIM8_CH1", "MOTOR1"),
             ("PC7/TIM3_CH2/TIM8_CH2", "MOTOR2"),
             ("PC8/TIM3_CH3/TIM8_CH3", "MOTOR3"),
             ("PA9/USART1_TX", "TX1"),
             ("PA10/USART1_RX", "RX1")]

    @staticmethod
    def _pin(name):
        return name.split("/")[0]

    def sheet(self, rise=None, page=1):
        rise = self.LABEL_RISE if rise is None else rise
        words = []
        for i, (name, net) in enumerate(self.LEFT):
            y = 100 + i * self.PITCH
            words.append(Word(name, self.LEFT_EDGE, y,
                              self.LEFT_EDGE + 4 * len(name), y + 2, page))
            words.append(Word(net, 1.0, y - rise, 9.0, y - rise + 2, page))
        for i, (name, net) in enumerate(self.RIGHT):
            y = 100 + i * self.PITCH
            words.append(Word(name, self.RIGHT_EDGE - 4 * len(name), y,
                              self.RIGHT_EDGE, y + 2, page))
            words.append(Word(net, 205.0, y - rise, 225.0, y - rise + 2, page))
        return words

    def test_the_symbol_is_found_with_both_edges(self):
        sym = netmap.find_symbol(self.sheet())
        self.assertEqual(len(sym.rows), 10)
        self.assertEqual(sym.pitch, self.PITCH)
        self.assertEqual({r.side for r in sym.rows}, {"L", "R"})
        self.assertEqual(sorted(r.pin for r in sym.rows),
                         sorted(self._pin(n) for n, _ in self.LEFT + self.RIGHT))

    def test_every_gutter_label_is_collected(self):
        sym = netmap.find_symbol(self.sheet())
        labels = netmap.find_net_labels(self.sheet(), sym)
        self.assertEqual(sorted(l.text for l in labels),
                         sorted(n for _, n in self.LEFT + self.RIGHT))

    def test_the_offset_is_recovered_and_every_net_agrees(self):
        words = self.sheet()
        sym = netmap.find_symbol(words)
        res = netmap.resolve(sym, netmap.find_net_labels(words, sym), fake_caps())
        # Any offset within half a row of the true rise produces the same
        # pairing; among those the smallest shift wins, so that is the bound.
        self.assertLessEqual(abs(res.offset - self.LABEL_RISE), self.PITCH / 2)
        self.assertEqual(res.score, (10, 10))
        self.assertEqual(dict((l.net, l.pin) for l in res.links),
                         {net: self._pin(n) for n, net in self.LEFT + self.RIGHT})

    def test_a_whole_row_of_shift_is_recovered_not_accepted(self):
        """
        The same sheet drawn with the labels a full row higher. Geometry alone
        cannot tell that from the correct alignment - the firmware map is what
        rejects it, which is the entire design.
        """
        rise = self.LABEL_RISE + self.PITCH
        words = self.sheet(rise=rise)
        sym = netmap.find_symbol(words)
        res = netmap.resolve(sym, netmap.find_net_labels(words, sym), fake_caps())
        self.assertLessEqual(abs(res.offset - rise), self.PITCH / 2)
        self.assertEqual(res.score, (10, 10))
        self.assertEqual(dict((l.net, l.pin) for l in res.links),
                         {net: self._pin(n) for n, net in self.LEFT + self.RIGHT})

    def test_no_label_is_dropped_in_silence(self):
        stray_y = 100 + len(self.LEFT) * self.PITCH   # past the last row, still
        words = self.sheet() + [                       # inside the search band
            Word("STRAY-NET", 1.0, stray_y, 9.0, stray_y + 2, 1)]
        sym = netmap.find_symbol(words)
        labels = netmap.find_net_labels(words, sym)
        res = netmap.resolve(sym, labels, fake_caps())
        self.assertEqual(len(res.links) + len(res.orphans), len(labels))

    def test_unwired_pins_are_reported(self):
        words = [x for x in self.sheet() if x.text != "MOTOR3"]
        sym = netmap.find_symbol(words)
        res = netmap.resolve(sym, netmap.find_net_labels(words, sym), fake_caps())
        self.assertIn("PC8", res.unmapped)

    def test_a_second_sheet_cannot_steal_rows(self):
        """
        Sheets of one plot share a coordinate space, so page 2's text sits on
        the same rows as page 1's. It must not compete to be the net-label
        column (ROADMAP 1.1).
        """
        other = [Word("MOTOR8", 1.0, 100.0 - self.LABEL_RISE, 9.0, 102.0, 2),
                 Word("MOTOR7", 1.0, 104.0 - self.LABEL_RISE, 9.0, 106.0, 2),
                 Word("MOTOR6", 1.0, 108.0 - self.LABEL_RISE, 9.0, 110.0, 2)]
        words = self.sheet() + other
        sym = netmap.find_symbol(words)
        labels = netmap.find_net_labels(words, sym)
        self.assertEqual(sorted(l.text for l in labels),
                         sorted(n for _, n in self.LEFT + self.RIGHT))


class TwoBoxSheetTests(unittest.TestCase):
    """
    One MCU drawn as two boxes on a single sheet - the ordinary way a large
    package is plotted, one box per port group.

    Taking only the strongest box read half the package and said nothing about
    it: the pins that were read were all correct and agreement stayed at 100%,
    so the loss was invisible in every diagnostic the tool prints. On the
    corpus this was the single biggest source of missing pins.
    """

    PITCH = 4.0
    RISE = 0.3

    # Box A is left-aligned with its labels out to the left; box B sits well to
    # its right, right-aligned, with its labels further right again. Both share
    # a y band - which is what makes a page-keyed lookup collapse them into one.
    #
    # A's names are all the same width, so they share an x1 as well as an x0 and
    # the two alignments hold the identical set; the guard against counting a
    # single column twice then blanks the right edge, and A comes out as a
    # left-only box. B's names differ in width, so only their x1 agrees and B
    # comes out right-only. That is the real shape of these plots, and it is
    # why one pass over the sheet can only ever find one of the two.
    BOX_A = [("PA5", "GYRO-SCK"), ("PA6", "GYRO-MISO"), ("PA7", "GYRO-MOSI"),
             ("PB8", "I2C1-SCL"), ("PB9", "I2C1-SDA"), ("PB6", "TX1"),
             ("PB3", None), ("PB4", None), ("PB5", None), ("PA0", None),
             ("PA1", None), ("PB0", None), ("PA8", None)]
    BOX_B = [("PC6", "MOTOR1"), ("PC7", "MOTOR2"), ("PC8", "MOTOR3"),
             ("PA10", "RX1"), ("PB13", "SPI2_SCK"), ("PB14", "SPI2_MISO"),
             ("PB15", "SPI2_MOSI"), ("PC9", None), ("PB10", None),
             ("PC1", None), ("PC2", None), ("PC3", None)]

    A_EDGE, B_EDGE = 60.0, 200.0

    def sheet(self):
        words = []
        for i, (name, net) in enumerate(self.BOX_A):
            y = 100 + i * self.PITCH
            words.append(Word(name, self.A_EDGE, y, self.A_EDGE + 4 * len(name), y + 2))
            if net:
                words.append(Word(net, 1.0, y - self.RISE, 9.0, y - self.RISE + 2))
        for i, (name, net) in enumerate(self.BOX_B):
            y = 100 + i * self.PITCH
            words.append(Word(name, self.B_EDGE - 4 * len(name), y, self.B_EDGE, y + 2))
            if net:
                words.append(Word(net, 205.0, y - self.RISE, 225.0, y - self.RISE + 2))
        return words

    def test_the_boxes_are_one_sided_as_the_real_plots_are(self):
        sym = netmap.find_symbol(self.sheet())
        self.assertEqual(len(sym.parts), 2)
        sides = sorted("".join(sorted({r.side for r in p.rows})) for p in sym.parts)
        self.assertEqual(sides, ["L", "R"])

    def test_both_boxes_are_found(self):
        sym = netmap.find_symbol(self.sheet())
        self.assertEqual(sorted(r.pin for r in sym.rows),
                         sorted(n for n, _ in self.BOX_A + self.BOX_B))

    def test_every_net_on_both_boxes_binds(self):
        words = self.sheet()
        sym = netmap.find_symbol(words)
        res = netmap.resolve(sym, netmap.find_net_labels(words, sym), fake_caps())
        self.assertEqual(res.orphans, [])
        self.assertEqual(res.agreement, 1.0)
        self.assertEqual({l.net: l.pin for l in res.links},
                         {net: name for name, net in self.BOX_A + self.BOX_B if net})

    def test_a_label_is_bound_once_not_once_per_box(self):
        # A label drawn between the boxes is in one box's right gutter and the
        # other's left, so it is collected twice; binding it twice would report
        # the same net on two different pins.
        words = self.sheet()
        sym = netmap.find_symbol(words)
        res = netmap.resolve(sym, netmap.find_net_labels(words, sym), fake_caps())
        nets = [l.net for l in res.links]
        self.assertEqual(len(nets), len(set(nets)))

    def test_a_box_does_not_claim_the_other_boxs_gutter(self):
        # Box A has pin names only on its left edge, so it has no right-hand
        # gutter. Searching one anyway reaches across the gap and takes box B's
        # labels, which then bind to A's rows and strand B's.
        words = self.sheet()
        sym = netmap.find_symbol(words)
        a = min(sym.parts, key=lambda p: p.left_edge)
        got = {w.text for w in netmap._labels_for_part(words, a, sym.pitch)}
        self.assertEqual(got, {net for _, net in self.BOX_A if net})


# --------------------------------------------------------------------------- #
# Whole-file generation, on a synthetic sheet
# --------------------------------------------------------------------------- #

class SyntheticBoard:
    """
    A made-up schematic run all the way through genconfig.build().

    The corpus of real schematics exercises none of the features tested below -
    no board in it has a PPM net, an ESC serial net, an SD card or a second IMU
    - so a golden digest cannot pin any of this down. Driving the whole pipeline
    from invented geometry can, and it costs a temporary firmware.json plus a
    file for the schematic hash to be taken of.

    Only `extract_words` is replaced: everything after it - symbol detection,
    net labelling, the firmware check, bus solving, timer and DMA allocation -
    runs exactly as it does on a vendor PDF.
    """

    PITCH = 4.0
    LABEL_RISE = 0.3
    LEFT_EDGE = 10.0
    RIGHT_EDGE = 200.0

    def __init__(self, left, right=(), extra=(), style="fixed"):
        self.left, self.right, self.extra, self.style = left, right, extra, style

    def words(self):
        out = []
        for i, (name, net) in enumerate(self.left):
            y = 100 + i * self.PITCH
            out.append(Word(name, self.LEFT_EDGE, y,
                            self.LEFT_EDGE + 4 * len(name), y + 2, 1))
            out.append(Word(net, 1.0, y - self.LABEL_RISE, 9.0,
                            y - self.LABEL_RISE + 2, 1))
        for i, (name, net) in enumerate(self.right):
            y = 100 + i * self.PITCH
            out.append(Word(name, self.RIGHT_EDGE - 4 * len(name), y,
                            self.RIGHT_EDGE, y + 2, 1))
            out.append(Word(net, 205.0, y - self.LABEL_RISE, 225.0,
                            y - self.LABEL_RISE + 2, 1))
        # Part markings, well clear of the row band so they cannot be mistaken
        # for net labels.
        for i, text in enumerate(self.extra):
            out.append(Word(text, 100.0, 300.0 + i * 6, 140.0, 303.0 + i * 6, 1))
        return out


TARGET = "FAKE32X100"


def generate(board: "SyntheticBoard"):
    """(text, cfg, meta) for one synthetic board."""
    import tempfile
    from pathlib import Path
    from unittest import mock

    data = {
        "schema": 1,
        "generated": "1970-01-01T00:00:00Z",
        "firmware": {"rev": "synthetic", "date": "", "branch": "", "path": ""},
        "drivers": support.frozen_firmware()["drivers"],
        "targets": {TARGET: fake_caps(board.style)},
    }
    with tempfile.TemporaryDirectory(prefix="schema-convert-synth-") as tmp:
        tmp = Path(tmp)
        (tmp / "firmware.json").write_text(json.dumps(data))
        pdf = tmp / "sheet.pdf"
        pdf.write_bytes(b"%PDF-synthetic")
        old = genconfig.DATA_DIR
        genconfig.DATA_DIR = tmp
        try:
            with mock.patch.object(genconfig, "extract_words",
                                   return_value=board.words()):
                cfg, meta = genconfig.build(pdf=pdf, board="SYNTH",
                                            manufacturer="TEST", target=TARGET,
                                            gyro_align="CW0_DEG")
        finally:
            genconfig.DATA_DIR = old
    return "\n".join(cfg.lines).rstrip() + "\n", cfg, meta


# A fixed left column - a gyro on SPI1 plus an I2C bus - and a right column that
# each test extends. Rows are added on the right because find_symbol picks each
# edge as the largest cluster of shared x0 / x1, and a lopsided left column
# would win the right-hand alignment too. Real symbols are not lopsided.
BASE_LEFT = [("PA5/SPI1_SCK", "GYRO-SCK"),
             ("PA6/SPI1_MISO", "GYRO-MISO"),
             ("PA7/SPI1_MOSI", "GYRO-MOSI"),
             ("PA4", "GYRO-CS"),
             ("PB8/I2C1_SCL", "I2C1-SCL"),
             ("PB9/I2C1_SDA", "I2C1-SDA")]
BASE_RIGHT = [("PC6/TIM3_CH1/TIM8_CH1", "MOTOR1"),
              ("PC7/TIM3_CH2/TIM8_CH2", "MOTOR2"),
              ("PC8/TIM3_CH3/TIM8_CH3", "MOTOR3"),
              ("PC9/TIM3_CH4/TIM8_CH4", "MOTOR4")]
PARTS = ("MPU-6000",)
SPI2_ROWS = [("PB13/SPI2_SCK", "{0}-SCK"),
             ("PB14/SPI2_MISO", "{0}-MISO"),
             ("PB15/SPI2_MOSI", "{0}-MOSI")]


def spi2_for(device):
    return [(pin, net.format(device)) for pin, net in SPI2_ROWS]


def board(extra_rows=(), parts=PARTS, style="fixed"):
    return SyntheticBoard(BASE_LEFT, BASE_RIGHT + list(extra_rows), parts, style)


class CaptureInputTests(unittest.TestCase):
    """
    RX_PPM_PIN and ESCSERIAL_PIN - 54% and 18% of the corpus, and neither had a
    rule at all.

    Both are timer *input captures*, not GPIO. Both drivers reach the pin via
    timerAllocate(), which walks timerIOConfig() - the TIMER_PIN_MAPPING table -
    and returns nothing for a pin that is not in it. So the pin needs a timer
    channel in the firmware table and a row in the map, and it must not be given
    a DMA option: the drivers read the capture register from the ISR, and every
    RX_PPM_PIN row in the config repo carries dmaopt -1.
    """

    def rows(self, text):
        return {label: (occ, opt)
                for _, label, occ, opt in support.timer_rows(text)}

    def test_ppm_is_emitted_with_an_undma_ed_timer_row(self):
        text, _, _ = generate(board([("PA0/TIM2_CH1", "PPM")]))
        self.assertEqual(support.defines(text).get("RX_PPM_PIN"), "PA0")
        self.assertEqual(self.rows(text).get("RX_PPM_PIN"), (1, -1))

    def test_escserial_is_emitted_with_an_undma_ed_timer_row(self):
        text, _, _ = generate(board([("PA1/TIM2_CH2", "ESCSERIAL")]))
        self.assertEqual(support.defines(text).get("ESCSERIAL_PIN"), "PA1")
        self.assertEqual(self.rows(text).get("ESCSERIAL_PIN"), (1, -1))

    def test_a_pin_the_firmware_gives_no_timer_is_refused(self):
        """
        The whole point of the check. PB13 is a real GPIO with a real SPI
        function and no timer at all, so a PPM net there is a define the build
        could never honour - and netmap cannot catch it, because the name 'PPM'
        implies no requirement it knows how to test.
        """
        text, cfg, _ = generate(board([("PB13/SPI2_SCK", "PPM")]))
        self.assertNotIn("RX_PPM_PIN", support.defines(text))
        self.assertTrue(any("RX_PPM_PIN" in w and "timer" in w
                            for w in cfg.warnings), cfg.warnings)

    def test_the_motors_keep_a_distinct_dma_option_each(self):
        """A capture row must not consume a DMA number the motors need."""
        text, _, _ = generate(board([("PA0/TIM2_CH1", "PPM")], style="mux"))
        opts = [opt for _, _, _, opt in support.timer_rows(text) if opt >= 0]
        self.assertEqual(sorted(opts), list(range(len(opts))))
        self.assertEqual(self.rows(text)["RX_PPM_PIN"][1], -1)


class SecondGyroTests(unittest.TestCase):
    """
    GYRO_2_* - 25% of the corpus, and the classifier had no rule for it.

    Two cases, and they end differently. When the sheet names the second IMU's
    own data nets the existing solver handles it: the groups have different data
    pins, so they cannot share an instance and are assigned apart.

    When it names only GYRO2-CS, nothing is emitted. That is not timidity:
    common_pre.h counts gyros by which GYRO_n_CS_PIN exist, so a lone
    GYRO_2_CS_PIN raises GYRO_COUNT to 2 and gyrodev.c then leaves devconf[1] at
    BUS_TYPE_NONE. Nor can the bus be borrowed from gyro 1 - across the config
    repo, 66 of the 119 dual-gyro boards naming both instances put the second
    IMU on a *different* bus.
    """

    def test_two_imus_on_different_buses_are_solved_apart(self):
        text, _, _ = generate(board(spi2_for("GYRO2") + [("PB12", "GYRO2-CS"),
                                                         ("PB10", "GYRO2-EXTI")]))
        d = support.defines(text)
        self.assertEqual(d.get("GYRO_1_SPI_INSTANCE"), "SPI1")
        self.assertEqual(d.get("GYRO_2_SPI_INSTANCE"), "SPI2")
        self.assertEqual(d.get("SPI2_SCK_PIN"), "PB13")
        self.assertEqual(d.get("GYRO_1_CS_PIN"), "PA4")
        self.assertEqual(d.get("GYRO_2_CS_PIN"), "PB12")
        self.assertEqual(d.get("GYRO_2_EXTI_PIN"), "PB10")
        self.assertEqual(d.get("GYRO_2_ALIGN"), "CW0_DEG")

    def test_the_index_may_be_written_after_the_signal_name(self):
        """`GYRO-CS2` names the same device as `GYRO2-CS`, so it must not be
        read as a second chip select for gyro 1 - which is what the old rule,
        matching the digit without capturing it, silently did."""
        text, cfg, _ = generate(board([("PB12", "GYRO-CS2")]))
        d = support.defines(text)
        self.assertEqual(d.get("GYRO_1_CS_PIN"), "PA4")
        self.assertNotIn("GYRO_2_CS_PIN", d)
        self.assertTrue(any("PB12" in w and "second IMU" in w
                            for w in cfg.warnings), cfg.warnings)

    def test_a_chip_select_alone_emits_nothing_and_says_why(self):
        text, cfg, _ = generate(board([("PB12", "GYRO2-CS"),
                                       ("PB10", "GYRO2-EXTI")]))
        d = support.defines(text)
        for name in ("GYRO_2_CS_PIN", "GYRO_2_SPI_INSTANCE", "GYRO_2_EXTI_PIN",
                     "GYRO_2_ALIGN"):
            self.assertNotIn(name, d)
        self.assertTrue(any("GYRO_COUNT" in w for w in cfg.warnings), cfg.warnings)
        # The pin is still named, so the reviewer knows which one to wire up.
        self.assertTrue(any("PB12" in w for w in cfg.warnings), cfg.warnings)
        # ...and gyro 1 survives intact.
        self.assertEqual(d.get("GYRO_1_CS_PIN"), "PA4")

    def test_a_third_imu_is_reported_rather_than_half_emitted(self):
        text, cfg, _ = generate(board([("PB12", "GYRO3-CS")]))
        self.assertNotIn("GYRO_3", text)
        self.assertTrue(any("third" in w for w in cfg.warnings), cfg.warnings)

    def test_a_board_with_one_imu_says_nothing_about_a_second(self):
        text, _, _ = generate(board())
        self.assertNotIn("GYRO_2", text)
        self.assertEqual(support.defines(text).get("GYRO_1_CS_PIN"), "PA4")


class SdcardTests(unittest.TestCase):
    """
    USE_SDCARD - 15% of the corpus. The `sdcard_cs` role already existed, but
    the feature define did not, and the chip select was emitted under a name the
    firmware does not read.

    pg/sdcard.c reads SDCARD_SPI_CS_PIN; SDCARD_CS_PIN appears nowhere in the
    firmware and in none of the 619 configs. And common_pre.h only defines
    USE_SDCARD inside `#if !defined(USE_CONFIG)`, which the config-repo build
    path does define - so without it the card had neither a driver nor a
    readable chip select, and the file still compiled.
    """

    def card(self):
        return spi2_for("SDCARD") + [("PB12", "SD-CS")]

    def test_the_feature_defines_and_the_firmware_s_own_cs_name(self):
        text, _, _ = generate(board(self.card()))
        d = support.defines(text)
        self.assertIn("#define USE_SDCARD\n", text)
        self.assertIn("#define USE_SDCARD_SPI\n", text)
        self.assertEqual(d.get("SDCARD_SPI_CS_PIN"), "PB12")
        self.assertEqual(d.get("SDCARD_SPI_INSTANCE"), "SPI2")
        self.assertNotIn("SDCARD_CS_PIN", text)

    def test_a_card_alone_takes_the_blackbox_default(self):
        text, _, _ = generate(board(self.card()))
        self.assertEqual(support.defines(text).get("DEFAULT_BLACKBOX_DEVICE"),
                         "BLACKBOX_DEVICE_SDCARD")

    def test_a_fitted_flash_still_wins_but_the_split_is_reported(self):
        text, cfg, _ = generate(board(self.card(),
                                      parts=PARTS + ("W25Q128JVEIQ",)))
        self.assertEqual(support.defines(text).get("DEFAULT_BLACKBOX_DEVICE"),
                         "BLACKBOX_DEVICE_FLASH")
        self.assertTrue(any("SD card" in n for n in cfg.notes), cfg.notes)

    def test_no_card_means_no_sdcard_defines(self):
        self.assertNotIn("SDCARD", generate(board())[0])


class GeneratedFileInvariantTests(unittest.TestCase):
    """
    The invariants the real boards are held to, applied to a synthetic board
    carrying every new feature at once - which no vendor sheet in reach does.
    """

    def setUp(self):
        self.text = generate(board(
            spi2_for("GYRO2") + [("PB12", "GYRO2-CS"), ("PB10", "GYRO2-EXTI"),
                                 ("PA0/TIM2_CH1", "PPM"),
                                 ("PA1/TIM2_CH2", "ESCSERIAL")]))[0]
        self.defines = support.defines(self.text)

    def test_every_timer_row_names_a_define_the_file_makes(self):
        missing = [label for _, label, _, _ in support.timer_rows(self.text)
                   if label not in self.defines]
        self.assertEqual(missing, [])

    def test_no_two_defines_claim_the_same_pin(self):
        seen = {}
        for name, value in self.defines.items():
            if name.endswith("_PIN") and support.PIN_VALUE_RE.match(value):
                seen.setdefault(value, []).append(name)
        self.assertEqual([v for v in seen.values() if len(v) > 1], [])

    def test_every_timer_row_occurrence_is_in_range_for_its_pin(self):
        caps = fake_caps()
        for _, label, occurrence, _ in support.timer_rows(self.text):
            pin = self.defines[label]
            channels = caps["timers"].get(pin) or []
            with self.subTest(label=label):
                self.assertTrue(1 <= occurrence <= len(channels))

    def test_a_net_that_is_refused_is_still_named(self):
        """
        The suite's `unaccounted_roles` invariant, on the nets this change can
        refuse. A net that reaches neither a define nor a diagnostic just looks
        like an incomplete config later, with no clue why - and every new rule
        here has a path that deliberately emits nothing.
        """
        refused = [("PE1", "GYRO2-CS"),      # no data nets: bus unknown
                   ("PE0", "GYRO2-EXTI"),    # goes with it
                   ("PB13/SPI2_SCK", "PPM"), # no timer on that pin
                   ("PE2", "GYRO3-CS")]      # a third IMU
        text, cfg, meta = generate(board(refused))
        emitted = set(support.defines(text).values())
        said = " ".join(cfg.warnings + cfg.notes)
        for link in meta["links"]:
            if genconfig.classify(link["net"])[0] == "ignore" or not link["gpio"]:
                continue
            with self.subTest(net=link["net"]):
                self.assertTrue(link["pin"] in emitted or link["net"] in said,
                                f"{link['net']} on {link['pin']} vanished")

    def test_no_spi_bus_is_emitted_half_finished(self):
        buses = {}
        for name in self.defines:
            m = re.fullmatch(r"SPI(\d)_(SCK|SDI|SDO)_PIN", name)
            if m:
                buses.setdefault(m.group(1), set()).add(m.group(2))
        for bus, roles in buses.items():
            with self.subTest(bus=bus):
                self.assertEqual(roles, {"SCK", "SDI", "SDO"})


if __name__ == "__main__":
    unittest.main()
