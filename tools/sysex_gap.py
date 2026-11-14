#!/usr/bin/env python3
"""Get/set one persistent global HOST SysEx gap on experimental 150 over USB MIDI.

Refuses stock/142 before any extension request. Checks application and module
hashes against the local build. Writes only the fixed 16-byte setting endpoint;
never exposes arbitrary memory writes, firmware installation or automatic retry.
"""
from __future__ import annotations

import argparse
import binascii
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import time

try:
    from . import config_snapshot as codec
    from .build_gap_150 import IDENTITY, PATCH_ADDRESS, APP_BYTES
    from .prepare_142 import durable_write, require
except ImportError:
    import config_snapshot as codec
    from build_gap_150 import IDENTITY, PATCH_ADDRESS, APP_BYTES
    from prepare_142 import durable_write, require

OFFSET, RECORD_BYTES = 0x53580000, 16
DEFAULT_MS, STEP_MS, MAX_MS = 20, 10, 1000
RAM_BASE, APP_BASE, USR_VIEW = 0x13490, 0x1E00120, 0x1E38000
CONFIG_BYTES, FLASH_BYTES = 23652, 0x6000
FOOTER = CONFIG_BYTES + 8
def encode(milliseconds):
    require(type(milliseconds) is int and 0 <= milliseconds <= MAX_MS and milliseconds % STEP_MS == 0,
            "Intervallo SysEx: usare 0..1000 ms, in passi di 10 ms.")
    body = struct.pack("<4sBBHHHH", b"SGAP", 1, STEP_MS, milliseconds,
                       milliseconds ^ 0xFFFF, MAX_MS, DEFAULT_MS)
    return body + struct.pack("<H", binascii.crc_hqx(body, 0))


def decode(record):
    require(len(record) == RECORD_BYTES, "Incomplete global setting record")
    ms = int.from_bytes(record[6:8], "little")
    require(record == encode(ms), "Global setting signature, version or checksum differs")
    return ms


class UsbLink:
    def __init__(self, midi, incoming, outgoing):
        self.midi, self.incoming, self.outgoing = midi, incoming, outgoing
        self.verified = False
        self.writes = 0

    def exchange(self, command, payload=b""):
        require(command in (0x11, 0x22, 0x23), "Unsupported request")
        if command != 0x11:
            require(len(payload) >= 8 and payload[0] == 4, "Expected memory type 4")
            offset = int.from_bytes(payload[1:5], "little")
            count = int.from_bytes(payload[5:8], "little")
            if command == 0x22:
                require(self.verified and offset == OFFSET and count == RECORD_BYTES and
                        len(payload) == 8 + RECORD_BYTES, "Only the verified global setting may be written")
                decode(payload[8:])
                self.writes += 1  # increment before send, including a lost ACK
            else:
                require(len(payload) == 8 and 0 < count <= 512, "Invalid read request")
                ranges = ((APP_BASE - RAM_BASE, APP_BYTES), (PATCH_ADDRESS - RAM_BASE, 0x900))
                if self.verified:
                    ranges += ((0, CONFIG_BYTES), (USR_VIEW - RAM_BASE, FLASH_BYTES),
                               (0x19100 - RAM_BASE, 4), (OFFSET, RECORD_BYTES))
                require(any(start <= offset < offset + count <= start + size for start, size in ranges),
                        "Read outside the audited ranges")
        else:
            require(not payload, "Identity request has no body")
        raw = b"\0Y" + bytes([command]) + len(payload).to_bytes(3, "little")
        raw += payload + bytes([~sum(payload) & 255])
        self.outgoing.send(self.midi.Message("sysex", data=codec.encode7(raw)))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = self.incoming.poll()
            if message is None:
                time.sleep(0.002)
                continue
            if message.type != "sysex":
                continue
            kind, body = codec.reply(bytes(message.data))
            if command == 0x22:
                require((kind, body) == (0, b"\0"), "Setting write was not acknowledged")
                return body
            require(kind == command, "Unexpected reply; close other MIDI editors")
            if command == 0x23:
                require(body[:8] == payload and len(body) == 8 + count, "Read reply differs from request")
                return body[8:]
            return body
        raise TimeoutError("No reply; the write is not retried automatically")

    def read(self, offset, count):
        result = bytearray()
        while len(result) < count:
            length = min(512, count - len(result))
            prefix = b"\x04" + (offset + len(result)).to_bytes(4, "little") + length.to_bytes(3, "little")
            result.extend(self.exchange(0x23, prefix))
        return bytes(result)

    def identify(self, manifest):
        identity = self.exchange(0x11).split(b"\0", 1)[0].decode("ascii")
        require(identity == IDENTITY, f"Richiede {IDENTITY}; rilevato {identity}. Nessuna impostazione scritta.")
        require(manifest.get("schema") == 1 and manifest.get("identity") == IDENTITY and
                type(manifest.get("module_bytes")) is int and 0 < manifest["module_bytes"] <= 0x900,
                "Unsupported candidate manifest")
        for offset, size, digest in ((APP_BASE - RAM_BASE, APP_BYTES, manifest["application_sha256"]),
                                     (PATCH_ADDRESS - RAM_BASE, manifest["module_bytes"], manifest["module_sha256"])):
            require(codec.digest(self.read(offset, size)) == digest, "Installed code differs from the local build")
        self.verified = True
        return identity

    def get(self):
        return decode(self.read(OFFSET, RECORD_BYTES))

    def set(self, milliseconds):
        record = encode(milliseconds)
        prefix = b"\x04" + OFFSET.to_bytes(4, "little") + RECORD_BYTES.to_bytes(3, "little")
        self.exchange(0x22, prefix + record)


def change(link, milliseconds, output):
    """Keep a durable local recovery copy before the single bounded request."""
    wanted = encode(milliseconds)  # validate before any I/O or directory creation
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="preflight", captured_utc=datetime.now(timezone.utc).isoformat(),
                  identity=IDENTITY, requested_ms=milliseconds, scope="global", firmware_image_writes=0)

    def save():
        report["protocol_write_attempts"] = link.writes
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        before = link.get()
        report["previous_ms"] = before
        pointer = link.read(0x19100 - RAM_BASE, 4)
        require(pointer == struct.pack("<I", 0x39000), "Native USRFLASH pointer differs")
        ram = link.read(0, CONFIG_BYTES)
        flash = link.read(USR_VIEW - RAM_BASE, FLASH_BYTES)
        require(link.read(0, CONFIG_BYTES) == ram and link.read(USR_VIEW - RAM_BASE, FLASH_BYTES) == flash,
                "Configuration changed during preflight")
        require(flash[:CONFIG_BYTES] == ram and flash[CONFIG_BYTES:FOOTER] == b"usrdata\0",
                "RAM and saved configuration differ; wait for native saving to finish")
        record = flash[FOOTER:FOOTER + RECORD_BYTES]
        require(record in (b"\xff" * RECORD_BYTES, encode(before)) and
                flash[FOOTER + RECORD_BYTES:] == b"\xff" * (FLASH_BYTES - FOOTER - RECORD_BYTES),
                "Unknown data occupies the extension footer")
        durable_write(output / "configuration-before.bin", ram)
        durable_write(output / "usrflash-before.bin", flash)
        durable_write(output / "setting-before.bin", encode(before))
        if before == milliseconds:
            report.update(status="unchanged", effective_ms=before)
            return report
        report["status"] = "write_about_to_send"
        save()
        link.set(milliseconds)
        require(link.get() == milliseconds, "Setting readback differs from requested value")
        expected = ram + b"usrdata\0" + wanted
        expected += b"\xff" * (FLASH_BYTES - len(expected))
        require(link.read(0, CONFIG_BYTES) == ram, "Native configuration changed")
        require(link.read(USR_VIEW - RAM_BASE, FLASH_BYTES) == expected and
                link.read(USR_VIEW - RAM_BASE, FLASH_BYTES) == expected,
                "Persistent readback differs from the expected full saved region")
        report.update(status="saved_and_read_back", effective_ms=milliseconds,
                      configuration_unchanged=True, power_cycle_tested=False)
        return report
    except BaseException as error:
        report.update(status="stopped", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path, help="Local 150 manifest.json")
    parser.add_argument("--input")
    parser.add_argument("--output-port")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("get")
    setter = commands.add_parser("set")
    setter.add_argument("milliseconds", type=int)
    setter.add_argument("--backup", required=True, type=Path, help="New local backup/report directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            encode(args.milliseconds)
            require(not args.backup.exists(), "Choose a new backup directory")
        manifest = json.loads(args.build.read_text(encoding="utf-8"))
        import mido
        i, o = codec.select_ports(mido.get_input_names(), mido.get_output_names(), args.input, args.output_port)
        with ExitStack() as stack:
            incoming = stack.enter_context(mido.open_input(i))
            outgoing = stack.enter_context(mido.open_output(o))
            link = UsbLink(mido, incoming, outgoing)
            link.identify(manifest)
            result = dict(scope="global", milliseconds=link.get()) if args.command == "get" else change(link, args.milliseconds, args.backup)
            print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError, RuntimeError, ImportError, TimeoutError) as error:
        parser.exit(2, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
