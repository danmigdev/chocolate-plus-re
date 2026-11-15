# Experimental HOST SysEx splitting: 143-split

Upstream 142 can pack multiple complete SysEx messages into one HOST USB
transfer. In the tested Chocolate Plus -> TANK Mini setup, a bank containing
FX OFF followed by REV Mix 73 did not change either parameter, while each
message worked by itself. Ending the transfer after each SysEx addressed
this behavior in an earlier prototype, including five trials without any
added delay. This candidate retains only that boundary change.

**143-split is a candidate build number, not an upstream release.**
The existing `build.py` remains unchanged and reproduces the 142 baseline.
`split_143.py` builds the compact experimental image; it does not produce an
OTA installer. The exact 143 candidate has passed offline validation and was
installed on 15 November 2026. Two complete physical flash reads matched the
target, and the application and module hashes matched after a cold boot with
the original configuration preserved. HOST delivery validation is still pending.

## Patch behavior

The hook at `0x1e0ff54` replaces the two additions that advance the current
packet pointer and total length. The glue executes both displaced additions
and then checks the last event in the just-appended chunk. When the native
output callback is `0x1e119ae` (HOST), CIN is 5/6/7 and its final valid MIDI
byte is `F7`, it returns the accumulated packet length. Otherwise it resumes
the original builder. Remaining messages are consumed by later invocations.

There is no timer, configurable interval, flash setting or replacement save
routine. No extra RAM is allocated. Non-SysEx events, continuation chunks and
USB device/Bluetooth output callbacks retain the native branch behavior.
The predicate checks the valid MIDI byte rather than relying on unused padding.
One press still reads the same stock-editor bank; the configuration format and
the TANK firmware are unchanged.

The module is 116 bytes at `0x1e265f0`, stored in an added sequential resource.
The compact image grows from `0x27600` to `0x27700`; the native VM banks remain
at `0x28000` and `0x29000`. Boot, Bluetooth/calibration and USRFLASH reservations
do not move. Application/area/resource CRCs and the application MD5 are rebuilt.
The only executable hook is the builder boundary; identity and greeting are
changed to `FootCtrlPlus_143-split` and `143` for identification during testing.

## Compatibility

Combining MIDI events in a USB transfer is legitimate. This patch is a
compatibility workaround for observed TANK behavior, not a claim that grouped
SysEx violates [USB-MIDI 1.0](https://www.usb.org/sites/default/files/midi10.pdf).
More transfers may increase overhead and affect dense-traffic throughput or
scheduling. Other receivers, long SysEx and sustained loads still need testing.
No USB wire timestamps or audio latency have been measured. No added wait
does not mean zero transfer time.

The original editor layout has one 128-byte SysEx area per bank (one length
byte plus up to 127 bytes). Multiple configured messages or command rows do
not independently control USB transfer boundaries. The tested 142 source
can coalesce even two different switch events queued in the same timer tick.

## Reproduce

1. Build the exact 142 baseline using the existing `build.py` and the verified
   vendor firmware 012. No vendor binary is included in this repository.
2. Compile the extension in Linux/WSL with the extracted JieLi toolchain:

   ```bash
   export JL_TOOLCHAIN=/root/jl-tc/jieli-linux-toolchains-20250324.1
   bash firmware/patch_src/build_sysex_split.sh
   ```

3. Build the candidate using a new output directory:

   ```powershell
   python firmware/split_143.py --baseline firmware/out/142-baseline/FootCtrlPlus_142-dtnprt.fwsc --module firmware/out/sysex-split-143 --output firmware/out/split-143-candidate
   python -m unittest discover -s tests -q
   ```

The C harness runs with ASan/UBSan and covers all cable numbers, CINs and final
byte values, invalid lengths, continuation packets and nonzero padding. The
Python checks validate source hashes, ELF sections and native bindings, the
actual glue branches, metadata, unchanged regions and rejection of altered
inputs. Firmware-dependent tests skip explicitly without local fixtures.

Expected compact SHA-256:
`963418009370c91927e85847968bae25157a0812c69bdec65c5654383b59221c`.
Expected module SHA-256:
`0cd5d58e7017f2f3ff855cddd1a77f6235b9219c5de0ce385c5ac6483d517593`.

The separate Windows migration and BLE acceptance tools, original measurements
and recovery notes are maintained on the fork's
[hardware investigation branch](https://github.com/danmigdev/chocolate-plus-re/blob/feat/windows-usb-142-validation/docs/sysex-split.md).
The compact `.bin` is not a SincoOTA file and must not be renamed to `.fwsc`.
