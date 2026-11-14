#!/usr/bin/env python3
"""Prepare an offline, device-specific 013 -> exact upstream 142 flash plan.

Requires two identical 256-KiB physical reads from br25_usb.py. Only the two
previously audited compact 013 images are accepted. No device I/O is exposed.
Every load reconstructs the target and checks the immutable original backup.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import struct

try:
    from .audit_142 import SHA256, crc, load_package, require, sha
except ImportError:
    from audit_142 import SHA256, crc, load_package, require, sha
from fwsctool import fwsc_unwrap, member_decrypt, xcode_block

FULL_SIZE, SECTOR_SIZE, PAGE_SIZE = 0x40000, 4096, 256
COMPACT_END = 0x27600
COMPACT_SHA256 = "2c7160bbd55144344d19ce40ed24f64683f92e070cd9d0816be5a321af9659b7"
SOURCES = {
    "013-stock": (0x27600, "881e7ed60ab3e7d72ac605e7c044cb8dab1b86bbb197752f8450fde2a43e2218"),
    "013-paced": (0x27700, "39e52d64027ee28223b1ce066a4d8910562f75121f5178d9ff5d7c1ac682782a"),
}


def candidate_image(path):
    # Validate the complete package and both app copies before selecting by
    # geometry: 013's compact image is flash2.bin; 142's is flash.bin.
    load_package(path, "142")
    ufw = fwsc_unwrap(path.read_bytes(), "")
    matches = []
    for index in range(8):
        entry = xcode_block(ufw[64 + index * 80:144 + index * 80])
        name = bytes(entry[64:80]).split(b"\0", 1)[0]
        offset, size = struct.unpack_from("<II", entry, 8)
        if name in (b"flash.bin", b"flash2.bin") and size == COMPACT_END:
            matches.append(ufw[offset:offset + size])
    require(len(matches) == 1 and sha(matches[0]) == COMPACT_SHA256,
            "Expected the exact compact 142 image; do not use the aligned image.")
    return matches[0]


def reservations(raw):
    area = member_decrypt(raw[0x1000:], key=0x180F, pos=0x20000)
    records = {}
    for pos in range(0x60, 0x100, 32):
        entry = area[pos:pos + 32]
        require(crc(entry[2:]) == int.from_bytes(entry[:2], "little"),
                "Reservation directory CRC mismatch.")
        name = entry[16:32].split(b"\0", 1)[0].decode("ascii")
        offset, size = struct.unpack_from("<II", entry, 4)
        records[name] = dict(offset=offset, size=size, flags=entry[12])
    require(set(records) == {"VM", "PRCT", "BTIF", "EXIF", "USRFLASH"},
            "Unexpected reservation directory.")
    return records


def vm_geometry(start, size):
    # Audited native 013 at 0x1e14da6..0x1e14e38 and 012/142 at
    # 0x1e14ddc..0x1e14e8a: clamp to 16 KiB, force 4-KiB mode at that
    # length, round start up to 4 KiB and usable length down to 8 KiB.
    require(min(size, 16384) == 16384, "VM size outside the audited native branch.")
    aligned = (start + 4095) & -4096
    usable = (start + 16384 - aligned) & -8192
    return dict(start=aligned, second_bank=aligned + usable // 2,
                bank_bytes=usable // 2, end=aligned + usable)


def construct_target(original, compact):
    require(len(original) == FULL_SIZE, "Physical backup must contain exactly 256 KiB.")
    require(len(compact) == COMPACT_END and sha(compact) == COMPACT_SHA256,
            "Target is not the exact compact upstream 142 image.")
    matches = [(name, end) for name, (end, digest) in SOURCES.items()
               if sha(original[:end]) == digest]
    require(len(matches) == 1, "Installed image differs from both audited 013 builds.")
    source, source_end = matches[0]
    require(original[:0x1000] == compact[:0x1000], "Boot prefix differs; migration refused.")
    before, after = reservations(original[:source_end]), reservations(compact)
    for name, offset, size in (("BTIF", 0x37000, 0x1000), ("EXIF", 0x38000, 0x1000),
                               ("USRFLASH", 0x39000, 0x6000)):
        require(before[name] == after[name] == dict(offset=offset, size=size, flags=0x92),
                f"{name} reservation changes.")
    geometries = []
    for records, end in ((before, source_end), (after, COMPACT_END)):
        require(records["PRCT"] == dict(offset=0, size=end, flags=0x92)
                and records["VM"] == dict(offset=end, size=0x37000 - end, flags=0x12),
                "Firmware protection or VM reservation extent differs.")
        geometries.append(vm_geometry(end, records["VM"]["size"]))
    require(geometries[0] == geometries[1] == dict(start=0x28000, second_bank=0x29000,
                                                 bank_bytes=0x1000, end=0x2A000),
            "Migration changes native VM bank locations.")
    preserved_from = max(source_end, COMPACT_END)
    # Remove the old pacing extension when present; its extent is part of the
    # pinned source image and precedes both native VM banks.
    target = compact + b"\xff" * (preserved_from - COMPACT_END) + original[preserved_from:]
    changed = [pos for pos in range(0, FULL_SIZE, SECTOR_SIZE)
               if target[pos:pos + SECTOR_SIZE] != original[pos:pos + SECTOR_SIZE]]
    require(changed and all(0x1000 <= pos <= 0x27000 for pos in changed),
            "Changes extend outside application sectors.")
    order = [pos for pos in changed if pos != 0x1000] + ([0x1000] if 0x1000 in changed else [])
    require(target[:0x1000] == original[:0x1000]
            and target[preserved_from:] == original[preserved_from:],
            "Boot prefix or reserved device data changed.")
    return target, dict(source=source, source_image_bytes=source_end,
                        preserved_from=preserved_from, sector_order=order,
                        vm_geometry=geometries[0])


def plan_details(original, target, geometry):
    return dict(schema=1, status="prepared_NOT_INSTALLED", **geometry,
                package_sha256=SHA256["142"], compact_image_sha256=COMPACT_SHA256,
                original_sha256=sha(original), target_sha256=sha(target),
                flash_bytes=FULL_SIZE, boot_unchanged=True,
                configuration_sha256=sha(original[0x39000:0x3F000]),
                sector_hashes=[dict(address=pos,
                                    original_sha256=sha(original[pos:pos + SECTOR_SIZE]),
                                    target_sha256=sha(target[pos:pos + SECTOR_SIZE]))
                               for pos in geometry["sector_order"]],
                activation="Application sectors first; directory and CRC sector 0x1000 last",
                independent_cold_recovery_verified=False, native_execution_verified=False)


def read_backup(directory):
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    require(report.get("status") == "two_physical_reads_verified" and report.get("identical") is True
            and report.get("flash_writes") == report.get("flash_erases") == 0
            and report.get("identity") == "BR25 UBOOT1.00" and report.get("jedec_id") == "854512",
            "Backup diagnostic did not complete for the expected controller.")
    first = (directory / "flash-1.bin").read_bytes()
    second = (directory / "flash-2.bin").read_bytes()
    require(len(first) == FULL_SIZE and first == second
            and report.get("sha256") == [sha(first)] * 2, "Backup copies or fingerprints differ.")
    return first


def load_plan(directory):
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    original = (directory / "original.bin").read_bytes()
    target = (directory / "target.bin").read_bytes()
    # Recheck the exact target body; no package path or editable allow-list in
    # the manifest can enlarge the write scope.
    expected, geometry = construct_target(original, target[:COMPACT_END])
    require(target == expected, "Target differs from reconstructed migration.")
    require(plan == plan_details(original, expected, geometry), "Plan metadata differs from reconstruction.")
    return plan, original, target


def durable_write(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(backup, firmware, output):
    original = read_backup(backup)
    target, geometry = construct_target(original, candidate_image(firmware))
    plan = plan_details(original, target, geometry)
    output.mkdir(parents=True, exist_ok=False)
    durable_write(output / "original.bin", original)
    durable_write(output / "target.bin", target)
    durable_write(output / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
    load_plan(output)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--firmware", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.backup, args.firmware, args.output), indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
