#!/usr/bin/env python3
"""Stage, measure and restore a physical U/H or power-cycle retention test.

USB cannot prove the selector's physical position. Record the actual physical
procedure separately; this tool measures storage, never simulates H. Only the
exact pinned 142 app and its bounded native configuration writes are used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

try:
    from . import acceptance_142 as usb
except ImportError:
    import acceptance_142 as usb

require, digest = usb.require, usb.codec.digest


def stamp():
    return datetime.now(timezone.utc).isoformat()


def record(path, report):
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k in
                     ("status", "restored", "ram_changed_bytes", "saved_changed_bytes", "pattern_bytes")}), flush=True)


def changes(before, after):
    require(len(before) == len(after), "Comparison lengths differ.")
    rows = []
    for offset, (old, new) in enumerate(zip(before, after)):
        if old != new:
            rows.append(dict(offset=offset, written=f"{old:02X}", read=f"{new:02X}",
                             cleared_bits=(old & ~new).bit_count(), set_bits=(new & ~old).bit_count(),
                             sysex=usb.codec.sysex_location(offset)))
    return dict(changed_bytes=len(rows), cleared_bits=sum(r["cleared_bits"] for r in rows),
                set_bits=sum(r["set_bits"] for r in rows), differences=rows)


def chunks(data):
    return [(usb.RAM_BASE + offset, data[offset:offset + 256]) for offset in range(0, len(data), 256)]


def load_session(folder):
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    require(plan["version"] == 1 and plan["profile"] == "142", "Unknown retention plan.")
    blobs = {name: (folder / f"{name}.bin").read_bytes() for name in
             ("original", "saved-original", "flips-original", "expected", "saved-expected")}
    for name, data in blobs.items():
        require(digest(data) == plan["sha256"][name], "A retention backup has changed.")
    original, flash = blobs["original"], blobs["saved-original"]
    require(len(original) == usb.CONFIG_BYTES and len(flash) == usb.FLASH_BYTES
            and len(blobs["flips-original"]) == 4 and original == flash[:usb.CONFIG_BYTES]
            and flash[usb.CONFIG_BYTES:usb.CONFIG_BYTES + 8] == b"usrdata\0", "Invalid original storage image.")
    spans = usb.padding_spans(original)
    expected = usb.pattern_image(original, spans, b"\xff")
    require(blobs["expected"] == expected and blobs["saved-expected"] == expected + flash[usb.CONFIG_BYTES:],
            "Test image differs from the independently reconstructed FF pattern.")
    return plan, blobs, spans


def restore_link(link, blobs):
    original = blobs["original"]
    writes = chunks(original)
    # Full frozen configuration restoration also repairs any corruption of
    # active banks, rows or length bytes observed during a physical restart.
    link.allow = tuple(writes + [(usb.FLIPS, blobs["flips-original"]), (usb.KEYS, bytes(8))])
    link.write(usb.KEYS, bytes(8))
    for address, data in writes:
        link.write(address, data)
    link.write(usb.FLIPS, blobs["flips-original"])
    require(link.read(usb.RAM_BASE, usb.CONFIG_BYTES) == original, "RAM restoration mismatch.")
    result = usb.wait_saved(link, blobs["saved-original"])
    require(link.read(usb.RAM_BASE, usb.CONFIG_BYTES) == original
            and link.read(usb.FLIPS, 4) == blobs["flips-original"]
            and link.read(usb.KEYS, 8) == bytes(8), "Restored runtime state differs.")
    return result


def prepare(midi, folder):
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(status="preflight", created_utc=stamp(), firmware_writes=0, restored=False)
    manifest = folder / "prepare.json"
    record(manifest, report)
    with usb.UsbLink(midi) as link:
        original = link.read(usb.RAM_BASE, usb.CONFIG_BYTES)
        flash = link.read(usb.USR_VIEW, usb.FLASH_BYTES)
        flips = link.read(usb.FLIPS, 4)
        require(original == link.read(usb.RAM_BASE, usb.CONFIG_BYTES)
                and flash == link.read(usb.USR_VIEW, usb.FLASH_BYTES)
                and original == flash[:usb.CONFIG_BYTES]
                and flash[usb.CONFIG_BYTES:usb.CONFIG_BYTES + 8] == b"usrdata\0"
                and link.read(usb.KEYS, 8) == bytes(8), "Unstable, unsaved, or busy baseline.")
        spans = usb.padding_spans(original)
        expected = usb.pattern_image(original, spans, b"\xff")
        blobs = {"original": original, "saved-original": flash, "flips-original": flips,
                 "expected": expected, "saved-expected": expected + flash[usb.CONFIG_BYTES:]}
        for name, data in blobs.items():
            usb.durable_write(folder / f"{name}.bin", data)
        plan = dict(version=1, profile="142", created_utc=stamp(), pattern_bytes=sum(n for _, n in spans),
                    sha256={name: digest(data) for name, data in blobs.items()})
        usb.durable_write(folder / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
        load_session(folder)  # Validate immutable recovery files before mutation.
        link.allow = tuple((usb.RAM_BASE + offset, expected[offset:offset + size]) for offset, size in spans)
        try:
            report.update(status="writing_pattern", pattern_bytes=plan["pattern_bytes"])
            record(manifest, report)
            for address, data in link.allow:
                link.write(address, data)
            report["last_write_utc"] = stamp()
            require(link.read(usb.RAM_BASE, usb.CONFIG_BYTES) == expected, "Test RAM readback differs.")
            report["save"] = usb.wait_saved(link, blobs["saved-expected"])
            report.update(status="prepared_wait_for_physical_transition", saved_verified_utc=stamp())
            record(manifest, report)
        except BaseException as error:
            report.update(status="preparation_failed_restoring", error=f"{type(error).__name__}: {error}")
            record(manifest, report)
            report["restore_save"] = restore_link(link, blobs)
            report.update(status="preparation_failed_restored", restored=True)
            record(manifest, report)
            raise
    return report


def capture(midi, folder, output, restore=True):
    _, blobs, _ = load_session(folder)  # Must fail before hardware access if tampered.
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="reading_after_physical_transition", captured_utc=stamp(),
                  firmware_writes=0, selector_position_independently_verified=False, restored=False)
    manifest = output / "report.json"
    record(manifest, report)
    with usb.UsbLink(midi) as link:
        try:
            ram1 = link.read(usb.RAM_BASE, usb.CONFIG_BYTES)
            flash1 = link.read(usb.USR_VIEW, usb.FLASH_BYTES)
            ram2 = link.read(usb.RAM_BASE, usb.CONFIG_BYTES)
            flash2 = link.read(usb.USR_VIEW, usb.FLASH_BYTES)
            for name, data in (("ram-1", ram1), ("saved-1", flash1), ("ram-2", ram2), ("saved-2", flash2)):
                usb.durable_write(output / f"{name}.bin", data)
            ram_diff, saved_diff = changes(blobs["expected"], ram1), changes(blobs["saved-expected"], flash1)
            report.update(ram=ram_diff, saved=saved_diff, stable=ram1 == ram2 and flash1 == flash2,
                          ram_matches_saved=ram1 == flash1[:usb.CONFIG_BYTES],
                          ram_changed_bytes=ram_diff["changed_bytes"], saved_changed_bytes=saved_diff["changed_bytes"])
            report["retention_passed"] = report["stable"] and ram1 == blobs["expected"] and flash1 == blobs["saved-expected"]
            report["status"] = "measured"
            record(manifest, report)  # Evidence exists before restoration.
        except BaseException as error:
            report.update(error=f"{type(error).__name__}: {error}", status="measurement_failed")
            record(manifest, report)
            raise
        finally:
            if restore:
                report["status"] = "restoring_original"
                record(manifest, report)
                report["restore_save"] = restore_link(link, blobs)
                report["restored"] = True
                report["status"] = "complete" if "retention_passed" in report else "measurement_failed_restored"
                record(manifest, report)
    return report


def recover(midi, folder, output):
    _, blobs, _ = load_session(folder)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="restoring_original", firmware_writes=0, restored=False)
    record(output / "report.json", report)
    with usb.UsbLink(midi) as link:
        report["save"] = restore_link(link, blobs)
    report.update(status="restored", restored=True)
    record(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "capture", "restore"))
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-pattern", action="store_true", help="Capture only, for another physical transition.")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, operation=args.operation, pattern_bytes=7620,
                              firmware_writes=0, physical_transition="manual selector / power, recorded separately")))
        return
    if args.operation == "prepare":
        require(not args.session.exists(), "Session exists; preserve its backups and use a new directory.")
    else:
        require(args.output is not None and not args.output.exists(), "A new output directory is required.")
    require(not args.keep_pattern or args.operation == "capture", "keep-pattern is valid only for capture.")
    import mido
    if args.operation == "prepare":
        prepare(mido, args.session)
    elif args.operation == "capture":
        capture(mido, args.session, args.output, not args.keep_pattern)
    else:
        recover(mido, args.session, args.output)


if __name__ == "__main__":
    main()
