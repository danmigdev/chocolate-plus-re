#!/usr/bin/env bash
# Offline only. JL_TOOLCHAIN is the extracted JieLi Linux toolchain root.
set -euo pipefail
study=$(cd -- "$(dirname -- "$0")" && pwd)
out="$study/../out/sysex-gap-150"
tc=${JL_TOOLCHAIN:?Set JL_TOOLCHAIN to the JieLi toolchain root}
mkdir -p "$out"
cd "$study"
cc -std=c99 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
  -fno-omit-frame-pointer sysex_gap.c test_sysex_gap.c -o "$out/test-sysex-gap"
"$out/test-sysex-gap"
"$tc/pi32v2/bin/cc" -mcpu=r3 -std=c99 -Oz -Wall -Wextra -Werror \
  -ffreestanding -fno-builtin -c sysex_gap.c -o "$out/gap.o"
"$tc/pi32v2/bin/cc" -mcpu=r3 -c sysex_gap_glue.S -o "$out/glue.o"
"$tc/pi32v2/bin/cc" -mcpu=r3 -nostdlib -Wl,-T,sysex_gap.ld \
  "$out/gap.o" "$out/glue.o" -o "$out/sysex-gap.elf"
"$tc/common/bin/objcopy" -O binary -j .patch "$out/sysex-gap.elf" "$out/sysex-gap.bin"
for hook in boundary after_send save read write; do
  "$tc/common/bin/objcopy" -O binary -j ".hook_$hook" \
    "$out/sysex-gap.elf" "$out/hook-$hook.bin"
done
"$tc/common/bin/objdump" --mcpu=r3 --address-mask=0x1ffffff -h -t -d \
  "$out/sysex-gap.elf" > "$out/disassembly.txt"
"$tc/common/bin/objdump" --mcpu=r3 -h "$out/sysex-gap.elf"
python3 - "$out" <<'PY'
import hashlib, json, sys
from pathlib import Path
names = ("sysex_gap.c", "sysex_gap.h", "sysex_gap_glue.S", "sysex_gap.ld",
         "test_sysex_gap.c", "build_sysex_gap.sh")
values = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in names}
(Path(sys.argv[1]) / "sources.json").write_text(json.dumps(values, indent=2) + "\n")
PY
