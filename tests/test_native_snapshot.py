"""Validate native capture boundaries and the optional boot-entry preconditions."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tools import config_snapshot as config
from tools import native_snapshot as native


class FakeMidi:
    def __init__(self, profile="142", unsaved=False, bad_app=False):
        self.profile = profile
        self.identity = native.PROFILES[profile]["identity"]
        self.app = bytes([37]) * native.PROFILES[profile]["app_size"]
        self.ram = bytes([17]) * config.CONFIG_SIZE + bytes(6)
        self.flash = self.ram + b"usrdata\0" + b"\xff" * (0x6000 - 23660)
        self.original = bytes(0x39000) + self.flash + bytes(0x1000)
        self.memory = [(native.APP_BASE, self.app), (native.RAM_BASE, self.ram),
                       (native.USR_VIEW, self.flash), (0x19100, b"\x00\x90\x03\x00"),
                       (native.BOOT_RAM, bytes(native.BOOT_SIZE)), (0x193A0, bytes(260))]
        if unsaved:
            self.memory[1] = (native.RAM_BASE, bytes(native.NATIVE_CONFIG_BYTES))
        if bad_app:
            self.memory[0] = (native.APP_BASE, bytes(len(self.app)))
        self.pending, self.sent, self.reads = [], [], []
        self.input_open = False
        self.boot_sent_with_input_open = None

    def get_input_names(self): return ["SINCO 0"]
    def get_output_names(self): return ["SINCO 1"]

    class Message:
        def __init__(self, kind, data):
            self.type, self.data = kind, data

        @staticmethod
        def from_bytes(data):
            assert data == native.BOOT_COMMAND
            return SimpleNamespace(type="boot", data=data)

    def port(self, incoming):
        owner = self

        class Port:
            def __enter__(self):
                if incoming: owner.input_open = True
                return self

            def __exit__(self, *args):
                if incoming: owner.input_open = False

            def poll(self): return owner.pending.pop(0) if owner.pending else None
            def send(self, message): owner.send(message)

        return Port()

    def open_input(self, name): return self.port(True)
    def open_output(self, name): return self.port(False)

    def send(self, message):
        if message.type == "boot":
            self.boot_sent_with_input_open = self.input_open
            self.sent.append(0x7d)
            return
        command, prefix = config.reply(message.data)
        self.sent.append(command)
        if command == 0x11:
            body = self.identity.encode() + b"\0"
        elif command == 0x23:
            address = int.from_bytes(prefix[1:5], "little") + native.RAM_BASE
            count = int.from_bytes(prefix[5:8], "little")
            self.reads.append((address, count))
            matches = [data[address - base:address - base + count] for base, data in self.memory
                       if base <= address < address + count <= base + len(data)]
            assert len(matches) == 1, "Unexpected memory read"
            body = prefix + matches[0]
        else:
            raise AssertionError("Mutation sent")
        raw = b"\0\x59" + bytes([command]) + len(body).to_bytes(3, "little")
        raw += body + bytes([~sum(body) & 255])
        self.pending.append(self.Message("sysex", config.encode7(raw)))


class NativeTests(unittest.TestCase):
    def test_extension_mismatch_prevents_boot_for_143_and_legacy_150(self):
        for name in ("143", "150"):
            midi = FakeMidi(name)
            profile = dict(native.PROFILES[name], hashes={config.digest(midi.app)})
            midi.memory.append((profile["module_base"], bytes(profile["module_bytes"])))
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as path, \
                    patch.dict(native.PROFILES, {name: profile}), \
                    patch.object(native, "BOOT_SHA256", config.digest(bytes(native.BOOT_SIZE))):
                with self.assertRaisesRegex(ValueError, "extension differs"):
                    native.capture(midi, name, Path(path) / "capture", enter_boot=True)
            self.assertNotIn(0x7d, midi.sent)

    def test_unsaved_native_bluetooth_flag_is_detected_before_boot(self):
        midi = FakeMidi()
        ram = bytearray(midi.ram)
        ram[23651] = 1
        midi.memory[1] = (native.RAM_BASE, bytes(ram))
        profile = dict(native.PROFILES["142"], hashes={config.digest(midi.app)})
        with tempfile.TemporaryDirectory() as path, patch.dict(native.PROFILES, {"142": profile}), \
                patch.object(native, "BOOT_SHA256", config.digest(bytes(native.BOOT_SIZE))):
            with self.assertRaisesRegex(ValueError, "differ from saved flash"):
                native.capture(midi, "142", Path(path) / "capture", enter_boot=True)
        self.assertNotIn(0x7d, midi.sent)

    def test_fixed_ranges_reject_arbitrary_or_cross_boundary_reads(self):
        for profile in native.PROFILES:
            for address, size in ((0, 1), (native.RAM_BASE, 513),
                                  (native.RAM_BASE + native.NATIVE_CONFIG_BYTES - 1, 2),
                                  (native.USR_VIEW + 0x6000, 1), (0x1E40000, 256)):
                with self.subTest(profile=profile, address=address), self.assertRaises(ValueError):
                    native.read_request(profile, address, size)
            packet, prefix = native.read_request(profile, native.APP_BASE, 512)
            self.assertEqual(config.reply(packet), (0x23, prefix))

    def test_142_boot_requires_audited_relocated_routine(self):
        midi = FakeMidi()
        profile = dict(native.PROFILES["142"], hashes={config.digest(midi.app)})
        with tempfile.TemporaryDirectory() as path, patch.dict(native.PROFILES, {"142": profile}):
            with self.assertRaisesRegex(ValueError, "boot routine differs"):
                native.capture(midi, "142", Path(path) / "capture", enter_boot=True)
        self.assertNotIn(0x7d, midi.sent)

    def test_142_boot_sent_once_after_verified_capture_and_input_close(self):
        midi = FakeMidi()
        profile = dict(native.PROFILES["142"], hashes={config.digest(midi.app)})
        with tempfile.TemporaryDirectory() as path, patch.dict(native.PROFILES, {"142": profile}), \
                patch.object(native, "BOOT_SHA256", config.digest(bytes(native.BOOT_SIZE))):
            report = native.capture(midi, "142", Path(path) / "capture", enter_boot=True)
        self.assertFalse(midi.boot_sent_with_input_open)
        self.assertEqual(midi.sent.count(0x7d), 1)
        self.assertEqual(report["entry_requests"], 1)
        self.assertTrue(report["ram_matches_saved_flash"])

    def test_complete_native_capture_matches_backup_and_never_writes(self):
        midi = FakeMidi()
        profile = dict(native.PROFILES["142"], hashes={config.digest(midi.app)})
        with tempfile.TemporaryDirectory() as path, patch.dict(native.PROFILES, {"142": profile}):
            report = native.capture(midi, "142", Path(path) / "capture", midi.original)
            self.assertEqual(report["status"], "native_snapshot_verified")
            self.assertTrue(report["configuration_matches_backup"])
            self.assertTrue(report["usrflash_matches_backup"])
            self.assertEqual(set(midi.sent), {0x11, 0x23})
            self.assertEqual(report["entry_requests"], 0)

    def test_unknown_application_stops_before_configuration_reads(self):
        midi = FakeMidi(bad_app=True)
        profile = dict(native.PROFILES["142"], hashes={config.digest(midi.app)})
        with tempfile.TemporaryDirectory() as path, patch.dict(native.PROFILES, {"142": profile}):
            with self.assertRaisesRegex(ValueError, "audited build"):
                native.capture(midi, "142", Path(path) / "capture")
        self.assertTrue(all(address >= native.APP_BASE for address, _ in midi.reads))

    def test_unsaved_configuration_prevents_013_boot_entry(self):
        midi = FakeMidi("013", unsaved=True)
        profile = dict(native.PROFILES["013"], hashes={config.digest(midi.app)})
        with tempfile.TemporaryDirectory() as path, patch.dict(native.PROFILES, {"013": profile}), \
                patch.object(native, "BOOT_SHA256", config.digest(bytes(native.BOOT_SIZE))):
            with self.assertRaisesRegex(ValueError, "differ from saved flash"):
                native.capture(midi, "013", Path(path) / "capture", enter_boot=True)
            self.assertEqual(set(midi.sent), {0x11, 0x23})
            self.assertFalse(midi.input_open)


if __name__ == "__main__":
    unittest.main()
