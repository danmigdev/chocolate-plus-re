"""Radio isolation must use the native request and recover after a lost ACK."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import radio_142 as radio
from tests.test_tank_relay_142 import Controller, Device, config

host, usb = radio.host, radio.usb


class RadioController(Controller):
    def __init__(self, original, tank=None, lost_ack=False):
        super().__init__(original)
        self.native = bytearray(180)
        self.native[164] = self.native[168] = 1
        self.native[5] = 0x43
        self.native[70] = 0x50
        self.tank, self.lost_ack = tank, lost_ack
        self.requests = []

    def read(self, address, count):
        if address == usb.STATE: return bytes(self.native[:count])
        if address == radio.RADIO_FLAG: return bytes(self.memory[23651:23652])
        return super().read(address, count)

    def write(self, address, data):
        self.requests.append((address, data))
        if address == radio.BC_REQUEST:
            assert (address, data) in self.allow and data == b"\x90\x01"
            self.memory[23651] ^= 1
            enabled = 1 - self.memory[23651]
            self.native[164] = self.native[168] = enabled
            self.native[70] = 0x50 if enabled else 0
            if self.lost_ack:
                self.lost_ack = False
                raise TimeoutError("Lost B+C acknowledgement after radio changed")
        else:
            super().write(address, data)
            if address == usb.KEYS + 6 and data == b"\0\x64" and self.native[168] and self.tank:
                self.tank.memory[8] ^= 1


class RadioTests(unittest.TestCase):
    def test_stale_off_flag_does_not_hide_a_live_radio_and_only_native_request_is_written(self):
        original = config()[:-1] + b"\1"
        link = RadioController(original)
        link.allow = radio.RADIO_WRITES
        result = radio.set_enabled(link, False)
        self.assertTrue(result["before"]["enabled"])
        self.assertEqual(result["before"]["off_flag"], 1)
        self.assertFalse(result["after"]["enabled"])
        self.assertEqual(result["after"]["client_handle"], 0)
        self.assertEqual(link.requests, [(radio.RADIO_FLAG, b"\0"), (radio.BC_REQUEST, b"\x90\x01")])
        self.assertEqual(bytes(link.memory), original)
        self.assertEqual(radio.set_enabled(link, False)["writes"], 0)

    def test_busy_keys_or_unknown_radio_state_prevent_writes(self):
        for offset, value in ((76, 1), (168, 2)):
            link = RadioController(config())
            link.native[offset] = value
            link.allow = radio.RADIO_WRITES
            with self.assertRaises(ValueError): radio.set_enabled(link, False)
            self.assertEqual(link.requests, [])

    def test_route_comparison_and_lost_ACK_both_restore_devices_and_radio(self):
        for lost_ack in (False, True):
            original = config()[:-1] + b"\1"
            tank, preparer = Device(), Controller(original)
            with self.subTest(lost_ack=lost_ack), tempfile.TemporaryDirectory() as directory, \
                    patch("builtins.print"), patch.object(radio.time, "sleep"), \
                    patch.object(host.relay, "Tank", return_value=tank):
                root = Path(directory)
                with patch.object(usb, "UsbLink", return_value=preparer):
                    host.prepare(None, root / "session", "split")
                    host.recover(None, root / "session", root / "recovery")
                link = RadioController(original, tank, lost_ack)
                with patch.object(usb, "UsbLink", return_value=link):
                    if lost_ack:
                        with self.assertRaises(TimeoutError): radio.run(None, root / "session", root / "result")
                    else:
                        result = radio.run(None, root / "session", root / "result")
                        self.assertTrue(result["passed"] and result["radio_path"]["passed"])
                        self.assertEqual(result["radio_path"]["PC_forwarded_CC"], 0)
                result = json.loads((root / "result/report.json").read_text())
                self.assertTrue(result["tank_restored"] and result["plus_restored"] and result["radio_restored"])
                self.assertEqual(bytes(link.memory), original)
                self.assertEqual(tank.stable(), tank.original)
                self.assertTrue(radio.state(link)["enabled"])

    def test_HOST_handoff_calibrates_with_radio_off_and_restores_radio_and_original_flag(self):
        original = config()[:-1] + b"\1"
        tank = Device()
        link = RadioController(original, tank)
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"), \
                patch.object(radio.time, "sleep"), patch.object(host.relay, "Tank", return_value=tank), \
                patch.object(usb, "UsbLink", return_value=link):
            root = Path(directory)
            result = host.prepare(None, root / "session", "split-radio")
            blobs = host.load(root / "session")
            self.assertTrue(all(check["passed"] for check in result["USB_calibration"]))
            self.assertTrue(result["requires_physical_BC_radio_off_after_H_start"])
            self.assertEqual(bytes(link.memory), blobs["plus-staged"])
            self.assertEqual(link.memory[-1], 0)
            self.assertTrue(radio.state(link)["enabled"])
            self.assertEqual(tank.stable(), blobs["tank-staged"])
            radio.set_enabled(link, False)  # Represents the separate physical B+C step in H.
            measured = host.compare_plus_storage(blobs, bytes(link.memory), link.read(usb.USR_VIEW, usb.FLASH_BYTES))
            self.assertEqual(measured["radio_off_marker"]["ram"], 1)
            self.assertTrue(measured["configuration_data_intact"] and measured["saved_data_intact"])
            recovered = host.recover(None, root / "session", root / "recovery")
            self.assertTrue(recovered["tank_restored"] and recovered["plus_restored"])
            self.assertTrue(radio.state(link)["enabled"])
            self.assertEqual(bytes(link.memory), original)
            self.assertEqual(tank.stable(), tank.original)


if __name__ == "__main__": unittest.main()
