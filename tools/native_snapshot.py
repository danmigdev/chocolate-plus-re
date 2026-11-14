#!/usr/bin/env python3
"""Capture the two audited Chocolate Plus builds through read-only USB MIDI.

Supports stock/paced 013 before migration and the exact upstream 142 afterwards.
The optional --enter-boot sends only the audited 013 software boot request,
after checking its application, RAM boot routine and saved configuration.
No flash/configuration writes or arbitrary memory ranges are exposed.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import time

try:
    from . import config_snapshot as config
    from .prepare_142 import load_plan, durable_write, require
except ImportError:
    import config_snapshot as config
    from prepare_142 import load_plan, durable_write, require

RAM_BASE, APP_BASE, USR_VIEW = 0x13490, 0x1E00120, 0x1E38000
BOOT_RAM, BOOT_SIZE = 0x1C934, 28
BOOT_SHA256 = "e3097a7fdbfa447c973df4975ab54e34b9e82f26fae1d927da847fde6c1efdf4"
BOOT_COMMAND = bytes.fromhex("f0 22 24 35 7d f7")
PACED_SHA256 = "c8f000ec252f20987df9bb1ed98ea4bbf718a11806c153787ec41dc3b08ad85a"
MODULE_BASE, MODULE_SIZE = 0x1E265C0, 224
MODULE_SHA256 = "b084c9d607210116d5887729c391541e786d6760fa027f83639455c973d4e5cd"
PROFILES = {
    "013": dict(identity="FootCtrlPlus_013", app_size=155488,
                hashes={"f535c717a1ce23ef1b0fe5251c0ab7c906f2be068d2c172ad8440be1e0edeadc", PACED_SHA256}),
    "142": dict(identity="FootCtrlPlus_142-dtnprt", app_size=155536,
                hashes={"6affa33554ce40450f791afb1c6c8ca0d27da53ad82e1eb769a152a2fbec21f9"}),
}


def read_request(profile, address, size):
    ranges = [(RAM_BASE, config.CONFIG_SIZE), (APP_BASE, PROFILES[profile]["app_size"]),
              (0x19100, 4), (USR_VIEW, 0x6000)]
    if profile == "013":
        ranges += [(BOOT_RAM, BOOT_SIZE), (MODULE_BASE, MODULE_SIZE)]
    require(0 < size <= 512 and any(start <= address < address + size <= start + length
                                   for start, length in ranges), "Outside audited native read ranges.")
    prefix = b"\x04" + (address - RAM_BASE).to_bytes(4, "little") + size.to_bytes(3, "little")
    raw = b"\x00\x59\x23\x08\x00\x00" + prefix + bytes([~sum(prefix) & 255])
    return config.encode7(raw), prefix


def read_fixed(midi, incoming, outgoing, profile, address, size):
    result = bytearray()
    while len(result) < size:
        count = min(512, size - len(result))
        packet, prefix = read_request(profile, address + len(result), count)
        outgoing.send(midi.Message("sysex", data=packet))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            message = incoming.poll()
            if message is None:
                time.sleep(0.005)
                continue
            if message.type != "sysex":
                continue
            command, body = config.reply(bytes(message.data))
            require(command == 0x23 and body[:8] == prefix and len(body) == count + 8,
                    "Unexpected native read response.")
            result.extend(body[8:])
            break
        else:
            raise ValueError("Native read timed out.")
    return bytes(result)


def capture(midi, profile, output, original=None, enter_boot=False):
    require(profile in PROFILES, "Unsupported native profile.")
    require(not enter_boot or profile == "013", "Software boot entry is audited only for 013.")
    if original is not None:
        require(len(original) == 0x40000, "Expected complete original flash image.")
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="starting", profile=profile, device_flash_writes=0,
                  entry_requests=0, captured_utc=datetime.now(timezone.utc).isoformat(),
                  evidence="Native app, configuration RAM and mapped USRFLASH; not a physical flash read")

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def keep(name, value):
        durable_write(output / f"{name}.bin", value)
        return value

    save()
    try:
        i, o = config.select_ports(midi.get_input_names(), midi.get_output_names())
        with ExitStack() as stack:
            incoming = stack.enter_context(midi.open_input(i))
            outgoing = stack.enter_context(midi.open_output(o))
            identity = config.exchange(midi, incoming, outgoing, 0x11).split(b"\0", 1)[0].decode("ascii")
            require(identity == PROFILES[profile]["identity"], "Device identity differs from selected profile.")
            report.update(identity=identity, input_port=i, output_port=o)

            def read(address, size):
                return read_fixed(midi, incoming, outgoing, profile, address, size)

            app = keep("application", read(APP_BASE, PROFILES[profile]["app_size"]))
            app_sha = config.digest(app)
            require(app_sha in PROFILES[profile]["hashes"], "Installed application is not an audited build.")
            report["application_sha256"] = app_sha
            if profile == "013":
                boot = keep("boot-routine", read(BOOT_RAM, BOOT_SIZE))
                require(config.digest(boot) == BOOT_SHA256, "Relocated boot routine differs.")
                if app_sha == PACED_SHA256:
                    module = keep("pacing-module", read(MODULE_BASE, MODULE_SIZE))
                    require(config.digest(module) == MODULE_SHA256, "Installed pacing extension differs.")
            require(int.from_bytes(read(0x19100, 4), "little") == 0x39000,
                    "Configuration flash pointer differs from audited map.")
            ram1 = keep("configuration-1", read(RAM_BASE, config.CONFIG_SIZE))
            flash1 = keep("usrflash-1", read(USR_VIEW, 0x6000))
            flash2 = keep("usrflash-2", read(USR_VIEW, 0x6000))
            ram2 = keep("configuration-2", read(RAM_BASE, config.CONFIG_SIZE))
            report.update(stable_ram=ram1 == ram2, stable_mapped_flash=flash1 == flash2,
                          ram_matches_saved_flash=ram1 == flash1[:len(ram1)],
                          configuration_sha256=config.digest(ram1), usrflash_sha256=config.digest(flash1))
            require(ram1 == ram2 and flash1 == flash2, "Native snapshots are unstable.")
            require(flash1[23652:23660] == b"usrdata\0", "Configuration marker differs.")
            require(ram1 == flash1[:len(ram1)], "RAM contains settings that differ from saved flash.")
            if original is not None:
                report.update(configuration_matches_backup=ram1 == original[0x39000:0x39000 + len(ram1)],
                              usrflash_matches_backup=flash1 == original[0x39000:0x3F000])
                require(report["configuration_matches_backup"] and report["usrflash_matches_backup"],
                        "Configuration differs from pre-migration backup; captures retained.")
            report["status"] = "native_snapshot_verified"
            save()
        if enter_boot:
            # Input handles must be closed before Windows removes USB MIDI.
            with midi.open_output(o) as outgoing:
                report.update(status="boot_entry_about_to_send", entry_requests=1)
                save()
                outgoing.send(midi.Message.from_bytes(BOOT_COMMAND))
            report["status"] = "boot_entry_sent_enumeration_not_yet_verified"
        return report
    except BaseException as error:
        report.update(status="stopped", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New capture directory")
    parser.add_argument("--plan", type=Path, help="Also compare to this plan's original configuration")
    parser.add_argument("--enter-boot", action="store_true", help="013 only: enter ROM after verified captures")
    args = parser.parse_args()
    try:
        original = load_plan(args.plan)[1] if args.plan else None
        import mido
        print(json.dumps(capture(mido, args.profile, args.output, original, args.enter_boot), indent=2))
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        parser.exit(2, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
