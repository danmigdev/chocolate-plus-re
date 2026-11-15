"""143 identity, retired setting endpoint and paired-bank restoration."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tools import ble_split_143 as split
from tests.test_ble_host_142 import FakePlus, FakeTank, baseline, client


def saved_baseline(legacy=False):
    blobs = baseline()
    flash = blobs["plus-saved"]
    tail = b"\xff" * (len(flash) - split.FOOTER)
    if legacy:
        tail = bytes.fromhex("5347415001011400ebffe8031400aa0c") + tail[16:]
    blobs["plus-saved"] = flash[:split.FOOTER] + tail
    return blobs


class Plus(FakePlus):
    async def write(self, address, data):
        before = self.writes
        try:
            await super().write(address, data)
        finally:
            # Original 142/143 saver erases the complete configuration region.
            if self.writes != before and split.usb.RAM_BASE <= address < split.usb.RAM_BASE + split.usb.CONFIG_BYTES:
                self.flash = bytes(self.config) + b"usrdata\0" + b"\xff" * (split.usb.FLASH_BYTES - split.FOOTER)


class GateTests(unittest.TestCase):
    def test_profile_pins_application_and_extension_before_writes(self):
        channel = split.Channel(client(), True)
        self.assertEqual(channel.identity, "FootCtrlPlus_143-split")
        channel.verified = True
        channel.read = AsyncMock(return_value=b"wrong")
        with self.assertRaisesRegex(ValueError, "application"):
            asyncio.run(channel.verify_app())
        self.assertFalse(channel.pinned)
        with patch.object(split.host.codec, "digest", side_effect=[split.build.APP_SHA256, "wrong"]):
            with self.assertRaisesRegex(ValueError, "Extension"):
                asyncio.run(channel.verify_app())
        self.assertFalse(channel.pinned)

    def test_removed_endpoint_and_flash_writes_are_rejected_even_if_armed(self):
        channel = split.Channel(client(), True)
        channel.verified = channel.pinned = True
        old_endpoint = split.usb.RAM_BASE + 0x53580000
        for address in (old_endpoint, old_endpoint + 1, split.build.MODULE_BASE, split.usb.USR_VIEW):
            data = bytes(16)
            channel.arm([(address, data)])
            with self.subTest(address=address), self.assertRaises(ValueError):
                channel.guard(0x22, channel.descriptor(address, len(data)) + data)
        with self.assertRaises(ValueError):
            channel.guard(0x23, channel.descriptor(old_endpoint, 16))
        self.assertEqual(channel.client.write_gatt_char.await_count, 0)

    def test_legacy_record_is_retired_by_unchanged_native_configuration_save(self):
        blobs, report = saved_baseline(legacy=True), {}
        plus = Plus(blobs, FakeTank(blobs))
        with tempfile.TemporaryDirectory() as folder:
            result = asyncio.run(split.retire_legacy_record(plus, blobs,
                lambda **values: report.update(values), Path(folder) / "recovery"))
            self.assertEqual(split.host.recovery(Path(folder) / "recovery"), result)
        self.assertEqual(plus.writes, 1)
        self.assertEqual(result, saved_baseline())
        self.assertEqual(bytes(plus.config), blobs["plus-original"])
        self.assertEqual(plus.flash, result["plus-saved"])
        self.assertTrue(report["legacy_record_erased"])

    def test_unknown_trailing_data_is_not_erased(self):
        blobs = saved_baseline(legacy=True)
        for offset in (split.FOOTER + 14, split.FOOTER + 16, len(blobs["plus-saved"]) - 1):
            changed = bytearray(blobs["plus-saved"])
            changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                split.normalized_blobs(dict(blobs, **{"plus-saved": bytes(changed)}))

    def test_lost_cleanup_ack_keeps_recovery_and_never_retries(self):
        blobs = saved_baseline(legacy=True)
        plus = Plus(blobs, FakeTank(blobs), lost_ack=True)
        with tempfile.TemporaryDirectory() as folder:
            recovery = Path(folder) / "recovery"
            with self.assertRaises(ConnectionError):
                asyncio.run(split.retire_legacy_record(plus, blobs, lambda **values: None, recovery))
            self.assertEqual(split.host.recovery(recovery), saved_baseline())
        self.assertEqual(plus.writes, 1)


class TrialTests(unittest.TestCase):
    def exercise(self, *, pair_fails=False, lost_ack=False, marker=None):
        blobs, report = saved_baseline(), {}
        tank = FakeTank(blobs)
        plus = Plus(blobs, tank, pair_fails=pair_fails, lost_ack=lost_ack)
        with patch.object(split.asyncio, "sleep", new=AsyncMock()):
            try:
                result = asyncio.run(split.paired_trials(plus, tank, blobs,
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
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "confirmed"
            marker.touch()
            with self.assertRaisesRegex(ValueError, "fresh manual"):
                self.exercise(marker=marker)


if __name__ == "__main__":
    unittest.main()
