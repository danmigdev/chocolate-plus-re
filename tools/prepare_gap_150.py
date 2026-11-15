"""Offline, device-specific exact 142 -> pinned 1-ms 150 migration.

Requires two identical physical backups. Reconstructs the complete target on
every load; editable plan metadata cannot enlarge the allowed flash extent.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .prepare_142 import (FULL_SIZE, SECTOR_SIZE, COMPACT_END, COMPACT_SHA256,
                              reservations, vm_geometry, read_backup, durable_write, require, sha)
except ImportError:
    from prepare_142 import (FULL_SIZE, SECTOR_SIZE, COMPACT_END, COMPACT_SHA256,
                             reservations, vm_geometry, read_backup, durable_write, require, sha)

IMAGE_BYTES = 0x27A00
IMAGE_SHA256 = "73e7a6013551812d2d0b1eb7d5c60d88231ff34de3b6adecb9d8659691aa7a20"
APP_SHA256 = "9ed951513a3169c32fd80da564e4a5043915a54124446f8d4cddb7590a39a7c8"
MODULE_SHA256 = "9c9e32ba6411ee48ac7480a003ee753b759dd208027ef9125b6dded0dfb8e774"
MODULE_BASE, MODULE_BYTES = 0x1E265F0, 928
IDENTITY = "FootCtrlPlus_150-gap"


def construct_target(original, compact):
    require(len(original) == FULL_SIZE, "Expected complete 256-KiB physical backup")
    require(sha(original[:COMPACT_END]) == COMPACT_SHA256, "Source is not exact upstream 142")
    require(len(compact) == IMAGE_BYTES and sha(compact) == IMAGE_SHA256,
            "Target is not the pinned one-millisecond 150 candidate")
    require(original[:0x1000] == compact[:0x1000], "Boot prefix changes")
    require(original[COMPACT_END:IMAGE_BYTES] == b"\xff" * (IMAGE_BYTES - COMPACT_END),
            "The extension would overwrite occupied bytes")
    before, after = reservations(original[:COMPACT_END]), reservations(compact)
    for name, address, count in (("BTIF", 0x37000, 0x1000), ("EXIF", 0x38000, 0x1000),
                                 ("USRFLASH", 0x39000, 0x6000)):
        require(before[name] == after[name] == dict(offset=address, size=count, flags=0x92),
                f"{name} reservation changes")
    geometry = dict(start=0x28000, second_bank=0x29000, bank_bytes=0x1000, end=0x2A000)
    for records, end in ((before, COMPACT_END), (after, IMAGE_BYTES)):
        require(records["PRCT"] == dict(offset=0, size=end, flags=0x92) and
                records["VM"] == dict(offset=end, size=0x37000 - end, flags=0x12),
                "Unexpected protected image or VM reservation")
        require(vm_geometry(end, records["VM"]["size"]) == geometry, "Native VM banks move")
    target = compact + original[IMAGE_BYTES:]
    changed = [pos for pos in range(0, FULL_SIZE, SECTOR_SIZE)
               if target[pos:pos + SECTOR_SIZE] != original[pos:pos + SECTOR_SIZE]]
    require(0x1000 in changed and all(0x1000 <= pos <= 0x27000 for pos in changed),
            "Unexpected application sector set")
    order = [pos for pos in changed if pos != 0x1000] + [0x1000]
    require(target[:0x1000] == original[:0x1000] and target[IMAGE_BYTES:] == original[IMAGE_BYTES:],
            "Preserved device data changed")
    return target, order, geometry


def details(original, target, order, geometry):
    return dict(schema=1, migration="exact-142-to-pinned-150-1ms", status="prepared_NOT_INSTALLED",
                source_image_bytes=COMPACT_END, candidate_image_bytes=IMAGE_BYTES,
                compact_image_sha256=IMAGE_SHA256, source_compact_sha256=COMPACT_SHA256,
                original_sha256=sha(original), target_sha256=sha(target), flash_bytes=FULL_SIZE,
                preserved_from=IMAGE_BYTES, sector_order=order, vm_geometry=geometry,
                configuration_sha256=sha(original[0x39000:0x3F000]), boot_unchanged=True,
                default_gap_ms=20, gap_step_ms=1, max_gap_ms=1000,
                sector_hashes=[dict(address=pos, original_sha256=sha(original[pos:pos + SECTOR_SIZE]),
                                    target_sha256=sha(target[pos:pos + SECTOR_SIZE])) for pos in order],
                activation="Application sectors first; directory and CRC sector 0x1000 last",
                independent_cold_recovery_verified=False, native_execution_verified=False)


def load_plan(directory):
    original, target = [(directory / name).read_bytes() for name in ("original.bin", "target.bin")]
    expected, order, geometry = construct_target(original, target[:IMAGE_BYTES])
    require(expected == target, "Target differs from reconstructed migration")
    report = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    require(report == details(original, target, order, geometry), "Plan metadata differs from reconstruction")
    return report, original, target


def prepare(backup, candidate, output):
    original = read_backup(backup)
    target, order, geometry = construct_target(original, candidate.read_bytes())
    report = details(original, target, order, geometry)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in (("original.bin", original), ("target.bin", target),
                       ("plan.json", (json.dumps(report, indent=2) + "\n").encode())):
        durable_write(output / name, data)
    load_plan(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.backup, args.candidate, args.output), indent=2))


if __name__ == "__main__":
    main()
