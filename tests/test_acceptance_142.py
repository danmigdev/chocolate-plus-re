"""Write-scope and restoration checks for the automatic hardware diagnostic."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import acceptance_142 as test


def fixture():
    data = bytearray(test.CONFIG_BYTES)
    data[0] = 3
    for switch in (0, 1):
        base = 93 + switch * 417
        data[base] = 1
        for bank in (0, 1):
            row = base + 1 + bank * 80
            data[row:row + 5] = b"\x01\0\x04\0\0"
            payload = bytes([0xF0, 0x7D, switch + 1, bank, 0xF7])
            if switch == 1 and bank == 0:
                payload += b"\xf0\x7d\x03\x05\xf7"
            area = base + 161 + bank * 128
            data[area] = len(payload)
            data[area + 1:area + 1 + len(payload)] = payload
    return bytes(data)


class FakeLink:
    def __init__(self, original, failure=None):
        self.original = original
        self.memory = bytearray(original)
        self.flips = bytearray(4)
        self.observed = []
        self.allow = ()
        self.writes = 0
        self.failure = failure

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def read(self, address, count):
        if address == test.RAM_BASE: return bytes(self.memory[:count])
        if address == test.USR_VIEW:
            return bytes(self.memory) + b"usrdata\0" + bytes(test.FLASH_BYTES - test.CONFIG_BYTES - 8)
        if address == test.FLIPS: return bytes(self.flips)
        if address == test.KEYS: return bytes(8)
        if address == test.STATE + 82: return bytes(2)
        raise AssertionError("Unknown read")

    def write(self, address, data):
        assert (address, data) in self.allow, "Unexpected write"
        self.writes += 1
        if test.KEYS <= address < test.KEYS + 4 and data == b"\0\x64":
            switch = (address - test.KEYS) // 2
            bank = 1 - self.flips[switch]
            payload = bytes([0xF0, 0x7D, switch + 1, bank, 0xF7])
            self.observed.append(payload)
            if switch == 1 and bank == 0:
                self.observed.append(b"\xf0\x7d\x03\x05\xf7")
            self.flips[switch] ^= 1
            if self.failure == "wrong_usb":
                self.observed = [b"\xc0\x01"]
                self.failure = None
        elif address == test.KEYS:
            assert data == bytes(8)
        elif address == test.FLIPS:
            self.flips[:] = data
        else:
            offset = address - test.RAM_BASE
            self.memory[offset:offset + len(data)] = data
            if self.failure == "lost_pattern_ack" and data == b"\xff" * 127:
                self.failure = None
                raise OSError("Lost write acknowledgement after memory changed")

    def collect(self, seconds=0.45):
        result, self.observed = self.observed, []
        return result


class AcceptanceTests(unittest.TestCase):
    def test_padding_never_overlaps_active_assignments_rows_or_length_bytes(self):
        original = fixture()
        spans = test.padding_spans(original)
        self.assertEqual(len(spans), 60)
        target = test.pattern_image(original, spans, b"\xff")
        allowed = {p for offset, count in spans for p in range(offset, offset + count)}
        self.assertEqual(len(allowed), 7620)
        self.assertEqual(target[:93 + 2 * 417], original[:93 + 2 * 417])
        self.assertTrue(all(a == b for p, (a, b) in enumerate(zip(original, target)) if p not in allowed))
        for switch in range(2, 32):
            for bank in range(2):
                self.assertEqual(target[93 + switch * 417 + 161 + bank * 128], 0)

    def test_in_use_bank_or_additional_pages_block_pattern_test(self):
        for offset in (23639, 23640, 93 + 31 * 417 + 161):
            data = bytearray(fixture())
            data[offset] = 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                test.padding_spans(data)
        data=bytearray(fixture())
        data[93+2*417+1:93+2*417+6]=b'\x01\0\x04\0\0'
        with self.assertRaises(ValueError): test.padding_spans(data)

    def test_vendor_pc_defaults_do_not_use_sysex_storage(self):
        data=bytearray(fixture())
        data[93+31*417+1:93+31*417+6]=b'\x01\0\0\0\0'
        self.assertEqual(len(test.padding_spans(data)),60)

    def test_sysex_split_and_invalid_bank_rejection(self):
        self.assertEqual(test.split_sysex(b"\xf0\x7d\xf7\xf0\x01\xf7"),
                         [b"\xf0\x7d\xf7", b"\xf0\x01\xf7"])
        for value in (b"\xb0\x01", b"\xf0\x01", b"\xf0\x80\xf7"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                test.split_sysex(value)

    def test_unknown_commands_and_unlisted_writes_stop_before_midi_send(self):
        link = test.UsbLink(None)
        for command in (0x21, 0x24, 0x35, 0xFF):
            with self.assertRaises(ValueError): link.exchange(command, b"", 0)
        for offset in (0, 123, test.KEYS - test.RAM_BASE, 0x1E00000):
            payload = b"\x04" + offset.to_bytes(4, "little") + b"\x01\0\0\xff"
            with self.assertRaises(ValueError): link.exchange(0x22, payload, 0)
        self.assertEqual(link.writes, 0)

    def test_completed_run_restores_all_configuration_and_alternating_state(self):
        original = fixture()
        link = FakeLink(original)
        with tempfile.TemporaryDirectory() as path, patch.object(test, "UsbLink", return_value=link), \
                patch("builtins.print"):
            report = test.run(None, Path(path) / "capture")
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["tests_passed"] and report["restored"])
        self.assertEqual(len(report["key_tests"]), 4)
        self.assertEqual(len(report["save_tests"]), 3)
        self.assertEqual(bytes(link.memory), original)
        self.assertEqual(bytes(link.flips), bytes(4))

    def test_observation_failure_or_lost_ack_restores_before_raising(self):
        for failure in ("wrong_usb", "lost_pattern_ack"):
            original = fixture()
            link = FakeLink(original, failure)
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as path, \
                    patch.object(test, "UsbLink", return_value=link), patch("builtins.print"):
                with self.assertRaises((OSError, ValueError)):
                    test.run(None, Path(path) / "capture")
            self.assertEqual(bytes(link.memory), original)
            self.assertEqual(bytes(link.flips), bytes(4))


if __name__ == "__main__":
    unittest.main()
