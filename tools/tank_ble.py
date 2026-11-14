#!/usr/bin/env python3
"""Read TANK MINI_009 over its BLE configuration service while USB remains on HOST.

Only identity and bounded memory reads can be transmitted. Notifications are
configuration replies, not a USB bus capture. Optional polling records live
parameter changes without changing presets or firmware. Windows pairing is
optional and happens only after verifying the exact application identity.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

try:
    from . import tank_relay_142 as tank
except ImportError:
    import tank_relay_142 as tank

require = tank.require
SERVICE = "0000ae40-0000-1000-8000-00805f9b34fb"
WRITE = "0000ae41-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000ae42-0000-1000-8000-00805f9b34fb"
NAME, IDENTITY = "TANK MINI_BLE", "TANK MINI_009"
RANGES = ((4, tank.LIVE, tank.LIVE_SIZE), (4, tank.TANK_STATE, 4),
          (4, 0, tank.BANK_SIZE), (5, tank.FLASH_BANK, tank.BANK_SIZE))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def read_prefix(address, count, kind=4):
    require(0 < count <= 128 and any(kind == k and base <= address < address + count <= base + size
                                     for k, base, size in RANGES), "Read outside audited TANK ranges.")
    return bytes([kind]) + address.to_bytes(4, "little") + count.to_bytes(3, "little")


def request_frame(command, body=b""):
    if command == 0x11:
        require(not body, "Identity request must be empty.")
    else:
        require(command == 0x23 and len(body) == 8, "Only identity and bounded reads are implemented.")
        require(read_prefix(int.from_bytes(body[1:5], "little"), int.from_bytes(body[5:], "little"), body[0]) == body,
                "Invalid read descriptor.")
    return b"\0Y" + bytes([command]) + len(body).to_bytes(3, "little") + body + bytes([~sum(body) & 255])


class Frames:
    """Reassemble checksummed native replies across arbitrary GATT notifications."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 2:
            start = self.buffer.find(b"\0Y")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                break
            del self.buffer[:start]
            if len(self.buffer) < 6:
                break
            size = int.from_bytes(self.buffer[3:6], "little")
            if size > 512:
                del self.buffer[:2]
                continue
            if len(self.buffer) < size + 7:
                break
            frame = bytes(self.buffer[:size + 7])
            del self.buffer[:size + 7]
            body = frame[6:-1]
            if frame[-1] == (~sum(body) & 255):
                frames.append((frame[2], body))
        return frames


class Reader:
    def __init__(self, client, record=lambda *args: None):
        self.client, self.record = client, record
        self.frames, self.queue = Frames(), asyncio.Queue()
        self.verified = False
        self.requests = 0
        self.attempts = 0

    def notified(self, _, payload):
        self.record("notification", bytes(payload))
        for frame in self.frames.feed(payload):
            self.queue.put_nowait(frame)

    async def exchange(self, command, prefix=b""):
        packet = request_frame(command, prefix)
        require(command == 0x11 or self.verified, "Verify the exact TANK identity before any memory read.")
        while not self.queue.empty():
            self.queue.get_nowait()
        self.record("request_attempt", packet)
        self.attempts += 1
        await self.client.write_gatt_char(WRITE, packet, response=False)
        self.requests += 1
        self.record("request_submitted", packet)
        deadline = time.monotonic() + 6
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "TANK BLE response timeout.")
            opcode, data = await asyncio.wait_for(self.queue.get(), remaining)
            if opcode == command and data.startswith(prefix):
                return data[len(prefix):]

    async def identify(self):
        identity = (await self.exchange(0x11)).split(b"\0", 1)[0].decode("ascii")
        require(identity == IDENTITY, f"Unsupported BLE identity: {identity!r}.")
        self.verified = True
        return identity

    async def read(self, address, count, kind=4):
        require(count > 0 and any(kind == k and base <= address < address + count <= base + size
                                 for k, base, size in RANGES), "Read outside audited TANK ranges.")
        result = bytearray()
        while len(result) < count:
            size = min(128, count - len(result))
            prefix = read_prefix(address + len(result), size, kind)
            data = await self.exchange(0x23, prefix)
            require(len(data) == size, "TANK BLE response length differs from the request.")
            result.extend(data)
        return bytes(result)

    async def snapshot(self):
        return dict(state=await self.read(tank.TANK_STATE, 4), live=await self.read(tank.LIVE, tank.LIVE_SIZE),
                    ram=await self.read(0, tank.BANK_SIZE), flash=await self.read(tank.FLASH_BANK, tank.BANK_SIZE, 5))


def values(live, state):
    require(len(live) == tank.LIVE_SIZE and len(state) == 4, "Invalid TANK snapshot geometry.")
    return dict(preset=state[0], state=state.hex(), volume=int.from_bytes(live[:2], "little"),
                FX=live[8], MOD=live[10], REV=live[12], REV_mix=int.from_bytes(live[70:72], "little"))


async def pair_windows(reader):
    """Use encrypted Just Works pairing after the reader verified its exact peer."""
    require(reader.verified, "Verify the exact application identity before pairing.")
    return await _pair_windows_address(reader.client.address)


async def _pair_windows_address(address):
    """Internal transport operation; caller must verify which peer this address identifies."""
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation, DevicePairingKinds, DevicePairingProtectionLevel
    device = await BluetoothLEDevice.from_bluetooth_address_async(int(address.replace(":", ""), 16))
    require(device is not None, "Verified device is no longer available for pairing.")
    token = None
    try:
        info = await DeviceInformation.create_from_id_async(device.device_information.id)
        if info.pairing.is_paired:
            return "ALREADY_PAIRED"
        pairing = info.pairing.custom
        def requested(_, args):
            if args.pairing_kind == DevicePairingKinds.CONFIRM_ONLY:
                args.accept()
        token = pairing.add_pairing_requested(requested)
        result = await asyncio.wait_for(pairing.pair_with_protection_level_async(
            DevicePairingKinds.CONFIRM_ONLY, DevicePairingProtectionLevel.ENCRYPTION), 45)
        require(result.status.name in ("PAIRED", "ALREADY_PAIRED"), f"Device pairing failed: {result.status.name}.")
        return result.status.name
    finally:
        if token is not None:
            pairing.remove_pairing_requested(token)
        device.close()


async def run(args):
    from bleak import BleakClient, BleakScanner
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(status="scanning", created_utc=stamp(), configuration_writes=0, firmware_writes=0,
                  pairing_requested=False, pairing_enabled=args.pair, usb_traffic_captured=False, expected_identity=IDENTITY,
                  watch_seconds=args.watch, interval_seconds=args.interval, scan_seconds=args.scan_seconds)
    path = args.output / "report.json"
    def save(**fields):
        report.update(fields)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(fields), flush=True)
    save(status="scanning")
    reader, samples = None, 0
    with (args.output / "ble-configuration.jsonl").open("x", encoding="utf-8") as log:
        def record(direction, data):
            log.write(json.dumps(dict(utc=stamp(), direction=direction, data=data.hex(" "))) + "\n")
            log.flush()
        try:
            if args.adapter_fragment:
                from winrt.windows.devices.bluetooth import BluetoothAdapter
                adapter = await BluetoothAdapter.get_default_async()
                require(adapter is not None and args.adapter_fragment.upper() in adapter.device_id.upper(),
                        "Windows is using a different Bluetooth adapter.")
                report["adapter_id"] = adapter.device_id
            device = await BleakScanner.find_device_by_filter(lambda d, a: (a.local_name or d.name or "") == NAME,
                                                            timeout=args.scan_seconds)
            require(device is not None, "TANK MINI_BLE was not discovered.")
            save(status="connecting")
            async with BleakClient(device, timeout=35, services=[SERVICE], winrt={"use_cached_services": False}) as client:
                require(client.services.get_characteristic(WRITE) is not None and client.services.get_characteristic(NOTIFY) is not None,
                        "TANK configuration characteristics are unavailable.")
                reader = Reader(client, record)
                await client.start_notify(NOTIFY, reader.notified)
                save(status="identifying")
                identity = await reader.identify()
                if args.pair:
                    save(status="pairing", identity=identity, pairing_requested=True)
                    result = await pair_windows(reader)
                    save(status="paired", pairing_completed=True, pairing_result=result)
                save(status="reading_baseline", identity=identity)
                baseline = await reader.snapshot()
                require(baseline == await reader.snapshot(), "TANK baseline is unstable.")
                for name, data in baseline.items():
                    (args.output / f"baseline-{name}.bin").write_bytes(data)
                save(status="watching" if args.watch else "baseline_verified", baseline=values(baseline["live"], baseline["state"]),
                     baseline_sha256={name: tank.codec.digest(data) for name, data in baseline.items()},
                     stop_file=str(args.output / "stop"))
                started, samples = time.monotonic(), 0
                previous = (baseline["live"], baseline["state"])
                with (args.output / "live-changes.jsonl").open("x", encoding="utf-8") as changes:
                    while time.monotonic() - started < args.watch and not (args.output / "stop").exists():
                        await asyncio.sleep(args.interval)
                        live = await reader.read(tank.LIVE, tank.LIVE_SIZE)
                        state = await reader.read(tank.TANK_STATE, 4)
                        samples += 1
                        if (live, state) != previous:
                            event = dict(utc=stamp(), sample=samples, live=live.hex(), **values(live, state))
                            changes.write(json.dumps(event) + "\n"); changes.flush()
                            print(json.dumps(dict(event="live_changed", **values(live, state))), flush=True)
                            previous = live, state
                final = await reader.snapshot()
                for name, data in final.items():
                    (args.output / f"final-{name}.bin").write_bytes(data)
                await client.stop_notify(NOTIFY)
                save(status="complete", requests=reader.requests, request_attempts=reader.attempts, samples=samples,
                     final=values(final["live"], final["state"]),
                     saved_ram_unchanged=final["ram"] == baseline["ram"], saved_flash_unchanged=final["flash"] == baseline["flash"],
                     live_matches_baseline=final["live"] == baseline["live"], state_matches_baseline=final["state"] == baseline["state"])
        except BaseException as error:
            save(status="stopped", error=f"{type(error).__name__}: {error}", samples=samples,
                 request_attempts=reader.attempts if reader else 0, requests=reader.requests if reader else 0)
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch", type=int, default=0)
    parser.add_argument("--interval", type=float, default=.5)
    parser.add_argument("--scan-seconds", type=int, default=15)
    parser.add_argument("--adapter-fragment")
    parser.add_argument("--pair", action="store_true", help="Pair with the verified TANK; preserves the resulting OS association.")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    require(0 <= args.watch <= 600 and .25 <= args.interval <= 10 and 1 <= args.scan_seconds <= 120,
            "Watch/interval/scan duration outside supported bounds.")
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, configuration_writes=0, firmware_writes=0, pairing_enabled=args.pair,
                              purpose="Read TANK state over BLE configuration service; no USB traffic capture")))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
