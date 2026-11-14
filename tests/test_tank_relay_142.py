"""Relay scope, actual byte changes, and recovery from transmission failures."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import tank_relay_142 as relay
from tests.test_acceptance_142 import FakeLink


def config():
    data = bytearray(relay.plus.CONFIG_BYTES)
    data[0] = 3
    banks = ((relay.live_frame(10, b"\1"), relay.live_frame(10, b"\0")),
             (relay.live_frame(8, b"\1") + relay.live_frame(70, b"\x0a\0"),
              relay.write_frame(relay.TANK_STATE + 1, b"\xff")))
    for switch, frames in enumerate(banks):
        base = 93 + switch * 417
        data[base] = 1
        for bank, frame in enumerate(frames):
            row, area = base + 1 + bank * 80, base + 161 + bank * 128
            data[row:row + 5] = b"\1\0\4\0\0"
            data[area] = len(frame)
            data[area + 1:area + 1 + len(frame)] = frame
    return bytes(data)


class Controller(FakeLink):
    def write(self, address, data):
        if relay.plus.KEYS <= address < relay.plus.KEYS + 8 and data == b"\0\x64":
            assert (address, data) in self.allow
            switch = (address - relay.plus.KEYS) // 2
            bank = 1 - self.flips[switch]
            self.flips[switch] ^= 1
            if switch == 3:
                offset = 93 + 3 * 417 + 1 + 80 * bank
                assert self.memory[offset:offset + 80] == b"\1\0\1\0\0" + bytes(75)
                self.observed.append(b"\xb0\0\0")
            else:
                self.observed.extend(relay.plus.expected_frames(bytes(self.memory), switch, bank))
            self.memory[23646] = switch
            if self.failure == "bad_frame":
                self.observed = [b"\xb0\0\1"]
                self.failure = None
        else:
            super().write(address, data)


class Device:
    def __init__(self, failure=None):
        self.memory = bytearray(92)
        self.memory[0], self.memory[70] = 80, 17
        self.original = bytes(self.memory)
        self.saved = bytes(92)
        self.allowed = set()
        self.failure = failure
        self.sent = []

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def snapshot(self):
        return dict(state=bytes(4), live=bytes(self.memory), ram=self.saved * 21 + bytes(10), flash=bytes(1942))
    def stable(self, delay=0.08): return bytes(self.memory)
    def write(self, offset, data): self.transmit(relay.live_frame(offset, data))
    def transmit(self, frame):
        assert frame in self.allowed
        if frame == b"\xb0\0\0":
            self.memory[8] = 1 - self.memory[8]
        else:
            address, data = relay.decode_write(frame)
            if address == relay.TANK_STATE + 1:
                self.memory[:] = self.saved
            else:
                self.memory[address - relay.LIVE:address - relay.LIVE + len(data)] = data
        self.sent.append(frame)
        if self.failure == "lost_ack":
            self.failure = None
            raise TimeoutError("Lost acknowledgement after TANK write")
    def wait(self, *args): return b"\0"
    def restore(self, original): self.memory[:] = original


class RelayTests(unittest.TestCase):
    def test_golden_on_off_parameter_and_reload_wires(self):
        self.assertEqual(relay.live_frame(70, b"\x0a\0").hex(" "),
                         "f0 00 32 09 51 00 00 00 02 46 00 00 00 21 00 00 00 0a 00 64 04 f7")
        before = bytes(92)
        after = relay.expected_live(before, [relay.live_frame(8, b"\1"), relay.live_frame(70, b"\5\0")], bytes(92))
        self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b], [8, 70])
        self.assertEqual(after[8], 1)
        self.assertEqual(after[70:72], b"\5\0")

    def test_saves_firmware_out_of_bounds_and_unknown_packets_never_transmit(self):
        device = relay.Tank(None)
        frames = [relay.packet(op) for op in (0x21, 0x24, 0x35)]
        frames += [relay.write_frame(a, b"\0") for a in (0, 1942, relay.FLASH_BANK, relay.TANK_STATE, relay.LIVE + 92)]
        frames += [relay.live_frame(70, b"\5\0")]  # Not in this session's allow-list.
        device.allowed = set(frames[:-1])
        for frame in frames:
            with self.subTest(frame=frame.hex()), self.assertRaises(ValueError):
                device.transmit(frame)
        with self.assertRaises(ValueError):
            relay.expected_live(bytes(92), [relay.live_frame(14, b"\1")], bytes(92))

    def test_temporary_assignment_rejects_existing_user_assignment(self):
        original = bytearray(config())
        original[relay.C_OFFSET + 90] = 1
        with self.assertRaises(ValueError): relay.temporary_c(original)

    def test_actual_emissions_are_forwarded_and_both_devices_restored(self):
        controller, tank = Controller(config()), Device()
        with tempfile.TemporaryDirectory() as folder, patch.object(relay.plus, "UsbLink", return_value=controller), \
                patch.object(relay, "Tank", return_value=tank), patch("builtins.print"):
            report = relay.run(None, Path(folder) / "capture")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["checks"]), 6)
        self.assertTrue(report["tank_restored"] and report["plus_restored"])
        self.assertEqual(tank.stable(), tank.original)
        self.assertEqual(bytes(controller.memory), config())
        values = [bytes.fromhex(c["after"])[70] for c in report["checks"] if c["switch"] == "C"]
        self.assertEqual(values, [75, 25])
        self.assertFalse(report["host_tested"])

    def test_unexpected_output_and_lost_tank_ack_restore_both_devices(self):
        for failure in ("bad_frame", "lost_ack"):
            controller, tank = Controller(config(), failure=failure), Device(failure=failure)
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder, \
                    patch.object(relay.plus, "UsbLink", return_value=controller), \
                    patch.object(relay, "Tank", return_value=tank), patch("builtins.print"):
                with self.assertRaises((ValueError, TimeoutError)):
                    relay.run(None, Path(folder) / "capture")
            self.assertEqual(bytes(controller.memory), config())
            self.assertEqual(bytes(controller.flips), bytes(4))
            self.assertEqual(tank.stable(), tank.original)

    def test_restoration_handles_model_default_reload_before_restoring_parameters(self):
        original = bytearray(92)
        original[0], original[8], original[14] = 80, 1, 2
        original[20:32] = bytes(range(12))
        current = bytearray(original)
        current[0], current[8], current[14] = 30, 0, 1
        current[20:32] = b"\x64" * 12
        device = relay.Tank(None)
        writes = []
        def write(offset, data):
            writes.append((offset, data))
            current[offset:offset + len(data)] = data
            if offset == 14:
                current[20:32] = b"\x32" * 12  # Firmware's asynchronous model defaults.
        with patch.object(device, "stable", side_effect=lambda *args: bytes(current)), \
                patch.object(device, "write", side_effect=write):
            device.restore(bytes(original))
        self.assertEqual(current, original)
        self.assertLess(writes.index((14, b"\2")), writes.index((20, bytes(range(12)))))
        self.assertLess(writes.index((20, bytes(range(12)))), writes.index((8, b"\1")))


if __name__ == "__main__":
    unittest.main()
