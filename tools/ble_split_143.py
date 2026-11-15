"""Exercise one bank containing two HOST SysEx on the pinned split-only 143 firmware.

Two BLE configuration links observe the direct HOST cable. This is a delivery
test, not a USB timestamp capture. Backups precede staging; every trial group
restores bank assignments and TANK live values. Saved TANK presets are read-only.
"""
from __future__ import annotations

import asyncio
import binascii
import json
import time

try:
    from . import ble_host_142 as host, prepare_split_143 as build
except ImportError:
    import ble_host_142 as host
    import prepare_split_143 as build

usb, relay, require = host.usb, host.relay, host.require
FOOTER = usb.CONFIG_BYTES + 8


class Channel(host.Channel):
    def __init__(self, client, plus=False, record=lambda *args: None):
        super().__init__(client, plus, record)
        if plus:
            self.identity = build.IDENTITY
            self.ranges += ((4, build.MODULE_BASE, build.MODULE_BYTES),)

    async def verify_app(self):
        require(self.plus and self.verified, "Expected identified 143 Plus")
        app = await self.read(usb.APP_BASE, usb.PROFILES["143"]["app_size"])
        require(host.codec.digest(app) == build.APP_SHA256, "Complete application differs from pinned 143")
        module = await self.read(build.MODULE_BASE, build.MODULE_BYTES)
        require(host.codec.digest(module) == build.MODULE_SHA256, "Extension differs from pinned 143")
        require(await self.read(0x19100, 4) == b"\0\x90\x03\0", "Unexpected save pointer")
        self.pinned = True
        return host.codec.digest(app)


def normalized_blobs(blobs):
    """Retire a validated legacy record through the next ordinary native save.

    This is a read-only decoder, not an endpoint for reading/changing an interval.
    Unknown trailing data is refused; complete original backups remain intact.
    """
    flash = blobs["plus-saved"]
    require(len(flash) == usb.FLASH_BYTES and flash[:usb.CONFIG_BYTES] == blobs["plus-original"]
            and flash[usb.CONFIG_BYTES:FOOTER] == b"usrdata\0", "Saved configuration layout differs")
    tail = flash[FOOTER:]
    if tail == b"\xff" * len(tail):
        return blobs
    record = tail[:16]
    ms = int.from_bytes(record[6:8], "little")
    require(record[:6] == b"SGAP\1\1" and ms <= 1000
            and int.from_bytes(record[8:10], "little") == ms ^ 0xFFFF
            and record[10:14] == b"\xe8\3\x14\0"
            and int.from_bytes(record[14:], "little") == binascii.crc_hqx(record[:14], 0)
            and tail[16:] == b"\xff" * (len(tail) - 16), "Unknown legacy footer; keep original backup")
    return dict(blobs, **{"plus-saved": flash[:FOOTER] + b"\xff" * len(tail)})


async def retire_legacy_record(plus, blobs, save, recovery_dir):
    normalized = normalized_blobs(blobs)
    if normalized == blobs:
        return blobs
    require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == blobs["plus-original"] and
            await plus.read(usb.USR_VIEW, usb.FLASH_BYTES) == blobs["plus-saved"], "Baseline changed before native save")
    # Recovery for the restored native saver expects erased unused bytes. Keep
    # this alongside (never overwrite) the original complete recovery baseline.
    recovery_dir.mkdir(parents=True, exist_ok=False)
    for name, data in normalized.items():
        usb.durable_write(recovery_dir / f"{name}.bin", data)
    usb.durable_write(recovery_dir / "recovery.json", (json.dumps(dict(sha256={
        name: host.codec.digest(data) for name, data in normalized.items()}), indent=2) + "\n").encode())
    require(host.recovery(recovery_dir) == normalized, "Normalized recovery verification failed")
    save(status="retiring_legacy_record", recovery_directory=str(recovery_dir), legacy_record_erased=False)
    address, data = usb.RAM_BASE + 23640, blobs["plus-original"][23640:]
    plus.arm([(address, data)])
    await plus.write(address, data)  # unchanged configuration; one attempt, no retry
    await host.wait_saved(plus, normalized["plus-saved"])
    require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == blobs["plus-original"], "Native save changed configuration")
    save(legacy_record_erased=True)
    return normalized


async def paired_trials(plus, tank, blobs, save, *, repetitions=5, manual_marker=None):
    require(type(repetitions) is int and 1 <= repetitions <= 10, "Choose 1..10 trials")
    original, flash, live = blobs["plus-original"], blobs["plus-saved"], blobs["tank-live"]
    require(original[host.C:host.D + 417] == bytes(834) and original[0] == 3 and
            original[23639:23641] == bytes(2), "Expected unused C/D and one advanced page")
    require(normalized_blobs(blobs) == blobs, "Retire the legacy record before staging a bank")
    assignment = host.assignment([relay.live_frame(8, b"\0"), relay.live_frame(70, b"\x49\0")]) + bytes(417)
    config = original[:host.C] + assignment + original[host.D + 417:]
    staged = bytearray(live)
    staged[:2], staged[8], staged[12], staged[70:72] = bytes(2), 1, 0, b"\x2f\0"
    staged = bytes(staged)
    expected = bytearray(staged)
    expected[8], expected[70:72] = 0, b"\x49\0"
    expected = bytes(expected)
    checks, dirty, failure = [], False, None
    try:
        await host.host_ready(plus)
        plus.arm([(usb.RAM_BASE + host.C, assignment), (usb.RAM_BASE + 23640, original[23640:]),
                  (usb.KEYS + 4, b"\0\x64")])
        tank.arm([(relay.LIVE + i, staged[i:i + n]) for i, n in host.CONTROLLED])
        dirty = True
        save(status="staging_paired_bank", added_wait_ms=0, manual=manual_marker is not None)
        await plus.write(usb.RAM_BASE + host.C, assignment)
        await plus.write(usb.RAM_BASE + 23640, original[23640:])
        require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == config, "Staged bank differs")
        await host.wait_saved(plus, config + flash[usb.CONFIG_BYTES:])
        for index in range(1 if manual_marker is not None else repetitions):
            for i, n in host.CONTROLLED:
                await tank.write(relay.LIVE + i, staged[i:i + n])
            require(await host.stable_live(tank) == staged, "TANK staging differs")
            await host.host_ready(plus)
            flips = await plus.read(usb.FLIPS, 4)
            await asyncio.sleep(.25)
            if manual_marker is not None:
                require(not manual_marker.exists(), "Use a fresh manual confirmation marker")
                save(status="waiting_for_physical_C", added_wait_ms=0, confirmation_file=str(manual_marker))
                deadline = time.monotonic() + 240
                while not manual_marker.exists():
                    require(time.monotonic() < deadline, "Physical C confirmation timed out")
                    await asyncio.sleep(.25)
                trigger = "user_confirmed_physical_C"
            else:
                await plus.write(usb.KEYS + 4, b"\0\x64")
                trigger = "one_native_C_event"
            # Avoid TANK BLE queries while it handles the HOST messages.
            await asyncio.sleep(.8)
            actual = await host.stable_live(tank)
            after = await plus.read(usb.FLIPS, 4)
            wanted = bytearray(flips)
            wanted[2] ^= 1
            require(after == wanted, "Expected exactly one C event")
            require(all(a == b for i, (a, b) in enumerate(zip(actual, staged)) if i not in (8, 70, 71)),
                    "Unexpected TANK parameter changed")
            require(await tank.read(relay.TANK_STATE, 4) == blobs["tank-state"], "TANK preset changed")
            await host.host_ready(plus)
            check = dict(added_wait_ms=0, repetition=index + 1, trigger=trigger, passed=actual == expected,
                         actual=actual.hex(), expected=expected.hex(), flips_before=flips.hex(), flips_after=after.hex(),
                         values=host.ble.values(actual, blobs["tank-state"]), usb_packet_capture=False)
            checks.append(check)
            save(status="paired_trial_measured", last_trial=check)
    except BaseException as error:
        failure = error
    finally:
        if dirty:
            save(status="restoring_trial_group", added_wait_ms=0)
            restored = await host.restore(plus, tank, blobs)
            save(**restored)
            require(not restored["restore_errors"], "Trial restoration incomplete; keep the recovery files")
    if failure:
        raise failure
    return checks


def main():
    parser = host.argument_parser()
    parser.description = __doc__
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--physical-press", action="store_true")
    args = parser.parse_args()
    require(1 <= args.repetitions <= 10, "Choose 1..10 trials")
    require(not (args.preflight_only and args.restore_from), "Choose preflight or recovery")
    if args.restore_from:
        blobs = host.recovery(args.restore_from)
        require(normalized_blobs(blobs) == blobs, "Use the normalized recovery directory for 143")
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, firmware_writes=0, added_wait_ms=0,
                              configurable_gap=False, repetitions=args.repetitions, physical_press=args.physical_press)))
        return
    async def experiment(plus, tank, blobs, save):
        blobs = await retire_legacy_record(plus, blobs, save, args.output / "normalized-recovery")
        checks = await paired_trials(plus, tank, blobs, save, repetitions=args.repetitions)
        save(checks=checks, tests_passed=all(c["passed"] for c in checks), added_wait_ms=0)
        if args.physical_press:
            require(all(c["passed"] for c in checks), "Automatic delivery failed; omit the physical trial")
            manual = await paired_trials(plus, tank, blobs, save, manual_marker=args.output / "physical-C-confirmed")
            checks += manual
            save(physical_keypresses=True, physical_check=manual[0], checks=checks,
                 tests_passed=all(c["passed"] for c in checks))
        save(plus_write_attempts=plus.writes, tank_write_attempts=tank.writes)
    asyncio.run(host.run(args, experiment=experiment, channel_factory=Channel))


if __name__ == "__main__":
    main()
