# Chocolate Plus / TANK Mini: 142 baseline

This fork starts from upstream commit `9af0be5` and retains the exact 142 patch
set. No experimental 013 SysEx pacing patch is carried into 142. Work is on
`dev`; vendor firmware and private device snapshots remain local.

## What is established

- Building from the pinned original 012 reproduces upstream 142 byte for byte:
  FWSC SHA-256 `eadd20b993b149753651f83a683d1af25fb4f966d340e0b269a33b300f2e3ba2`.
- 142 adds the improvements described in the upstream
  [changelog](../firmware/CHANGELOG.md), including quicker switch handling.
  Its USB fix restores enumeration broken by earlier custom builds.
- Selected save/load code windows in 142 are unchanged from 012. The audited
  USB MIDI packet builder and HOST sender windows also match stock 013.
  These comparisons do not establish that 142 fixes either stored SysEx
  corruption or consecutive-message delivery to the TANK Mini.
- The earlier investigation reproduced corruption in 013 without an H start:
  configuration writes could leave RAM correct while physical flash differed.
  A later erase/program/readback procedure preserved the test data. The old
  013 pacing build still failed the physical multi-message HOST test.

The historical ZIP reports 15 changed bytes / 17 cleared bits for test 1.
Test 2 has 1,948 changed bytes / 2,354 cleared bits overall; the 7,424-byte FF
subset accounts for 1,934 changed bytes / **2,338** cleared bits. The original
email used the overall bit count for the FF subset. Neither test set any bit
from 0 to 1. `audit_142.py --evidence-zip` recomputes these figures from the
original files without publishing them.

## Reproduce the build and offline audit

Use the original 012 package obtained from M-VAVE; the build validates its
SHA-256. Do not substitute 013 as the build input.

```powershell
python -I firmware/build.py <FootCtrlPlus_012.fwsc> --out firmware/out/142-baseline --extras
python tools/audit_142.py --stock <FootCtrlPlus_012.fwsc> --firmware firmware/out/142-baseline/FootCtrlPlus_142-dtnprt.fwsc --stock-013 <FootCtrlPlus_013.fwsc> --output artifacts/audit-142.json
python -m unittest discover -s tests -v
```

The audit checks both firmware copies, package/header/directory/application
CRCs, selected code windows and the unchanged boot prefixes. It is an offline
integrity check, not a hardware acceptance test.

## Windows USB migration

The upstream installation route uses SincoOTA on Android. The tools here use
the BR25 USB bootloader for the specifically audited 256-KiB unit with SPI ID
`854512`. This is a separate route, adapted from the earlier local Stompilot
investigation. Unknown builds, other flash IDs and other image layouts fail
closed. The supported starting images are exact stock 013 or the pinned local
013 pacing build; a version string alone is insufficient.

MIDI capture needs `mido` and `python-rtmidi`; offline audit, planning and the
Windows SCSI transport use the Python standard library. The vendor RAM loader
is a separate local input, never committed here:

- Source: [jl-uboot-tool, pinned revision](https://github.com/kagaimiq/jl-uboot-tool/tree/adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a/data/loaderblobs/usb).
- File: `br25loader.bin`, 11,264 bytes.
- SHA-256: `0bc69023d6bf4c4d5d5bc048e06249ee39c484acd11196ffc73f88940787d77d`.

Each output directory must be new. Keep the controller powered and connected
throughout bootloader access. Close other MIDI editors first.

1. Connect USB-C in U mode. Capture the current configuration and native state:

   ```powershell
   python tools/config_snapshot.py list
   python tools/config_snapshot.py capture artifacts/migration-142/config-before --expect FootCtrlPlus_013
   python tools/native_snapshot.py --profile 013 --output artifacts/migration-142/native-before
   ```

2. Enter ROM using the audited software request. This checks the complete
   installed app, its relocated boot routine, any pacing extension, the
   configuration pointer, two RAM reads and two mapped USRFLASH reads first.
   Unsaved settings block entry. The MIDI input is closed before reset.

   ```powershell
   python tools/native_snapshot.py --profile 013 --output artifacts/migration-142/boot-entry --enter-boot
   ```

3. In an elevated Windows terminal, acquire two complete physical backups.
   The reader selects exactly one `BR25 UBOOT1.00 USB Device`, checks its SCSI
   identity, uploads only the pinned RAM loader with readback, verifies the
   flash ID and reads all 256 KiB twice. Its command gate forbids flash erase
   and program operations. Omit `--execute` for an offline description.

   ```powershell
   python tools/br25_usb.py --loader <br25loader.bin> --output artifacts/migration-142/physical-before --execute
   ```

   Use `--resume-loader` only when the verified RAM loader is already running
   in this uninterrupted boot session. Do not upload it again in that state.

4. Prepare and inspect the device-specific plan, with no device access:

   ```powershell
   python tools/prepare_142.py --backup artifacts/migration-142/physical-before --firmware firmware/out/142-baseline/FootCtrlPlus_142-dtnprt.fwsc --output artifacts/migration-142/plan
   python tools/install_142.py --plan artifacts/migration-142/plan
   python tools/install_142.py --plan artifacts/migration-142/plan --restore
   ```

   The compact image is **flash.bin in 142**, whereas it was **flash2.bin in
   013**. Selecting by filename alone would choose the wrong geometry. The
   compact image ends at `0x27600`; the old pacing extension ends at `0x27700`.
   Audited native VM alignment retains banks at `0x28000` and `0x29000`.
   Boot bytes below `0x1000`, all bytes beyond the original image extent,
   Bluetooth/calibration and USRFLASH are preserved. The obsolete pacing
   extension is cleared when migrating from that exact pinned source.

5. In the elevated terminal, install the reconstructed plan:

   ```powershell
   python tools/install_142.py --plan artifacts/migration-142/plan --execute
   ```

   Two full reads must match the original or complete target before writing.
   Only changed application sectors are erased, with directory/CRCs last.
   Each erase has two blank checks; each programmed 256-byte page is read
   back; each sector and the final full image are read twice. The durable
   journal records every attempted erase/program before issuing it. No
   device command is retried after a lost reply; no reset is sent. A temporary
   Windows sharing violation while publishing the local journal is retried
   for at most five seconds, with flash I/O paused until publication succeeds.

   After any failure, keep power connected and inspect the journal. Explicit
   `--restore --execute` restores the original bytes in the same sectors,
   provided the rest of flash still matches the backup and the RAM loader
   remains reachable. Recovery into ROM from a nonbooting application after
   loss of power has **not** been independently verified. A backup alone does
   not establish that recovery path.

6. Only after `physically_verified_restart_required`, power the controller
   fully off and restart in U. USB MIDI may now be named **SINCO**. Verify:

   ```powershell
   python tools/native_snapshot.py --profile 142 --plan artifacts/migration-142/plan --output artifacts/migration-142/native-after
   ```

   This verifies the complete running 142 app against its pinned hash and
   compares two configuration RAM reads and two mapped USRFLASH reads to the
   original backup. It does not send a boot request or a configuration write.

## Validation record

On 14 November 2026, the connected unit reported 013 and its complete native
application matched the previously installed pacing build. Its configuration
RAM matched saved USRFLASH. Two new physical 256-KiB reads were identical.
The prepared migration changes 39 application sectors and preserves all
device data outside that extent. Automated checks include a simulated install
and exact restoration of this actual private backup, fault injection for lost
erase/program replies, rejection of changed preserved regions, altered plans
and unknown applications. Private fixtures are optional in clean checkouts.

The subsequent hardware installation completed: 39 sectors erased, 614 pages
programmed and two complete physical reads matching the reconstructed 142
target. Both reads also confirmed the original boot prefix and every byte
from `0x27700` through the end of flash were preserved. The successful
installation report is `plan/install-/20261114T170658.320596Z/report.json`
under local `artifacts/migration-142/`.

After a manual full power cycle in U, the unit reported
`FootCtrlPlus_142-dtnprt`. The complete running application matched SHA-256
`6affa33554ce40450f791afb1c6c8ca0d27da53ad82e1eb769a152a2fbec21f9`.
Two public configuration RAM reads and two complete mapped USRFLASH reads
matched the pre-migration backup; the saved marker and physical-address
pointer also matched. Evidence is in `native-after/report.json`. Windows on
this PC retained the MIDI port name `FootCtrlPlus`; discovery also accepts
the `SINCO` name used by upstream.

All 42 automated tests passed, including the optional real-backup simulations
and Windows file-lock test. TANK Mini HOST parameter delivery and persistence
across U/H restarts remain acceptance tests; no success is inferred from the
firmware version or the offline checks.

The first installation attempt stopped when Windows denied an atomic local
journal replacement. No hardware command was retried. The original image was
then restored in the still-running RAM loader; two complete physical reads
matched the original backup byte for byte. Journal publication now handles
transient Windows sharing violations; tests cover both transient and permanent
failures, including a real held file handle on Windows. This successful restore
does not establish recovery into ROM after power loss with an invalid app.
