"""HOST test target provenance, reconnect control and recovery invariants."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import host_142 as host
from tests.test_tank_relay_142 import Controller, Device, config


class HostTests(unittest.TestCase):
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
