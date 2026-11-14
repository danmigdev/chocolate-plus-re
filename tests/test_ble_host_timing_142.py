"""Native timer requests, USB event boundaries and timing-test recovery."""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from tools import ble_host_timing_142 as timing
from tests.test_ble_host_142 import baseline, FakePlus, FakeTank, client

host = timing.host


class TimerPlus(FakePlus):
    write_size = 128

    def __init__(self, blobs, tank, lost_key_ack=False):
        super().__init__(blobs, tank, pair_fails=True)
        self.lost_key_ack, self.timer_requests = lost_key_ack, []

    async def read(self, address, size):
        if address == host.HOST_TX:
            return bytes(size)
        return await super().read(address, size)

    async def write(self, address, data):
        if address == host.usb.KEYS + 4 and len(data) == 4:
            if (address, data) not in self.allow:
                raise ValueError("Unlisted timer request")
            self.timer_requests.append(data)
            self.writes += 1
            # Device model: both events fire; same-tick MIDI coalesces and
            # fails, separated events succeed. This is a test scenario,
            # not an assertion about hardware behavior.
            for key in (2, 3):
                self.keys.append(key)
                self.state[43 + key] ^= 1
                self.config[23646] = key
                if data[0] != data[2]:
                    for frame in host.usb.expected_frames(self.config, key, self.state[43 + key]):
                        target, value = host.relay.decode_write(frame)
                        offset = target - host.relay.LIVE
                        self.tank.live[offset:offset + len(value)] = value
            if self.lost_key_ack:
                self.lost_key_ack = False
                raise ConnectionError("Lost timer acknowledgement")
            return
        await super().write(address, data)


class TimingTests(unittest.TestCase):
    def test_counter_requests_fire_on_requested_ticks_without_combo_states(self):
        for interval in range(0, 71, 5):
            data = timing.release_request(interval)
            counters, states, fired = list(data[::2]), list(data[1::2]), []
            for tick in range(1, 17):
                self.assertNotEqual(states, [100, 100])
                for key in range(2):
                    if states[key] == 1:
                        counters[key] += 1
                        if counters[key] >= 15:
                            fired.append((key, tick))
                            counters[key] = states[key] = 0
            self.assertEqual(fired, [(0, 1), (1, 1 + interval // 5)])
        for invalid in (-5, 1, 75, 5.0, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                timing.release_request(invalid)

    def test_usb_events_preserve_end_boundaries_and_padding(self):
        self.assertEqual(timing.usb_events(b"\xf0\xf7"), bytes.fromhex("06 f0 f7 00"))
        self.assertEqual(timing.usb_events(b"\xf0\1\xf7"), bytes.fromhex("07 f0 01 f7"))
        self.assertEqual(timing.usb_events(b"\xf0\1\2\xf7"), bytes.fromhex("04 f0 01 02 05 f7 00 00"))
        self.assertEqual(timing.usb_events(b"\xf0\1\2\3\4\xf7"), bytes.fromhex("04 f0 01 02 07 03 04 f7"))
        with self.assertRaises(ValueError):
            timing.usb_events(b"\xf0\xf7\xf0\xf7")

    def test_tx_buffer_is_read_only_even_when_a_write_is_session_armed(self):
        link = host.Channel(client(), True)
        link.verified = link.pinned = True
        link.guard(0x23, link.descriptor(host.HOST_TX, 64))
        with self.assertRaises(ValueError):
            link.guard(0x23, link.descriptor(host.HOST_TX, 65))
        link.arm([(host.HOST_TX, bytes(64))])
        with self.assertRaises(ValueError):
            asyncio.run(link.write(host.HOST_TX, bytes(64)))
        self.assertEqual(link.client.write_gatt_char.await_count, 0)

    def test_retained_prefix_distinguishes_unused_padding_from_midi_damage(self):
        frame = b"\xf0\1\2\xf7"
        data = bytearray(timing.usb_events(frame) + bytes(56))
        data[6:8], data[8:] = b"\x68\2", bytes([0xAA]) * 56
        result = timing.compare_tx_prefix(data, [frame])
        self.assertFalse(result["expected_prefix_matches"])
        self.assertTrue(result["midi_and_cin_match"])
        self.assertEqual(result["nonzero_padding"], [dict(offset=6, value=104), dict(offset=7, value=2)])
        data[1] = 0xB0
        self.assertFalse(timing.compare_tx_prefix(data, [frame])["midi_and_cin_match"])

    def exercise(self, lost_key_ack=False):
        blobs, report = baseline(), {}
        tank = FakeTank(blobs)
        plus = TimerPlus(blobs, tank, lost_key_ack)
        with patch.object(host.asyncio, "sleep", new=AsyncMock()):
            task = timing.sweep(plus, tank, blobs, lambda **fields: report.update(deepcopy(fields)), (20, 5, 0), 2)
            if lost_key_ack:
                with self.assertRaisesRegex(ConnectionError, "Lost timer"):
                    asyncio.run(task)
            else:
                asyncio.run(task)
        self.assertTrue(report["tank_restored"] and report["plus_restored"])
        self.assertEqual(report["restore_errors"], [])
        self.assertEqual(bytes(plus.config), blobs["plus-original"])
        self.assertEqual(plus.flash, blobs["plus-saved"])
        self.assertEqual(asyncio.run(tank.tank_snapshot()), {k: blobs["tank-" + k] for k in ("live", "state", "ram", "flash")})
        return plus, report

    def test_reports_failures_by_spacing_and_uses_one_write_per_pair(self):
        plus, report = self.exercise()
        self.assertEqual(len(plus.timer_requests), 6)
        self.assertEqual(report["paired_control_passed"], 0)
        self.assertEqual(report["timing_summary"], [dict(nominal_interval_ms=ms, passed=passed, trials=2)
                                                   for ms, passed in ((20, 2), (5, 2), (0, 0))])

    def test_lost_trigger_ack_restores_both_devices_without_retry(self):
        plus, report = self.exercise(True)
        self.assertEqual(len(plus.timer_requests), 1)
        self.assertNotIn("timing_summary", report)

    def test_small_mtu_fails_before_any_application_write(self):
        blobs = baseline()
        tank = FakeTank(blobs)
        plus = TimerPlus(blobs, tank)
        plus.write_size = 3
        with self.assertRaisesRegex(ValueError, "unfragmented"):
            asyncio.run(timing.sweep(plus, tank, blobs, lambda **fields: None))
        self.assertEqual((plus.writes, tank.writes), (0, 0))


if __name__ == "__main__":
    unittest.main()
