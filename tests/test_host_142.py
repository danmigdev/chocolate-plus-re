"""HOST test target provenance, reconnect control and recovery invariants."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tools import host_142 as host
from tests.test_tank_relay_142 import Controller, Device, config


class HostTests(unittest.TestCase):
    def test_split_has_one_sysex_on_C_and_CC_only_on_D(self):
        original = config()
        staged, _, _ = host.images(original, Device().original, "split")
        for bank in range(2):
            self.assertEqual(host.usb.expected_frames(staged, 2, bank), [host.relay.live_frame(70, b"\x49\0")])
            offset = 93 + 3 * 417
            self.assertEqual(staged[offset + 1 + 80 * bank:offset + 81 + 80 * bank], b"\1\0\1\0\0" + bytes(75))
            self.assertEqual(staged[offset + 161 + 128 * bank:offset + 289 + 128 * bank], bytes(128))
        self.assertEqual(staged[:927], original[:927])
        self.assertEqual(staged[1761:], original[1761:])

    def test_split_calibration_reinstates_baseline_before_HOST_and_survives_reload(self):
        controller, tank = Controller(config()), Device()
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch("builtins.print"):
            root = Path(directory)
            report = host.prepare(None, root / "session", "split")
            loaded = host.load(root / "session")
            self.assertEqual(len(report["USB_calibration"]), 2)
            self.assertTrue(all(c["passed"] for c in report["USB_calibration"]))
            self.assertTrue(report["tank_baseline_reinstated_after_calibration"])
            self.assertEqual(tank.stable(), loaded["tank-staged"])
            self.assertEqual((tank.memory[8], tank.memory[70]), (1, 47))
            self.assertNotIn(b"\xb0\0\0", tank.allowed)
            self.assertNotIn(host.relay.live_frame(70, b"\x49\0"), tank.allowed)
            self.assertEqual(bytes(controller.memory), loaded["plus-staged"])
            self.assertEqual(bytes(controller.flips), bytes(4))
            result = host.recover(None, root / "session", root / "recover")
            self.assertTrue(result["tank_restored"] and result["plus_restored"])

    def test_failed_split_calibration_restores_both_devices(self):
        controller, tank = Controller(config(), failure="bad_frame"), Device()
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "unexpected USB MIDI"):
                host.prepare(None, Path(directory) / "session", "split")
            self.assertEqual(bytes(controller.memory), config())
            self.assertEqual(tank.stable(), tank.original)

    def test_stock_CC_gate_requires_exact_message_and_session_allow_list(self):
        device = host.relay.Tank(None)
        for message in (b"\xb0\0\0", b"\xb0\1\0", b"\xb1\0\0", b"\xc0\0", b"\xf8"):
            with self.subTest(message=message.hex()), self.assertRaises(ValueError):
                device.transmit(message)
        device.allowed = {b"\xb1\0\0"}
        with self.assertRaises(ValueError): device.transmit(b"\xb1\0\0")

    def test_failed_CC_effect_is_recorded_before_restoration(self):
        controller, tank = Controller(config()), Device()
        transmit = tank.transmit
        def drop_cc(frame):
            if frame != b"\xb0\0\0":
                transmit(frame)
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch.object(tank, "transmit", side_effect=drop_cc), \
                patch("builtins.print"):
            folder = Path(directory) / "session"
            with self.assertRaisesRegex(ValueError, "expected TANK patch"):
                host.prepare(None, folder, "split")
            report = json.loads((folder / "prepare.json").read_text())
            self.assertEqual([c["passed"] for c in report["USB_calibration"]], [True, False])
            self.assertEqual(report["USB_calibration"][1]["actual_frames"], ["b0 00 00"])
            self.assertEqual(bytes.fromhex(report["USB_calibration"][1]["observed_live"])[8], 1)
            self.assertTrue(report["tank_restored"] and report["plus_restored"])
            self.assertEqual(tank.stable(), tank.original)
            self.assertEqual(bytes(controller.memory), config())

    def test_second_MIDI_path_stops_before_double_toggling_and_restores_both(self):
        controller, tank = Controller(config()), Device()
        write = controller.write
        def other_path(address, data):
            write(address, data)
            if address == host.usb.KEYS + 6 and data == b"\0\x64":
                tank.memory[8] ^= 1
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch.object(controller, "write", side_effect=other_path), \
                patch("builtins.print"):
            folder = Path(directory) / "session"
            with self.assertRaisesRegex(ValueError, "before the PC relay"):
                host.prepare(None, folder, "split")
            report = json.loads((folder / "prepare.json").read_text())
            cc = report["USB_calibration"][1]
            self.assertEqual(bytes.fromhex(cc["before_key"])[8], 1)
            self.assertEqual(bytes.fromhex(cc["before_relay"])[8], 0)
            self.assertNotIn(b"\xb0\0\0", tank.sent)
            self.assertTrue(report["tank_restored"] and report["plus_restored"])
            self.assertEqual(tank.stable(), tank.original)
            self.assertEqual(bytes(controller.memory), config())

    def test_both_C_banks_emit_the_same_two_targets_and_do_not_overlap_other_assignments(self):
        original, live = config(), Device().original
        staged, tank, target = host.images(original, live)
        self.assertEqual(staged[:927], original[:927])
        self.assertEqual(staged[1344:], original[1344:])
        frames = host.usb.expected_frames(staged, 2, 0)
        self.assertEqual(frames, host.usb.expected_frames(staged, 2, 1))
        self.assertEqual([host.relay.decode_write(f) for f in frames], [(0x10000008, b"\0"), (0x10000046, b"\x49\0")])
        self.assertEqual(host.relay.expected_live(tank, frames, bytes(92)), target)
        self.assertEqual([i for i, (a, b) in enumerate(zip(tank, target)) if a != b], [8, 70])

    def test_control_partial_delivery_full_delivery_and_restoration(self):
        controller, tank = Controller(config()), Device()
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch("builtins.print"):
            root = Path(directory)
            host.prepare(None, root / "session")
            self.assertNotIn(host.relay.live_frame(70, b"\x49\0"), tank.sent)
            self.assertTrue(host.measure_tank(None, root / "session", root / "control", True)["passed"])
            tank.memory[8] = 0  # Only the first HOST message arrives.
            partial = host.measure_tank(None, root / "session", root / "partial", True)
            self.assertFalse(partial["passed"])
            tank.memory[70] = 73
            result = host.measure_tank(None, root / "session", root / "host")
            self.assertTrue(result["passed"] and result["restored"])
            self.assertEqual(tank.stable(), tank.original)
            controller.memory[23646] = 2
            result = host.finish(None, root / "session", root / "plus")
            self.assertEqual(result["configuration"]["changed_bytes"], 1)
            self.assertTrue(result["configuration_data_intact"] and result["saved_data_intact"])
            self.assertEqual(result["current_switch_marker"]["ram"], 2)
            self.assertTrue(result["restored"])
            self.assertEqual(bytes(controller.memory), config())

    def test_failed_delivery_is_recorded_before_TANK_restoration(self):
        controller, tank = Controller(config()), Device()
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch("builtins.print"):
            root = Path(directory)
            host.prepare(None, root / "session")
            staged = tank.stable()
            result = host.measure_tank(None, root / "session", root / "host")
            self.assertFalse(result["passed"])
            self.assertTrue(result["restored"])
            self.assertEqual((root / "host/tank-live.bin").read_bytes(), staged)
            self.assertEqual(tank.stable(), tank.original)

    def test_lost_ack_while_preparing_restores_both_originals(self):
        controller, tank = Controller(config()), Device("lost_ack")
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch.object(host.relay, "Tank", return_value=tank), patch("builtins.print"):
            with self.assertRaises(TimeoutError): host.prepare(None, Path(directory) / "session")
            self.assertEqual(tank.stable(), tank.original)
            self.assertEqual(bytes(controller.memory), config())

    def test_explicit_recovery_restores_Plus_even_when_TANK_is_unavailable(self):
        controller, tank = Controller(config()), Device()
        with tempfile.TemporaryDirectory() as directory, patch.object(host.usb, "UsbLink", return_value=controller), \
                patch("builtins.print"):
            root = Path(directory)
            with patch.object(host.relay, "Tank", return_value=tank):
                host.prepare(None, root / "session")
            with patch.object(host.relay, "Tank", side_effect=OSError("TANK disconnected")):
                with self.assertRaisesRegex(ValueError, "TANK disconnected"):
                    host.recover(None, root / "session", root / "recovery")
            self.assertEqual(bytes(controller.memory), config())

    def test_unchanged_saved_switch_marker_is_not_a_corruption_or_keypress_claim(self):
        original = bytearray(config())
        original[23646] = 1
        staged, _, _ = host.images(bytes(original), Device().original)
        flash = staged + b"usrdata\0" + bytes(host.usb.FLASH_BYTES - len(staged) - 8)
        result = host.compare_plus_storage({"plus-staged": staged, "plus-saved": flash}, staged, flash)
        self.assertEqual(result["configuration"]["changed_bytes"], 0)
        self.assertEqual(result["saved"]["changed_bytes"], 0)
        self.assertTrue(result["configuration_data_intact"] and result["saved_data_intact"])
        self.assertEqual(result["current_switch_marker"],
                         dict(staged=1, ram=1, saved=1, proves_physical_C_press=False))

    def test_a_corrupted_C_payload_cannot_be_treated_as_a_runtime_marker_change(self):
        staged, _, _ = host.images(config(), Device().original)
        flash = staged + b"usrdata\0" + bytes(host.usb.FLASH_BYTES - len(staged) - 8)
        damaged = bytearray(staged)
        damaged[host.relay.C_OFFSET + 162] &= 0x7f  # F0 -> 70 in C bank A.
        result = host.compare_plus_storage({"plus-staged": staged, "plus-saved": flash}, bytes(damaged), flash)
        self.assertFalse(result["configuration_data_intact"])
        self.assertTrue(result["saved_data_intact"])
        self.assertFalse(result["ram_matches_saved"])


if __name__ == "__main__":
    unittest.main()
