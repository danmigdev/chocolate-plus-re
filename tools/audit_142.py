#!/usr/bin/env python3
"""Offline audit of the exact upstream 142 baseline and selected code windows.

No firmware modifications or device access. Original packages and generated
firmware stay local. Output contains hashes and findings, not vendor code.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import json
from pathlib import Path
import struct
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware"))
from fwsctool import fwsc_unwrap, member_decrypt, xcode_block
try:
    from .config_snapshot import compare
except ImportError:
    from config_snapshot import compare

SHA256 = {
    "012": "9deb9d90691415aa7f4514103842237a1500c681db34fe35e5f5136d173cb3bc",
    "013": "b5ba2041482640315c7dc70a0f7c79c9e81d24989226763868dc36dab81801c3",
    "142": "eadd20b993b149753651f83a683d1af25fb4f966d340e0b269a33b300f2e3ba2",
}
VBASE = 0x1E00000
# These are reviewed windows, not a complete call graph or a hardware test.
WINDOWS = {
    "usb_midi_packet_builder": (0x1E0FE4C, 0x1E10050),
    "usb_host_sender": (0x1E119AE, 0x1E11A22),
    "configuration_load_save": (0x1E125D2, 0x1E126C8),
    "configuration_read": (0x1E13B50, 0x1E13B5C),
    "configuration_write_prefix": (0x1E13B5C, 0x1E13B7A),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def crc(data):
    return binascii.crc_hqx(data, 0)


def load_package(path, version):
    data = path.read_bytes()
    require(sha(data) == SHA256[version], f"{version}: file is not the pinned package.")
    ufw = fwsc_unwrap(data, "")
    header = xcode_block(ufw[:64])
    require(crc(header[2:]) == int.from_bytes(header[:2], "little"), "UFW header CRC mismatch.")
    require(int.from_bytes(header[4:8], "little") == len(ufw), "UFW length mismatch.")
    count = int.from_bytes(header[8:10], "little")
    table = ufw[64:64 + count * 80]
    require(count == 8 and crc(table) == int.from_bytes(header[2:4], "little"),
            "UFW directory CRC/count mismatch.")
    areas, images = {}, {}
    for index in range(count):
        entry = xcode_block(table[index * 80:(index + 1) * 80])
        name = bytes(entry[64:80]).split(b"\0", 1)[0].decode("ascii")
        if name not in ("flash.bin", "flash2.bin"):
            continue
        offset, size = struct.unpack_from("<II", entry, 8)
        member = ufw[offset:offset + size]
        require(len(member) == size and crc(member) == int.from_bytes(entry[4:6], "little"),
                f"{name}: member CRC/length mismatch.")
        area = member_decrypt(member[0x1000:], key=0x180F, pos=0x20000)
        require(crc(area[2:32]) == int.from_bytes(area[:2], "little"), "App area header CRC mismatch.")
        area_size = int.from_bytes(area[8:12], "little")
        require(32 <= area_size <= len(area), "App area extent mismatch.")
        require(crc(area[32:area_size]) == int.from_bytes(area[2:4], "little"),
                "App area data CRC mismatch.")
        require(crc(area[34:64]) == int.from_bytes(area[32:34], "little"),
                "App file header CRC mismatch.")
        app_offset, app_size = struct.unpack_from("<II", area, 36)
        require(area[48:64].split(b"\0", 1)[0] == b"app.bin"
                and 0x120 <= app_offset < app_offset + app_size <= area_size, "App file extent mismatch.")
        app = area[app_offset:app_offset + app_size]
        require(crc(app) == int.from_bytes(area[34:36], "little"), "App code CRC mismatch.")
        areas[name] = area
        images[name] = dict(member_bytes=size, application_sha256=sha(app),
                            boot_prefix_sha256=sha(member[:0x1000]))
    require(set(areas) == {"flash.bin", "flash2.bin"}, "Expected both firmware copies.")
    require(images["flash.bin"]["application_sha256"] == images["flash2.bin"]["application_sha256"],
            "The two firmware copies contain different applications.")
    return areas, images


def audit(stock, candidate, stock_013=None, evidence=None):
    original, original_images = load_package(stock, "012")
    patched, patched_images = load_package(candidate, "142")
    older = load_package(stock_013, "013")[0] if stock_013 else None
    windows = []
    for name, (start, end) in WINDOWS.items():
        extent = slice(start - VBASE, end - VBASE)
        for image in original:
            require(original[image][extent] == patched[image][extent], f"{name} changed in {image}.")
        result = dict(name=name, start=hex(start), end_exclusive=hex(end), bytes=end - start,
                      unchanged_from_012_in_both_copies=True,
                      sha256=sha(patched["flash.bin"][extent]))
        if older is not None and name.startswith("usb_"):
            result["also_identical_to_013"] = all(
                older[image][extent] == patched[image][extent] for image in patched)
        windows.append(result)
    for image in original_images:
        require(original_images[image]["boot_prefix_sha256"] == patched_images[image]["boot_prefix_sha256"],
                f"{image}: boot prefix changed.")
    report = dict(schema=1, baseline="FootCtrlPlus_142-dtnprt",
                  sha256=SHA256["142"], source_sha256=SHA256["012"],
                  matches_upstream_142=True, firmware_copies=patched_images, code_windows=windows,
                  hardware_validation="not performed by this offline audit",
                  interpretation="Selected save/load and USB HOST windows are preserved from 012.")
    if evidence:
        findings = {}
        with zipfile.ZipFile(evidence) as archive:
            for name in ("test1", "test2"):
                filenames = [f"{name}-written.bin", f"{name}-read-after-H.bin"]
                require(all(archive.getinfo(filename).file_size == 23646 for filename in filenames),
                        "Expected two 23646-byte configuration files per test.")
                result = compare(*(archive.read(filename) for filename in filenames))
                findings[name] = {key: value for key, value in result.items() if key != "changes"}
        report["historical_configuration_evidence"] = findings
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", type=Path, required=True, help="Original 012 FWSC.")
    parser.add_argument("--firmware", type=Path, required=True, help="Exact upstream 142 FWSC.")
    parser.add_argument("--stock-013", type=Path, help="Optional original 013 for the USB code comparison.")
    parser.add_argument("--evidence-zip", type=Path, help="Optional historical test1/test2 snapshots.")
    parser.add_argument("--output", type=Path, help="Local JSON report; defaults to stdout.")
    args = parser.parse_args(argv)
    try:
        report = audit(args.stock, args.firmware, args.stock_013, args.evidence_zip)
        encoded = json.dumps(report, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(2, f"ERROR: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
