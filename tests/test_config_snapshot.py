from collections import deque
from types import SimpleNamespace
import unittest

from tools import config_snapshot as snapshot


def response(command, payload):
    raw = b"\x00\x59" + bytes([command]) + len(payload).to_bytes(3, "little")
    raw += payload + bytes([~sum(payload) & 255])
    return snapshot.encode7(raw)


class FakeMidi:
    def __init__(self, identity="FootCtrlPlus_142-dtnprt", unstable=False, wrong_prefix=False):
        self.identity = identity
        self.unstable = unstable
        self.wrong_prefix = wrong_prefix
        self.messages = deque()
        self.commands = []
        self.reads = 0
        self.closed = 0
        self.image = bytes(range(256)) * 92 + bytes(range(94))

    def get_input_names(self):
        return ["SINCO 1"]

    def get_output_names(self):
        return ["Microsoft GS Wavetable Synth 0", "SINCO 2"]

    def Message(self, kind, data):
        return SimpleNamespace(type=kind, data=data)

    def open_input(self, name):
        return self

    def open_output(self, name):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed += 1

    def poll(self):
        return self.messages.popleft() if self.messages else None

    def send(self, message):
        command, payload = snapshot.reply(message.data)
        self.commands.append((command, payload))
        if command == 0x11:
            body = self.identity.encode() + b"\0"
        elif command == 0x23:
            offset = int.from_bytes(payload[1:5], "little")
            length = int.from_bytes(payload[5:8], "little")
            body = payload + self.image[offset:offset + length]
            if self.wrong_prefix:
                body = b"\x05" + body[1:]
            if self.unstable and self.reads >= 47:
                body = body[:-1] + bytes([body[-1] ^ 1])
            self.reads += 1
        else:
            raise AssertionError(f"Unexpected device command: {command:#x}")
        self.messages.append(self.Message("sysex", response(command, body)))


class ProtocolTests(unittest.TestCase):
    def test_identity_request_matches_captured_wire_bytes(self):
        self.assertEqual(snapshot.request(0x11), bytes.fromhex("00 32 45 00 00 00 40 7F"))

    def test_no_mutating_command_is_encodable(self):
        for command in (0x15, 0x21, 0x22, 0x24):
            with self.subTest(command=command), self.assertRaises(snapshot.SnapshotError):
                snapshot.request(command)

    def test_read_ranges_are_bounded(self):
        for address, size in ((0, 0), (0, 513), (23645, 2), (0x100000, 1)):
            prefix = b"\x04" + address.to_bytes(4, "little") + size.to_bytes(3, "little")
            with self.subTest(address=address, size=size), self.assertRaises(snapshot.SnapshotError):
                snapshot.request(0x23, prefix)

    def test_reply_checks_checksum(self):
        raw = b"\x00\x59\x11\x01\x00\x00\x41\x00"
        with self.assertRaisesRegex(snapshot.SnapshotError, "checksum"):
            snapshot.reply(snapshot.encode7(raw))

    def test_reply_checks_length_and_padding(self):
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.reply(snapshot.encode7(b"\x00\x59\x11\x04\x00\x00\xFF"))
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.decode7(b"\x01")

    def test_redundant_vendor_zero_group(self):
        identity = snapshot.request(0x11)
        self.assertEqual(snapshot.reply(identity + b"\x00"), (0x11, b""))

    def test_sinco_discovery_and_two_complete_reads(self):
        midi = FakeMidi()
        copies, report = snapshot.capture(midi, "FootCtrlPlus_142-dtnprt")
        self.assertEqual(copies, [midi.image, midi.image])
        self.assertTrue(report["stable"])
        self.assertEqual(midi.reads, 94)
        self.assertEqual(midi.closed, 2)
        self.assertEqual({item[0] for item in midi.commands}, {0x11, 0x23})
        self.assertEqual(int.from_bytes(midi.commands[-1][1][5:], "little"), 94)

    def test_wrong_identity_stops_before_reads(self):
        midi = FakeMidi(identity="OTHER_DEVICE_142")
        with self.assertRaisesRegex(snapshot.SnapshotError, "expected"):
            snapshot.capture(midi, "FootCtrlPlus_142-dtnprt")
        self.assertEqual(midi.reads, 0)
        self.assertEqual(midi.closed, 2)

    def test_wrong_reply_prefix_aborts_capture(self):
        midi = FakeMidi(wrong_prefix=True)
        with self.assertRaisesRegex(snapshot.SnapshotError, "does not match"):
            snapshot.capture(midi, "FootCtrlPlus_142-dtnprt")
        self.assertEqual(midi.closed, 2)

    def test_unstable_configuration_is_reported(self):
        _, report = snapshot.capture(FakeMidi(unstable=True), "FootCtrlPlus_142-dtnprt")
        self.assertFalse(report["stable"])

    def test_ambiguous_or_unrelated_ports_rejected(self):
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.select_ports(["SINCO 1", "FootCtrlPlus 2"], ["SINCO 1", "FootCtrlPlus 2"])
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.select_ports(["TANK"], ["TANK"], "TANK", "TANK")

    def test_comparison_separates_ff_pattern_from_total(self):
        before = bytearray(snapshot.CONFIG_SIZE)
        after = before.copy()
        before[383], after[383] = 0xF0, 0xB0
        before[1088], after[1088] = 0xFF, 0xFC
        result = snapshot.compare(before, after)
        self.assertEqual((result["changed_bytes"], result["cleared_bits"]), (2, 3))
        self.assertEqual(result["cleared_ff_sysex_bits"], 2)
        self.assertEqual(result["changed_outside_sysex"], 0)
        self.assertEqual(result["changes"][0]["sysex"],
                         dict(page=1, switch="A", bank="B", byte=1))

    def test_comparison_counts_set_bits_and_outside_changes(self):
        before = bytes(snapshot.CONFIG_SIZE)
        after = bytearray(before)
        after[0] = 3
        result = snapshot.compare(before, after)
        self.assertEqual((result["set_bits"], result["changed_outside_sysex"]), (2, 1))
        self.assertTrue(snapshot.compare(before, before)["identical"])


if __name__ == "__main__":
    unittest.main()
