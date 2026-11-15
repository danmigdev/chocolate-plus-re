#!/usr/bin/env python3
"""Compare single, paired and separated HOST SysEx using two BLE configuration links.

The PC connects to TANK first, then the user starts Plus in H. No MIDI is
forwarded by the PC. Native key requests drive the pinned 142 HOST sender.
Only unused C/D assignments, their runtime state and selected TANK live
values can be changed; backups precede writes and restoration is verified.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import time

try:
    from . import tank_ble as ble
except ImportError:
    import tank_ble as ble

relay, require = ble.tank, ble.require
usb, codec = relay.plus, relay.codec
C, D = relay.C_OFFSET, relay.C_OFFSET + 417
CONTROLLED = ((0, 2), (8, 1), (12, 1), (70, 2))
HOST_SENDER = 0x1E119AE
HOST_TX = 0x13280  # 0x1e11932 selects this 64-byte OUT buffer for HOST port 1.


def chunks(address, data, size):
    return [(address + i, data[i:i + size]) for i in range(0, len(data), size)]


class Channel:
    def __init__(self, client, plus=False, record=lambda *args: None):
        self.client, self.plus, self.record = client, plus, record
        self.identity = usb.PROFILES["142"]["identity"] if plus else ble.IDENTITY
        self.base = usb.RAM_BASE if plus else 0
        self.ranges = ((4, usb.RAM_BASE, usb.CONFIG_BYTES), (4, usb.APP_BASE, usb.PROFILES["142"]["app_size"]),
                       (4, usb.USR_VIEW, usb.FLASH_BYTES), (4, usb.STATE, 260), (4, usb.KEYS, 8),
                       (4, 0x19100, 4), (4, HOST_TX, 64)) if plus else ble.RANGES
        self.windows = ((usb.RAM_BASE + C, 834), (usb.RAM_BASE + 23640, 12), (usb.FLIPS, 4), (usb.KEYS, 8)) if plus else ((relay.LIVE, relay.LIVE_SIZE),)
        self.frames, self.queue = ble.Frames(), asyncio.Queue()
        self.verified = self.pinned = False
        self.allow = set()
        self.writes = 0
        characteristic = client.services.get_characteristic(ble.WRITE)
        self.write_size = min(128, characteristic.max_write_without_response_size - 15)
        require(self.write_size >= 2, "BLE write size is too small for a native key request.")

    def notified(self, _, data):
        self.record("notification", bytes(data))
        for frame in self.frames.feed(data):
            self.queue.put_nowait(frame)

    def guard(self, command, body):
        if command == 0x11:
            require(not body, "Identity must have no payload.")
            return
        require(self.verified and command in (0x22, 0x23) and len(body) >= 8, "Unverified or unsupported command.")
        kind, address, count = body[0], (self.base + int.from_bytes(body[1:5], "little")) & 0xFFFFFFFF, int.from_bytes(body[5:8], "little")
        if command == 0x23:
            require(len(body) == 8 and 0 < count <= 128 and any(kind == k and base <= address < address + count <= base + size
                    for k, base, size in self.ranges), "Read outside audited memory.")
        else:
            data = body[8:]
            require(self.pinned and kind == 4 and count == len(data) and 0 < count <= self.write_size
                    and any(base <= address < address + count <= base + size for base, size in self.windows)
                    and (address, data) in self.allow, "Write outside the exact bounded session scope.")

    async def exchange(self, command, body=b""):
        self.guard(command, body)
        packet = b"\0Y" + bytes([command]) + len(body).to_bytes(3, "little") + body + bytes([~sum(body) & 255])
        while not self.queue.empty():
            self.queue.get_nowait()
        self.record("request_attempt", packet)
        if command == 0x22:
            self.writes += 1  # Includes a submitted write whose ACK is lost.
        await self.client.write_gatt_char(ble.WRITE, packet, response=False)
        self.record("request_submitted", packet)
        wanted, prefix = (0, b"") if command == 0x22 else (command, body)
        deadline = time.monotonic() + 6
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "BLE response timed out; writes are never retried.")
            op, payload = await asyncio.wait_for(self.queue.get(), remaining)
            if op == wanted and payload.startswith(prefix):
                return payload[len(prefix):]

    async def identify(self):
        identity = (await self.exchange(0x11)).split(b"\0", 1)[0].decode("ascii")
        require(identity == self.identity, f"Unexpected identity: {identity!r}.")
        self.verified = True
        self.pinned = not self.plus
        return identity

    def descriptor(self, address, size, kind=4):
        return bytes([kind]) + ((address - self.base) & 0xFFFFFFFF).to_bytes(4, "little") + size.to_bytes(3, "little")

    async def read(self, address, size, kind=4):
        require(size > 0 and any(kind == k and base <= address < address + size <= base + count
                                for k, base, count in self.ranges), "Read outside audited memory.")
        result = bytearray()
        while len(result) < size:
            count = min(128, size - len(result))
            data = await self.exchange(0x23, self.descriptor(address + len(result), count, kind))
            require(len(data) == count, "Truncated BLE memory reply.")
            result.extend(data)
        return bytes(result)

    def arm(self, writes):
        self.allow = {part for address, data in writes for part in chunks(address, data, self.write_size)}

    async def write(self, address, data):
        parts = chunks(address, data, self.write_size)
        require(parts, "Empty write.")
        for start, block in parts:
            self.guard(0x22, self.descriptor(start, len(block)) + block)
        for start, block in parts:
            require(await self.exchange(0x22, self.descriptor(start, len(block)) + block) == b"\0", "Write not acknowledged.")

    async def verify_app(self):
        require(self.plus and self.verified, "Only an identified Plus can verify 142.")
        app = await self.read(usb.APP_BASE, usb.PROFILES["142"]["app_size"])
        digest = codec.digest(app)
        require(digest in usb.PROFILES["142"]["hashes"], "Complete running application differs from pinned 142.")
        require(await self.read(0x19100, 4) == b"\0\x90\x03\0", "Unexpected save pointer.")
        self.pinned = True
        return digest

    async def tank_snapshot(self):
        require(not self.plus, "Expected TANK channel.")
        return dict(state=await self.read(relay.TANK_STATE, 4), live=await self.read(relay.LIVE, relay.LIVE_SIZE),
                    ram=await self.read(0, relay.BANK_SIZE), flash=await self.read(relay.FLASH_BANK, relay.BANK_SIZE, 5))


def native_state(data):
    require(len(data) == 260, "Incomplete native HOST state.")
    index = data[4]
    # 0x1e0dacc..0x1e0daee scans exactly one slot (loop index <= 0).
    # Words at +72/+74 are not additional client handles in pinned 142.
    require(index == 0, "Unknown native Bluetooth client index in pinned 142.")
    return dict(host_port=data[228], output_callback=hex(int.from_bytes(data[256:260], "little")),
                client_index=index, client_stage=data[5], client_handle=int.from_bytes(data[70:72], "little"),
                server_handle=int.from_bytes(data[68:70], "little"),
                server_enabled=data[164], client_enabled=data[168])


async def host_ready(plus):
    state = native_state(await plus.read(usb.STATE, 260))
    require(state["host_port"] == 1 and state["output_callback"] == hex(HOST_SENDER), "Native HOST output is not active.")
    require(not state["client_handle"], "Plus has a native Bluetooth client connection; HOST route is not isolated.")
    require(await plus.read(usb.KEYS, 8) == bytes(8), "Native key scanner is busy.")
    return state


def assignment(frames):
    data = b"".join(frames)
    require(0 < len(data) <= 127 and usb.split_sysex(data) == frames, "Invalid test SysEx bank.")
    result = bytearray(417)
    result[0] = 1
    for bank in (0, 1):
        row, area = 1 + 80 * bank, 161 + 128 * bank
        result[row:row + 5] = b"\1\0\4\0\0"
        result[area] = len(data)
        result[area + 1:area + 1 + len(data)] = data
    return bytes(result)


def cases():
    fx, mix = relay.live_frame(8, b"\0"), relay.live_frame(70, b"\x49\0")
    return [dict(name="single", C=assignment([mix]), D=bytes(417), keys=[2], targets=[(1, 73)]),
            dict(name="paired", C=assignment([fx, mix]), D=bytes(417), keys=[2], targets=[(0, 73)]),
            dict(name="separated", C=assignment([fx]), D=assignment([mix]), keys=[2, 3], targets=[(0, 47), (0, 73)])]


async def stable_live(tank):
    first = await tank.read(relay.LIVE, relay.LIVE_SIZE)
    await asyncio.sleep(.1)
    second = await tank.read(relay.LIVE, relay.LIVE_SIZE)
    require(first == second, "TANK live patch did not settle.")
    return second


async def wait_saved(plus, expected):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        actual = await plus.read(usb.USR_VIEW, usb.FLASH_BYTES)
        if actual == expected and await plus.read(usb.STATE + 82, 2) == bytes(2):
            require(await plus.read(usb.USR_VIEW, usb.FLASH_BYTES) == expected, "Saved Plus configuration is unstable.")
            return
        await asyncio.sleep(.25)
    raise ValueError("Saved Plus configuration did not converge.")


async def restore(plus, tank, blobs):
    errors, results = [], {}
    try:
        tank.arm([(relay.LIVE + i, blobs["tank-live"][i:i + size]) for i, size in CONTROLLED])
        for i, size in CONTROLLED:
            await tank.write(relay.LIVE + i, blobs["tank-live"][i:i + size])
        require(await tank.tank_snapshot() == {key: blobs["tank-" + key] for key in ("state", "live", "ram", "flash")},
                "Full TANK restoration does not match backup.")
        results["tank_restored"] = True
    except BaseException as error:
        errors.append(f"TANK: {type(error).__name__}: {error}")
    try:
        writes = [(usb.KEYS + 2 * i, bytes(2)) for i in range(4)] + [
            (usb.RAM_BASE + C, blobs["plus-original"][C:D + 417]),
            (usb.FLIPS, blobs["plus-flips"]), (usb.RAM_BASE + 23640, blobs["plus-original"][23640:])]
        plus.arm(writes)
        for address, data in writes:
            await plus.write(address, data)
        require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == blobs["plus-original"], "Plus RAM restoration differs.")
        await wait_saved(plus, blobs["plus-saved"])
        require(await plus.read(usb.FLIPS, 4) == blobs["plus-flips"] and await plus.read(usb.KEYS, 8) == bytes(8),
                "Plus runtime key state was not restored.")
        results["plus_restored"] = True
    except BaseException as error:
        errors.append(f"Plus: {type(error).__name__}: {error}")
    return dict(**results, restore_errors=errors)


async def matrix(plus, tank, blobs, save):
    original, flash, live = blobs["plus-original"], blobs["plus-saved"], blobs["tank-live"]
    require(original[C:D + 417] == bytes(834) and original[0] == 3 and original[23639:23641] == bytes(2),
            "Tests require unused C/D, advanced mode and only page 1.")
    staged = bytearray(live)
    staged[:2], staged[8], staged[12], staged[70:72] = bytes(2), 1, 0, b"\x2f\0"
    staged = bytes(staged)
    tank.arm([(relay.LIVE + i, staged[i:i + size]) for i, size in CONTROLLED])
    checks, dirty, failure = [], False, None
    try:
        for case in cases():
            before_state = await host_ready(plus)
            config = original[:C] + case["C"] + case["D"] + original[D + 417:]
            plus.arm([(usb.RAM_BASE + C, config[C:D + 417]),
                      *[(usb.KEYS + 2 * key, b"\0\x64") for key in case["keys"]],
                      (usb.RAM_BASE + 23640, original[23640:])])
            dirty = True
            save(status="staging", case=case["name"])
            for i, size in CONTROLLED:
                await tank.write(relay.LIVE + i, staged[i:i + size])
            require(await stable_live(tank) == staged, "TANK staging differs from target.")
            await plus.write(usb.RAM_BASE + C, config[C:D + 417])
            await plus.write(usb.RAM_BASE + 23640, original[23640:])
            require(await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES) == config, "Plus staged configuration differs.")
            await wait_saved(plus, config + flash[usb.CONFIG_BYTES:])
            check = dict(case=case["name"], native_before=before_state, key_checks=[])
            checks.append(check)
            save(status="testing", checks=checks)
            for key, (fx, mix) in zip(case["keys"], case["targets"]):
                await host_ready(plus)
                await asyncio.sleep(.25)
                pressed_utc = ble.stamp()
                await plus.write(usb.KEYS + 2 * key, b"\0\x64")
                # No TANK BLE requests during HOST delivery: avoid changing
                # its configuration-reply route while it consumes the SysEx.
                await asyncio.sleep(.8)
                after = await stable_live(tank)
                expected = bytearray(staged)
                expected[8], expected[70:72] = fx, mix.to_bytes(2, "little")
                native_after = await host_ready(plus)
                check["key_checks"].append(dict(key="ABCD"[key], requested_utc=pressed_utc,
                    expected=bytes(expected).hex(), actual=after.hex(), passed=after == expected,
                    values=ble.values(after, blobs["tank-state"]), native_after=native_after))
                save(status="measured", checks=checks)
                require(all(a == b for i, (a, b) in enumerate(zip(after, staged)) if i not in (8, 70, 71)),
                        "Unexpected TANK parameter changed.")
            require(await tank.read(relay.TANK_STATE, 4) == blobs["tank-state"], "TANK preset state changed.")
            check["passed"] = all(step["passed"] for step in check["key_checks"])
        save(tests_passed=all(check["passed"] for check in checks), checks=checks)
    except BaseException as error:
        failure = error
        save(error=f"{type(error).__name__}: {error}")
    finally:
        if dirty:
            save(status="restoring")
            restored = await restore(plus, tank, blobs)
            save(**restored, plus_write_attempts=plus.writes, tank_write_attempts=tank.writes)
            require(not restored["restore_errors"], "Restoration incomplete; retain the recovery files.")
    if failure:
        raise failure
    return checks


def recovery(folder):
    manifest = json.loads((folder / "recovery.json").read_text())
    blobs = {name: (folder / f"{name}.bin").read_bytes() for name in manifest["sha256"]}
    require(all(codec.digest(data) == manifest["sha256"][name] for name, data in blobs.items()), "Recovery hash mismatch.")
    require(set(blobs) == {"plus-original", "plus-saved", "plus-flips", "tank-live", "tank-state", "tank-ram", "tank-flash"}, "Invalid recovery fields.")
    require(len(blobs["plus-original"]) == usb.CONFIG_BYTES and len(blobs["plus-saved"]) == usb.FLASH_BYTES
            and blobs["plus-saved"][:usb.CONFIG_BYTES] == blobs["plus-original"] and len(blobs["plus-flips"]) == 4
            and len(blobs["tank-live"]) == relay.LIVE_SIZE and len(blobs["tank-state"]) == 4
            and len(blobs["tank-ram"]) == len(blobs["tank-flash"]) == relay.BANK_SIZE, "Invalid recovery geometry.")
    return blobs


def verify_pairing_evidence(report, address):
    peer = report.get("peers", {}).get("FootCtrlPlus", {})
    require(peer.get("identity") == usb.PROFILES["142"]["identity"]
            and peer.get("address", "").casefold() == address.casefold(),
            "Plus pairing requires a previous exact identity at this discovered address.")


async def paired_plus_address(evidence_path):
    """Windows may keep the paired HID connection open, suppressing advertisements."""
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation
    report = json.loads(evidence_path.read_text())
    address = report.get("peers", {}).get("FootCtrlPlus", {}).get("address", "")
    verify_pairing_evidence(report, address)
    device = await BluetoothLEDevice.from_bluetooth_address_async(int(address.replace(":", ""), 16))
    if device is None:
        return None
    try:
        info = await DeviceInformation.create_from_id_async(device.device_information.id)
        return address if device.name == "FootCtrlPlus" and info.pairing.is_paired else None
    finally:
        device.close()


def use_paired_cache(client, address, version):
    # Bleak 3.0.2 otherwise scans even for a Windows-connected paired address.
    # This pinned WinRT hook supplies the same numeric cache key that its
    # scanner supplies. Identity and the complete firmware are still re-read.
    require(version == "3.0.2" and type(client._backend).__name__ == "BleakClientWinRT"
            and hasattr(client._backend, "_device_info"), "Paired Windows cache fallback requires Bleak 3.0.2 WinRT.")
    require(len(address.split(":")) == 6 and all(len(part) == 2 for part in address.split(":")), "Invalid cached BLE address.")
    client._backend._device_info = int(address.replace(":", ""), 16)


async def run(args, experiment=matrix, channel_factory=Channel):
    from bleak import BleakClient, BleakScanner
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(created_utc=ble.stamp(), firmware_writes=0, topology="Plus HOST -> TANK; two PC BLE configuration links",
                  physical_keypresses=False, usb_packet_capture=False, tank_restored=False, plus_restored=False,
                  peers={})
    def save(**fields):
        report.update(fields)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in fields.items() if key != "checks"}), flush=True)
    with (args.output / "ble-configuration.jsonl").open("x", encoding="utf-8") as log:
        def record(peer, direction, data):
            log.write(json.dumps(dict(utc=ble.stamp(), peer=peer, direction=direction, data=data.hex(" "))) + "\n")
            log.flush()
        async with AsyncExitStack() as stack:
            try:
                if args.adapter_fragment:
                    from winrt.windows.devices.bluetooth import BluetoothAdapter
                    adapter = await BluetoothAdapter.get_default_async()
                    require(adapter is not None and args.adapter_fragment.upper() in adapter.device_id.upper(), "Unexpected Windows Bluetooth adapter.")
                channels = []
                for is_plus, name in ((False, ble.NAME), (True, "FootCtrlPlus")):
                    save(status="waiting_for_plus_H" if is_plus else "connecting_tank_first")
                    cached = await paired_plus_address(args.pair_plus_from) if is_plus and args.pair_plus_from else None
                    device = cached or await BleakScanner.find_device_by_filter(lambda d, a: (a.local_name or d.name or "") == name,
                                                                              timeout=120 if is_plus else 30)
                    require(device is not None, f"{name} was not discovered.")
                    address = cached or device.address
                    report["peers"][name] = dict(address=address, pairing_requested=False,
                                                discovery="paired_windows_cache" if cached else "advertisement")
                    if is_plus and args.pair_plus_from and not cached:
                        evidence = json.loads(args.pair_plus_from.read_text())
                        verify_pairing_evidence(evidence, address)
                        report["peers"][name].update(pairing_requested=True, pairing_before_GATT=True,
                                                    previous_identity_evidence=str(args.pair_plus_from))
                        save(status="pairing_verified_plus_before_GATT", peers=report["peers"])
                        report["peers"][name]["pairing_result"] = await ble._pair_windows_address(address)
                        save(peers=report["peers"])
                    client = BleakClient(device, timeout=35, services=[ble.SERVICE], winrt={"use_cached_services": False})
                    if cached:
                        from importlib.metadata import version
                        use_paired_cache(client, cached, version("bleak"))
                    client = await stack.enter_async_context(client)
                    channel = channel_factory(client, is_plus, lambda direction, data, peer=name: record(peer, direction, data))
                    await client.start_notify(ble.NOTIFY, channel.notified)
                    report["peers"][name]["identity"] = await channel.identify()
                    save(peers=report["peers"])
                    # TANK requested authentication and dropped unpaired links
                    # in measurement. Plus AE40 access does not establish that
                    # it needs pairing; verify its sustained reads directly.
                    if args.pair and not is_plus:
                        report["peers"][name]["pairing_requested"] = True
                        report["peers"][name]["pairing_result"] = await ble.pair_windows(channel)
                        save(peers=report["peers"])
                    channels.append(channel)
                    if not is_plus:
                        before = await channel.tank_snapshot()
                        require(before == await channel.tank_snapshot(), "TANK baseline is unstable.")
                tank, plus = channels
                save(status="verifying_complete_application")
                save(app_sha256=await plus.verify_app())
                save(native=await host_ready(plus))
                if args.restore_from:
                    restored = await restore(plus, tank, recovery(args.restore_from))
                    save(**restored)
                    require(not restored["restore_errors"], "Recovery incomplete.")
                else:
                    save(status="capturing_recovery_baseline")
                    original = await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES)
                    flash = await plus.read(usb.USR_VIEW, usb.FLASH_BYTES)
                    flips = await plus.read(usb.FLIPS, 4)
                    require(original == flash[:usb.CONFIG_BYTES] and original == await plus.read(usb.RAM_BASE, usb.CONFIG_BYTES)
                            and flash == await plus.read(usb.USR_VIEW, usb.FLASH_BYTES) and before == await tank.tank_snapshot(),
                            "Unstable or unsaved baseline; no writes performed.")
                    blobs = {"plus-original": original, "plus-saved": flash, "plus-flips": flips,
                             **{"tank-" + key: data for key, data in before.items()}}
                    for name, data in blobs.items():
                        usb.durable_write(args.output / f"{name}.bin", data)
                    usb.durable_write(args.output / "recovery.json", (json.dumps(dict(sha256={name: codec.digest(data) for name, data in blobs.items()}), indent=2) + "\n").encode())
                    blobs = recovery(args.output)
                    if args.preflight_only:
                        save(status="preflight_complete_no_writes")
                        return report
                    await experiment(plus, tank, blobs, save)
                save(status="complete")
            except BaseException as error:
                save(status="stopped", error=f"{type(error).__name__}: {error}")
                raise
    return report


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adapter-fragment")
    parser.add_argument("--pair", action="store_true", help="Pair the verified TANK with Windows if needed; leave Plus pairing unchanged.")
    parser.add_argument("--pair-plus-from", type=Path, help="Previous report verifying the exact Plus identity/address; pair before GATT access.")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--restore-from", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def main():
    args = argument_parser().parse_args()
    require(not (args.preflight_only and args.restore_from), "Choose preflight or recovery.")
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, firmware_writes=0, cases=[case["name"] for case in cases()],
                              native_key_requests=True, recovery=True, usb_packet_capture=False)))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
