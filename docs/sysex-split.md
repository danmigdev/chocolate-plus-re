# Separate HOST SysEx transfers (143-split)

143-split is based on exact upstream 142. It stops the HOST USB-MIDI packet
builder after a complete SysEx message, allowing the next message to be sent
in the next transfer. **There is no added delay and no configurable interval.**
The user selected 143 as this fork's build number. It is not an upstream
release announcement. The default `firmware/build.py` still reproduces 142
for comparison; the additional builder creates the candidate described here.

The candidate has passed 129 Python tests and the C checks described below.
On 15 November 2026 it replaced the experimental 150 on the physical controller:
two complete physical flash reads matched the installation target, and the
application and module hashes matched again after a cold boot. The exact 143
then passed **10 automatic paired-message trials and one physical C press**
on the direct HOST connection, with both original device states restored.
The earlier experimental
150 and its hardware results are recorded in
[the previous implementation](https://github.com/danmigdev/chocolate-plus-re/blob/c9a29e9d8a462d8de3c71efe356680bc73f860e2/docs/sysex-gap.md).
No TANK Mini firmware is changed by this work.

A focused review branch, `pr/143-host-sysex-split`, starts at upstream `dev`
(`16a405f`) and contains the standalone firmware builder, patch sources,
build tests and firmware documentation. It produces the identical compact
image. [Upstream pull request #2](https://github.com/dtnprt/chocolate-plus-re/pull/2)
targets `dev` and includes that hardware validation.

## What changes

Upstream 142 already accepts multiple complete `F0 ... F7` messages in one
switch bank. The stock editor's format has one 128-byte SysEx area per bank:
a length byte and up to 127 bytes of message data. It does not have an
independent SysEx buffer for every MIDI command row. A configuration row or
message boundary does not require the USB sender to finish its transfer.

The 142 builder can combine multiple complete SysEx in its 64-byte output
buffer. The observed TANK setup failed with the concatenated pair and with
two separate switch events in the same timer tick. Separate events at 5 ms
nominal spacing passed. Later, 150 with splitting enabled and its added wait
set to zero passed five paired trials. This motivates removing the timer
and configuration machinery, but does not replace testing the exact 143 code.
See [the original measurements](tank-mini-142.md#native-host-timing-sweep).

The only executable hook relative to 142 is at `0x1e0ff54`. It preserves the
displaced pointer/length updates, checks the completed chunk's last event,
and returns the accumulated output length if all of these hold:

- The native output callback is the HOST sender, `0x1e119ae`.
- The chunk length is 4..64 bytes and a multiple of four.
- Its last event has CIN 5, 6 or 7, with `F7` in the final valid MIDI byte.

Continuation chunks and non-SysEx events keep the native builder's behavior.
The condition reads the valid byte rather than assuming unused padding is
zero. USB device and Bluetooth output callbacks do not take the new return.
The module occupies 116 bytes, with no writable data or new RAM allocation.

Relative to experimental 150, the timer, clock/interrupt calls, flash record,
configuration read/write callbacks and replacement saver have been removed.
The original 142 sender and configuration functions are restored byte for
byte. There is no interval get/set tool or virtual setting endpoint in 143.
The existing assignments and stock editor configuration format are unchanged.

## Compatibility and pull request scope

Grouping MIDI events in USB transfers is legitimate; 142's grouping is not
itself a USB-MIDI protocol violation. Message boundaries are encoded by MIDI
and the event CIN, not by the requirement to use a new USB transfer for each
message. See the [USB-MIDI 1.0 specification, sections 4 and 5](https://www.usb.org/sites/default/files/midi10.pdf).

Splitting may increase transfer overhead and affect throughput or scheduling
with dense SysEx traffic. Only the Chocolate Plus / TANK Mini setup has been
tested so far. The change should be reviewed as a compatibility workaround;
additional receivers, long SysEx and high message rates need regression
testing. No USB wire or audio timestamps have been measured: zero added wait
does not mean zero transfer time or zero press-to-sound latency.

## Build and offline validation

First reproduce upstream 142 with `firmware/build.py` using the verified
vendor 012 input. Vendor firmware, generated binaries and captures are local
only and are not redistributed.

```bash
export JL_TOOLCHAIN=/root/jl-tc/jieli-linux-toolchains-20250324.1
bash firmware/patch_src/build_sysex_split.sh
```

This compiles and tests the actual C with address and undefined-behavior
sanitizers (65,602 exhaustive cases plus continuation/padding checks), then
builds the PI32v2 module and its single hook. In PowerShell:

```powershell
python firmware/split_143.py --baseline firmware/out/142-baseline/FootCtrlPlus_142-dtnprt.fwsc --module firmware/out/sysex-split-143 --output firmware/out/split-143-candidate-2
python -m unittest discover -s tests -q
```

The builder verifies source hashes, ELF sections, native bindings, the hook
and return destinations, every changed byte, CRC/MD5 metadata and unchanged
native VM geometry. The 161,536-byte compact candidate SHA-256 is
`963418009370c91927e85847968bae25157a0812c69bdec65c5654383b59221c`.
Its application SHA-256 is
`973b9817029e2863df5b925bf8547691ef4341c1ed5c0417daa476fb0b9a1433`;
the module SHA-256 is
`0cd5d58e7017f2f3ff855cddd1a77f6235b9219c5de0ce385c5ac6483d517593`.
This builder produces a compact image, not an OTA installer.

## Windows migration

`native_snapshot.py` supports read-only capture/verified boot entry for 142,
143 and the pinned legacy 150. The legacy profile grants no interval access.
Use fresh matching physical reads from `br25_usb.py` before preparing a plan:

```powershell
python tools/prepare_split_143.py --backup artifacts/migration-split-143/physical-before --candidate firmware/out/split-143-candidate-2/FootCtrlPlus_143-split.compact.bin --output artifacts/migration-split-143/plan
python tools/install_split_143.py --plan artifacts/migration-split-143/plan
```

The plan accepts only exact upstream 142 or the pinned installed 150. It
preserves boot, native VM banks, Bluetooth/calibration and complete USRFLASH.
Growing from 142 requires erased extension space. Shrinking from 150 erases
only the known obsolete application tail. The directory/CRC sector is written
last. `--execute` performs the bounded plan in the already running RAM loader;
`--restore` restores that device's complete original sector contents. Both
use double readback and never retry uncertain writes automatically. Independent
cold recovery from a nonbooting application has not been verified.

### Recorded installation on 15 November 2026

The migration used two identical 256 KiB physical backups of the installed 150.
Seven sectors were updated, with the directory sector last; two complete final
physical reads matched the target. After power-off and USB disconnection, the
controller booted as `FootCtrlPlus_143-split`. The complete application and
116-byte module matched the hashes above. RAM configuration and the complete
saved user region matched the pre-installation backup.

The Bluetooth indicator had a saved off flag despite the native radio already
being enabled. `tools/radio_indicator_143.py` verified the exact 143 image and
idle keys, backed up RAM and saved configuration, then changed only configuration
byte 23651 from 1 to 0 in one bounded write. The unchanged native saver also
retired the validated legacy 150 footer. Two complete saved-region reads matched
the expected result. This is a configuration correction, not another firmware
patch; it does not establish that the PC can open the Chocolate's GATT service.

Initial GATT connections timed out despite that active radio. A later USB
diagnostic showed an incoming native connection while Windows reported the
Chocolate disconnected. That connection remained present with the PC radio
off, then cleared after the user switched off another nearby Bluetooth device.
With that device off, one native radio off/on cycle over USB let the PC reserve
the TANK link first, associate the Chocolate and read `FootCtrlPlus_143-split`
over AE40. Both native radio transitions completed, and the full original
configuration and TANK snapshot were verified unchanged. No additional firmware
patch was needed. Keep other automatically reconnecting Bluetooth peers off
while using the two-PC-link acceptance procedure below.

## Direct HOST acceptance

Connect the PC to TANK via Bluetooth while Chocolate is off, then start
Chocolate in H with TANK on HOST. Both devices are uncabled from the PC.

```powershell
python tools/ble_split_143.py --output artifacts/split-host-143/session-1 --pair --pair-plus-from artifacts/ble-host-142/timing-1/report.json --repetitions 10 --physical-press --execute
```

The tool verifies the exact 143 application and module before any writes.
It backs up both devices and temporarily configures unused C with FX OFF
and REV Mix 73. Each trial triggers one native C event and checks the complete
TANK live patch, preset state and Plus toggle state. `--physical-press` also
waits for one user-confirmed physical press. Original assignments and all
TANK state are restored and checked after each group, including failures.

When migrating from 150, the removed 16-byte setting is initially preserved
by the installer. Before testing, the tool validates its signature/checksum
and erased surroundings, creates a separate normalized recovery baseline,
then writes unchanged configuration bytes once. The original native saver
erases this obsolete record without changing assignments. Unknown trailing
data is refused. Original backups are retained; subsequent recovery uses
the reported normalized directory. No old virtual setting request is sent.

Delivery readback is not a passive USB traffic capture or timing measurement.

### Exact 143 results, 15 November 2026

`artifacts/split-host-143/session-4/report.json` completed with the exact
application and module above. Neither device had a USB connection to the PC;
the PC used two BLE configuration links. The Plus reported HOST port 1,
output callback `0x1e119ae` and no native Bluetooth client connection.

| Trigger for the C bank containing both messages | Complete targets matched |
| --- | --- |
| One native C event, generated automatically | 10 / 10 |
| One user-confirmed physical C press | 1 / 1 |

Each trial started with FX on and REV Mix 47, then reached FX off and REV Mix
73. All 92 live-patch bytes matched the expected result; no other live parameter
changed during delivery, preset state remained unchanged, and the C toggle
changed exactly once. The firmware has no added wait and no interval setting.

After each group, full Plus configuration RAM, saved configuration and toggle
state matched the backups. The full TANK live patch, preset state and saved
RAM/flash bank also matched the original snapshot. There were no restoration
errors. The acceptance run made 52 bounded Plus configuration/runtime writes
and 52 TANK live writes, with zero firmware writes or saved TANK preset writes.
The temporary C/D assignments were removed; the original assignments remain.

Sessions 1-3 stopped before staging any test settings because the Chocolate
BLE connection was unavailable. They are connection failures, not failed
SysEx-delivery trials; the incoming connection conflict is described above.
