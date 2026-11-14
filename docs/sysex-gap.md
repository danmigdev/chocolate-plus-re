# Global configurable HOST SysEx gap (experimental 150)

One footswitch bank can contain two complete `F0 ... F7` messages. The proposed
150 extension splits them into separate HOST USB transfers and uses **one global
interval for all switches and banks**. Its initial value is **20 ms**, editable
from a Windows PC without rebuilding or reinstalling the firmware after the
extension has been installed. There are no per-bank overrides.

This is an **offline-built experimental candidate**, not an installed or
hardware-validated release. The tested pedal still runs exact upstream 142.
The [142 timing experiments](tank-mini-142.md#native-host-timing-sweep) justify
trying this approach; they did not test this new firmware implementation.

## Behavior

| Setting | Meaning |
| --- | --- |
| Initial / factory-reset value | 20 ms |
| Accepted values | 0 through 1000 ms, in steps of 10 ms |
| Scope | All HOST SysEx messages, across switches and banks |
| Zero | Separate messages into transfers, without an added task delay |
| Persistence | Device USRFLASH; independent of bank contents |
| Editing | `tools/sysex_gap.py` over USB MIDI, after installing the candidate |

The first message has no added initial wait. After submitting a packet ending
in SysEx `F7`, the MIDI routing task waits the configured number of native
10 ms scheduler ticks before it can submit another packet. Three messages thus
use the same interval twice between their submissions. The task also waits
after the final SysEx; another queued command, including CC/PC, can therefore
wait behind it. Continuation packets of a longer SysEx do not introduce pauses.
The packet boundary change applies only when the native output callback is
the HOST sender. USB device mode and Bluetooth packet builders keep their
existing behavior.

The millisecond values are **nominal scheduler delays**. Tick phase, USB
completion and device processing affect actual timing. They are not a measured
minimum wire interval, nor a promise of total press-to-sound latency. The
single-footpress implementation and these timing values still require the
hardware test below.

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
then compiles its PI32v2 module and hooks. The tests cover 79,529 boundary/record
cases plus configuration preservation, NOR programming rules, defaults, reset,
uninitialized VM, forwarding of ordinary configuration access and zero delay.

In PowerShell, from the repository:

```powershell
python tools/build_gap_150.py --baseline firmware/out/142-baseline/FootCtrlPlus_142-dtnprt.fwsc --module firmware/out/sysex-gap-150 --output firmware/out/gap-150-candidate
python -m unittest tests.test_sysex_gap tests.test_build_gap_150 -q
```

Use a new output directory on each build. The result is a compact image,
`FootCtrlPlus_150-gap.compact.bin`, and `manifest.json`. This command performs
no device I/O and produces no OTA installer. **The 013-to-142 installation
tools must not be reused with this image.** A 142-to-150 migration and rollback
plan must first be reconstructed from a fresh physical backup.

## Configure from Windows after installation

Keep the Plus in U, connected through USB-C, with other MIDI editors closed.
The command identifies `FootCtrlPlus_150-gap` and checks both the application
and extension against the supplied build manifest before accessing the new
setting endpoint. It refuses 012, 013 and 142.

```powershell
python tools/sysex_gap.py --build firmware/out/gap-150-candidate/manifest.json get
python tools/sysex_gap.py --build firmware/out/gap-150-candidate/manifest.json set 30 --backup artifacts/sysex-gap/set-30
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

Native configuration consists of 23,652 saved bytes followed by eight bytes
`usrdata\0` inside 24 KiB USRFLASH at physical `0x39000`. The existing capture's
remaining 916 bytes are erased. The extension puts its 16-byte record immediately
after the marker, at physical `0x3ec6c`. It does **not** extend the native RAM
configuration into adjacent globals.

The record contains `SGAP`, schema 1, step 10, a little-endian gap and its
16-bit complement, maximum 1000, default 20, and CRC-16/XMODEM over its first
14 bytes. Missing/invalid records use 20 ms. A new value invokes native full
save to erase before writing, never a NOR 0-to-1 overwrite. Native mode 1
full saves preserve a valid record; mode 2 factory reset erases it; mode 3
tail saves do not reach it. Boot mode 0 follows the original load path.
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

## Remaining hardware validation

1. Prepare a fresh physical 142 backup, bounded migration and verified rollback.
2. Check candidate boot, USB enumeration, Bluetooth connections and code hashes.
3. Change the global value, edit an ordinary bank, wait for native save, then
   verify both after U/H changes and power cycles. Test reset on backed-up data.
4. Use the established TANK BLE observation with the direct HOST cable. Trigger
   two SysEx from **one** temporary bank/event and check the complete live state
   at zero and several positive gaps, then restore both device configurations.
5. Measure actual timing where the instrumentation permits it, distinguish
   task ticks from USB wire timing, and check consecutive presses/long SysEx.

The offline tests verify logic and image construction. They do not establish
successful boot, hardware persistence or reliable delivery on the TANK.
