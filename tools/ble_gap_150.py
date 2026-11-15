"""Exercise one bank containing two HOST SysEx on the pinned 1-ms 150 firmware.

Two BLE configuration links observe the direct HOST cable. This is a delivery
test, not a USB timestamp capture. Backups precede staging; every trial group
restores bank assignments and TANK live values. Saved TANK presets are read-only.
"""
from __future__ import annotations

import asyncio
import json
import time

try:
    from . import ble_host_142 as host, prepare_gap_150 as build, sysex_gap as setting
except ImportError:
    import ble_host_142 as host
    import prepare_gap_150 as build
    import sysex_gap as setting

usb, relay, require = host.usb, host.relay, host.require
GAP_ADDRESS = usb.RAM_BASE + setting.OFFSET


class Channel(host.Channel):
    def __init__(self, client, plus=False, record=lambda *args: None):
        super().__init__(client, plus, record)
        if plus:
            self.identity = build.IDENTITY
            self.ranges += ((4, build.MODULE_BASE, build.MODULE_BYTES), (4, GAP_ADDRESS, setting.RECORD_BYTES))
            self.windows += ((GAP_ADDRESS, setting.RECORD_BYTES),)

    async def verify_app(self):
        require(self.plus and self.verified, "Expected identified 150 Plus")
        app = await self.read(usb.APP_BASE, usb.PROFILES["150"]["app_size"])
        require(host.codec.digest(app) == build.APP_SHA256, "Complete application differs from pinned 150")
        module = await self.read(build.MODULE_BASE, build.MODULE_BYTES)
        require(host.codec.digest(module) == build.MODULE_SHA256, "Extension differs from pinned 150")
        require(await self.read(0x19100, 4) == b"\0\x90\x03\0", "Unexpected save pointer")
        self.pinned = True
        return host.codec.digest(app)

    def guard(self, command, body):
        super().guard(command, body)
        if self.plus and command == 0x22:
            address = self.base + int.from_bytes(body[1:5], "little")
            if GAP_ADDRESS <= address < GAP_ADDRESS + setting.RECORD_BYTES:
                require(address == GAP_ADDRESS and len(body) == 8 + setting.RECORD_BYTES,
                        "Only a complete gap record may be written")
                setting.decode(body[8:])

    async def get_gap(self):
        require(self.plus and self.pinned, "Only a verified 150 may access its setting")
        return setting.decode(await self.read(GAP_ADDRESS, setting.RECORD_BYTES))

    async def set_gap(self, milliseconds, blobs):
        record = setting.encode(milliseconds)
        require(self.write_size >= setting.RECORD_BYTES, "BLE MTU cannot carry the complete setting")
        require(await self.read(usb.RAM_BASE, usb.CONFIG_BYTES) == blobs["plus-original"] and
                await self.read(usb.USR_VIEW, usb.FLASH_BYTES) == blobs["plus-saved"],
                "Restore the backed-up configuration before changing the interval")
        if await self.get_gap() == milliseconds:
            return blobs
        self.arm([(GAP_ADDRESS, record)])
        await self.write(GAP_ADDRESS, record)  # one attempt; no retry after an uncertain ACK
        expected = blobs["plus-saved"][:setting.FOOTER] + record + blobs["plus-saved"][setting.FOOTER + 16:]
        require(await self.get_gap() == milliseconds and
                await self.read(usb.RAM_BASE, usb.CONFIG_BYTES) == blobs["plus-original"], "Setting/RAM readback differs")
        await host.wait_saved(self, expected)
        return dict(blobs, **{"plus-saved": expected})


async def paired_trials(plus, tank, blobs, save, *, repetitions=5, manual_marker=None):
    require(type(repetitions) is int and 1 <= repetitions <= 10, "Choose 1..10 trials")
    original, flash, live = blobs["plus-original"], blobs["plus-saved"], blobs["tank-live"]
    require(original[host.C:host.D + 417] == bytes(834) and original[0] == 3 and
            original[23639:23641] == bytes(2), "Expected unused C/D and one advanced page")
    gap = await plus.get_gap()
    require(flash[setting.FOOTER:setting.FOOTER + 16] == setting.encode(gap), "Saved gap differs from baseline")
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
        save(status="staging_paired_bank", gap_ms=gap, manual=manual_marker is not None)
        await plus.write(usb.RAM_BASE + host.C, assignment)
        await plus.write(usb.RAM_BASE + 23640, original[23640:])
        require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == config, "Staged bank differs")
        await host.wait_saved(plus, config + flash[usb.CONFIG_BYTES:])
        require(await plus.get_gap() == gap, "Ordinary bank save lost the global interval")
        for index in range(1 if manual_marker is not None else repetitions):
            for i, n in host.CONTROLLED:
                await tank.write(relay.LIVE + i, staged[i:i + n])
            require(await host.stable_live(tank) == staged, "TANK staging differs")
            await host.host_ready(plus)
            flips = await plus.read(usb.FLIPS, 4)
            await asyncio.sleep(.25)
            if manual_marker is not None:
                require(not manual_marker.exists(), "Use a fresh manual confirmation marker")
                save(status="waiting_for_physical_C", gap_ms=gap, confirmation_file=str(manual_marker))
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
            check = dict(gap_ms=gap, repetition=index + 1, trigger=trigger, passed=actual == expected,
                         actual=actual.hex(), expected=expected.hex(), flips_before=flips.hex(), flips_after=after.hex(),
                         values=host.ble.values(actual, blobs["tank-state"]), usb_packet_capture=False)
            checks.append(check)
            save(status="paired_trial_measured", last_trial=check)
    except BaseException as error:
        failure = error
    finally:
        if dirty:
            save(status="restoring_trial_group", gap_ms=gap)
            restored = await host.restore(plus, tank, blobs)
            save(**restored)
            require(not restored["restore_errors"], "Trial restoration incomplete; keep the recovery files")
    if failure:
        raise failure
    return checks


def main():
    parser = host.argument_parser()
    parser.description = __doc__
    parser.add_argument("--intervals-ms", type=int, nargs="+", default=[21, 20, 1, 0])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--expect-initial-gap", type=int, default=21)
    parser.add_argument("--physical-press", action="store_true")
    args = parser.parse_args()
    require(not args.restore_from, "Recovery requires the interval in the saved backup; use a dedicated recovery run")
    require(1 <= args.repetitions <= 10 and len(set(args.intervals_ms)) == len(args.intervals_ms), "Invalid trial selection")
    for ms in [*args.intervals_ms, args.expect_initial_gap]:
        setting.encode(ms)
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, firmware_writes=0, intervals_ms=args.intervals_ms,
                              repetitions=args.repetitions, physical_press=args.physical_press, final_gap_ms=20)))
        return
    async def experiment(plus, tank, blobs, save):
        initial = await plus.get_gap()
        require(initial == args.expect_initial_gap, "Interval did not persist through the confirmed restart")
        save(initial_gap_ms=initial, persistent_gap_verified=True, checks=[])
        checks = []
        for milliseconds in args.intervals_ms:
            blobs = await plus.set_gap(milliseconds, blobs)
            checks += await paired_trials(plus, tank, blobs, save, repetitions=args.repetitions)
            save(checks=checks, results=[dict(gap_ms=ms, passed=sum(c["passed"] for c in checks if c["gap_ms"] == ms),
                 trials=sum(c["gap_ms"] == ms for c in checks)) for ms in dict.fromkeys(c["gap_ms"] for c in checks)])
        blobs = await plus.set_gap(20, blobs)
        if args.physical_press:
            require(any(c["gap_ms"] == 20 and c["passed"] for c in checks), "Check automatic delivery at 20 ms before a physical press")
            manual = await paired_trials(plus, tank, blobs, save, manual_marker=args.output / "physical-C-confirmed")
            checks += manual
            save(physical_keypresses=True, physical_check=manual[0], checks=checks)
        save(final_gap_ms=await plus.get_gap(), plus_write_attempts=plus.writes, tank_write_attempts=tank.writes)
    asyncio.run(host.run(args, experiment=experiment, channel_factory=Channel))


if __name__ == "__main__":
    main()
