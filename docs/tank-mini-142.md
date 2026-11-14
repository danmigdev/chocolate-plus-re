# Chocolate Plus / TANK Mini: 142 baseline

This fork starts from upstream commit `9af0be5` and retains the exact 142 patch
set. No experimental 013 SysEx pacing patch is carried into 142. Windows
diagnostic work is on `feat/windows-usb-142-validation`, separate from `dev`;
vendor firmware and private device snapshots remain local.

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
- On this unit, 142 passed the physical H retention test and applied a single
  REV Mix SysEx through the tested HOST setup after the user performed the
  B+C radio-off procedure in H. The standard CC FX toggle also passed. Two
  consecutive SysEx messages assigned to one press failed with the same
  requested procedure, but their post-restart radio flag differed.
  The detailed measurements and their limits are recorded below.
- BLE can read the TANK live patch while the powered TANK is on Plus HOST,
  Plus is in H and neither device is cabled to the PC. A physical MOD toggle
  and restoration were observed automatically. The Plus native HOST state
  was also read through its BLE configuration service during that monitor.
  This is parameter/state monitoring, not a capture of USB packets.
- A subsequent automatic HOST comparison, with the native Bluetooth client
  disconnected and both diagnostic BLE links active, reproduced the paired
  failure: each message worked individually, but neither effect occurred
  when both messages were concatenated in one bank. Both devices were fully
  restored. See the automatic BLE HOST comparison below.

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

At the end of migration, all 42 automated tests passed, including the optional
real-backup simulations and Windows file-lock test. Subsequent hardware
acceptance results are recorded below; no success is inferred from the
firmware version or the offline checks.

The first installation attempt stopped when Windows denied an atomic local
journal replacement. No hardware command was retried. The original image was
then restored in the still-running RAM loader; two complete physical reads
matched the original backup byte for byte. Journal publication now handles
transient Windows sharing violations; tests cover both transient and permanent
failures, including a real held file handle on Windows. This successful restore
does not establish recovery into ROM after power loss with an invalid app.

## Automatic USB acceptance

The scripts below need `mido` and `python-rtmidi`. They open only the expected
MIDI port pairs. Each output folder must be new; private backups and reports
remain in ignored `artifacts/`. Omit `--execute` for an offline description.

```powershell
python tools/acceptance_142.py --output artifacts/acceptance-142/usb-run --execute
python tools/tank_relay_142.py --output artifacts/acceptance-142/tank-run --execute
```

`acceptance_142.py` requires the exact pinned 142 application, advanced mode,
one active page, the existing A/B SysEx assignments, and unused SysEx storage
in C/D and disabled pages. It backs up the native 23,652-byte configuration
(including six bytes outside the public editor view), the complete 24-KiB
saved region, and the advanced-mode bank toggles. Its exact write allow-list
permits only the frozen test data, restoration data, and native key events.

The key events write `[counter=0, pending-state=100]` to the existing scanner
at `0x1958e + 2*switch`. The pinned 142 tap fix processes these through its
normal press/release path with the physical switches released. No code,
callback, save counter, firmware or bootloader is modified. This checks bank
selection and actual USB MIDI emission; it bypasses physical contacts and
does not measure physical switch latency. Advanced bank state is at
`0x193cb + switch`: the next value 1 selects B, and 0 selects A.

After checking both banks of A/B, the script writes FF, AA55, then FF into
7,620 unused SysEx payload bytes. Zero length bytes, command rows and active
assignments remain intact. Each round must match the complete RAM image,
two complete mapped saved-region reads, and an expired native save timer.
The final restoration must satisfy those same checks. An exception attempts
restoration before exiting; a lost write acknowledgement never causes an
automatic retry of that write.

On 14 November 2026, `usb-automatic-3/report.json` recorded four successful
native taps and five byte-identical SysEx frames. All three saved-memory
rounds passed, taking 5.828, 5.812 and 5.860 seconds respectively, including
readback time. The final configuration and saved region matched their
original backups exactly. This establishes successful rewriting in U mode;
it does not establish persistence through a new H start or a power cycle
with the test patterns. The two earlier local runs stopped because of test
harness preconditions/expectations, which were corrected before this run;
they are not evidence of firmware corruption.

`tank_relay_142.py` requires a TANK reporting `TANK MINI_009` and a Plus in U,
each connected separately to the PC. It backs up the TANK's live patch,
state and saved presets in both RAM and flash. It captures real output from
native Plus key events, checks it byte for byte, then forwards those captured
frames unchanged to the TANK. The TANK's full 92-byte live patch is read back
after each bank: the complete expected image must match, including all
unrelated bytes. A setup value different from the target prevents a no-op from
passing. The existing A/B assignments cover MOD ON/OFF, the two-message FX
ON plus REV Mix bank, and preset reload. An unused C is temporarily assigned
REV Mix 25/75; these parameter tests run with REV disabled. All baseline
state is restored, including unsaved TANK edits and the original empty C.
Saved TANK presets are never written and must compare equal afterwards.

This relay uses PC USB output and buffered forwarding. It does not reproduce
the Plus HOST sender or its timing, and memory readback does not establish
the audible effect. Neither script can certify the direct Plus HOST-to-TANK
connection. A passing simulation or USB relay test must not be reported as
that hardware result.

On the same date, `tank-relay-3/report.json` recorded **six passing live relay
cases and seven intact SysEx frames**. A selected MOD OFF and ON. B reloaded
the saved preset and sent FX ON plus REV Mix **10**, which is the raw value
actually encoded in the existing assignment. Temporary C selected raw Mix
75 and 25 in that order. Each full live-patch readback matched its expected
image. The TANK's complete live patch, four-byte state, and saved presets in
RAM and flash were restored byte for byte. The Plus's complete RAM
configuration, 24-KiB saved region, key scanner and bank toggles were also
restored and checked; C remained empty afterwards. The starting Plus
configuration and saved region still matched the earlier USB acceptance
backup. All **55 automated software tests** passed, including interrupted
transmission and restoration checks.

Before the simultaneous relay was possible, a separate replay of the five
previously captured 142 SysEx frames passed all four A/B cases on the TANK
and restored its full snapshot. That earlier result is explicitly labelled
capture replay in its local report; the six-case result above used live Plus
emissions. Windows initially failed to enumerate one of the devices. Hub and
port resets did not resolve it; replacing the second USB cable allowed both
devices to be recognised together. The first two relay runs stopped at
preflight without configuration writes because the Plus was not enumerated.

## Physical U/H retention

`retention_142.py` separates staging, measurement and restoration so that a
real selector transition or power cycle can occur with the PC disconnected.
It validates immutable backups and reconstructs the exact test image before
any restoration. `prepare` writes FF into the same 7,620 unused SysEx payload
bytes as the USB acceptance test. `capture` reads RAM and saved storage twice,
saves both samples and counts changed bytes, cleared bits and set bits before
restoring the original full native configuration. Full restoration can also
repair corruption in active banks outside the diagnostic padding. Firmware,
bootloader, save counter and callbacks are never written. `--keep-pattern`
allows a read-only capture between successive physical tests; `restore` is
the explicit recovery operation if the normal sequence is interrupted.

```powershell
python tools/retention_142.py prepare --session artifacts/retention-142/session --execute
# Leave U powered for at least two minutes after the last pattern write.
# With HOST empty and no footswitches pressed, move to H for >=10 s, then U.
python tools/retention_142.py capture --session artifacts/retention-142/session --output artifacts/retention-142/after-H --execute
# If only restoration is needed, use a fresh output directory:
python tools/retention_142.py restore --session artifacts/retention-142/session --output artifacts/retention-142/recovery --execute
```

On 14 November 2026, this physical sequence was completed on the connected
unit. The user confirmed H with HOST empty and no footswitches pressed, then
U. The PC observed the USB port disappear **196.288 seconds after the last
pattern write** and remain absent for **54.938 seconds**. USB presence alone
does not identify the selector position; the physical H setting is a user
observation recorded separately. The two configuration RAM samples and two
24-KiB saved-region samples were identical to the staged images: **zero
changed bytes, zero cleared bits and zero set bits**. The entire original
configuration, saved region and runtime bank toggles were restored and
verified afterwards. Local evidence is in `artifacts/retention-142/session-1/`
and `artifacts/retention-142/after-H-1/`.

The stored-SysEx corruption was therefore **not reproduced in this physical
H cycle on 142**. This is one tested transition, not a guarantee for every
unit, configuration, startup sequence or power-loss condition.

## Direct HOST measurement workflow

`host_142.py` stages a distinct live-patch marker on the TANK and two identical
banks on unused C: each sends FX OFF followed by raw REV Mix 73. The PC stages
FX ON and Mix 47, disables REV and temporarily mutes the preset. The target
Mix 73 is not sent from the PC during staging. Saved presets remain read-only.
Immutable backups precede any mutation. C's identical banks remove ambiguity
from bank-toggle state after startup.

First disconnect and reconnect only the TANK's USB cable while keeping it
powered. `check-usb` requires its complete live patch, state and saved presets
to remain equal to the staged baseline. Only after this passes, connect the
TANK's cable to the Plus HOST port, start the Plus in H, wait for startup and
press C once. Keep the TANK powered while returning its USB cable to the PC.
`read-tank` captures its full state before restoring the original live patch
and checking saved-preset integrity. Return the Plus to U; `finish` captures
its stored configuration and restores the original configuration, including
empty C. The native current-switch marker is recorded separately from stored
configuration integrity; its value after reboot cannot confirm a physical C
press. Physical cable routing and the C press are user observations;
the expected parameter changes are measured by USB readback.

```powershell
python tools/host_142.py prepare --session artifacts/host-142/session --execute
# Disconnect and reconnect TANK USB, without powering it off.
python tools/host_142.py check-usb --session artifacts/host-142/session --output artifacts/host-142/control --execute
# TANK to Plus HOST, Plus to H, wait for startup, press C, wait, TANK back to PC.
python tools/host_142.py read-tank --session artifacts/host-142/session --output artifacts/host-142/measurement --execute
# Plus back to U; both devices now on their separate PC USB cables.
python tools/host_142.py finish --session artifacts/host-142/session --output artifacts/host-142/plus-after --execute
# To abort and restore, connect both to the PC (Plus in U), use a fresh output:
python tools/host_142.py restore --session artifacts/host-142/session --output artifacts/host-142/recovery --execute
```

The explicit recovery attempts both devices independently. An unavailable
device is reported and does not prevent restoring the other one. Keep the
local backups until both restoration checks have passed. A HOST result is
established only after the physical sequence and measured target match;
the preparation or a simulated test alone cannot establish delivery.

The first physical HOST attempt on 14 November 2026 did **not** apply the
target changes. The USB reconnect control passed with the complete staged
TANK snapshot preserved. With the HOST connection, H selection and one C
press confirmed by the user, subsequent USB readback still showed FX ON and
Mix 47, exactly the staged live patch, instead of FX OFF and Mix 73. Saved
presets and preset state were unchanged. The complete original TANK snapshot
was restored and later independently rechecked with reads only.

After the Plus returned to U, its complete configuration and 24-KiB saved
region matched the **prepared** images byte for byte, including both C banks.
Thus storage corruption was not observed during this HOST attempt either.
The original Plus configuration, saved region and runtime bank state were
restored and verified; C is empty again. Evidence is in local
`artifacts/host-142/usb-control-1/`, `measurement-1/` and `plus-after-H-1/`.

The first version of `finish` incorrectly expected configuration byte 23646
to persist as 2 after C. It was 1 both before and after the test. The pinned
142 code writes this current-switch marker in RAM at `0x1e1304c`; the normal
advanced-mode press path does not itself arm the configuration save timer.
The configuration-write handler arms that timer at `0x1e13b76`. Consequently,
after restart an unchanged marker is not evidence of a different switch or
corrupted storage. The comparison now uses the actual prepared image and
reports the marker separately. The historical raw capture and original report
were retained; `analysis-after-H.json` records the corrected offline comparison.

This first attempt establishes that the expected two-message HOST action did
not take effect under those conditions. By itself it does not identify whether
the failure concerns physical event handling, HOST connection, SysEx
transmission or consecutive-message handling. The following tests isolate an
additional radio route and test single and paired messages separately.

### Single SysEx and standard MIDI comparison

`host_142.py prepare --variant split` uses two unused switches: C sends one
complete SysEx setting REV Mix to 73, and D sends channel-1 CC 0/value 0,
the stock FX toggle. Both banks of each switch are identical. Preparation
captures each actual Plus USB emission, forwards it to the TANK, and checks
the entire live patch. It then reinstates and verifies the HOST baseline
(muted, FX ON, REV disabled, Mix 47), removing the target-message permissions
from the PC transport. Later commands reconstruct the variant from its
immutable plan. Version-1 paired-SysEx session backups remain supported.

Calibration also reads the TANK before the native key event and before the
PC relay. A change between those reads stops preparation and restores both
devices: a second MIDI path could otherwise toggle FX twice and hide a
successful delivery. Captured frames and intermediate readbacks are saved
before assertions, including failed checks. Unexpected output is never
forwarded. Saved TANK presets and firmware remain read-only.

On 14 November 2026, the split preparation exposed exactly this ambiguity.
The single SysEx changed Mix 47 to 73 through the explicit PC relay, with an
exact full-patch match. After the D key event, FX changed from ON to OFF
**before the PC forwarded any CC** (`split-session-5/prepare.json`). The tool
stopped without forwarding the CC and restored the complete original TANK
snapshot and Plus configuration/saved region. Earlier preparations had
forwarded the CC again and observed FX ON; their delayed-read experiments
did not establish a timing fix. Direct PC CC controls succeeded with the
Plus MIDI ports both closed and open (`cc-routing-1/report.json`).

These observations required identifying and isolating the extra route before
using the split workflow as a HOST test. They are not a new direct-HOST
result or proof that the physical C/D contacts were exercised. The native
key events used for USB calibration bypass physical contact/debounce.

### Radio isolation and the extra CC route

The user confirmed that only the two separate PC USB cables were connected.
On 14 November 2026, native Plus readback nevertheless showed its radio
enabled and a client connection present (handle `0x0050`, client stage
`0x43`). The configuration's Bluetooth-off flag at byte 23651 was 1 at the
same time. Restoring that byte alone does not invoke the radio stop handler.
It is also inside the native saved image, although outside the public
editor's shorter configuration view. Neither that flag nor the LED alone
can establish the actual connection state after a raw configuration restore.

`radio_142.py` audits the running radio through the existing USB link. For
the exact pinned 142 application it can request the stock B+C action: its
16-bit counter at `STATE+76` is set to 400, then consumed by the stock timer
at `0x1e12a30`. The pre-toggle flag is explicitly aligned first. This runs
the normal radio stop/start routine in its native context. It never writes
firmware, callbacks, live connection handles or the save timer. Busy keys,
unexpected state or failed readback stop the operation; recovery attempts
the TANK, radio and full Plus configuration independently.

In `artifacts/host-142/radio-isolation-2/report.json`, both USB calibrations
passed while the native radio was disabled and both connection handles were
zero. Neither key changed the TANK before the explicit PC relay. After the
radio was enabled and the original connection stage returned, one native D
event changed FX from ON to OFF with **zero PC-forwarded CC messages**.
This identifies the additional route as the Plus's radio connection. Sending
another CC from the PC would toggle FX back, explaining the earlier apparent
failure. Both complete device baselines and the original enabled/connected
radio state were restored and verified afterwards.

The first radio-isolation attempt waited only for a nonzero connection handle
and did not observe the final CC effect. Its restoration passed. The successful
attempt also waited for the original client stage: connection establishment
alone does not establish that the MIDI path is ready. Both reports are retained.
These results establish neither a Windows MIDI routing bug nor direct USB HOST
delivery. They also do not establish arbitrary SysEx delivery over Bluetooth.

The `split-radio` preparation variant performs C/D USB calibration with
the native radio disabled, then reinstates the distinct TANK baseline. It
leaves the radio enabled and its saved flag at 0 for a consistent H handoff.
After H startup, the user must hold **B+C together for about three seconds**,
release them and confirm the Bluetooth status LED is off before pressing C
once, then D once. This physical step must be repeated after any restart in
the test; disabling the radio earlier in U is not sufficient evidence for H.

```powershell
python tools/host_142.py prepare --variant split-radio --session artifacts/host-142/isolated-session --execute
# TANK stays powered. Move its PC USB plug to Plus HOST; start Plus in H.
# Wait for startup, hold B+C ~3 s, release; confirm Bluetooth status LED off.
# Press/release C once, then D once. Return the powered TANK's cable to the PC.
python tools/host_142.py read-tank --session artifacts/host-142/isolated-session --output artifacts/host-142/isolated-readback --execute
# Return Plus to U.
python tools/host_142.py finish --session artifacts/host-142/isolated-session --output artifacts/host-142/isolated-plus-after --execute
```

The immutable plan records the original native radio state for `finish` and
`restore`. Those operations restore the radio through its native handler and
then restore the complete original Plus configuration. For this variant,
the Bluetooth-off marker is reported separately, like the current-switch
marker; a legitimate 0/1 change is not classified as payload corruption.
Readback of the marker after reboot still does not prove radio isolation
during the physical HOST sequence.

### Isolated single-SysEx HOST result

On 14 November 2026, `split-session-6` completed the physical sequence with
the powered TANK on Plus HOST, Plus in H, B+C held and released after startup
and the Bluetooth LED off. The user pressed C once, then D once, and returned
the powered TANK to the PC. `split-radio-measurement-1/report.json` records
**FX OFF and REV Mix 73**, with the entire 92-byte live patch matching the
independent target in two reads. The preset state and saved presets in RAM
and flash were unchanged. The original TANK snapshot was restored and checked.
This verifies the requested parameter effects under that physical HOST setup;
the cabling, LED and physical presses remain user observations rather than a
USB bus capture.

The Plus did not enumerate after returning from H to U, including one USB-C
reconnection. A complete restart restored access: select O, disconnect USB-C,
wait ten seconds, select U, then reconnect USB-C. The subsequent capture in
`split-radio-plus-after-H-2/report.json` is therefore after both the H test
and that additional U restart. RAM and saved storage differed from staging
only at Bluetooth-off byte 23651, changing from 0 to 1. All configuration
payloads, including SysEx, were intact. The original full configuration,
24-KiB saved region, bank state and native enabled/connected radio state were
restored and verified. Physical steps and the USB recovery are recorded in
`split-session-6/physical-procedure.json`.

### Paired SysEx with radio isolation

The `pair-radio` variant uses the same radio handling but sends FX OFF and
REV Mix 73 from one C press. D is left unchanged. USB calibration captures
both actual frames and forwards them consecutively before waiting for either
acknowledgement. It verifies the complete target patch, then resets and
verifies the distinct HOST baseline before the physical test. The hardware
sequence is otherwise the same as `split-radio`, with **C only** and no D
press. `read-tank`, `finish` and `restore` use the variant and original radio
state recorded in the immutable session plan.

On 14 November 2026, `pair-radio-session-1` passed this USB calibration with
both consecutive frames. The C assignment, staged TANK patch and target patch
were identical to the first failed paired HOST attempt. The only difference
in the staged Plus configuration was the Bluetooth flag at byte 23651,
explicitly set to 0 for the physical B+C transition after H startup.

The user confirmed H, the powered TANK on HOST, B+C held and released with
the Bluetooth LED then off, and one C press. After ten seconds, the powered
TANK returned to the PC. `pair-radio-measurement-1/report.json` records
**FX still ON and REV Mix still 47**. Two complete reads agreed, and the
entire live patch matched the staged baseline exactly. Neither requested
effect occurred. The preset state and saved presets in RAM and flash were
unchanged; the original TANK snapshot was restored and verified.

After a complete O/disconnect/wait/U/reconnect sequence, the Plus was read
and restored in `pair-radio-plus-after-H-1/report.json`. The complete RAM
configuration and 24-KiB saved region matched staging byte for byte: no stored
SysEx corruption was detected. Unlike the successful single-message test,
the Bluetooth-off flag was still **0** after restart, despite the reported
B+C action and dark LED. This difference remains unexplained; it prevents
treating the two sessions as proven to have identical native radio state in H.

The pinned B+C handler schedules `STATE+80 = 250` at `0x1e12a84`. When this
timer expires, it invokes save mode 3, which writes the 12-byte configuration
tail starting at 23640. USB configuration writes instead schedule the full
save through `STATE+82`. Thus the full USB save check does not validate the
physical B+C tail-save path. No native radio-state read was available during
the physical H test, and the post-restart flag alone cannot reconstruct it.

The complete original TANK snapshot, Plus RAM configuration, saved region,
bank toggles and native radio state were restored. A final independent read
of both devices after radio reconnection passed all checks, recorded in
`pair-radio-session-1/final-restoration-check.json`. Both temporary C/D
assignments are removed. The firmware image remains the unchanged 142 build.

The measured outcomes under the requested radio-off HOST procedure are:

| Assignment | Measured result |
| --- | --- |
| C: one SysEx setting REV Mix to 73 | Mix changed from 47 to 73 |
| D: channel-1 CC 0/value 0 | FX changed from ON to OFF |
| C: FX OFF SysEx followed by REV Mix 73 SysEx | Neither effect; full staged patch unchanged |

Radio isolation resolved the extra CC route during PC calibration. The
physical paired test still produced no target effect, but the radio-state
uncertainty must be resolved before assigning that failure specifically to
consecutive SysEx delivery. The single-message result supports using that
tested parameter command on its own; it does not establish all TANK parameters
or combinations. No firmware timing patch was made on the strength of this
result. The updated diagnostic and restoration checks passed all 78 automated
software tests; these are separate from the hardware measurements above.

## BLE monitoring with both devices disconnected from the PC's USB

`tools/tank_ble.py` reads the vendor configuration service `AE40`, using
`AE41` for requests and `AE42` for notifications. The payload is the native
checksummed `00 59` protocol, without the USB SysEx wrapper or BLE-MIDI
timestamps. The tool first requires the exact identity `TANK MINI_009`.
It permits only command `0x11` (identity) and bounded `0x23` reads of the
92-byte live patch, four-byte preset state and the 1,942-byte saved preset
banks in RAM and flash. It cannot send parameter, preset or firmware writes.

Hardware access needs `bleak` (tested with Python 3.12 and Bleak 3.0.2 on
Windows). Offline planning and the unit tests do not require Bleak. Each
output directory must be new; captures contain private device data and stay
under ignored `artifacts/`.

```powershell
python tools/tank_ble.py --output artifacts/ble-142/monitor
# With the PC Bluetooth radio already on:
python tools/tank_ble.py --output artifacts/ble-142/monitor --watch 600 --interval 0.5 --execute
```

The first invocation describes the plan without accessing hardware. The
second verifies two complete baseline snapshots, polls live/state changes,
then reads the complete final snapshot and compares it with the baseline.
Create the `stop` file inside the output directory to end polling early and
perform the final checks. `report.json` records results and interruptions;
`ble-configuration.jsonl` records attempted/submitted requests and replies;
`live-changes.jsonl` records changes. A submitted request is not itself proof
of a reply. After an interruption, a missing final snapshot must not be
treated as a successful restoration check.

`--adapter-fragment` checks the Windows default adapter without changing it.
`--scan-seconds` can extend discovery to 120 seconds for a physical handoff.
Optional `--pair` requests encrypted Windows pairing only after application
identity verification; it preserves the resulting association. Pairing may
store Bluetooth keys but does not write the application preset/configuration
ranges. Without this option the tool leaves pairing to the OS's existing
association. It never enables, disables or resets a PC radio.

### Hardware observation, 14 November 2026

The internal RZ616 radio initially found no advertisers; the motherboard had
no external antennas attached. The user added a Realtek USB Bluetooth 5.0
adapter and disabled RZ616. A later USB descriptor failure was resolved by
reconnecting the Realtek to another direct PC port. It then discovered both
the TANK and, later, the Plus.

Unpaired TANK reads worked but disconnected after approximately 30 seconds.
The Windows System log recorded BTHUSB events 29 (Security Manager Protocol
timeout) and 37 (incoming authentication rejected) at both disconnections.
The second session had obtained 48 live/state samples while Plus was in H.
An initial default Bleak pairing attempt after identity failed. A separate
WinRT pairing request with `ConfirmOnly` and `Encryption`, made before GATT
service access against the previously verified TANK address, returned
`PAIRED`. No TANK parameter write was sent.

For `tank-watch-paired-2`, the PC connected to the TANK while Plus was O and
the TANK stayed powered on HOST. After more than one minute of stable reads,
the user started Plus in H, with neither device cabled to the PC and no B+C
action after startup. The monitor remained connected. Three separate A
presses produced these measured MOD states: OFF, ON, OFF. The first press
therefore made no live change; the other two changed only the MOD byte. The
monitor completed **506 live/state samples and 1,115 submitted requests**.
The final complete live patch, preset state and saved RAM/flash banks all
matched the original baseline. MOD was restored physically with A.

During that same monitor, a read-only Plus probe identified
`FootCtrlPlus_142-dtnprt` and read 260 native-state bytes and the 12-byte
configuration tail through AE40. HOST port state was 1 and the output
callback was `0x1e119ae`, the audited HOST sender. Both native Bluetooth
enable fields were 1, the server connection handle was nonzero and the
client connection handle was zero. The configuration's Bluetooth-off flag
in RAM was also 1:
this directly confirms that the flag alone does not describe the running
radio state. The probe used five identity/read requests, with five replies,
and no configuration writes. It did not modify either firmware.

These observations establish usable wireless parameter monitoring and
read-only access to Plus state in H. They do not capture the USB bytes
between Plus and TANK, prove that USB traffic is mirrored on BLE-MIDI, or
resolve the previous paired-SysEx failure. The tested startup order keeps
the TANK connected to the PC over BLE before starting Plus in H; connecting
to a TANK already occupied by the Plus was not established. Evidence remains
local under `artifacts/ble-142/`.

## Automatic HOST comparison over BLE

`tools/ble_host_142.py` compares three configurations using the same messages
and live baseline. It connects to TANK first, then Plus in H. The PC sends
configuration requests over the two AE40 links and does **not** forward MIDI
between the devices. Native short-tap requests trigger the existing 142
switch handler and HOST sender; physical footswitches are not pressed.

Before any application write, the tool verifies the exact identities, the
complete running 142 application SHA-256 and the native save pointer. It
captures two matching complete configurations/saved regions and checks the
TANK live patch, preset state and saved banks. C and D must be unused, with
advanced mode and only page 1 active. Private recovery files are written
durably before the first change.

HOST routing checks require port state 1, output callback `0x1e119ae`, an idle
key scanner and a zero native Bluetooth client handle. In pinned 142 the
slot search at `0x1e0dacc..0x1e0daee` iterates exactly once: `STATE+70` is the
client handle, with index 0 and stage at `STATE+5`. Adjacent words at +72 and
+74 are other state fields, not extra connection handles. The nonzero server
handle belongs to the PC's diagnostic connection.

The exact write scope covers temporary C/D assignments, the original
configuration tail and bank toggles, native key requests, and four TANK live
fields: volume, FX enable, REV enable and REV Mix. Firmware, radio controls,
callbacks and saved TANK presets cannot be written. The TANK is temporarily
muted, FX enabled, REV disabled and Mix set to 47 before each case. The tool
verifies the Plus configuration in both RAM and saved storage before sending.
No TANK BLE request is made during the 0.8-second HOST delivery window.

```powershell
# Offline plan; no hardware access.
python tools/ble_host_142.py --output artifacts/ble-host-142/run
# Keep TANK powered on HOST. Connect its BLE link first with Plus O,
# then start Plus in H when the tool reports waiting_for_plus_H.
python tools/ble_host_142.py --output artifacts/ble-host-142/run --pair --execute
```

`--pair` handles Windows association for the verified TANK. The Plus also
requested authentication on this PC and unpaired reads ended after about
30 seconds with BTHUSB events 29 and 37. Pairing after opening GATT failed.
Pairing before GATT, immediately after discovering the Plus while the PC
already held the TANK connection, succeeded. For that case, supply a previous
report that verified the exact Plus identity and Bluetooth address:

```powershell
python tools/ble_host_142.py --output artifacts/ble-host-142/next --pair --pair-plus-from artifacts/ble-host-142/identity-run/report.json --execute
```

The evidence is checked against the discovered peer before pairing; its
identity and complete firmware are verified again after connecting. Once
paired, Windows can retain the Plus HID connection, so the Plus may disappear
from advertisement scans even while connected. The same option can reuse
Windows' paired-device cache after checking the name and association. This
fallback uses a narrowly pinned **Bleak 3.0.2 WinRT** internal cache key;
other versions/backends are rejected. Both Windows associations are retained.

`--preflight-only` captures the recovery baseline without application writes.
If an interrupted session needs recovery, use a new output directory and
`--restore-from <session-folder>`; recovery hashes and geometry are checked,
and the complete running firmware is verified before writing. Normal runs
restore both devices even if a case fails. A failure to restore one device
does not skip restoration of the other. A missing acknowledgement is never
treated as permission to retry a write. Reports distinguish a completed
diagnostic with a failed case from an interrupted diagnostic or failed recovery.

### Measured comparison, 14 November 2026

`artifacts/ble-host-142/matrix-5/report.json` completed all three cases:

| Case | HOST action | Measured TANK result |
| --- | --- | --- |
| Single | C: REV Mix 73 SysEx | FX stayed ON; Mix changed 47 -> 73 |
| Paired | C: FX OFF SysEx followed by REV Mix 73 SysEx in the same bank | FX stayed ON and Mix stayed 47; neither effect occurred |
| Separated | C: FX OFF SysEx; later D: REV Mix 73 SysEx | First FX OFF / Mix 47, then FX OFF / Mix 73 |

The entire 92-byte patch matched each successful target, and the paired case
matched its staged baseline exactly. The paired C assignment was byte-for-byte
identical to the earlier failed physical `pair-radio-session-1` assignment.
The HOST callback and zero native client handle were checked before and after
each trigger. Thus this comparison resolves the earlier native radio-state
uncertainty for this session. It does not by itself identify the faulty USB
packet or prove whether aggregation, framing or timing is responsible.

Restoration matched the complete original TANK live/state/saved RAM/saved flash
and Plus RAM configuration/saved region/bank toggles, with the key scanner idle.
There were 41 bounded Plus write attempts and 16 TANK live write attempts;
no firmware writes occurred. The diagnostic completed with `tests_passed=false`
because the paired case failed, while both restoration checks passed.
The firmware remains the exact upstream 142. The next firmware investigation
can now focus on how consecutive SysEx messages reach the HOST USB endpoint.

## Native HOST timing sweep

`tools/ble_host_timing_142.py` reuses the BLE identity/firmware checks, recovery
files and bounded write scope above. It measures delivery success at nominal
native key-event spacings of 0, 5, 10, 20, 30, 50 and 70 ms. It does not modify
the firmware or implement a sequence for a physical footswitch.

The 142 timer registration at `0x1e1449e` schedules `0x1e11cf2` every 5 ms.
For a released switch in state 1, the tap cave returns to `0x1e12006`; its
counter increments and invokes event 0 once it reaches 15, then clears the
key state. Advanced alternating mode 1 sends its bank on event 0 through
`0x1e13198..0x1e131c0`.

The diagnostic primes C and D with one four-byte write at `KEYS+4`:
`[14, 1, 14-N, 1]`. C therefore fires on the next timer tick and D fires N
ticks later, for N from 0 to 14. State 1 avoids the C+D combination, which
requires both keys in state 100. All other keys must be idle. A transport
that would split the four-byte request is rejected. Thus no second Bluetooth
write or PC sleep supplies the tested interval. Timer scheduling, the small
memory-copy window and USB queueing remain unmeasured sources of timing
uncertainty; these are **nominal event spacings, not USB wire timestamps**.

Each trial resets the muted TANK to FX ON / REV OFF / Mix 47, checks the
isolated native HOST route, triggers the sequence and makes no request to
either peer for 0.8 seconds after the acknowledgement. It then checks the
complete 92-byte target, unchanged preset state, key toggle parity and idle
scanner. Trial order alternates ascending/descending to reduce ordering bias.

```powershell
python tools/ble_host_timing_142.py --output artifacts/ble-host-142/timing-run --pair --pair-plus-from artifacts/ble-host-142/identity-run/report.json --execute
```

`--intervals-ms` accepts distinct multiples of 5 between 0 and 70;
`--repetitions` accepts 1..20 and defaults to 10. The original concatenated
pair is also tested up to three times before the separated sweep. Recovery
uses the same `--restore-from` files and procedure as `ble_host_142.py`.

### Measured timing, 14 November 2026

`artifacts/ble-host-142/timing-1/report.json` completed 73 trials using the
unchanged upstream 142 application and the same direct HOST connection:

| Delivery | Successful complete targets |
| --- | --- |
| Concatenated pair in one C bank | 0 / 3 |
| Separate C/D banks, same timer tick (0 ms nominal) | 0 / 10 |
| Separate C/D banks, 5 ms nominal | 10 / 10 |
| Separate C/D banks, 10 ms nominal | 10 / 10 |
| Separate C/D banks, 20 ms nominal | 10 / 10 |
| Separate C/D banks, 30 ms nominal | 10 / 10 |
| Separate C/D banks, 50 ms nominal | 10 / 10 |
| Separate C/D banks, 70 ms nominal | 10 / 10 |

Every successful trial produced FX OFF and Mix 73 with all other live bytes
unchanged. Every failed trial left the staged patch exactly unchanged. The
HOST callback remained `0x1e119ae` with no native Bluetooth client connection.
Both complete application configurations and saved regions were restored and
verified, including TANK preset state and Plus key toggles. The run made 102
bounded Plus write attempts, 158 TANK live writes and zero firmware writes.

Five milliseconds is the smallest **tested nominal** successful interval;
neither shorter positive intervals nor a true minimum were measured. Ten
trials per setting do not establish reliability under every load or for other
messages. A **20 ms inter-message gap** is the proposed initial firmware value
for a single-trigger sequence, leaving margin above the shortest tested case.
It is a design choice to validate after implementation, not a measured
press-to-sound latency or proof that a firmware patch already works.

### Retained HOST transmit buffer

The timing diagnostic also reads 64 RAM bytes at `0x13280`. The audited buffer
selector `0x1e11932` chooses this address for HOST port 1, and sender
`0x1e119ae` passes it to the USB-MIDI packet builder. No MMIO access or buffer
write is permitted. Reading the retained bytes after delivery is not an
on-wire capture and does not recover a transfer length or timestamp. Bytes
beyond the compared message extent can be leftovers from earlier transfers.

For all 13 failing trials, the first 60 retained bytes contained both expected
messages in order with their expected Code Index Numbers and MIDI bytes. For
all 60 successful separated trials, the first 32 bytes contained the second
message. The last event was `05 F7 68 02`: its two unused bytes were nonzero
instead of the zero padding required by [USB-MIDI 1.0 section 4, tables 4-1
and 4-2](https://www.usb.org/sites/default/files/midi10.pdf). This appeared in
both successful and failing trials, so it does not by itself explain the
failure. Reports distinguish exact bytes, MIDI/CIN content and unused padding;
the initial capture retains its original exact-prefix comparison.

These results support separating complete messages at the HOST transmit
boundary and testing a small gap. They do not establish which device or
USB parsing stage causes the combined-transfer failure. The existing 013
pacing source already attempted boundary splitting and a delay, but its
unsuccessful hardware test is not evidence that the same change works on 142;
any new candidate still needs validation on the current reproducible setup.
