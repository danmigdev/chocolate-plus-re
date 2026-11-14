#!/usr/bin/env python3
"""Read Chocolate Plus configuration over USB, or compare saved snapshots.

The device commands are identity (0x11) and configuration read (0x23) only.
No configuration, firmware, bootloader or arbitrary-memory writes are exposed.
MIDI dependencies are loaded only for list/capture; compare uses the stdlib.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

CONFIG_SIZE = 23646
READ_CHUNK = 512
IDENTITIES = ("FootCtrlPlus_012", "FootCtrlPlus_013", "FootCtrlPlus_142-dtnprt")


class SnapshotError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode7(raw):
    result = bytearray()
    accumulator = bits = 0
    for byte in raw:
        accumulator |= byte << bits
        bits += 8
        while bits >= 7:
            result.append(accumulator & 127)
            accumulator >>= 7
            bits -= 7
    if bits:
        result.append(accumulator & 127)
    return bytes(result)


def decode7(packed):
    accumulator = bits = 0
    result = bytearray()
    for byte in packed:
        if not 0 <= byte < 128:
            raise SnapshotError("Non-data MIDI byte inside SysEx.")
        accumulator |= byte << bits
        bits += 7
        while bits >= 8:
            result.append(accumulator & 255)
            accumulator >>= 8
            bits -= 8
    if accumulator:
        raise SnapshotError("Nonzero SysEx padding.")
    return bytes(result)


def request(command, payload=b""):
    if command not in (0x11, 0x23):
        raise SnapshotError("Only identity and bounded configuration reads are allowed.")
    if command == 0x11 and payload:
        raise SnapshotError("Identity has no payload.")
    if command == 0x23:
        if len(payload) != 8 or payload[0] != 4:
            raise SnapshotError("Expected a memory-type-4 configuration read.")
        offset = int.from_bytes(payload[1:5], "little")
        length = int.from_bytes(payload[5:8], "little")
        if not (0 <= offset < offset + length <= CONFIG_SIZE and length <= READ_CHUNK):
            raise SnapshotError("Read outside the public configuration or chunk limit.")
    raw = b"\x00\x59" + bytes([command]) + len(payload).to_bytes(3, "little")
    raw += payload + bytes([~sum(payload) & 255])
    return encode7(raw)


def reply(packed):
    raw = decode7(packed)
    if len(raw) < 7 or raw[:2] != b"\x00\x59":
        raise SnapshotError("Not an M-VAVE configuration reply.")
    length = int.from_bytes(raw[3:6], "little")
    # The vendor may append one redundant zero group when packing 7 raw bytes.
    if len(raw) == length + 8 and raw[-1] == 0:
        raw = raw[:-1]
    if len(raw) != length + 7:
        raise SnapshotError("Reply length differs from its header.")
    payload = raw[6:-1]
    if raw[-1] != ~sum(payload) & 255:
        raise SnapshotError("Reply checksum mismatch.")
    return raw[2], payload


def sysex_location(offset):
    relative = offset - 93
    if not 0 <= relative < 32 * 417:
        return None
    switch, position = divmod(relative, 417)
    if position < 161:
        return None
    bank, byte = divmod(position - 161, 128)
    return dict(page=switch // 4 + 1, switch="ABCD"[switch % 4],
                bank="AB"[bank], byte=byte)


def compare(before, after):
    if len(before) != CONFIG_SIZE or len(after) != CONFIG_SIZE:
        raise SnapshotError(f"Both snapshots must contain exactly {CONFIG_SIZE} bytes.")
    changes = []
    for offset, (old, new) in enumerate(zip(before, after)):
        if old != new:
            changes.append(dict(offset=offset, written=f"{old:02X}", read=f"{new:02X}",
                                cleared_bits=(old & ~new).bit_count(),
                                set_bits=(new & ~old).bit_count(),
                                sysex=sysex_location(offset)))
    ff = [item for item in changes if item["sysex"] is not None and item["written"] == "FF"]
    return dict(before_sha256=digest(before), after_sha256=digest(after),
                identical=not changes, changed_bytes=len(changes),
                cleared_bits=sum(item["cleared_bits"] for item in changes),
                set_bits=sum(item["set_bits"] for item in changes),
                changed_outside_sysex=sum(item["sysex"] is None for item in changes),
                ff_sysex_bytes=sum(byte == 255 and sysex_location(i) is not None
                                   for i, byte in enumerate(before)),
                changed_ff_sysex_bytes=len(ff),
                cleared_ff_sysex_bits=sum(item["cleared_bits"] for item in ff),
                changes=changes)


def port_key(name):
    return re.sub(r"\s+\d+$", "", name).casefold()


def select_ports(inputs, outputs, input_name=None, output_name=None):
    if input_name is not None or output_name is not None:
        if input_name not in inputs or output_name not in outputs:
            raise SnapshotError("Supply both exact port names shown by the list command.")
        candidates = [(input_name, output_name)]
    else:
        candidates = [(i, o) for i in inputs for o in outputs
                      if ("footctrlplus" in i.casefold() or "sinco" in i.casefold())
                      and port_key(i) == port_key(o)]
    if len(candidates) != 1:
        raise SnapshotError("Expected one controller port pair; use list and explicit --input/--output.")
    i, o = candidates[0]
    if port_key(i) != port_key(o) or not any(name in i.casefold()
                                            for name in ("footctrlplus", "sinco")):
        raise SnapshotError("Choose matching FootCtrlPlus or SINCO input/output ports.")
    return i, o


def exchange(midi, incoming, outgoing, command, payload=b"", timeout=3.0):
    outgoing.send(midi.Message("sysex", data=request(command, payload)))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = incoming.poll()
        if message is None:
            time.sleep(0.005)
            continue
        if message.type != "sysex":
            continue
        kind, body = reply(bytes(message.data))
        if kind != command:
            raise SnapshotError(f"Unexpected reply {kind:#x}; close other MIDI editors.")
        if command == 0x23:
            length = int.from_bytes(payload[5:8], "little")
            if body[:8] != payload or len(body) != 8 + length:
                raise SnapshotError("Read reply address/type/length does not match the request.")
            return body[8:]
        return body
    raise SnapshotError(f"Timeout waiting for command {command:#x}.")


def capture(midi, expected, input_name=None, output_name=None):
    if expected not in IDENTITIES:
        raise SnapshotError("Unsupported expected firmware identity.")
    i, o = select_ports(midi.get_input_names(), midi.get_output_names(), input_name, output_name)
    with ExitStack() as stack:
        incoming = stack.enter_context(midi.open_input(i))
        outgoing = stack.enter_context(midi.open_output(o))
        identity = exchange(midi, incoming, outgoing, 0x11)
        identity = identity.split(b"\0", 1)[0].decode("ascii")
        if identity != expected:
            raise SnapshotError(f"Device reports {identity!r}; expected {expected!r}. No configuration read.")
        copies = []
        for _ in range(2):
            data = bytearray()
            for offset in range(0, CONFIG_SIZE, READ_CHUNK):
                count = min(READ_CHUNK, CONFIG_SIZE - offset)
                prefix = b"\x04" + offset.to_bytes(4, "little") + count.to_bytes(3, "little")
                data.extend(exchange(midi, incoming, outgoing, 0x23, prefix))
            copies.append(bytes(data))
    return copies, dict(identity=identity, input_port=i, output_port=o, device_writes=0,
                       captured_utc=datetime.now(timezone.utc).isoformat(),
                       configuration_bytes=CONFIG_SIZE, stable=copies[0] == copies[1],
                       sha256=[digest(data) for data in copies],
                       evidence="RAM configuration only; not a physical flash backup")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List MIDI ports without sending messages.")
    reader = commands.add_parser("capture", help="Read the configuration twice; never write to the device.")
    reader.add_argument("directory", type=Path, help="New local output directory.")
    reader.add_argument("--expect", choices=IDENTITIES, default="FootCtrlPlus_142-dtnprt")
    reader.add_argument("--input")
    reader.add_argument("--output")
    comparer = commands.add_parser("compare", help="Compare two existing binary snapshots offline.")
    comparer.add_argument("before", type=Path)
    comparer.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            result = compare(args.before.read_bytes(), args.after.read_bytes())
            print(json.dumps(result, indent=2))
            return 0 if result["identical"] else 1
        import mido
        if args.command == "list":
            print(json.dumps(dict(inputs=mido.get_input_names(), outputs=mido.get_output_names()), indent=2))
            return 0
        if args.directory.exists():
            raise SnapshotError("Output directory already exists; choose a new one to preserve captures.")
        copies, report = capture(mido, args.expect, args.input, args.output)
        args.directory.mkdir(parents=True, exist_ok=False)
        for index, data in enumerate(copies, 1):
            (args.directory / f"configuration-{index}.bin").write_bytes(data)
        (args.directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0 if report["stable"] else 1
    except (SnapshotError, OSError, UnicodeError, ImportError, RuntimeError) as error:
        parser.exit(2, f"ERROR: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
