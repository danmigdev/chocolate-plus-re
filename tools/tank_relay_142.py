#!/usr/bin/env python3
"""Automatically check 142 -> PC USB relay -> TANK MINI_009 by memory readback.

Both devices must connect to the PC independently; Plus in U. Native key
events generate the MIDI that is captured, checked, and relayed unchanged.
This does not test physical switches, the HOST port, or audio output.
Backups precede mutations; both devices are restored even on test failure.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import time

try:
    from . import acceptance_142 as plus
except ImportError:
    import acceptance_142 as plus

codec, require = plus.codec, plus.require
LIVE, LIVE_SIZE = 0x10000000, 92
TANK_STATE, BANK_SIZE, FLASH_BANK = 0x20000000, 21 * 92 + 10, 0x7003C000
C_OFFSET = 93 + 2 * 417


def packet(command, payload=b""):
    raw = b"\0Y" + bytes([command]) + len(payload).to_bytes(3, "little") + payload
    return b"\xf0" + codec.encode7(raw + bytes([~sum(payload) & 255])) + b"\xf7"


def write_frame(address, data):
    return packet(0x22, b"\x04" + address.to_bytes(4, "little") + len(data).to_bytes(3, "little") + data)


def live_frame(offset, data):
    require(data and 0 <= offset < offset + len(data) <= LIVE_SIZE, "Write exceeds live patch.")
    return write_frame(LIVE + offset, data)


def decode_write(frame):
    require(frame[:1] == b"\xf0" and frame[-1:] == b"\xf7", "Expected complete SysEx.")
    command, body = codec.reply(frame[1:-1])
    require(command == 0x22 and len(body) >= 9 and body[0] == 4, "Only live writes or preset reload accepted.")
    address, data = int.from_bytes(body[1:5], "little"), body[8:]
    require(int.from_bytes(body[5:8], "little") == len(data), "Write length mismatch.")
    require(LIVE <= address < address + len(data) <= LIVE + LIVE_SIZE
            or (address == TANK_STATE + 1 and data == b"\xff"), "Saved presets and firmware are read-only.")
    return address, data


def expected_live(before, frames, saved):
    target = bytearray(before)
    for frame in frames:
        address, data = decode_write(frame)
        if address == TANK_STATE + 1:
            require(len(frames) == 1, "Reload must be the only message.")
            target[:] = saved
        else:
            offset = address - LIVE
            # Model changes asynchronously replace parameters. These tests
            # exercise only toggles and parameters of the existing models.
            require(offset + len(data) <= 14 or offset >= 20, "Model changes are not relay test cases.")
            target[offset:offset + len(data)] = data
    return bytes(target)


class Tank:
    def __init__(self, midi):
        self.midi, self.stack = midi, ExitStack()
        self.allowed = set()

    def __enter__(self):
        inputs = [n for n in self.midi.get_input_names() if "usb-midi" in n.lower() or "tank" in n.lower()]
        outputs = self.midi.get_output_names()
        pairs = [(i, o) for i in inputs for o in outputs if codec.port_key(i) == codec.port_key(o)]
        require(len(pairs) == 1, "Connect exactly one TANK to the PC by USB.")
        try:
            self.incoming = self.stack.enter_context(self.midi.open_input(pairs[0][0]))
            self.outgoing = self.stack.enter_context(self.midi.open_output(pairs[0][1]))
            self.transmit(packet(0x11))
            identity = self.wait(0x11).split(b"\0", 1)[0].decode("ascii")
            require(identity == "TANK MINI_009", f"Unsupported TANK identity: {identity!r}.")
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def transmit(self, frame):
        if frame[:1] != b"\xf0":
            require(frame == b"\xb0\0\0" and frame in self.allowed,
                    "Only the explicitly allowed TANK FX toggle CC is supported.")
            self.outgoing.send(self.midi.Message.from_bytes(list(frame)))
            return
        command, body = codec.reply(frame[1:-1])
        if command == 0x22:
            decode_write(frame)
            require(frame in self.allowed, "TANK write is absent from this session's allow-list.")
        elif command == 0x23:
            require(len(body) == 8, "Invalid read descriptor.")
            kind, address, count = body[0], int.from_bytes(body[1:5], "little"), int.from_bytes(body[5:], "little")
            ranges = ((4, LIVE, LIVE_SIZE), (4, TANK_STATE, 4), (4, 0, BANK_SIZE), (5, FLASH_BANK, BANK_SIZE))
            require(0 < count <= 256 and any(kind == k and base <= address < address + count <= base + size
                                           for k, base, size in ranges), "Read outside TANK audited ranges.")
        else:
            require(command == 0x11 and not body, "Unsupported TANK command.")
        self.outgoing.send(self.midi.Message.from_bytes(list(frame)))

    def wait(self, command, prefix=b"", timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.incoming.poll()
            if message is None:
                time.sleep(0.002)
                continue
            if message.type != "sysex":
                continue
            op, payload = codec.reply(bytes(message.data))
            if op == command and payload.startswith(prefix):
                return payload[len(prefix):]
        raise TimeoutError(f"TANK reply {command:#x} missing; no automatic write retry.")

    def read(self, address, count, kind=4):
        result = bytearray()
        while len(result) < count:
            size = min(256, count - len(result))
            prefix = bytes([kind]) + (address + len(result)).to_bytes(4, "little") + size.to_bytes(3, "little")
            self.transmit(packet(0x23, prefix))
            data = self.wait(0x23, prefix)
            require(len(data) == size, "Incomplete TANK read.")
            result.extend(data)
        return bytes(result)

    def live(self):
        return self.read(LIVE, LIVE_SIZE)

    def stable(self, delay=0.08):
        time.sleep(delay)
        current = self.live()
        for _ in range(20):
            time.sleep(0.08)
            again = self.live()
            if again == current:
                return again
            current = again
        raise ValueError("TANK live patch did not settle.")

    def write(self, offset, data):
        self.transmit(live_frame(offset, data))
        require(self.wait(0) == b"\0", "TANK write was not acknowledged.")

    def snapshot(self):
        return dict(state=self.read(TANK_STATE, 4), live=self.live(),
                    ram=self.read(0, BANK_SIZE), flash=self.read(FLASH_BANK, BANK_SIZE, 5))

    def restore(self, original):
        # Reload may change models; restore their defaults before parameters.
        live = self.stable()
        for block in range(6):
            enable, model, area = 8 + block, 14 + block, 20 + 12 * block
            offsets = [enable, model, *range(area, area + 12)]
            if all(live[i] == original[i] for i in offsets):
                continue
            self.write(enable, b"\0")
            if live[model] != original[model]:
                self.write(model, original[model:model + 1])
                self.stable(0.4)
            self.write(area, original[area:area + 12])
            self.write(enable, original[enable:enable + 1])
        for offset, (actual, wanted) in enumerate(zip(self.stable(), original)):
            if actual != wanted:
                self.write(offset, bytes([wanted]))
        require(self.stable() == original, "TANK live patch restoration differs from backup.")


def temporary_c(original):
    require(original[C_OFFSET:C_OFFSET + 417] == bytes(417), "Switch C must be unused.")
    result = bytearray(417)
    result[0] = 1  # Native alternating mode; both banks send one SysEx.
    for bank, value in enumerate((25, 75)):
        row, area = 1 + bank * 80, 161 + bank * 128
        result[row:row + 5] = b"\x01\0\x04\0\0"
        frame = live_frame(70, value.to_bytes(2, "little"))
        result[area] = len(frame)
        result[area + 1:area + 1 + len(frame)] = frame
    return bytes(result)


def run(midi, output):
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="preflight", topology="Plus U -> PC USB relay -> TANK USB",
                  host_tested=False, physical_switches_tested=False, firmware_writes=0,
                  checks=[], tank_restored=False, plus_restored=False)
    def log(status, **fields):
        report.update(status=status, **fields)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(dict(status=status, **fields)), flush=True)
    log("preflight")
    with Tank(midi) as tank, plus.UsbLink(midi) as link:
        start = tank.snapshot()
        require(start == tank.snapshot() and start["state"][0] < 21
                and start["state"][0] == start["state"][1], "TANK baseline is unstable.")
        original, flash = link.read(plus.RAM_BASE, plus.CONFIG_BYTES), link.read(plus.USR_VIEW, plus.FLASH_BYTES)
        flips = link.read(plus.FLIPS, 4)
        require(original == flash[:plus.CONFIG_BYTES] and original == link.read(plus.RAM_BASE, plus.CONFIG_BYTES)
                and flash == link.read(plus.USR_VIEW, plus.FLASH_BYTES), "Plus baseline has unsaved settings.")
        require(original[0] == 3 and original[23639:23641] == b"\0\0"
                and link.read(plus.KEYS, 8) == bytes(8), "Expected page 1 only, advanced mode, switches released.")
        c = temporary_c(original)
        altered = original[:C_OFFSET] + c + original[C_OFFSET + 417:]
        saved = start["ram"][start["state"][0] * 92:(start["state"][0] + 1) * 92]
        frames = [frame for switch in range(3) for bank in range(2)
                  for frame in plus.expected_frames(altered, switch, bank)]
        for frame in frames:
            expected_live(start["live"], [frame], saved)
        # Restore values are frozen from the baseline before the first write.
        tank.allowed = set(frames + [live_frame(i, bytes([b])) for i, b in enumerate(start["live"])])
        for block in range(6):
            area = 20 + 12 * block
            tank.allowed.update((live_frame(8 + block, b"\0"), live_frame(area, start["live"][area:area + 12])))
        tank.allowed.update((live_frame(10, b"\0"), live_frame(10, b"\1"),
                             live_frame(70, b"\x2f\0"), live_frame(70, b"\x3f\0")))
        link.allow = tuple([(plus.KEYS + 2 * switch, b"\0\x64") for switch in range(3)] +
                           [(plus.KEYS, bytes(8)), (plus.FLIPS, flips),
                            (plus.RAM_BASE + 23640, original[23640:]),
                            (plus.RAM_BASE + C_OFFSET, c),
                            (plus.RAM_BASE + C_OFFSET, original[C_OFFSET:C_OFFSET + 417])])
        for name, data in {**{f"tank-{k}": v for k, v in start.items()},
                           "plus-configuration": original, "plus-usrflash": flash, "plus-flips": flips}.items():
            plus.durable_write(output / f"{name}-before.bin", data)
        log("backups_saved", tank_preset=start["state"][0], tank_live_sha256=codec.digest(start["live"]))
        dirty = False
        try:
            dirty = True  # Even a lost acknowledgement may have applied a write.
            link.write(plus.RAM_BASE + C_OFFSET, c)
            require(link.read(plus.RAM_BASE, plus.CONFIG_BYTES) == altered, "Temporary C configuration differs.")
            for switch in range(3):
                for attempt in (1, 2):
                    require(link.read(plus.KEYS, 8) == bytes(8), "Native key scanner is busy.")
                    bank = 1 - link.read(plus.FLIPS, 4)[switch]
                    require(bank in (0, 1), "Invalid bank state.")
                    expected = plus.expected_frames(altered, switch, bank)
                    # Establish different initial values so a no-op cannot pass.
                    if switch == 0:
                        _, data = decode_write(expected[0])
                        tank.write(10, bytes([1 - data[0]]))
                    elif switch == 1 and bank == 0:
                        tank.write(8, b"\0")
                        tank.write(70, b"\x2f\0")
                    elif switch == 1:
                        tank.write(70, b"\x2f\0" if saved[70:72] != b"\x2f\0" else b"\x3f\0")
                    else:
                        tank.write(12, b"\0")  # Test mix values with REV disabled.
                    before = tank.stable()
                    wanted = expected_live(before, expected, saved)
                    require(before != wanted, "The test must change the live patch.")
                    link.collect(0.03)
                    link.write(plus.KEYS + 2 * switch, b"\0\x64")
                    actual = link.collect()
                    require(actual == expected, "Plus emitted unexpected MIDI; nothing relayed.")
                    for frame in actual:
                        tank.transmit(frame)  # No command regeneration or ACK delay between frames.
                    for _ in actual:
                        require(tank.wait(0) == b"\0", "TANK did not acknowledge a relayed SysEx.")
                    after = tank.stable(0.8 if switch == 1 and bank == 1 else 0.08)
                    result = dict(switch="ABC"[switch], bank="AB"[bank], attempt=attempt,
                                  frames=[f.hex(" ") for f in actual], before=before.hex(),
                                  expected=wanted.hex(), after=after.hex(), passed=after == wanted,
                                  changed_offsets=[i for i, (a, b) in enumerate(zip(before, after)) if a != b])
                    report["checks"].append(result)
                    log("relay_checked", last_switch=result["switch"], last_bank=result["bank"], passed=result["passed"])
                    require(result["passed"], "TANK live patch differs from expected result.")
            report["tests_passed"] = True
        except BaseException as error:
            report.update(tests_passed=False, error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if dirty:
                log("restoring_both_devices")
                errors = []
                try:
                    tank.restore(start["live"])
                    end = tank.snapshot()
                    require(end == start, "TANK state or saved presets changed.")
                    report["tank_restored"] = True
                    report["tank_saved_ram_unchanged"] = end["ram"] == start["ram"]
                    report["tank_saved_flash_unchanged"] = end["flash"] == start["flash"]
                except BaseException as error:
                    errors.append(f"TANK: {type(error).__name__}: {error}")
                try:
                    link.write(plus.KEYS, bytes(8))
                    link.write(plus.RAM_BASE + C_OFFSET, original[C_OFFSET:C_OFFSET + 417])
                    link.write(plus.FLIPS, flips)
                    link.write(plus.RAM_BASE + 23640, original[23640:])
                    require(link.read(plus.RAM_BASE, plus.CONFIG_BYTES) == original, "Plus RAM restoration mismatch.")
                    report["plus_restore_save"] = plus.wait_saved(link, flash)
                    require(link.read(plus.FLIPS, 4) == flips and link.read(plus.KEYS, 8) == bytes(8), "Plus key-state mismatch.")
                    report["plus_restored"] = True
                except BaseException as error:
                    errors.append(f"Plus: {type(error).__name__}: {error}")
                report["restore_errors"] = errors
                log("complete" if report.get("tests_passed") and not errors else "stopped")
                require(not errors, "Restoration failed; keep backups: " + "; ".join(errors))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, topology="Plus U -> PC relay -> TANK",
                              checks=["existing A/B banks", "temporary C: REV Mix 25/75"], restore=True)))
        return
    if args.output.exists():
        parser.error("Output already exists; use a new directory to preserve previous evidence.")
    import mido
    try:
        run(mido, args.output)
    except BaseException as error:
        manifest = args.output / "report.json"
        if manifest.is_file():
            report = json.loads(manifest.read_text(encoding="utf-8"))
            if report["status"] == "preflight":
                report["status"] = "preflight_failed_no_writes"
            report.setdefault("error", f"{type(error).__name__}: {error}")
            manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
