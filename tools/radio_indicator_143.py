"""Align a stale saved Bluetooth-off flag with an already active 143 radio.

Only the pinned 143 application/module are accepted. One unchanged configuration
tail, except for the final off flag = 0, is submitted once. The original native
save also retires a validated obsolete 150 footer. No B+C event, firmware write,
radio-handle manipulation, preset write or arbitrary configuration is exposed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import acceptance_142 as usb, native_snapshot as native, radio_142 as radio
    from .ble_split_143 import normalized_blobs
except ImportError:
    import acceptance_142 as usb
    import native_snapshot as native
    import radio_142 as radio
    from ble_split_143 import normalized_blobs

require = usb.require


class UsbLink(usb.UsbLink):
    def __enter__(self):
        try:
            incoming, outgoing = usb.codec.select_ports(self.midi.get_input_names(), self.midi.get_output_names())
            self.incoming = self.stack.enter_context(self.midi.open_input(incoming))
            self.outgoing = self.stack.enter_context(self.midi.open_output(outgoing))
            spec = native.PROFILES["143"]
            identity = self.exchange(0x11, b"", 0x11).split(b"\0", 1)[0].decode("ascii")
            require(identity == spec["identity"], "Only split-only 143 identity accepted")
            app = self.read(usb.APP_BASE, spec["app_size"])
            require(usb.codec.digest(app) in spec["hashes"], "Running 143 application differs")
            module = native.read_fixed(self.midi, self.incoming, self.outgoing, "143",
                                       spec["module_base"], spec["module_bytes"])
            require(usb.codec.digest(module) == spec["module_sha256"], "Running 143 module differs")
            require(self.read(0x19100, 4) == b"\0\x90\3\0", "Unexpected save pointer")
            return self
        except BaseException:
            self.stack.close()
            raise


def run(midi, output):
    output.mkdir(parents=True, exist_ok=False)
    report = dict(status="preflight", firmware_writes=0, configuration_write_attempts=0)

    def save(**fields):
        report.update(fields)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    try:
        with UsbLink(midi) as link:
            original = link.read(usb.RAM_BASE, usb.CONFIG_BYTES)
            flash = link.read(usb.USR_VIEW, usb.FLASH_BYTES)
            before = radio.state(link)
            require(original == link.read(usb.RAM_BASE, usb.CONFIG_BYTES) and
                    flash == link.read(usb.USR_VIEW, usb.FLASH_BYTES), "Unstable baseline")
            require(before["enabled"] and before["server_enabled"] and before["combo_counter"] == 0
                    and link.read(usb.KEYS, 8) == bytes(8), "Radio must already be active and keys idle")
            clean = normalized_blobs({"plus-original": original, "plus-saved": flash})["plus-saved"]
            target = original[:23651] + b"\0"
            expected = target + clean[usb.CONFIG_BYTES:]
            for name, data in (("configuration-before", original), ("usrflash-before", flash),
                               ("configuration-expected", target), ("usrflash-expected", expected)):
                usb.durable_write(output / f"{name}.bin", data)
            save(radio_before=before, configuration_before_sha256=usb.codec.digest(original),
                 usrflash_before_sha256=usb.codec.digest(flash))
            if (original, flash) != (target, expected):
                address, data = usb.RAM_BASE + 23640, target[23640:]
                link.allow = ((address, data),)
                save(status="about_to_submit_unchanged_tail_with_radio_flag_on", configuration_write_attempts=1)
                link.write(address, data)  # one attempt, including uncertain acknowledgements
            require(link.read(usb.RAM_BASE, usb.CONFIG_BYTES) == target, "Configuration readback differs")
            persistence = usb.wait_saved(link, expected)
            after = radio.state(link)
            require(after["off_flag"] == 0 and after["enabled"] and after["server_enabled"], "Radio state differs")
            usb.durable_write(output / "usrflash-verified.bin", link.read(usb.USR_VIEW, usb.FLASH_BYTES))
            save(status="indicator_setting_verified", radio_after=after, persistence=persistence,
                 changed_configuration_offsets=[i for i, (a, b) in enumerate(zip(original, target)) if a != b],
                 legacy_footer_retired=clean != flash, configuration_write_attempts=link.writes)
    except BaseException as error:
        save(status="stopped_keep_backups", error=f"{type(error).__name__}: {error}")
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(dict(hardware_access=False, firmware_writes=0, configuration_flag=23651,
                              target_off_flag=0, requires_radio_already_active=True)))
        return
    import mido
    print(json.dumps(run(mido, args.output), indent=2))


if __name__ == "__main__":
    main()
