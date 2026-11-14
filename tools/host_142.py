#!/usr/bin/env python3
"""Measure a physical Plus HOST -> TANK test, with separate USB readback.

The user moves the cable, keeps the TANK powered and presses C. The PC stages
only the baseline; target FX OFF / REV Mix 73 is emitted by the Plus. C has
identical A/B banks, so bank-toggle startup state cannot alter the test.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

try:
    from . import tank_relay_142 as relay
    from . import retention_142 as retention
except ImportError:
    import tank_relay_142 as relay
    import retention_142 as retention

usb, require, digest = relay.plus, relay.require, relay.codec.digest


def images(original, live):
    require(len(original) == usb.CONFIG_BYTES and len(live) == relay.LIVE_SIZE
            and original[0] == 3 and original[23639:23641] == bytes(2)
            and original[relay.C_OFFSET:relay.C_OFFSET + 417] == bytes(417), "Expected unused C on page 1 only.")
    frames = relay.live_frame(8, b"\0") + relay.live_frame(70, b"\x49\0")
    c = bytearray(417)
    c[0] = 1
    for bank in range(2):
        row, area = 1 + bank * 80, 161 + bank * 128
        c[row:row + 5] = b"\1\0\4\0\0"
        c[area] = len(frames)
        c[area + 1:area + 1 + len(frames)] = frames
    config = original[:relay.C_OFFSET] + bytes(c) + original[relay.C_OFFSET + 417:]
    staged = bytearray(live)
    staged[0:2] = bytes(2)  # Mute during the test, restore original level afterwards.
    staged[8] = 1
    staged[12] = 0  # REV disabled while its parameter is changed.
    staged[70:72] = b"\x2f\0"
    target = bytearray(staged)
    target[8], target[70] = 0, 73
    return config, bytes(staged), bytes(target)


def load(folder):
    manifest = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    require(manifest["version"] == 1, "Unknown HOST test plan.")
    names = ("plus-original", "plus-saved", "plus-flips", "tank-state", "tank-live", "tank-ram", "tank-flash",
             "plus-staged", "tank-staged", "tank-target")
    blobs = {name: (folder / f"{name}.bin").read_bytes() for name in names}
    for name, data in blobs.items():
        require(digest(data) == manifest["sha256"][name], "HOST backup hash mismatch.")
    config, staged, target = images(blobs["plus-original"], blobs["tank-live"])
    require((config, staged, target) == (blobs["plus-staged"], blobs["tank-staged"], blobs["tank-target"]),
            "HOST test differs from the independently reconstructed targets.")
    require(len(blobs["plus-saved"]) == usb.FLASH_BYTES and len(blobs["plus-flips"]) == 4
            and blobs["plus-saved"][:usb.CONFIG_BYTES] == blobs["plus-original"]
            and len(blobs["tank-state"]) == 4 and len(blobs["tank-ram"]) == len(blobs["tank-flash"]) == relay.BANK_SIZE,
            "Invalid original backup geometry.")
    return blobs


def tank_scope(tank, blobs):
    original = blobs["tank-live"]
    tank.allowed = {relay.live_frame(i, bytes([b])) for i, b in enumerate(original)}
    for block in range(6):
        area = 20 + 12 * block
        tank.allowed.update((relay.live_frame(8 + block, b"\0"), relay.live_frame(area, original[area:area + 12])))
    tank.allowed.update((relay.live_frame(0, bytes(2)), relay.live_frame(8, b"\1"),
                         relay.live_frame(12, b"\0"), relay.live_frame(70, b"\x2f\0")))
    # The PC never sends the target Mix 73. It only stages 47 and can restore
    # the original. If original was already 73, the staged 47 still differs.


def snapshot(blobs, live):
    return dict(state=blobs["tank-state"], live=live, ram=blobs["tank-ram"], flash=blobs["tank-flash"])


def restore_tank(tank, blobs):
    tank_scope(tank, blobs)
    tank.restore(blobs["tank-live"])
    require(tank.snapshot() == snapshot(blobs, blobs["tank-live"]), "TANK restoration differs from baseline.")


def restore_plus(link, blobs):
    return retention.restore_link(link, {"original": blobs["plus-original"],
                                        "saved-original": blobs["plus-saved"], "flips-original": blobs["plus-flips"]})


def prepare(midi, folder):
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(status="preflight", firmware_writes=0, tank_restored=False, plus_restored=False)
    path = folder / "prepare.json"
    retention.record(path, report)
    with relay.Tank(midi) as tank, usb.UsbLink(midi) as link:
        before = tank.snapshot()
        original, flash = link.read(usb.RAM_BASE, usb.CONFIG_BYTES), link.read(usb.USR_VIEW, usb.FLASH_BYTES)
        flips = link.read(usb.FLIPS, 4)
        require(before == tank.snapshot() and before["state"][0] == before["state"][1]
                and original == flash[:usb.CONFIG_BYTES] and original == link.read(usb.RAM_BASE, usb.CONFIG_BYTES)
                and flash == link.read(usb.USR_VIEW, usb.FLASH_BYTES) and link.read(usb.KEYS, 8) == bytes(8),
                "Unstable, unsaved or busy baseline.")
        config, staged, target = images(original, before["live"])
        blobs = {"plus-original": original, "plus-saved": flash, "plus-flips": flips,
                 **{f"tank-{k}": v for k, v in before.items()},
                 "plus-staged": config, "tank-staged": staged, "tank-target": target}
        for name, data in blobs.items():
            usb.durable_write(folder / f"{name}.bin", data)
        plan = dict(version=1, created_utc=retention.stamp(), sha256={name: digest(data) for name, data in blobs.items()})
        usb.durable_write(folder / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
        load(folder)
        tank_scope(tank, blobs)
        link.allow = ((usb.RAM_BASE + relay.C_OFFSET, config[relay.C_OFFSET:relay.C_OFFSET + 417]),)
        try:
            report["status"] = "staging_usb_control_and_C"
            retention.record(path, report)
            for offset, data in ((0, bytes(2)), (8, b"\1"), (12, b"\0"), (70, b"\x2f\0")):
                tank.write(offset, data)
            require(tank.stable() == staged and tank.snapshot() == snapshot(blobs, staged), "TANK staging mismatch.")
            link.write(*link.allow[0])
            require(link.read(usb.RAM_BASE, usb.CONFIG_BYTES) == config, "C staging differs from expected configuration.")
            report["save"] = usb.wait_saved(link, config + flash[usb.CONFIG_BYTES:])
            report.update(status="prepared_for_USB_reconnect_control", saved_utc=retention.stamp())
            retention.record(path, report)
        except BaseException as error:
            report.update(status="preparation_failed", error=f"{type(error).__name__}: {error}")
            errors = []
            try:
                restore_tank(tank, blobs)
                report["tank_restored"] = True
            except BaseException as recovery:
                errors.append(f"TANK: {recovery}")
            try:
                restore_plus(link, blobs)
                report["plus_restored"] = True
            except BaseException as recovery:
                errors.append(f"Plus: {recovery}")
            report["recovery_errors"] = errors
            retention.record(path, report)
            raise
    return report


def measure_tank(midi, folder, output, control=False):
    blobs = load(folder)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="reading_TANK", created_utc=retention.stamp(), kind="USB reconnect control" if control else "HOST target readback",
                  host_connection_independently_verified=False, restored=False)
    path = output / "report.json"
    retention.record(path, report)
    with relay.Tank(midi) as tank:
        try:
            first, second = tank.snapshot(), tank.snapshot()
            for name, data in first.items():
                usb.durable_write(output / f"tank-{name}.bin", data)
            expected = snapshot(blobs, blobs["tank-staged" if control else "tank-target"])
            report.update(stable=first == second, passed=first == second == expected,
                          differences=retention.changes(expected["live"], first["live"]),
                          FX=first["live"][8], REV_mix=int.from_bytes(first["live"][70:72], "little"),
                          saved_ram_unchanged=first["ram"] == blobs["tank-ram"],
                          saved_flash_unchanged=first["flash"] == blobs["tank-flash"],
                          preset_state_unchanged=first["state"] == blobs["tank-state"])
            report["status"] = "measured"
            retention.record(path, report)
        finally:
            if not control:
                report["status"] = "restoring_TANK"
                retention.record(path, report)
                restore_tank(tank, blobs)
                report.update(status="complete", restored=True)
                retention.record(path, report)
    return report


def compare_plus_storage(blobs, ram, flash):
    expected = blobs["plus-staged"]
    configuration = retention.changes(expected, ram)
    saved = retention.changes(expected + blobs["plus-saved"][usb.CONFIG_BYTES:], flash)
    def data_intact(result):
        return all(row["offset"] == 23646 and int(row["read"], 16) < 4 for row in result["differences"])
    # 142 updates the display's current-switch byte in RAM at 0x1e1304c.
    # A physical advanced-mode press does not itself arm the save counter;
    # the protocol-write path at 0x1e13b76 does. After reboot this byte can
    # therefore retain its staged value. It cannot prove which key was pressed.
    return dict(configuration=configuration, saved=saved,
                ram_matches_saved=ram == flash[:usb.CONFIG_BYTES],
                configuration_data_intact=data_intact(configuration), saved_data_intact=data_intact(saved),
                current_switch_marker=dict(staged=expected[23646], ram=ram[23646], saved=flash[23646],
                                           proves_physical_C_press=False))


def finish(midi, folder, output):
    blobs = load(folder)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="reading_Plus_after_H", restored=False)
    path = output / "report.json"
    retention.record(path, report)
    with usb.UsbLink(midi) as link:
        try:
            ram, flash = link.read(usb.RAM_BASE, usb.CONFIG_BYTES), link.read(usb.USR_VIEW, usb.FLASH_BYTES)
            usb.durable_write(output / "configuration-after.bin", ram)
            usb.durable_write(output / "saved-after.bin", flash)
            report.update(compare_plus_storage(blobs, ram, flash))
            report["status"] = "measured"
            retention.record(path, report)
        finally:
            report["status"] = "restoring_Plus"
            retention.record(path, report)
            report["restore_save"] = restore_plus(link, blobs)
            report.update(status="complete", restored=True)
            retention.record(path, report)
    return report


def recover(midi, folder, output):
    blobs = load(folder)
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="restoring_both_devices", tank_restored=False, plus_restored=False, errors=[])
    path = output / "report.json"
    retention.record(path, report)
    # Attempt each independently: an unavailable TANK must not prevent
    # restoration of the Plus, or vice versa.
    try:
        with relay.Tank(midi) as tank:
            restore_tank(tank, blobs)
        report["tank_restored"] = True
    except BaseException as error:
        report["errors"].append(f"TANK: {type(error).__name__}: {error}")
    try:
        with usb.UsbLink(midi) as link:
            report["plus_restore_save"] = restore_plus(link, blobs)
        report["plus_restored"] = True
    except BaseException as error:
        report["errors"].append(f"Plus: {type(error).__name__}: {error}")
    report["status"] = "restored" if not report["errors"] else "restoration_incomplete"
    retention.record(path, report)
    require(not report["errors"], "; ".join(report["errors"]))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "check-usb", "read-tank", "finish", "restore"))
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, operation=args.operation, host_target="FX OFF + REV Mix 73",
                              requires_physical_cable_move_and_C_press=True)))
        return
    if args.operation == "prepare":
        require(not args.session.exists(), "Session exists; use a new path to preserve backups.")
    else:
        require(args.output is not None and not args.output.exists(), "A new evidence output folder is required.")
    import mido
    if args.operation == "prepare":
        prepare(mido, args.session)
    elif args.operation in ("check-usb", "read-tank"):
        result = measure_tank(mido, args.session, args.output, args.operation == "check-usb")
        require(result["passed"], "TANK readback differs; inspect the report before another physical step.")
    elif args.operation == "finish":
        finish(mido, args.session, args.output)
    else:
        recover(mido, args.session, args.output)


if __name__ == "__main__":
    main()
