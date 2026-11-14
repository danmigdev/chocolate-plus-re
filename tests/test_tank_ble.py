"""Read-only BLE transport guards and fragmented native protocol replies."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import tank_ble as ble


def reply_frame(command, body):
    return b"\0Y" + bytes([command]) + len(body).to_bytes(3, "little") + body + bytes([~sum(body) & 255])


class FakeClient:
    def __init__(self, identity=ble.IDENTITY):
        self.identity, self.sent, self.reader = identity, [], None

    async def write_gatt_char(self, characteristic, packet, response=False):
        self.sent.append(packet)
        command, body = packet[2], packet[6:-1]
        payload = self.identity.encode() + b"\0" if command == 0x11 else body + bytes(int.from_bytes(body[5:], "little"))
        raw = reply_frame(command, payload)
        for offset in range(0, len(raw), 7):
            self.reader.notified(None, raw[offset:offset + 7])


class TankBleTests(unittest.TestCase):
    def test_all_write_reset_update_commands_are_rejected(self):
        for command in range(256):
            if command in (0x11, 0x23):
                continue
            with self.subTest(command=command), self.assertRaises(ValueError):
                ble.request_frame(command)
        with self.assertRaises(ValueError):
            ble.request_frame(0x11, b"\0")

    def test_reads_are_confined_to_known_TANK_memory(self):
        for address, count, kind in ((ble.tank.LIVE, 93, 4), (ble.tank.LIVE - 1, 1, 4),
                                     (0, 129, 4), (0, 0, 4), (ble.tank.FLASH_BANK, 1, 4),
                                     (ble.tank.FLASH_BANK + ble.tank.BANK_SIZE, 1, 5)):
            with self.subTest(address=address, count=count, kind=kind), self.assertRaises(ValueError):
                ble.read_prefix(address, count, kind)
        prefix = ble.read_prefix(ble.tank.LIVE, 92)
        packet = ble.request_frame(0x23, prefix)
        self.assertEqual(ble.tank.codec.reply(ble.tank.codec.encode7(packet)), (0x23, prefix))

    def test_every_notification_boundary_and_multiple_replies(self):
        first = reply_frame(0x11, ble.IDENTITY.encode() + b"\0")
        second = reply_frame(0x23, ble.read_prefix(ble.tank.LIVE, 92) + bytes(range(92)))
        stream = first + second
        for boundary in range(len(stream) + 1):
            parser = ble.Frames()
            self.assertEqual(parser.feed(stream[:boundary]) + parser.feed(stream[boundary:]),
                             [(0x11, first[6:-1]), (0x23, second[6:-1])])

    def test_noise_bad_checksum_and_oversized_frames_do_not_pass(self):
        good = reply_frame(0x11, ble.IDENTITY.encode())
        bad = good[:-1] + bytes([good[-1] ^ 1])
        self.assertEqual(ble.Frames().feed(b"noise\0Y\x11\xff\xff\xff" + bad + good), [(0x11, good[6:-1])])

    def test_unverified_and_wrong_identity_never_read_memory(self):
        async def check():
            client = FakeClient("UNKNOWN")
            reader = ble.Reader(client); client.reader = reader
            with self.assertRaises(ValueError):
                await reader.read(ble.tank.LIVE, 92)
            self.assertEqual(client.sent, [])
            with self.assertRaises(ValueError):
                await reader.identify()
            with self.assertRaises(ValueError):
                await reader.read(ble.tank.LIVE, 92)
            self.assertEqual([p[2] for p in client.sent], [0x11])
        asyncio.run(check())

    def test_verified_reader_reassembles_chunked_snapshot_without_writes(self):
        async def check():
            client = FakeClient()
            reader = ble.Reader(client); client.reader = reader
            self.assertEqual(await reader.identify(), ble.IDENTITY)
            snapshot = await reader.snapshot()
            self.assertEqual({k: len(v) for k, v in snapshot.items()}, dict(state=4, live=92, ram=1942, flash=1942))
            self.assertTrue(all(p[2] in (0x11, 0x23) for p in client.sent))
        asyncio.run(check())

    def test_failed_submission_is_not_counted_as_sent(self):
        class Disconnected:
            async def write_gatt_char(self, *args, **kwargs):
                raise ConnectionError("Disconnected")
        async def check():
            recorded = []
            reader = ble.Reader(Disconnected(), lambda direction, data: recorded.append(direction))
            with self.assertRaises(ConnectionError):
                await reader.identify()
            self.assertEqual((reader.attempts, reader.requests), (1, 0))
            self.assertEqual(recorded, ["request_attempt"])
        asyncio.run(check())

    def test_pairing_requires_verified_application_identity(self):
        async def check():
            with self.assertRaisesRegex(ValueError, "identity before pairing"):
                await ble.pair_windows(ble.Reader(FakeClient()))
        asyncio.run(check())

    def test_windows_pairing_uses_encryption_and_releases_handles(self):
        async def check(already, result_status):
            calls = []
            class Pairing:
                def add_pairing_requested(self, handler):
                    self.handler = handler
                    calls.append("registered")
                    return 7
                async def pair_with_protection_level_async(self, kind, level):
                    calls.append((kind, level))
                    for requested in ("confirm", "pin"):
                        self.handler(None, SimpleNamespace(pairing_kind=requested,
                            accept=lambda: calls.append("accepted " + requested)))
                    return SimpleNamespace(status=SimpleNamespace(name=result_status))
                def remove_pairing_requested(self, token):
                    calls.append(("removed", token))
            pairing = Pairing()
            device = SimpleNamespace(device_information=SimpleNamespace(id="verified-tank"),
                                     close=lambda: calls.append("closed"))
            class BluetoothDevice:
                @staticmethod
                async def from_bluetooth_address_async(address):
                    calls.append(address)
                    return device
            class Information:
                @staticmethod
                async def create_from_id_async(identifier):
                    return SimpleNamespace(pairing=SimpleNamespace(is_paired=already, custom=pairing))
            modules = {
                "winrt.windows.devices.bluetooth": SimpleNamespace(BluetoothLEDevice=BluetoothDevice),
                "winrt.windows.devices.enumeration": SimpleNamespace(DeviceInformation=Information,
                    DevicePairingKinds=SimpleNamespace(CONFIRM_ONLY="confirm"),
                    DevicePairingProtectionLevel=SimpleNamespace(ENCRYPTION="encrypted")),
            }
            reader = ble.Reader(SimpleNamespace(address="11:22:33:44:55:66"))
            reader.verified = True
            with patch.dict("sys.modules", modules):
                if not already and result_status == "FAILED":
                    with self.assertRaisesRegex(ValueError, "pairing failed"):
                        await ble.pair_windows(reader)
                else:
                    self.assertEqual(await ble.pair_windows(reader), "ALREADY_PAIRED" if already else "PAIRED")
            self.assertEqual(calls[0], 0x112233445566)
            self.assertEqual(calls[-1], "closed")
            if already:
                self.assertEqual(len(calls), 2)
            else:
                self.assertIn(("confirm", "encrypted"), calls)
                self.assertIn("accepted confirm", calls)
                self.assertNotIn("accepted pin", calls)
                self.assertIn(("removed", 7), calls)
        for already, status in ((True, "PAIRED"), (False, "PAIRED"), (False, "FAILED")):
            with self.subTest(already=already, status=status):
                asyncio.run(check(already, status))


if __name__ == "__main__":
    unittest.main()
