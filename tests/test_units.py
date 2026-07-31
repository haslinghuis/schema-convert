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
        ("SPI1_MOSI", "spi_bus", "1", "sdo"),
        ("OSD-CS", "osd_cs", None, None),
        ("AT7456-SCK", "osd_spi", None, "sck"),
        ("FLASH-CS", "flash_cs", None, None),
        ("BARO-CS", "baro_cs", None, None),
        ("SDCARD-CS", "sdcard_cs", None, None),
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

    def test_motor_and_uart_do_not_collide(self):
        """VTX-SW is a rail switch, not UART SW; S1 is a motor, not a signal."""
        self.assertEqual(genconfig.classify("VTX-SW")[0], "pinio")
        self.assertEqual(genconfig.classify("S1")[0], "motor")


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


if __name__ == "__main__":
    unittest.main()
