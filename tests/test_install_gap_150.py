"""Pinned 150 migration, rollback and tamper rejection; never accesses hardware."""
import json
from pathlib import Path
import tempfile
import unittest

from tools import prepare_gap_150 as gap
from tools.install_142 import transfer, verify_preserved
from tests.test_install_142 import NorDevice

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "artifacts/migration-142/plan/target.bin"
CANDIDATE = ROOT / "firmware/out/gap-150-1ms-candidate-1/FootCtrlPlus_150-gap.compact.bin"


@unittest.skipUnless(ORIGINAL.is_file() and CANDIDATE.is_file(), "Private firmware fixtures unavailable")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.original = ORIGINAL.read_bytes()
        self.compact = CANDIDATE.read_bytes()
        self.target, self.order, self.geometry = gap.construct_target(self.original, self.compact)

    def test_install_and_exact_rollback_with_seven_bounded_sectors(self):
        self.assertEqual(self.order, [0x10000, 0x12000, 0x13000, 0x19000, 0x1a000, 0x27000, 0x1000])
        verify_preserved(self.target, self.original, self.order)
        self.assertEqual(self.target[gap.IMAGE_BYTES:], self.original[gap.IMAGE_BYTES:])
        self.assertEqual(self.geometry["start"], 0x28000)
        dev = NorDevice(self.original, self.target, order=self.order)
        self.assertEqual(transfer(dev, self.original, self.target, False, lambda *a, **k: None),
                         (self.target, self.target))
        restored = NorDevice(dev.memory, self.original, order=self.order)
        self.assertEqual(transfer(restored, self.original, self.original, True, lambda *a, **k: None),
                         (self.original, self.original))

    def test_occupied_extension_and_unknown_images_are_refused(self):
        for offset in (0, 0x1120, gap.COMPACT_END, gap.IMAGE_BYTES - 1):
            changed = bytearray(self.original)
            changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                gap.construct_target(changed, self.compact)
        changed = bytearray(self.compact)
        changed[0x1000] ^= 1
        with self.assertRaises(ValueError):
            gap.construct_target(self.original, changed)

    def test_plan_is_reconstructed_and_cannot_expand_writes(self):
        plan = gap.details(self.original, self.target, self.order, self.geometry)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "original.bin").write_bytes(self.original)
            (root / "target.bin").write_bytes(self.target)
            (root / "plan.json").write_text(json.dumps(plan))
            self.assertEqual(gap.load_plan(root), (plan, self.original, self.target))
            for address in (0, 0x28000, 0x39000, 0x3f000):
                changed = bytearray(self.target)
                changed[address] ^= 1
                forged = dict(plan, target_sha256=gap.sha(changed), sector_order=[*self.order, address])
                (root / "target.bin").write_bytes(changed)
                (root / "plan.json").write_text(json.dumps(forged))
                with self.subTest(address=address), self.assertRaises(ValueError):
                    gap.load_plan(root)

    def test_partial_install_can_restore_without_touching_other_sectors(self):
        dev = NorDevice(self.original, self.target, failure="lost_program", order=self.order)
        with self.assertRaises(OSError):
            transfer(dev, self.original, self.target, False, lambda *a, **k: None)
        self.assertEqual(len(dev.erases), 1)
        self.assertEqual(len(dev.programs), 1)
        restored = NorDevice(dev.memory, self.original, order=self.order)
        transfer(restored, self.original, self.original, True, lambda *a, **k: None)
        self.assertEqual(bytes(restored.memory), self.original)


if __name__ == "__main__":
    unittest.main()
