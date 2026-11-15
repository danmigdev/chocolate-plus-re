# Global configurable HOST SysEx gap (experimental 150)

One footswitch bank can contain two complete `F0 ... F7` messages. The
150 extension splits them into separate HOST USB transfers and uses **one global
interval for all switches and banks**. Its initial value is **20 ms**, editable
from a Windows PC without rebuilding or reinstalling the firmware after the
extension has been installed. There are no per-bank overrides.

This is an **experimental build tested on one Chocolate Plus / TANK Mini**.
The pinned 1-ms candidate was installed and checked on 15 November 2026;
the hardware record below separates delivery tests from timing measurement.
The default firmware build still reproduces upstream 142. The earlier
[142 timing experiments](tank-mini-142.md#native-host-timing-sweep) remain the
comparison baseline. No TANK firmware was modified.

## Behavior

| Setting | Meaning |
| --- | --- |
| Initial / factory-reset value | 20 ms |
| Accepted values | 0 through 1000 ms, in steps of 1 ms |
| Scope | All HOST SysEx messages, across switches and banks |
| Zero | Separate messages into transfers, without an added task delay |
| Persistence | Device USRFLASH; independent of bank contents |
| Editing | `tools/sysex_gap.py` over USB MIDI, after installing the candidate |

The first message has no added initial wait. After submitting a packet ending
in SysEx `F7`, the MIDI routing task waits against the native interpolated
clock before it can submit another packet. The interval is **not rounded to
10 ms**: 1, 5, 19, 20 and 21 ms are distinct settings. Three messages use the
same interval twice between their submissions. The task also waits
after the final SysEx; another queued command, including CC/PC, can therefore
wait behind it. Continuation packets of a longer SysEx do not introduce pauses.
The packet boundary change applies only when the native output callback is
the HOST sender. USB device mode and Bluetooth packet builders keep their
existing behavior.

The OS still sleeps in 10 ms ticks for the coarse part, but the deadline uses
the native clock's 0.5 ms units. Only the final at-most-10-ms remainder is
actively polled, with interrupts enabled between short clock reads. USB IRQs,
key timers and task preemption can continue. No hardware timer or global OS
tick frequency is reprogrammed.

An extra 0.5 ms clock unit covers initial-sample quantization, so the clock
model does not return before the requested duration. The tests allow less than
0.5 ms plus polling overhead above that duration without scheduling delays.
Actual USB timing and total press-to-sound latency are still **unmeasured on
hardware**; preemption, configuration reads, transfer completion and TANK
processing can add latency. A 1 ms setting step is not a claim of 1 ms wire
accuracy under every load.

## Build locally

First reproduce upstream 142 using the existing `firmware/build.py`. The new
builder requires that exact pinned FWSC and does not change the default build.
Vendor firmware, disassembly, generated binaries and hardware captures stay
outside version control.

In WSL, with the extracted JieLi toolchain:

```bash
export JL_TOOLCHAIN=/root/jl-tc/jieli-linux-toolchains-20250324.1
bash firmware/patch_src/build_sysex_gap.sh
```

This runs the actual freestanding C with address/undefined-behavior sanitizers,
then compiles its PI32v2 module and hooks. The tests cover 205,841
boundary/record/deadline cases plus configuration preservation, NOR programming
rules, defaults, reset, uninitialized VM and ordinary configuration access.
Every integer setting is checked across multiple initial clock/OS tick phases
and the native clock wrap. Consecutive settings must give distinct waits;
zero, preemption, late wake-ups, bounded final polling and older saved records
are also tested.

In PowerShell, from the repository:

```powershell
python tools/build_gap_150.py --baseline firmware/out/142-baseline/FootCtrlPlus_142-dtnprt.fwsc --module firmware/out/sysex-gap-150 --output firmware/out/gap-150-1ms-candidate-1
python -m unittest tests.test_sysex_gap tests.test_build_gap_150 -q
```

Use a new output directory on each build. The result is a compact image,
`FootCtrlPlus_150-gap.compact.bin`, and `manifest.json`. This command performs
no device I/O and produces no OTA installer. The separate Windows procedure
below accepts the pinned one-millisecond candidate. The 013-to-142 plan format
does not accept this image.

## Windows installation of the pinned candidate

`prepare_gap_150.py` accepts only exact upstream 142 as its source and the
162,304-byte 1-ms candidate with SHA-256
`73e7a6013551812d2d0b1eb7d5c60d88231ff34de3b6adecb9d8659691aa7a20`.
It requires two matching fresh physical reads from `br25_usb.py`, checks that
the extension's new extent is erased, and reconstructs the complete target.
The seven changed sectors are `0x10000`, `0x12000`, `0x13000`, `0x19000`,
`0x1a000`, `0x27000`, then the directory/CRC sector `0x1000` last. Native VM,
Bluetooth data, calibration, configuration and all bytes from `0x27a00` onward
are preserved, including the unchanged part of the final erased sector.

Keep the Plus in U with a working USB data cable, its HOST port empty and other
MIDI editors closed. The [Windows bootloader reader](tank-mini-142.md#windows-usb-migration)
still requires the pinned vendor RAM loader and administrator access.

```powershell
python tools/native_snapshot.py --profile 142 --output artifacts/migration-gap-150/boot-entry --enter-boot
# After BR25 UBOOT1.00 enumeration, in an administrator process:
python tools/br25_usb.py --loader <br25loader.bin> --output artifacts/migration-gap-150/physical-before --execute
python tools/prepare_gap_150.py --backup artifacts/migration-gap-150/physical-before --candidate firmware/out/gap-150-1ms-candidate-1/FootCtrlPlus_150-gap.compact.bin --output artifacts/migration-gap-150/plan
python tools/install_gap_150.py --plan artifacts/migration-gap-150/plan
python tools/install_gap_150.py --plan artifacts/migration-gap-150/plan --restore
# Only with the same verified RAM-loader session still running:
python tools/install_gap_150.py --plan artifacts/migration-gap-150/plan --execute
```

The installer shares the bounded, journaled sector transport with the earlier
142 migration, but uses a separate exact-image plan validator. It rereads the
complete flash twice before writing, blank-checks every erase twice, verifies
every programmed page and sector, and requires two final complete matching
reads. It never resets the device or retries an uncertain erase/program.

`--restore --execute` restores the original seven sectors while the RAM loader
remains reachable and the rest of flash still matches the backup. This is not
proof of recovery from a nonbooting app after power loss. Only after
`physically_verified_restart_required`, perform a full restart in U and check:

```powershell
python tools/native_snapshot.py --profile 150 --gap-plan artifacts/migration-gap-150/plan --output artifacts/migration-gap-150/native-after
python tools/sysex_gap.py --build firmware/out/gap-150-1ms-candidate-1/manifest.json get
```

The new native profile pins both the complete application and the 928-byte
extension. Boot entry for 142 and this 150 is audited separately: the USB
handler at `0x1e10298` compares the eight USB-MIDI bytes against its six-byte
prefix, final `F7` and command `7D`, then branches at `0x1e102be` to
`0x1e10438`. That call reaches RAM `0x1c934`; its 28 bytes are identical to the
previously verified 013 boot routine. A fresh hash check of that relocated
routine, the complete application, stable saved configuration and (for 150)
the extension precedes the single boot request. The MIDI input is closed
before sending it.

## Configure from Windows after installation

Keep the Plus in U, connected through USB-C, with other MIDI editors closed.
The command identifies `FootCtrlPlus_150-gap` and checks both the application
and extension against the supplied build manifest before accessing the new
setting endpoint. It refuses 012, 013, 142 and the earlier 10-ms build manifest.

```powershell
python tools/sysex_gap.py --build firmware/out/gap-150-1ms-candidate-1/manifest.json get
python tools/sysex_gap.py --build firmware/out/gap-150-1ms-candidate-1/manifest.json set 21 --backup artifacts/sysex-gap/set-21
```

`set` saves local copies of the previous setting, full native configuration and
saved region before sending one bounded request. It checks the setting and
the complete saved region after the ACK and verifies unchanged bank bytes.
It does not retry an uncertain write or claim that a power cycle was tested.
An already-effective value causes no write. Unknown footer data, unsaved or
changing configuration, a code mismatch, or a failed readback stops the command.

The setting is not included in the existing 23,646-byte bank/editor export.
Native factory reset returns it to 20 ms. An older firmware's full configuration
save erases the extension footer; returning to 150 then uses the default again.

## Native integration and storage

The exact 142 native image is the binding reference. No new mutable static
RAM is allocated, no VM record ID is claimed, and no native code cave is reused.

| Hook / binding | Purpose |
| --- | --- |
| `0x1e0ff54` | Preserve `r8 += r7; r5 += r7`, then stop the HOST packet builder at a complete SysEx |
| `0x1e11a16` | Preserve the native IRQ-enable call, then inspect the submitted packet and delay in the routing task |
| `0x1e125d2` | Wrap native configuration saves; trampoline reproduces the displaced four-byte prologue |
| `0x1e18b44`, `0x1e18b48` | Replace configuration read/write callback pointers; delegate ordinary accesses |
| `0x1e1506e`, `0x1e15090` | Native VM mutex lock/unlock, shared by setting reads, changes and full-save preservation |
| `0x1a25d` bit 0 | Native VM initialization flag; do not use its mutex before initialization |
| `0x1e0cd9c`, `0x1e0cd8e` | Native flash read/write, with its separate flash lock |
| `0x201c606` | Native interpolated clock, in half-millisecond units |
| `0x201c54c`, `0x201c576` | Native IRQ nesting wrappers around one clock sample only |

The builder's SysEx staging path includes CIN 5/6/7 end events: `0x1e0fffe`
routes those to staging, sets the flush flag, and copies the completed chunk
through `0x1e0ff3e`. The hook examines the last event's CIN and the corresponding
`F7` byte; it does not assume unused USB padding is zero. Non-SysEx events and
the special configuration-reply path retain native handling.

The application sets the memory-type-4 descriptor at `STATE+976` to
`0x1e18b40` at `0x1e14496..0x1e1449a`, before registering the key timer.
The descriptor's `+4`/`+8` slots are the original read/write callbacks above.

The after-send hook runs only after the original sender has submitted the
transfer and re-enabled IRQs. Empty/disconnected sender return paths bypass it.
The routing task invokes the output callback at `0x1e13fe2`/`0x1e14030`; the
key scanner does not sleep. The linked IRQ target is checked against the
original encoded branch, including the processor's address wrap.

The startup copy at `0x1e00162..0x1e0017c` places 5,604 bytes from flash
`0x1e24aa4` into RAM `0x1bd60`. In that image, clock routine `0x1c606..0x1c65c`
combines the OS tick counter at `0x11298` (masked to 27 bits) with the hardware
tick counter/period registers at `0x10f0f0`/`0x10f0f4`. It uses 20 units per
10 ms OS tick, with cached monotonic correction at `0x19310`. Native getter
`0x1c65e` calls it and shifts right by one to obtain milliseconds. The existing
user-timer scheduler already uses these routines.

The extension calls the raw half-ms routine through an assembly wrapper that
preserves the return value and restores IRQs immediately. Its deadline is
`2 * milliseconds + 1` half-ms units. It sleeps while more than 20 units remain,
then polls. Elapsed time handles the native `0xa0000000`-unit wrap explicitly,
rather than assuming a 32-bit unsigned wrap. ELF validation checks all three
clock-wrapper call targets and register preservation. This audit establishes
the intended units and integration, not measured timing on the pedal.

Native configuration consists of 23,652 saved bytes followed by eight bytes
`usrdata\0` inside 24 KiB USRFLASH at physical `0x39000`. The existing capture's
remaining 916 bytes are erased. The extension puts its 16-byte record immediately
after the marker, at physical `0x3ec6c`. It does **not** extend the native RAM
configuration into adjacent globals.

The record contains `SGAP`, schema 1, step 1, a little-endian gap and its
16-bit complement, maximum 1000, default 20, and CRC-16/XMODEM over its first
14 bytes. Missing/invalid records use 20 ms. A new value invokes native full
save to erase before writing, never a NOR 0-to-1 overwrite. Native mode 1
full saves preserve a valid record; mode 2 factory reset erases it; mode 3
tail saves do not reach it. Boot mode 0 follows the original load path.
Saved records from the earlier step-10 candidate remain readable when their
value is a multiple of ten. Full saves rewrite them in the step-1 format;
the current configuration endpoint accepts only the step-1 format for writes.
The mutex order is native VM lock followed by native flash lock; no added
sleep is performed while those locks are held. Native reset/save concurrency
and power-loss behavior remain hardware validation items. This follows the
native erase/save model; it is not an atomic power-failure-safe configuration
transaction.

Commands `0x23`/`0x22`, memory type 4, expose the canonical 16-byte record at
virtual offset `0x53580000`. Only an exact 16-byte write with valid fields and
CRC changes it. Other accesses inside that reserved virtual 4 KiB page return
zero or are ignored. Ordinary addresses still call the original callbacks.
The stock protocol's write ACK cannot convey callback failure, so readback is
mandatory. Never probe this offset on unpatched firmware.

The new code starts at `0x1e265f0` in an added sequential `sysexgap.bin` resource,
after the existing resources. The compiler/linker reject added data/BSS, missing
symbols and oversized extensions. Packaging checks original hook bytes, ELF
exports, ABI addresses, trampoline, source fingerprints, directory and data
CRCs, the refreshed app MD5 and the complete set of changed bytes. The compact
image stays below `0x28000`, retaining the native VM banks at `0x28000`/`0x29000`.
Boot prefix, BTIF, EXIF, USRFLASH and all existing application payload outside
the specified hooks/identity/greeting are retained. The protected-image and
nominal VM reservation boundaries grow to include the extension.

## Hardware record, 15 November 2026

Two fresh physical 256-KiB backups matched. The plan changed seven application
sectors and 106 pages. Every programmed sector passed its two readbacks. A
Windows USB error (`WinError 31`) interrupted the subsequent complete-image
verification; no erase or program command was retried. The still-running RAM
loader then supplied two new complete reads, both exactly matching the target.
The separate `physical-install-verification.json` records that successful
verification without overwriting the original interrupted-install report.

After a full power cycle in U, the unit identified as `FootCtrlPlus_150-gap`.
The complete native application and extension matched their pinned hashes;
configuration RAM and saved USRFLASH matched the pre-installation backup.
The setting initially read 20 ms. A USB change to 21 ms was saved and its
complete USRFLASH read back twice, with all native configuration bytes intact.

After another confirmed full power cycle, this time into H with TANK on HOST,
the PC connected to both devices over BLE, without either device cabled to
the PC. The setting still read 21 ms, and the complete 23,652-byte native
configuration matched the earlier U capture exactly. Native HOST output was
active and the Chocolate Bluetooth client had no connected peer.

`tools/ble_gap_150.py` then assigned two complete messages to the same temporary
C bank: FX OFF and REV Mix 73. Each automatic trial triggered one native C
event, checked one toggle transition, and read the entire TANK live patch and
preset state after delivery. The starting state was restored between trials.

| Configured global interval | Both changes applied | Trigger |
| --- | --- | --- |
| 21 ms | 5 / 5 | One native C event |
| 20 ms | 5 / 5 | One native C event |
| 1 ms | 5 / 5 | One native C event |
| 0 ms | 5 / 5 | One native C event |

The global setting was changed over BLE with a single complete 16-byte record
and verified against the saved region. Ordinary bank saves preserved it. Each
group restored the original Chocolate assignments, key state and TANK live
patch; TANK saved RAM/flash and preset state were verified unchanged.

A final user-confirmed physical press/release of C at 20 ms also applied both
changes. The native C toggle changed exactly once and the complete TANK live
patch matched the target. Both devices' temporary settings were restored and
verified again. The session completed with **150 installed, a global 20-ms
setting, original Chocolate assignments and original TANK live/saved values**.
The full report records 20 automatic successes and one physical success.

Zero means no extra timer wait, **not zero elapsed USB time**. These results
show successful delivery with message splitting alone in this setup; they do
not provide transfer timestamps or a general reliability guarantee. Neither
the Bluetooth readback nor native event ACK measures press-to-audio latency.

Upstream 142 already supports storing multiple complete SysEx in one bank;
the change is at the HOST transfer boundary. In the audited path, its packet
builder can put those distinct MIDI messages in one USB transfer. The 150
hook ends the transfer at the first complete SysEx and sends the next one
separately. This is a compatibility change for the tested TANK behavior, not
a new SysEx message format or proof that multiple SysEx in one USB transfer
are inherently invalid.

Private evidence is in `artifacts/migration-gap-150/` and
`artifacts/gap-host-150/session-1/`. These files and firmware images are not
committed. The offline regression suite passed 133 tests, including the bounded
migration/rollback simulation, failure restoration and BLE write guards.

## Repeat the HOST delivery test

Use a fresh output directory. The PC connects to TANK first while Chocolate is
off; then start Chocolate in H with TANK on HOST. The prior pairing report is
evidence for the same physical Chocolate address, not proof of current code:
both the 150 application and extension are verified again before writes.

```powershell
python tools/ble_gap_150.py --output artifacts/gap-host-150/new-session --pair --pair-plus-from artifacts/ble-host-142/timing-1/report.json --expect-initial-gap 20 --intervals-ms 21 20 1 0 --repetitions 5 --physical-press --execute
```

`--physical-press` stages one final pair at 20 ms and waits for
`waiting_for_physical_C`. Press/release C once, then create the indicated
`physical-C-confirmed` file. The script makes no TANK BLE queries during that
waiting window. It checks the resulting state and restores the temporary
assignments and live values. A stale confirmation file is refused. Without
`--execute`, the command only describes its operations. The final global
setting is 20 ms.

## Further validation

Factory reset and save/reset concurrency, additional hardware units, long
SysEx, sustained Bluetooth load and actual USB wire/audio timing remain to be
tested. In particular, delivery at adjacent settings such as 20/21 ms does not
by itself measure their physical timing difference. Recovery from a nonbooting
application after power loss remains unverified.
