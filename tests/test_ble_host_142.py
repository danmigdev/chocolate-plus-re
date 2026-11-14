"""BLE HOST command boundaries, route isolation and restoration on failure."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tools import ble_host_142 as host


def client(size=143):
    return SimpleNamespace(services=SimpleNamespace(get_characteristic=lambda _: SimpleNamespace(
        max_write_without_response_size=size)), write_gatt_char=AsyncMock())


def baseline():
    config = bytearray(host.usb.CONFIG_BYTES)
    config[0], config[100], config[-1] = 3, 0xAB, 1
    live = bytearray(92)
    live[0], live[8], live[12], live[70] = 80, 1, 1, 10
    return {"plus-original": bytes(config), "plus-saved": bytes(config) + b"usrdata\0" + bytes(host.usb.FLASH_BYTES - len(config) - 8),
            "plus-flips": b"\0\1\0\1", "tank-live": bytes(live), "tank-state": b"\x14\x14\0\6",
            "tank-ram": bytes(host.relay.BANK_SIZE), "tank-flash": bytes(host.relay.BANK_SIZE)}


class FakeTank:
    def __init__(self, blobs):
        self.data = {key: blobs["tank-" + key] for key in ("live", "state", "ram", "flash")}
        self.live = bytearray(self.data["live"])
        self.writes, self.allow = 0, set()

    def arm(self, writes):
        self.allow = set(writes)

    async def read(self, address, size, kind=4):
        if address == host.relay.TANK_STATE:
            return self.data["state"]
        return bytes(self.live[address - host.relay.LIVE:address - host.relay.LIVE + size])

    async def write(self, address, data):
        if (address, data) not in self.allow:
            raise ValueError("Unlisted fake TANK write")
        self.writes += 1
        self.live[address - host.relay.LIVE:address - host.relay.LIVE + len(data)] = data

    async def tank_snapshot(self):
        return dict(self.data, live=bytes(self.live))


class FakePlus:
    def __init__(self, blobs, tank, *, pair_fails=False, lost_ack=False):
        self.config, self.flash = bytearray(blobs["plus-original"]), blobs["plus-saved"]
        self.state = bytearray(260)
        self.state[43:47] = blobs["plus-flips"]
        self.state[164] = self.state[168] = self.state[228] = 1
        self.state[256:260] = host.HOST_SENDER.to_bytes(4, "little")
        self.state[68:70] = b"\x50\0"
        self.tank, self.pair_fails, self.lost_ack = tank, pair_fails, lost_ack
        self.allow, self.keys, self.writes = set(), [], 0

    def arm(self, writes):
        self.allow = set(writes)

    async def read(self, address, size):
        if address == host.usb.KEYS:
            return bytes(size)
        for base, data in ((host.usb.RAM_BASE, self.config), (host.usb.USR_VIEW, self.flash), (host.usb.STATE, self.state)):
            if base <= address < address + size <= base + len(data):
                return bytes(data[address - base:address - base + size])
        raise ValueError("Invalid fake read")

    async def write(self, address, data):
        if (address, data) not in self.allow:
            raise ValueError("Unlisted fake Plus write")
        self.writes += 1
        if host.usb.KEYS <= address < host.usb.KEYS + 8:
            if data == b"\0\x64":
                key = (address - host.usb.KEYS) // 2
                self.keys.append(key)
                self.state[43 + key] ^= 1
                frames = host.usb.expected_frames(self.config, key, self.state[43 + key])
                self.config[23646] = key
                if not (len(frames) == 2 and self.pair_fails):
                    for frame in frames:
                        target, value = host.relay.decode_write(frame)
                        offset = target - host.relay.LIVE
                        self.tank.live[offset:offset + len(value)] = value
        elif address == host.usb.FLIPS:
            self.state[43:47] = data
        else:
            offset = address - host.usb.RAM_BASE
            self.config[offset:offset + len(data)] = data
            self.flash = bytes(self.config) + self.flash[host.usb.CONFIG_BYTES:]
            if self.lost_ack:
                self.lost_ack = False
                raise ConnectionError("Lost configuration acknowledgement")


class GateTests(unittest.TestCase):
    def test_paired_cache_hook_requires_the_audited_backend_and_version(self):
        backend = type("BleakClientWinRT", (), {"_device_info": None})()
        peer = SimpleNamespace(_backend=backend)
        with self.assertRaises(ValueError):
            host.use_paired_cache(peer, "11:22:33:44:55:66", "3.1.0")
        self.assertIsNone(backend._device_info)
        host.use_paired_cache(peer, "11:22:33:44:55:66", "3.0.2")
        self.assertEqual(backend._device_info, 0x112233445566)
        with self.assertRaises(ValueError):
            host.use_paired_cache(SimpleNamespace(_backend=object()), "11:22:33:44:55:66", "3.0.2")

    def test_pre_GATT_pairing_is_bound_to_previous_exact_identity_and_address(self):
        report = {"peers": {"FootCtrlPlus": {"identity": "FootCtrlPlus_142-dtnprt", "address": "11:22:33:44:55:66"}}}
        host.verify_pairing_evidence(report, "11:22:33:44:55:66")
        with self.assertRaises(ValueError):
            host.verify_pairing_evidence(report, "11:22:33:44:55:77")
        report["peers"]["FootCtrlPlus"]["identity"] = "FootCtrlPlus_013"
        with self.assertRaises(ValueError):
            host.verify_pairing_evidence(report, "11:22:33:44:55:66")

    def test_unknown_commands_and_unverified_reads_never_send(self):
        link = host.Channel(client(), True)
        for op in range(256):
            if op == 0x11:
                continue
            with self.subTest(command=op), self.assertRaises(ValueError):
                link.guard(op, b"")
        with self.assertRaises(ValueError):
            link.guard(0x23, link.descriptor(host.usb.STATE, 1))
        self.assertEqual(link.client.write_gatt_char.await_count, 0)

    def test_even_allow_list_cannot_write_firmware_radio_callbacks_or_presets(self):
        for is_plus, addresses in ((True, (host.usb.APP_BASE, host.usb.USR_VIEW, host.usb.STATE + 76, host.usb.STATE + 256)),
                                   (False, (0, host.relay.TANK_STATE + 1, host.relay.FLASH_BANK))):
            link = host.Channel(client(), is_plus)
            link.verified = link.pinned = True
            for address in addresses:
                link.arm([(address, b"\0")])
                with self.subTest(plus=is_plus, address=address), self.assertRaises(ValueError):
                    asyncio.run(link.write(address, b"\0"))
            self.assertEqual(link.client.write_gatt_char.await_count, 0)

    def test_entire_chunked_write_is_checked_before_first_submission(self):
        link = host.Channel(client(20), True)
        link.verified = link.pinned = True
        link.allow = {(host.usb.RAM_BASE + host.C, bytes(5))}
        with self.assertRaises(ValueError):
            asyncio.run(link.write(host.usb.RAM_BASE + host.C, bytes(10)))
        self.assertEqual(link.client.write_gatt_char.await_count, 0)

    def test_changed_application_cannot_arm_native_writes(self):
        link = host.Channel(client(), True)
        link.verified = True
        link.read = AsyncMock(return_value=b"unrecognized application")
        with self.assertRaisesRegex(ValueError, "pinned 142"):
            asyncio.run(link.verify_app())
        self.assertFalse(link.pinned)
        link.arm([(host.usb.KEYS + 4, b"\0\x64")])
        with self.assertRaises(ValueError):
            asyncio.run(link.write(host.usb.KEYS + 4, b"\0\x64"))

    def test_only_audited_client_slot_is_used_and_adjacent_fields_are_preserved(self):
        blobs = baseline()
        plus = FakePlus(blobs, FakeTank(blobs))
        plus.state[72:76] = b"\x0a\0\xff\3"
        self.assertEqual(asyncio.run(host.host_ready(plus))["client_handle"], 0)
        plus.state[70] = 80
        with self.assertRaisesRegex(ValueError, "route is not isolated"):
            asyncio.run(host.host_ready(plus))
        plus.state[70] = 0
        plus.state[4] = 1
        with self.assertRaisesRegex(ValueError, "client index"):
            asyncio.run(host.host_ready(plus))
        plus.state[4] = 0
        plus.state[256:260] = bytes(4)
        with self.assertRaisesRegex(ValueError, "HOST output"):
            asyncio.run(host.host_ready(plus))


class MatrixTests(unittest.TestCase):
    def exercise(self, pair_fails=False, lost_ack=False):
        blobs, report = baseline(), {}
        tank = FakeTank(blobs)
        plus = FakePlus(blobs, tank, pair_fails=pair_fails, lost_ack=lost_ack)
        with patch.object(host.asyncio, "sleep", new=AsyncMock()):
            if lost_ack:
                with self.assertRaisesRegex(ConnectionError, "Lost configuration"):
                    asyncio.run(host.matrix(plus, tank, blobs, lambda **fields: report.update(deepcopy(fields))))
            else:
                asyncio.run(host.matrix(plus, tank, blobs, lambda **fields: report.update(deepcopy(fields))))
        self.assertTrue(report["tank_restored"] and report["plus_restored"])
        self.assertEqual(report["restore_errors"], [])
        self.assertEqual(bytes(plus.config), blobs["plus-original"])
        self.assertEqual(plus.flash, blobs["plus-saved"])
        self.assertEqual(bytes(plus.state[43:47]), blobs["plus-flips"])
        self.assertEqual(asyncio.run(tank.tank_snapshot()), {k: blobs["tank-" + k] for k in ("live", "state", "ram", "flash")})
        return plus, report

    def test_three_cases_succeed_and_both_devices_are_restored(self):
        plus, report = self.exercise()
        self.assertEqual(plus.keys, [2, 2, 2, 3])
        self.assertTrue(report["tests_passed"])
        self.assertEqual([c["passed"] for c in report["checks"]], [True, True, True])

    def test_pair_failure_is_measured_separately_from_single_and_separated(self):
        _, report = self.exercise(pair_fails=True)
        self.assertFalse(report["tests_passed"])
        self.assertEqual([c["passed"] for c in report["checks"]], [True, False, True])

    def test_lost_ack_restores_staged_values_and_assignment_before_raising(self):
        plus, _ = self.exercise(lost_ack=True)
        self.assertEqual(plus.keys, [])

    def test_tank_recovery_error_does_not_skip_plus_recovery(self):
        blobs = baseline()
        tank, plus = FakeTank(blobs), None
        plus = FakePlus(blobs, tank)
        plus.config[host.C] = 1
        tank.write = AsyncMock(side_effect=ConnectionError("TANK gone"))
        with patch.object(host.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(host.restore(plus, tank, blobs))
        self.assertTrue(result["plus_restored"])
        self.assertEqual(bytes(plus.config), blobs["plus-original"])
        self.assertTrue(result["restore_errors"])


if __name__ == "__main__":
    unittest.main()
