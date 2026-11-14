#!/usr/bin/env python3
"""Isolate the Plus's native radio while checking USB MIDI delivery.

Only the pinned 142 image is accepted by UsbLink. Its stock B+C request is
the 16-bit counter STATE+76 = 400, consumed at 0x1e12a30. The native handler
toggles config byte 23651 and runs its normal radio start/stop routines in
the firmware's own context. No code, callback, radio handle or save counter
is written. Configuration and both device snapshots are restored afterwards.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

try:
    from . import host_142 as host
except ImportError:
    import host_142 as host

usb, relay, require = host.usb, host.relay, host.require
RADIO_FLAG = usb.RAM_BASE + 23651
BC_REQUEST = usb.STATE + 76
RADIO_WRITES = ((RADIO_FLAG, b"\0"), (RADIO_FLAG, b"\1"), (BC_REQUEST, b"\x90\x01"))


def state(link):
    data = link.read(usb.STATE, 180)
    flag = link.read(RADIO_FLAG, 1)[0]
    require(flag in (0, 1) and data[164] in (0, 1) and data[168] in (0, 1),
            "Unexpected native radio state.")
    return dict(off_flag=flag, enabled=bool(data[168]),
                server_enabled=bool(data[164]),
                client_stage=data[5],
                server_handle=int.from_bytes(data[68:70], "little"),
                client_handle=int.from_bytes(data[70:72], "little"),
                combo_counter=int.from_bytes(data[76:78], "little"))


def set_enabled(link, enabled):
    require(type(enabled) is bool, "Radio target must be boolean.")
    before = state(link)
    require(before["combo_counter"] == 0 and link.read(usb.KEYS, 8) == bytes(8),
            "Native key/combo scanner is busy.")
    if before["enabled"] == enabled:
        require(enabled or not (before["server_handle"] or before["client_handle"]),
                "Disabled radio still has a connection; stop and inspect.")
        return dict(before=before, after=before, writes=0)
    target_flag = int(not enabled)
    # The saved flag can disagree with the running radio after boot/restoration.
    # Set the pre-toggle flag explicitly; let the stock B+C handler perform
    # the actual transition and disconnect, rather than editing live handles.
    link.write(RADIO_FLAG, bytes([1 - target_flag]))
    link.write(BC_REQUEST, b"\x90\x01")
    started = time.monotonic()
    while time.monotonic() - started < 8:
        after = state(link)
        if (after["combo_counter"] == 0 and after["off_flag"] == target_flag
                and after["enabled"] == enabled and after["server_enabled"] == enabled
                and (enabled or not (after["server_handle"] or after["client_handle"]))):
            return dict(before=before, after=after, writes=2,
                        seconds=round(time.monotonic() - started, 3))
        time.sleep(0.05)
    raise ValueError("Native radio transition did not reach its verified target.")


def wait_connections(link, original, timeout=12):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        current = state(link)
        if (current["enabled"] == original["enabled"]
                and (not original["client_handle"] or current["client_stage"] == original["client_stage"])
                and all(bool(current[k]) == bool(original[k]) for k in ("server_handle", "client_handle"))):
            return current
        time.sleep(0.1)
    raise ValueError("Original radio connection presence did not return.")


def run(midi, session, output):
    blobs = host.load(session)
    manifest = json.loads((session / "plan.json").read_text())
    require(manifest.get("variant") == "split", "Use a validated split-test backup.")
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="preflight", firmware_writes=0, USB_calibration=[],
                  tank_restored=False, plus_restored=False, radio_restored=False)
    path = output / "report.json"
    def record(): host.retention.record(path, report)
    record()
    with relay.Tank(midi) as tank, usb.UsbLink(midi) as link:
        original_radio = state(link)
        require(original_radio["enabled"] and original_radio["client_handle"]
                and not original_radio["server_handle"], "Expected the observed active native client connection.")
        require(tank.snapshot() == host.snapshot(blobs, blobs["tank-live"])
                and link.read(usb.RAM_BASE, usb.CONFIG_BYTES) == blobs["plus-original"]
                and link.read(usb.USR_VIEW, usb.FLASH_BYTES) == blobs["plus-saved"]
                and link.read(usb.FLIPS, 4) == blobs["plus-flips"]
                and link.read(usb.KEYS, 8) == bytes(8), "Devices differ from the frozen recovery baseline.")
        report["radio_before"] = original_radio
        report["recovery_session"] = str(session)
        record()
        config = blobs["plus-staged"]
        writes = [(usb.RAM_BASE + offset, config[offset:offset + 417])
                  for offset in (relay.C_OFFSET, relay.C_OFFSET + 417)]
        link.allow = tuple(writes + list(RADIO_WRITES) +
                           [(usb.KEYS + 2 * key, b"\0\x64") for key in (2, 3)] +
                           [(usb.FLIPS, blobs["plus-flips"]), (usb.RAM_BASE + 23640, config[23640:])])
        host.tank_scope(tank, blobs)
        try:
            report["status"] = "disabling_native_radio"
            record()
            report["disable"] = set_enabled(link, False)
            record()
            host.stage_tank(tank, blobs)
            for address, data in writes: link.write(address, data)
            report["status"] = "USB_calibration_with_radio_off"
            record()
            host.calibrate_split(link, tank, blobs, report["USB_calibration"], record)
            require(not state(link)["enabled"], "Radio restarted during USB calibration.")
            report["status"] = "checking_radio_path_without_PC_relay"
            record()
            report["enable"] = set_enabled(link, True)
            report["radio_reconnected"] = wait_connections(link, original_radio)
            require(tank.stable() == blobs["tank-staged"], "TANK changed before radio-path control.")
            link.collect(0.03)
            link.write(usb.KEYS + 6, b"\0\x64")
            frames = link.collect()
            require(frames == [b"\xb0\0\0"], "Unexpected D output; no PC forwarding performed.")
            actual = tank.stable(1)
            expected = bytearray(blobs["tank-staged"])
            expected[8] = 0
            report["radio_path"] = dict(actual_frames=[f.hex(" ") for f in frames], PC_forwarded_CC=0,
                                         before=blobs["tank-staged"].hex(), after=actual.hex(),
                                         expected=bytes(expected).hex(), passed=actual == expected)
            record()
            require(actual == expected, "CC was not independently delivered after restoring the radio.")
            report["passed"] = True
        except BaseException as error:
            report.update(passed=False, error=f"{type(error).__name__}: {error}")
            raise
        finally:
            report["status"] = "restoring_both_devices_and_radio"
            record()
            errors = []
            try:
                host.restore_tank(tank, blobs)
                report["tank_restored"] = True
            except BaseException as error: errors.append(f"TANK: {error}")
            try:
                set_enabled(link, original_radio["enabled"])
                report["radio_after"] = wait_connections(link, original_radio)
                report["radio_restored"] = True
            except BaseException as error: errors.append(f"Radio: {error}")
            try:
                report["restore_save"] = host.restore_plus(link, blobs)
                report["plus_restored"] = True
            except BaseException as error: errors.append(f"Plus: {error}")
            report["restore_errors"] = errors
            report["status"] = "complete" if not errors else "restoration_incomplete"
            record()
            require(not errors, "; ".join(errors))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, native_BC_request=True, firmware_writes=0, restores=True)))
        return
    import mido
    run(mido, args.session, args.output)


if __name__ == "__main__": main()
