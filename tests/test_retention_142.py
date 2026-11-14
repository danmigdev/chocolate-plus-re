"""Physical-transition retention: evidence, corruption repair, and write bounds."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import retention_142 as retention
from tests.test_acceptance_142 import fixture, FakeLink


class RetentionTests(unittest.TestCase):
    def test_bit_loss_and_gain_are_counted_independently(self):
        result = retention.changes(bytes([0xFF, 0, 0xF7]), bytes([0xF0, 0x80, 0xB5]))
        self.assertEqual((result["changed_bytes"], result["cleared_bits"], result["set_bits"]), (3, 6, 1))

    def test_hardware_corruption_is_captured_before_repairing_active_and_unused_bytes(self):
        original = fixture()
        link = FakeLink(original)
        with tempfile.TemporaryDirectory() as directory, patch.object(retention.usb, "UsbLink", return_value=link), \
                patch("builtins.print"):
            folder = Path(directory)
            retention.prepare(None, folder / "session")
            link.memory[255] &= 0x7F  # Active bank: F0 -> 70, outside the test pattern.
            link.memory[1100] &= 0xDF  # Unused C SysEx payload.
            damaged = bytes(link.memory)
            report = retention.capture(None, folder / "session", folder / "after")
            self.assertFalse(report["retention_passed"])
            self.assertGreaterEqual(report["ram_changed_bytes"], 2)
            self.assertEqual((folder / "after/ram-1.bin").read_bytes(), damaged)
            self.assertTrue(report["restored"])
            self.assertEqual(bytes(link.memory), original)
            self.assertTrue(all(retention.usb.RAM_BASE <= a < a + len(d) <= retention.usb.RAM_BASE + len(original)
                                or a in (retention.usb.FLIPS, retention.usb.KEYS) for a, d in link.allow))

    def test_loss_of_write_reply_restores_the_full_original(self):
        original = fixture()
        link = FakeLink(original, failure="lost_pattern_ack")
        with tempfile.TemporaryDirectory() as directory, patch.object(retention.usb, "UsbLink", return_value=link), \
                patch("builtins.print"):
            folder = Path(directory) / "session"
            with self.assertRaises(OSError): retention.prepare(None, folder)
            report = json.loads((folder / "prepare.json").read_text())
            self.assertTrue(report["restored"])
            self.assertEqual(bytes(link.memory), original)

    def test_changed_backup_blocks_capture_before_hardware_access(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            folder = Path(directory) / "session"
            with patch.object(retention.usb, "UsbLink", return_value=FakeLink(fixture())):
                retention.prepare(None, folder)
            original = bytearray((folder / "original.bin").read_bytes())
            original[10] ^= 1
            (folder / "original.bin").write_bytes(original)
            with patch.object(retention.usb, "UsbLink") as factory, self.assertRaises(ValueError):
                retention.capture(None, folder, Path(directory) / "bad")
            factory.assert_not_called()

    def test_keep_pattern_preserves_it_for_control_followed_by_host_transition(self):
        original, link = fixture(), FakeLink(fixture())
        with tempfile.TemporaryDirectory() as directory, patch.object(retention.usb, "UsbLink", return_value=link), \
                patch("builtins.print"):
            folder = Path(directory)
            retention.prepare(None, folder / "session")
            writes = link.writes
            report = retention.capture(None, folder / "session", folder / "control", restore=False)
            self.assertTrue(report["retention_passed"])
            self.assertFalse(report["restored"])
            self.assertEqual(link.writes, writes)
            report = retention.capture(None, folder / "session", folder / "after-H")
            self.assertTrue(report["retention_passed"] and report["restored"])
            self.assertEqual(bytes(link.memory), original)


if __name__ == "__main__":
    unittest.main()
