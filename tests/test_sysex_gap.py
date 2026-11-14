from collections import deque
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tools import sysex_gap as gap
from tools import config_snapshot as codec


class Device:
    def __init__(self):
        self.identity = gap.IDENTITY
        self.app = b"a" * gap.APP_BYTES
        self.module = b"extension" * 12
        self.manifest = dict(schema=1, identity=gap.IDENTITY, gap_step_ms=1, module_bytes=len(self.module),
                             application_sha256=codec.digest(self.app), module_sha256=codec.digest(self.module))
        self.ram = b"c" * gap.CONFIG_BYTES
        self.flash = self.ram + b"usrdata\0" + b"\xff" * (gap.FLASH_BYTES - gap.FOOTER)
        self.value = 20
        self.messages, self.commands = deque(), []
        self.ignore_write = self.lose_ack = self.damage_flash = False

    def Message(self, kind, data):
        return SimpleNamespace(type=kind, data=data)

    def poll(self):
        return self.messages.popleft() if self.messages else None

    def send(self, message):
        command, payload = codec.reply(message.data)
        self.commands.append((command, payload))
        if command == 0x11:
            body = self.identity.encode() + b"\0"
        elif command == 0x23:
            offset, length = int.from_bytes(payload[1:5], "little"), int.from_bytes(payload[5:8], "little")
            ranges = ((0, self.ram), (gap.APP_BASE - gap.RAM_BASE, self.app),
                      (gap.PATCH_ADDRESS - gap.RAM_BASE, self.module),
                      (gap.USR_VIEW - gap.RAM_BASE, self.flash),
                      (gap.OFFSET, gap.encode(self.value)), (0x19100 - gap.RAM_BASE, b"\0\x90\x03\0"))
            matches = [data[offset - start:offset - start + length] for start, data in ranges
                       if start <= offset < offset + length <= start + len(data)]
            assert len(matches) == 1
            body = payload + matches[0]
        elif command == 0x22:
            assert payload[:8] == b"\x04\0\0XS\x10\0\0"
            if not self.ignore_write:
                self.value = gap.decode(payload[8:])
                self.flash = self.ram + b"usrdata\0" + gap.encode(self.value)
                self.flash += b"\xff" * (gap.FLASH_BYTES - len(self.flash))
            if self.damage_flash:
                self.flash = b"!" + self.flash[1:]
            if self.lose_ack:
                raise TimeoutError("simulated lost ACK after device saved")
            command, body = 0, b"\0"
        else:
            raise AssertionError(command)
        raw = b"\0Y" + bytes([command]) + len(body).to_bytes(3, "little") + body + bytes([~sum(body) & 255])
        self.messages.append(self.Message("sysex", codec.encode7(raw)))


class GlobalGapTests(unittest.TestCase):
    def setUp(self):
        self.device = Device()
        self.link = gap.UsbLink(self.device, self.device, self.device)

    def identify(self):
        self.link.identify(self.device.manifest)

    def test_record_all_settings_and_corruptions(self):
        self.assertEqual(gap.encode(20).hex(), "5347415001011400ebffe8031400aa0c")
        for value in range(0, 1001):
            record = gap.encode(value)
            self.assertEqual(len(record), 16)
            self.assertEqual(gap.decode(record), value)
            for bit in range(128):
                changed = bytearray(record)
                changed[bit // 8] ^= 1 << (bit % 8)
                with self.assertRaises(ValueError):
                    gap.decode(changed)
        for value in (-10, -1, 0.5, 1.5, 1001, 1010, 65536, True, 20.0, "20"):
            with self.assertRaises(ValueError):
                gap.encode(value)

    def test_142_refused_before_memory_requests(self):
        self.device.identity = "FootCtrlPlus_142-dtnprt"
        with self.assertRaisesRegex(ValueError, "Richiede"):
            self.identify()
        self.assertEqual([c for c, _ in self.device.commands], [0x11])

    def test_older_ten_ms_manifest_is_refused(self):
        self.device.manifest["gap_step_ms"] = 10
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.identify()
        self.assertEqual([c for c, _ in self.device.commands], [0x11])

    def test_legacy_footer_migrates_but_is_not_a_current_wire_record(self):
        legacy = bytes.fromhex("53474150010a1e00e1ffe80314006f1f")
        with self.assertRaises(ValueError):
            gap.decode(legacy)
        self.assertEqual(gap.decode(legacy, allow_legacy=True), 30)
        self.identify()
        self.device.value = 30
        self.device.flash = self.device.flash[:gap.FOOTER] + legacy + self.device.flash[gap.FOOTER + 16:]
        with tempfile.TemporaryDirectory() as directory:
            result = gap.change(self.link, 21, Path(directory) / "run")
        self.assertEqual(result["status"], "saved_and_read_back")
        self.assertEqual(self.link.get(), 21)

    def test_changed_application_or_extension_prevents_setting_access(self):
        for attribute in ("app", "module"):
            self.setUp()
            setattr(self.device, attribute, b"!" + getattr(self.device, attribute)[1:])
            with self.assertRaisesRegex(ValueError, "Installed code"):
                self.identify()
            self.assertFalse(self.link.verified)
            with self.assertRaises(ValueError):
                self.link.get()
            self.assertEqual(self.link.writes, 0)

    def test_write_scope_and_identity_gate(self):
        with self.assertRaises(ValueError):
            self.link.set(30)
        self.identify()
        for address in (0, gap.OFFSET + 1, gap.OFFSET - 1, gap.APP_BASE):
            payload = b"\x04" + address.to_bytes(4, "little") + b"\x10\0\0" + gap.encode(30)
            with self.assertRaises(ValueError):
                self.link.exchange(0x22, payload)
        self.assertEqual(self.link.writes, 0)

    def test_global_change_preserves_banks_and_checks_flash(self):
        self.identify()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = gap.change(self.link, 21, output)
            self.assertEqual(result["status"], "saved_and_read_back")
            self.assertEqual(result["scope"], "global")
            self.assertTrue(result["configuration_unchanged"])
            self.assertEqual(self.link.writes, 1)
            self.assertEqual((output / "configuration-before.bin").read_bytes(), self.device.ram)
            self.assertEqual(gap.decode((output / "setting-before.bin").read_bytes()), 20)
            self.assertEqual(self.link.get(), 21)

    def test_unchanged_value_does_not_wear_flash(self):
        self.identify()
        with tempfile.TemporaryDirectory() as directory:
            result = gap.change(self.link, 20, Path(directory) / "run")
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(self.link.writes, 0)

    def test_unpersisted_configuration_and_unknown_footer_are_refused(self):
        for offset in (0, gap.FOOTER, gap.FOOTER + 16):
            self.setUp()
            self.identify()
            value = bytearray(self.device.flash)
            value[offset] ^= 1
            self.device.flash = bytes(value)
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    gap.change(self.link, 30, Path(directory) / "run")
            self.assertEqual(self.link.writes, 0)

    def test_ack_is_not_treated_as_persistence(self):
        for attribute in ("ignore_write", "damage_flash"):
            self.setUp()
            self.identify()
            setattr(self.device, attribute, True)
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "run"
                with self.assertRaises(ValueError):
                    gap.change(self.link, 30, output)
                self.assertEqual(json.loads((output / "report.json").read_text())["status"], "stopped")
            self.assertEqual(self.link.writes, 1)

    def test_lost_ack_keeps_backup_and_does_not_retry(self):
        self.identify()
        self.device.lose_ack = True
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with self.assertRaises(TimeoutError):
                gap.change(self.link, 30, output)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["protocol_write_attempts"], 1)
            self.assertTrue((output / "usrflash-before.bin").is_file())
        self.assertEqual(self.device.value, 30)
        self.assertEqual(self.link.writes, 1)


if __name__ == "__main__":
    unittest.main()
