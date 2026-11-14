#!/usr/bin/env python3
"""Test nominal 142 HOST SysEx spacing with the native 5 ms key timer.

A single four-byte request primes the C/D release counters. The native timer
then queues FX OFF and REV Mix 73 without further PC requests between them.
This measures delivery success at nominal event spacing, not USB wire timing.
The firmware stays unchanged; all application changes are backed up/restored.
"""
from __future__ import annotations

import asyncio
import json
import time

try:
    from . import ble_host_142 as host
except ImportError:
    import ble_host_142 as host

usb, relay, ble, require = host.usb, host.relay, host.ble, host.require
INTERVALS = (70, 50, 30, 20, 10, 5, 0)
TICK_MS = 5


def release_request(interval_ms):
    """One write, two ordinary release events; never arm the C+D combo.

    Pinned 142: timer_add(0x1e11cf2, 5) at 0x1e1449e; released state 1
    returns through the tap cave to 0x1e12006. Counter >=15 invokes event 0
    at 0x1e1201e and clears the two bytes. Advanced toggle mode 1 sends on
    event 0 (0x1e13198..0x1e131c0). C+D combo requires state 100 for both
    (0x1e11f5a..0x1e11f80), whereas these requests use state 1.

    C fires on the next tick; D fires N ticks later. These are nominal timer
    intervals: memcpy/timer scheduling and USB queueing are not timestamped.
    """
    require(type(interval_ms) is int and 0 <= interval_ms <= 70 and interval_ms % TICK_MS == 0,
            "Spacing must be a multiple of 5 ms between 0 and 70 ms.")
    return bytes((14, 1, 14 - interval_ms // TICK_MS, 1))


def usb_events(frame):
    """Cable-0 events per USB-MIDI 1.0 section 4, tables 4-1 and 4-2."""
    require(usb.split_sysex(frame) == [frame], "Expected exactly one SysEx.")
    result = bytearray()
    for offset in range(0, len(frame), 3):
        part = frame[offset:offset + 3]
        final = offset + len(part) == len(frame)
        result.extend(bytes([4 + len(part) if final else 4]) + part.ljust(3, b"\0"))
    return bytes(result)


def compare_tx_prefix(data, frames):
    """Separate MIDI content/CIN differences from nonzero unused USB bytes.

    Only compare the expected extent: the rest of the RAM buffer can contain
    older transfers. No assertion about the last transmitted length is made.
    """
    expected = b"".join(usb_events(frame) for frame in frames)
    require(len(data) == 64 and 0 < len(expected) <= 64, "Invalid TX comparison extent.")
    padding = set()
    for offset in range(0, len(expected), 4):
        cin = expected[offset] & 15
        used = cin - 4 if cin in (5, 6, 7) else 3
        padding.update(range(offset + 1 + used, offset + 4))
    return dict(address=hex(host.HOST_TX), data=data.hex(), expected_prefix=expected.hex(),
                expected_prefix_matches=data.startswith(expected),
                midi_and_cin_match=all(data[i] == value for i, value in enumerate(expected) if i not in padding),
                nonzero_padding=[dict(offset=i, value=data[i]) for i in sorted(padding) if data[i]],
                usb_packet_capture=False)


async def retained_tx(plus, frames):
    """Read RAM, not MMIO; a retained buffer is not an on-wire capture."""
    return compare_tx_prefix(await plus.read(host.HOST_TX, 64), frames)


async def sweep(plus, tank, blobs, save, intervals=INTERVALS, repetitions=10):
    requests = {ms: release_request(ms) for ms in intervals}
    require(requests and len(requests) == len(intervals) and type(repetitions) is int and 1 <= repetitions <= 20,
            "Choose distinct spacings and 1..20 repetitions.")
    require(plus.write_size >= 4, "Timing requires one unfragmented four-byte key request.")
    original, flash, live = blobs["plus-original"], blobs["plus-saved"], blobs["tank-live"]
    require(original[host.C:host.D + 417] == bytes(834) and original[0] == 3
            and original[23639:23641] == bytes(2), "Timing requires unused C/D, advanced mode and only page 1.")
    fx, mix = relay.live_frame(8, b"\0"), relay.live_frame(70, b"\x49\0")
    staged = bytearray(live)
    staged[:2], staged[8], staged[12], staged[70:72] = bytes(2), 1, 0, b"\x2f\0"
    staged = bytes(staged)
    target = bytearray(staged)
    target[8], target[70:72] = 0, b"\x49\0"
    target = bytes(target)
    checks, dirty, failure = [], False, None
    tank.arm([(relay.LIVE + i, staged[i:i + size]) for i, size in host.CONTROLLED])
    save(measurement="nominal native key-event spacing; USB wire timestamps unavailable",
         nominal_tick_ms=TICK_MS, intervals_ms=list(intervals), repetitions=repetitions,
         one_physical_press_implementation=False)
    try:
        for phase in ("paired_control", "timer_separated"):
            await host.host_ready(plus)
            assignments = host.assignment([fx, mix]) + bytes(417) if phase == "paired_control" else (
                host.assignment([fx]) + host.assignment([mix]))
            config = original[:host.C] + assignments + original[host.D + 417:]
            triggers = [(usb.KEYS + 4, b"\0\x64")] if phase == "paired_control" else [
                (usb.KEYS + 4, data) for data in requests.values()]
            plus.arm([(usb.RAM_BASE + host.C, assignments), (usb.RAM_BASE + 23640, original[23640:]), *triggers])
            dirty = True
            save(status="staging_timing", phase=phase)
            for i, size in host.CONTROLLED:
                await tank.write(relay.LIVE + i, staged[i:i + size])
            await plus.write(usb.RAM_BASE + host.C, assignments)
            await plus.write(usb.RAM_BASE + 23640, original[23640:])
            require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == config, "Timing configuration differs.")
            await host.wait_saved(plus, config + flash[usb.CONFIG_BYTES:])
            trials = [(None, i) for i in range(min(3, repetitions))] if phase == "paired_control" else [
                (ms, i) for i in range(repetitions) for ms in (intervals if i % 2 == 0 else tuple(reversed(intervals)))]
            for ms, repetition in trials:
                for i, size in ((8, 1), (70, 2)):
                    await tank.write(relay.LIVE + i, staged[i:i + size])
                require(await host.stable_live(tank) == staged, "Timing baseline did not settle.")
                before = await host.host_ready(plus)
                flips = await plus.read(usb.FLIPS, 4)
                await asyncio.sleep(.25)
                trigger = b"\0\x64" if ms is None else requests[ms]
                started = time.monotonic()
                await plus.write(usb.KEYS + 4, trigger)
                ack_ms = (time.monotonic() - started) * 1000
                # No request to either peer between native C and D events.
                await asyncio.sleep(.8)
                tx = await retained_tx(plus, [fx, mix] if ms is None or ms == 0 else [mix])
                actual = await host.stable_live(tank)
                after = await host.host_ready(plus)
                flips_after = await plus.read(usb.FLIPS, 4)
                expected_flips = bytearray(flips)
                for key in ((2,) if ms is None else (2, 3)):
                    expected_flips[key] ^= 1
                require(flips_after == expected_flips, "Native event toggle parity differs from the requested sequence.")
                require(all(a == b for i, (a, b) in enumerate(zip(actual, staged)) if i not in (8, 70, 71)),
                        "Unexpected TANK parameter changed.")
                require(await tank.read(relay.TANK_STATE, 4) == blobs["tank-state"], "TANK preset state changed.")
                check = dict(phase=phase, nominal_interval_ms=ms, repetition=repetition + 1,
                             key_request=trigger.hex(), request_ack_ms=round(ack_ms, 3),
                             native_before=before, native_after=after, flips_before=flips.hex(), flips_after=flips_after.hex(),
                             actual=actual.hex(), expected=target.hex(), passed=actual == target,
                             values=ble.values(actual, blobs["tank-state"]), retained_tx=tx)
                checks.append(check)
                save(status="measured_timing", checks=checks, last_trial=dict(phase=phase, interval_ms=ms,
                     repetition=repetition + 1, passed=check["passed"], FX=actual[8], Mix=int.from_bytes(actual[70:72], "little")))
        summary = [dict(nominal_interval_ms=ms,
                        passed=sum(c["passed"] for c in checks if c["phase"] == "timer_separated" and c["nominal_interval_ms"] == ms),
                        trials=repetitions) for ms in intervals]
        save(timing_summary=summary, paired_control_passed=sum(c["passed"] for c in checks if c["phase"] == "paired_control"))
    except BaseException as error:
        failure = error
        save(error=f"{type(error).__name__}: {error}")
    finally:
        if dirty:
            save(status="restoring")
            restored = await host.restore(plus, tank, blobs)
            save(**restored, plus_write_attempts=plus.writes, tank_write_attempts=tank.writes)
            require(not restored["restore_errors"], "Timing restoration incomplete; retain recovery files.")
    if failure:
        raise failure
    return checks


def main():
    parser = host.argument_parser()
    parser.description = __doc__
    parser.add_argument("--intervals-ms", nargs="+", type=int, default=list(INTERVALS))
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args()
    for ms in args.intervals_ms:
        release_request(ms)
    require(len(set(args.intervals_ms)) == len(args.intervals_ms) and 1 <= args.repetitions <= 20,
            "Choose distinct spacings and 1..20 repetitions.")
    require(not (args.preflight_only and args.restore_from), "Choose preflight or recovery.")
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, firmware_writes=0, intervals_ms=args.intervals_ms,
                              repetitions=args.repetitions, native_tick_ms=TICK_MS, usb_packet_capture=False,
                              one_physical_press_implementation=False)))
        return
    async def experiment(plus, tank, blobs, save):
        return await sweep(plus, tank, blobs, save, tuple(args.intervals_ms), args.repetitions)
    asyncio.run(host.run(args, experiment=experiment))


if __name__ == "__main__":
    main()
