#!/usr/bin/env python3
"""Bounded, automatic USB acceptance test for the exact upstream 142 build.

Runs existing A/B assignments using the native short-tap path, checks actual
USB MIDI output, then exercises disabled SysEx padding with FF / AA55 / FF.
Uses only native identity/read/write commands. No firmware, erase, reset,
function-pointer or save-control writes. Configuration is backed up and restored.
Physical U/H and cold restarts are separate evidence, not simulated here.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import time

try:
    from . import config_snapshot as codec
    from .native_snapshot import RAM_BASE, APP_BASE, USR_VIEW, PROFILES
    from .prepare_142 import durable_write, require
except ImportError:
    import config_snapshot as codec
    from native_snapshot import RAM_BASE, APP_BASE, USR_VIEW, PROFILES
    from prepare_142 import durable_write, require

CONFIG_BYTES = 23652  # Native save includes six bytes beyond the editor's view.
STATE = 0x193A0
KEYS = STATE + 494
FLIPS = STATE + 43  # Advanced-mode toggle state, passed at 0x1e1329a..0x1e13306.
FLASH_BYTES = 0x6000


def split_sysex(data):
    frames = []
    while data:
        require(data[0] == 0xF0, "Bank does not begin with SysEx.")
        end = data.find(b"\xf7")
        require(end > 0 and all(b < 128 for b in data[1:end]), "Invalid stored SysEx.")
        frames.append(data[:end + 1])
        data = data[end + 1:]
    return frames


def padding_spans(config):
    require(len(config) == CONFIG_BYTES and config[0] == 3
            and config[23639] == config[23640] == 0, "Expected advanced mode, one page, page 1.")
    spans = []
    for switch in range(32):
        base = 93 + switch * 417
        if switch < 4 and switch not in (2, 3):
            continue
        for bank in range(2):
            rows = config[base + 1 + bank * 80:base + 81 + bank * 80]
            area = base + 161 + bank * 128
            # Unused pages can retain the vendor's enabled PC-0 defaults.
            # Those rows do not use SysEx storage; an enabled SysEx row does.
            uses_sysex = any(rows[i] and rows[i + 2] == 4 for i in range(0, 80, 5))
            require(not uses_sysex and config[area:area + 128] == bytes(128),
                    "A diagnostic bank is in use; refusing to replace its data.")
            spans.append((area + 1, 127))  # Preserve the zero length and all MIDI rows.
    return spans


def pattern_image(original, spans, pattern):
    require(pattern in (b"\xff", b"\xaa\x55"), "Unreviewed test pattern.")
    result = bytearray(original)
    for offset, count in spans:
        result[offset:offset + count] = (pattern * (count // len(pattern) + 1))[:count]
    return bytes(result)


class UsbLink:
    def __init__(self, midi):
        self.midi = midi
        self.stack = ExitStack()
        self.observed = []
        self.allow = ()
        self.writes = 0

    def __enter__(self):
        try:
            i, o = codec.select_ports(self.midi.get_input_names(), self.midi.get_output_names())
            self.incoming = self.stack.enter_context(self.midi.open_input(i))
            self.outgoing = self.stack.enter_context(self.midi.open_output(o))
            identity = self.exchange(0x11, b"", 0x11).split(b"\0", 1)[0].decode("ascii")
            require(identity == PROFILES["142"]["identity"], "Only exact 142 identity accepted.")
            app = self.read(APP_BASE, PROFILES["142"]["app_size"])
            require(codec.digest(app) in PROFILES["142"]["hashes"], "Complete running application differs from 142.")
            require(self.read(0x19100, 4) == b"\x00\x90\x03\x00", "Unexpected save pointer.")
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def exchange(self, command, payload, wanted, prefix=b""):
        require(command in (0x11, 0x22, 0x23), "Only identity/read/bounded write operations.")
        if command == 0x22:
            address = RAM_BASE + int.from_bytes(payload[1:5], "little")
            data = payload[8:]
            require(payload[:1] == b"\x04" and int.from_bytes(payload[5:8], "little") == len(data)
                    and (address, data) in self.allow, "Write differs from this session's exact allow-list.")
            self.writes += 1
        raw = b"\0Y" + bytes([command]) + len(payload).to_bytes(3, "little")
        raw += payload + bytes([~sum(payload) & 255])
        self.outgoing.send(self.midi.Message("sysex", data=codec.encode7(raw)))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            message = self.incoming.poll()
            if message is None:
                time.sleep(0.002)
                continue
            response = None
            if message.type == "sysex":
                try:
                    response = codec.reply(bytes(message.data))
                except codec.SnapshotError:
                    pass
            if response and response[0] == wanted and response[1].startswith(prefix):
                return response[1][len(prefix):]
            self.observed.append(bytes(message.bytes()))
        raise TimeoutError(f"No reply to {command:#x}; no automatic write retry.")

    def read(self, address, count):
        ranges = ((RAM_BASE, CONFIG_BYTES), (APP_BASE, PROFILES["142"]["app_size"]),
                  (USR_VIEW, FLASH_BYTES), (STATE, 260), (KEYS, 8), (0x19100, 4))
        require(count > 0 and any(base <= address < address + count <= base + size
                                  for base, size in ranges), "Read outside audited ranges.")
        result = bytearray()
        while len(result) < count:
            size = min(512, count - len(result))
            prefix = b"\x04" + (address + len(result) - RAM_BASE).to_bytes(4, "little") + size.to_bytes(3, "little")
            data = self.exchange(0x23, prefix, 0x23, prefix)
            require(len(data) == size, "Incomplete memory read.")
            result.extend(data)
        return bytes(result)

    def write(self, address, data):
        prefix = b"\x04" + (address - RAM_BASE).to_bytes(4, "little") + len(data).to_bytes(3, "little")
        require(self.exchange(0x22, prefix + data, 0) == b"\0", "Write was not acknowledged.")

    def collect(self, seconds=0.45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            message = self.incoming.poll()
            if message is None:
                time.sleep(0.002)
            else:
                self.observed.append(bytes(message.bytes()))
        frames, self.observed = self.observed, []
        return frames


def wait_saved(link, expected, timeout=18):
    started = time.monotonic()
    samples = 0
    while time.monotonic() - started < timeout:
        first = link.read(USR_VIEW, FLASH_BYTES)
        samples += 1
        if (first == expected and link.read(STATE + 82, 2) == b"\0\0"
                and link.read(USR_VIEW, FLASH_BYTES) == expected):
            return dict(seconds=round(time.monotonic() - started, 3), full_flash_samples=samples,
                        sha256=codec.digest(expected), two_matching_reads=True)
        time.sleep(0.25)
    raise ValueError("Saved USRFLASH did not converge to the expected complete image.")


def expected_frames(config, switch, bank):
    base = 93 + switch * 417
    require(config[base] == 1, "A/B must use alternating banks.")
    rows = config[base + 1 + bank * 80:base + 81 + bank * 80]
    require(rows == b"\x01\x00\x04\x00\x00" + bytes(75), "Expected one SysEx row only.")
    offset = base + 161 + bank * 128
    count = config[offset]
    require(0 < count <= 127, "Invalid bank length.")
    return split_sysex(config[offset + 1:offset + 1 + count])


def run(midi, output):
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="starting", firmware_writes=0, physical_transitions_tested=False,
                  host_to_tank_tested=False, key_tests=[], save_tests=[], restored=False)
    def log(status, **fields):
        report.update(status=status, **fields)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(dict(status=status, **fields)), flush=True)
    log("preflight")
    with UsbLink(midi) as link:
        original = link.read(RAM_BASE, CONFIG_BYTES)
        flash = link.read(USR_VIEW, FLASH_BYTES)
        flips = link.read(FLIPS, 4)
        require(original == link.read(RAM_BASE, CONFIG_BYTES) and flash == link.read(USR_VIEW, FLASH_BYTES)
                and original == flash[:CONFIG_BYTES] and flash[CONFIG_BYTES:CONFIG_BYTES + 8] == b"usrdata\0",
                "Baseline is unstable or has unsaved settings.")
        require(link.read(KEYS, 8) == bytes(8), "Release all switches before automatic testing.")
        spans = padding_spans(original)
        for switch in (0, 1):
            for bank in (0, 1):
                expected_frames(original, switch, bank)
        for name, data in (("configuration-before", original), ("usrflash-before", flash), ("flips-before", flips)):
            durable_write(output / f"{name}.bin", data)
        patterns = [pattern_image(original, spans, p) for p in (b"\xff", b"\xaa\x55", b"\xff")]
        allowed = [(RAM_BASE + offset, image[offset:offset + size])
                   for image in [original] + patterns for offset, size in spans]
        # Native scanner at 0x1e11fe0: two bytes [counter, pending-state] per
        # key. 142's pinned tap fix converts state 100 with released GPIO into
        # the existing press path, then emits release. No callback is replaced.
        allowed += [(KEYS + switch * 2, b"\0\x64") for switch in (0, 1)]
        allowed += [(KEYS, bytes(8)), (FLIPS, flips),
                    (RAM_BASE + 23640, original[23640:CONFIG_BYTES])]
        link.allow = tuple(allowed)
        log("baseline_saved", tested_padding_bytes=sum(size for _, size in spans),
            configuration_sha256=codec.digest(original), usrflash_sha256=codec.digest(flash))
        dirty = False
        try:
            for switch in (0, 1):
                for attempt in (1, 2):
                    require(link.read(KEYS, 8) == bytes(8), "Scanner is not idle.")
                    active = link.read(FLIPS, 4)[switch]
                    require(active in (0, 1), "Unexpected alternating-bank state.")
                    # 0x1e131a6 toggles this byte, then 0x1e130a2 selects B
                    # for the new value 1 and A for 0. Read it before each tap.
                    bank = 1 - active
                    expected = expected_frames(original, switch, bank)
                    link.collect(0.03)
                    dirty = True
                    link.write(KEYS + 2 * switch, b"\0\x64")
                    actual = link.collect()
                    result = dict(switch="AB"[switch], attempt=attempt, bank="AB"[bank],
                                  expected=[x.hex(" ") for x in expected], received=[x.hex(" ") for x in actual],
                                  passed=actual == expected)
                    report["key_tests"].append(result)
                    log("usb_assignment_checked", last_switch=result["switch"], last_passed=result["passed"])
                    require(result["passed"], "Actual USB MIDI differs from the configured bank.")
            require(link.read(KEYS, 8) == bytes(8) and link.read(FLIPS, 4) == flips,
                    "Scanner or alternating banks did not return to their initial state.")
            link.write(RAM_BASE + 23640, original[23640:CONFIG_BYTES])
            for index, target in enumerate(patterns, 1):
                for offset, size in spans:
                    link.write(RAM_BASE + offset, target[offset:offset + size])
                require(link.read(RAM_BASE, CONFIG_BYTES) == target, "Complete RAM readback differs after pattern.")
                target_flash = target + flash[CONFIG_BYTES:]
                result = dict(pattern=("FF", "AA55", "FF")[index - 1], **wait_saved(link, target_flash))
                report["save_tests"].append(result)
                durable_write(output / f"usrflash-pattern-{index}.bin", target_flash)
                log("pattern_saved_and_verified", pattern=result["pattern"], save_seconds=result["seconds"])
            report["tests_passed"] = True
        except BaseException as error:
            report.update(tests_passed=False, error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if dirty:
                log("restoring_original_configuration")
                # Restoration remains bounded to the original allowed spans.
                link.write(KEYS, bytes(8))
                for offset, size in spans:
                    link.write(RAM_BASE + offset, original[offset:offset + size])
                link.write(FLIPS, flips)
                link.write(RAM_BASE + 23640, original[23640:CONFIG_BYTES])
                require(link.read(RAM_BASE, CONFIG_BYTES) == original, "RAM restoration mismatch.")
                report["restore_save"] = wait_saved(link, flash)
                require(link.read(RAM_BASE, CONFIG_BYTES) == original and link.read(FLIPS, 4) == flips
                        and link.read(KEYS, 8) == bytes(8), "Final state differs from backup.")
                report["restored"] = True
            log("complete" if report.get("tests_passed") and report["restored"] else "stopped",
                configuration_write_requests=link.writes)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, operations=["A/B native tap events", "disabled SysEx padding",
                          "saved-memory verification", "restore"], firmware_writes=0)))
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
            if report["status"] in ("starting", "preflight"):
                report["status"] = "preflight_failed_no_writes"
            elif report["status"] == "restoring_original_configuration":
                report["status"] = "restoration_failed_keep_backups"
            report.setdefault("error", f"{type(error).__name__}: {error}")
            manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
