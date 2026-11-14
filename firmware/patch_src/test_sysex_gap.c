/* Host-side model of the native ABI: runs the actual freestanding C with
 * sanitizers, including NOR write restrictions, full saves and factory reset.
 */
#include "sysex_gap.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

unsigned int native_output_callback = CH_HOST_CALLBACK;
unsigned int native_user_flash = CH_USER_FLASH;
volatile unsigned char native_vm_flags = 1;
static unsigned char flash[0x6000], ram[CH_CONFIG_BYTES], packet[64];
static unsigned int locked, saves, writes, reads, delays, ticks, delegated, checks;

void native_settings_lock(void) { assert(!locked && (native_vm_flags & 1)); locked = 1; }
void native_settings_unlock(void) { assert(locked); locked = 0; }
const unsigned char *native_usb_buffer(unsigned int port)
{ assert(port == 1); return packet; }
void native_task_delay(int count)
{ assert(!locked && count > 0 && count <= 100); ++delays; ticks += count; }
int native_flash_read(void *data, unsigned int address, unsigned int length)
{
    assert(locked && address == CH_USER_FLASH + CH_GAP_FLASH_OFFSET);
    assert(length == CH_GAP_BYTES);
    memcpy(data, flash + CH_GAP_FLASH_OFFSET, length);
    ++reads;
    return (int)length;
}
int native_flash_write(const void *data, unsigned int address, unsigned int length)
{
    unsigned int i;
    const unsigned char *p = data;
    assert(locked && address == CH_USER_FLASH + CH_GAP_FLASH_OFFSET);
    assert(length == CH_GAP_BYTES);
    for (i = 0; i < length; ++i) {
        unsigned char *dest = flash + CH_GAP_FLASH_OFFSET + i;
        assert((*dest & p[i]) == p[i]); /* no NOR 0->1 write */
        *dest &= p[i];
    }
    ++writes;
    return (int)length;
}
void native_save(unsigned int mode)
{
    ++saves;
    if (mode == 0) {
        assert(!locked);
        memcpy(ram, flash, sizeof(ram));
    } else if (mode == 1 || mode == 2) {
        assert(locked || !(native_vm_flags & 1));
        if (mode == 2) memset(ram, 0, sizeof(ram));
        memset(flash, 255, sizeof(flash));
        memcpy(flash, ram, sizeof(ram));
        memcpy(flash + sizeof(ram), "usrdata", 8);
    } else if (mode == 3) {
        assert(!locked);
        memcpy(flash + CH_CONFIG_BYTES - 12, ram + CH_CONFIG_BYTES - 12, 12);
    }
}
void native_config_read(void *data, unsigned int offset, unsigned int length)
{ assert(offset + length <= sizeof(ram)); memcpy(data, ram + offset, length); ++delegated; }
void native_config_write(const void *data, unsigned int offset, unsigned int length)
{ assert(offset + length <= sizeof(ram)); memcpy(ram + offset, data, length); ++delegated; }

static void reset(void)
{
    assert(!locked);
    memset(flash, 255, sizeof(flash));
    memset(ram, 0x35, sizeof(ram));
    memset(packet, 0, sizeof(packet));
    native_user_flash = CH_USER_FLASH;
    native_vm_flags = 1;
    native_output_callback = CH_HOST_CALLBACK;
    saves = writes = reads = delays = ticks = delegated = 0;
}

static int setting(void)
{
    unsigned char record[CH_GAP_BYTES];
    ch_config_read(record, CH_GAP_OFFSET, sizeof(record));
    return ch_gap_decode(record);
}

static void set(unsigned int ms)
{
    unsigned char record[CH_GAP_BYTES];
    ch_gap_encode(record, ms);
    ch_config_write(record, CH_GAP_OFFSET, sizeof(record));
}

static void records_and_persistence(void)
{
    unsigned char record[CH_GAP_BYTES], copy[CH_GAP_BYTES], buf[32];
    unsigned int ms, bit, old;
    const unsigned char wire_default[CH_GAP_BYTES] = {
        0x53, 0x47, 0x41, 0x50, 0x01, 0x0a, 0x14, 0x00,
        0xeb, 0xff, 0xe8, 0x03, 0x14, 0x00, 0x11, 0x08
    };
    reset();
    ch_gap_encode(record, 20);
    assert(memcmp(record, wire_default, sizeof(record)) == 0);
    assert(setting() == 20 && saves == 0 && writes == 0);
    for (ms = 0; ms <= 1000; ms += 10) {
        ch_gap_encode(record, ms);
        assert(ch_gap_decode(record) == (int)ms);
        for (bit = 0; bit < 128; ++bit) {
            memcpy(copy, record, sizeof(copy));
            copy[bit / 8] ^= 1u << (bit % 8);
            assert(ch_gap_decode(copy) == -1);
            ++checks;
        }
        set(ms);
        assert(setting() == (int)ms);
        assert(memcmp(flash, ram, sizeof(ram)) == 0);
        assert(memcmp(flash + sizeof(ram), "usrdata", 8) == 0);
        old = saves;
        set(ms);
        assert(saves == old); /* avoid erase if unchanged */
        ram[927] ^= 1;
        ch_config_save(1);
        assert(setting() == (int)ms && flash[927] == ram[927]);
        ch_config_save(3);
        ch_config_save(0); /* simulate reboot, no added RAM state */
        assert(setting() == (int)ms);
    }
    old = saves;
    for (ms = 0; ms < 1100; ++ms) {
        if (ms <= 1000 && ms % 10 == 0) continue;
        set(ms);
        assert(saves == old && setting() == 1000);
        ++checks;
    }
    ch_gap_encode(record, 30);
    ch_config_write(record, CH_GAP_OFFSET, 15);
    ch_config_write(record, CH_GAP_OFFSET + 1, 16);
    assert(saves == old);
    memset(buf, 1, sizeof(buf));
    ch_config_read(buf, CH_GAP_OFFSET + 1, sizeof(buf));
    for (bit = 0; bit < sizeof(buf); ++bit) assert(buf[bit] == 0);
    native_user_flash = 0;
    set(50);
    assert(saves == old && setting() == 20);
    native_user_flash = CH_USER_FLASH;
    native_vm_flags = 0;
    set(80);
    assert(saves == old && setting() == 20);
    native_vm_flags = 1;
    ch_config_save(2);
    assert(setting() == 20);
    assert(flash[CH_GAP_FLASH_OFFSET] == 255);
    memset(buf, 0x62, sizeof(buf));
    ch_config_write(buf, 100, sizeof(buf));
    memset(buf, 0, sizeof(buf));
    ch_config_read(buf, 100, sizeof(buf));
    assert(buf[0] == 0x62 && delegated == 2);
    set(70);
    flash[CH_GAP_FLASH_OFFSET + 8] ^= 1;
    assert(setting() == 20);
    ch_config_save(1);
    assert(setting() == 20 && flash[CH_GAP_FLASH_OFFSET] == 255);
}

static void boundaries_and_delays(void)
{
    unsigned int cable, cin, value, length, expected, old;
    reset();
    for (cable = 0; cable < 16; ++cable)
        for (cin = 0; cin < 16; ++cin)
            for (value = 0; value < 256; ++value) {
                memset(packet, 0, sizeof(packet));
                packet[0] = (unsigned char)((cable << 4) | cin);
                packet[cin >= 5 && cin <= 7 ? cin - 4 : 1] = (unsigned char)value;
                expected = cin >= 5 && cin <= 7 && value == 0xf7;
                assert(ch_host_boundary(packet, 4) == (int)expected);
                native_output_callback = 0;
                assert(ch_host_boundary(packet, 4) == 0);
                native_output_callback = CH_HOST_CALLBACK;
                ++checks;
            }
    for (length = 0; length <= 65; ++length) {
        memset(packet, 0, sizeof(packet));
        if (length >= 4 && length <= 64) {
            packet[length - 4] = 0x05;
            packet[length - 3] = 0xf7;
        }
        expected = length >= 4 && length <= 64 && !(length & 3);
        assert(ch_host_boundary(packet, length) == (int)expected);
        old = delays;
        ch_host_after_send(1, length);
        assert(delays == old + expected);
        ++checks;
    }
    assert(!ch_host_boundary(NULL, 4));
    memset(packet, 0, sizeof(packet));
    packet[0] = 4; packet[1] = 0xf0; /* first/continuation packet: no delay */
    old = delays;
    ch_host_after_send(1, 4);
    assert(delays == old);
    packet[0] = 5; packet[1] = 0xf7;
    ch_host_after_send(0, 4);
    assert(delays == old);
    set(0);
    assert(ch_host_boundary(packet, 4)); /* zero never disables splitting */
    ch_host_after_send(1, 4);
    assert(delays == old);
    set(30);
    ticks = 0;
    ch_host_after_send(1, 4);
    ch_host_after_send(1, 4);
    ch_host_after_send(1, 4);
    assert(delays == old + 3 && ticks == 9);
}

int main(void)
{
    records_and_persistence();
    boundaries_and_delays();
    printf("SysEx gap: %u exhaustive cases plus persistence/NOR/dispatch checks passed\n", checks);
    return 0;
}
