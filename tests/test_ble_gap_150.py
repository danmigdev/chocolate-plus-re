"""150 BLE identity, setting boundaries and paired-bank restoration."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tools import ble_gap_150 as gap
from tests.test_ble_host_142 import FakePlus, FakeTank, baseline, client


def saved_baseline():
    blobs = baseline()
    flash = blobs["plus-saved"]
    blobs["plus-saved"] = flash[:gap.setting.FOOTER] + gap.setting.encode(21) + flash[gap.setting.FOOTER + 16:]
    return blobs


class Plus(FakePlus):
    async def get_gap(self):
        return gap.setting.decode(self.flash[gap.setting.FOOTER:gap.setting.FOOTER + 16])


class GateTests(unittest.TestCase):
    def test_profile_pins_application_and_extension_before_writes(self):
        channel = gap.Channel(client(), True)
        self.assertEqual(channel.identity, "FootCtrlPlus_150-gap")
        channel.verified = True
        channel.read = AsyncMock(return_value=b"wrong")
        with self.assertRaisesRegex(ValueError, "application"):
            asyncio.run(channel.verify_app())
        self.assertFalse(channel.pinned)
        with patch.object(gap.host.codec, "digest", side_effect=[gap.build.APP_SHA256, "wrong"]):
            with self.assertRaisesRegex(ValueError, "Extension"):
                asyncio.run(channel.verify_app())
        self.assertFalse(channel.pinned)

    def test_only_valid_complete_setting_is_allowed(self):
        channel = gap.Channel(client(), True)
        channel.verified = channel.pinned = True
        record = gap.setting.encode(21)
        channel.arm([(gap.GAP_ADDRESS, record)])
        channel.guard(0x22, channel.descriptor(gap.GAP_ADDRESS, 16) + record)
        for address, data in ((gap.GAP_ADDRESS, record[:8]), (gap.GAP_ADDRESS + 1, record[1:]),
                              (gap.GAP_ADDRESS, record[:-1] + bytes([record[-1] ^ 1])),
                              (gap.build.MODULE_BASE, record), (gap.usb.USR_VIEW, record)):
            channel.arm([(address, data)])
            with self.subTest(address=address, data=data.hex()), self.assertRaises(ValueError):
                channel.guard(0x22, channel.descriptor(address, len(data)) + data)
        self.assertEqual(channel.client.write_gatt_char.await_count, 0)

    def test_setting_write_ack_is_not_a_persistence_guarantee(self):
        blobs = saved_baseline()
        channel = gap.Channel(client(), True)
        channel.verified = channel.pinned = True
        channel.read = AsyncMock(side_effect=[blobs["plus-original"], blobs["plus-saved"]])
        channel.get_gap = AsyncMock(side_effect=[21, 21])
        channel.write = AsyncMock()
        with self.assertRaisesRegex(ValueError, "readback differs"):
            asyncio.run(channel.set_gap(20, blobs))
        self.assertEqual(channel.write.await_count, 1)


class TrialTests(unittest.TestCase):
    def exercise(self, *, pair_fails=False, lost_ack=False, marker=None):
        blobs, report = saved_baseline(), {}
        tank = FakeTank(blobs)
        plus = Plus(blobs, tank, pair_fails=pair_fails, lost_ack=lost_ack)
        with patch.object(gap.asyncio, "sleep", new=AsyncMock()):
            try:
                result = asyncio.run(gap.paired_trials(plus, tank, blobs,
                    lambda **fields: report.update(deepcopy(fields)), repetitions=5, manual_marker=marker))
            finally:
                self.assertTrue(report["tank_restored"] and report["plus_restored"])
                self.assertEqual(bytes(plus.config), blobs["plus-original"])
                self.assertEqual(plus.flash, blobs["plus-saved"])
                self.assertEqual(bytes(tank.live), blobs["tank-live"])
        return plus, result

    def test_five_single_C_events_deliver_both_parameters_and_restore(self):
        plus, checks = self.exercise()
        self.assertEqual(plus.keys, [2] * 5)
        self.assertTrue(all(c["passed"] and c["trigger"] == "one_native_C_event" for c in checks))

    def test_delivery_failure_is_measured_and_restored(self):
        _, checks = self.exercise(pair_fails=True)
        self.assertFalse(any(c["passed"] for c in checks))

    def test_lost_ack_restores_before_propagating_error(self):
        with self.assertRaisesRegex(ConnectionError, "Lost configuration"):
            self.exercise(lost_ack=True)

    def test_stale_manual_confirmation_is_refused_and_staging_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "confirmed"
            marker.touch()
            with self.assertRaisesRegex(ValueError, "fresh manual"):
                self.exercise(marker=marker)


if __name__ == "__main__":
    unittest.main()
