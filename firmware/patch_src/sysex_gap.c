/* Experimental 150, based on exact upstream 142. No added mutable static RAM.
 * Native binding addresses and call context are documented in docs/sysex-gap.md.
 */
#include "sysex_gap.h"

extern unsigned int native_output_callback;
extern unsigned int native_user_flash;
extern volatile unsigned char native_vm_flags;
extern const unsigned char *native_usb_buffer(unsigned int port);
extern void native_task_delay(int ticks);
extern void native_settings_lock(void);
extern void native_settings_unlock(void);
extern int native_flash_read(void *data, unsigned int address, unsigned int length);
extern int native_flash_write(const void *data, unsigned int address, unsigned int length);
extern void native_save(unsigned int mode); /* trampoline, bypasses our wrapper */
extern void native_config_read(void *data, unsigned int offset, unsigned int length);
extern void native_config_write(const void *data, unsigned int offset, unsigned int length);

static unsigned int le16(const unsigned char *p)
{
    return p[0] | ((unsigned int)p[1] << 8);
}

static void put16(unsigned char *p, unsigned int value)
{
    p[0] = (unsigned char)value;
    p[1] = (unsigned char)(value >> 8);
}

static unsigned int checksum(const unsigned char *p)
{
    unsigned int crc = 0, i, bit;
    for (i = 0; i < CH_GAP_BYTES - 2; ++i) {
        crc ^= (unsigned int)p[i] << 8;
        for (bit = 0; bit < 8; ++bit)
            crc = ((crc << 1) ^ ((crc & 0x8000) ? 0x1021 : 0)) & 0xffff;
    }
    return crc;
}

int ch_gap_decode(const unsigned char *p)
{
    unsigned int ms = le16(p + 6);
    if (p[0] != 'S' || p[1] != 'G' || p[2] != 'A' || p[3] != 'P' ||
        p[4] != 1 || p[5] != CH_GAP_STEP_MS ||
        ms > CH_GAP_MAX_MS || ms % CH_GAP_STEP_MS ||
        le16(p + 8) != (ms ^ 0xffff) || le16(p + 10) != CH_GAP_MAX_MS ||
        le16(p + 12) != CH_GAP_DEFAULT_MS || le16(p + 14) != checksum(p))
        return -1;
    return (int)ms;
}

void ch_gap_encode(unsigned char *p, unsigned int ms)
{
    p[0] = 'S'; p[1] = 'G'; p[2] = 'A'; p[3] = 'P';
    p[4] = 1; p[5] = CH_GAP_STEP_MS;
    put16(p + 6, ms);
    put16(p + 8, ms ^ 0xffff);
    put16(p + 10, CH_GAP_MAX_MS);
    put16(p + 12, CH_GAP_DEFAULT_MS);
    put16(p + 14, checksum(p));
}

/* Caller owns the native VM mutex. Flash operations acquire the separate
 * native flash mutex, in the same order as native VM operations. No new VM ID
 * is allocated. The boot/load path does not take this mutex.
 */
static int read_setting(void)
{
    unsigned char record[CH_GAP_BYTES];
    if (native_user_flash != CH_USER_FLASH ||
        native_flash_read(record, CH_USER_FLASH + CH_GAP_FLASH_OFFSET,
                          CH_GAP_BYTES) != CH_GAP_BYTES)
        return -1;
    return ch_gap_decode(record);
}

static unsigned int current_setting(void)
{
    int value;
    if (!(native_vm_flags & 1))
        return CH_GAP_DEFAULT_MS;
    native_settings_lock();
    value = read_setting();
    native_settings_unlock();
    return value < 0 ? CH_GAP_DEFAULT_MS : (unsigned int)value;
}

static int ends_sysex(const unsigned char *packet, unsigned int length)
{
    unsigned int cin;
    if (!packet || length < 4 || length > 64 || (length & 3))
        return 0;
    packet += length - 4;
    cin = packet[0] & 15;
    return cin >= 5 && cin <= 7 && packet[cin - 4] == 0xf7;
}

int ch_host_boundary(const unsigned char *chunk, unsigned int length)
{
    return native_output_callback == CH_HOST_CALLBACK && ends_sysex(chunk, length);
}

/* Called by midi_route AFTER submission and native IRQ re-enable. The task
 * cannot build/submit the next packet while it sleeps; USB interrupts can run.
 * No initial wait, no busy wait, no sleep inside the key scanner or an ISR.
 * The native 10 ms scheduler makes this a nominal delay, not a wire timestamp.
 */
void ch_host_after_send(unsigned int port, unsigned int length)
{
    unsigned int ms;
    if (port != 1 || !ends_sysex(native_usb_buffer(port), length))
        return;
    ms = current_setting();
    if (ms)
        native_task_delay((int)(ms / CH_GAP_STEP_MS));
}

/* Native mode 1 erases six sectors, then writes config + "usrdata\0".
 * Preserve our footer across that operation. Mode 2 is factory reset: native
 * erase removes the footer and the effective setting returns to 20 ms.
 * Mode 0 loads at boot, mode 3 writes only the native 12-byte tail.
 */
void ch_config_save(unsigned int mode)
{
    unsigned char record[CH_GAP_BYTES];
    int value;
    if ((mode != 1 && mode != 2) || !(native_vm_flags & 1)) {
        native_save(mode);
        return;
    }
    native_settings_lock();
    value = read_setting();
    native_save(mode);
    if (mode == 1 && value >= 0 && native_user_flash == CH_USER_FLASH) {
        ch_gap_encode(record, (unsigned int)value);
        native_flash_write(record, CH_USER_FLASH + CH_GAP_FLASH_OFFSET, CH_GAP_BYTES);
    }
    native_settings_unlock();
}

static int extension(unsigned int offset)
{
    return offset >= CH_GAP_OFFSET && offset - CH_GAP_OFFSET < 4096;
}

void ch_config_read(void *data, unsigned int offset, unsigned int length)
{
    unsigned char *p = data;
    unsigned int i;
    if (!extension(offset)) {
        native_config_read(data, offset, length);
        return;
    }
    if (offset == CH_GAP_OFFSET && length == CH_GAP_BYTES) {
        ch_gap_encode(p, current_setting());
        return;
    }
    for (i = 0; i < length; ++i)
        p[i] = 0;
}

void ch_config_write(const void *data, unsigned int offset, unsigned int length)
{
    unsigned char record[CH_GAP_BYTES];
    int value;
    if (!extension(offset)) {
        native_config_write(data, offset, length);
        return;
    }
    if (offset != CH_GAP_OFFSET || length != CH_GAP_BYTES ||
        native_user_flash != CH_USER_FLASH || !(native_vm_flags & 1) ||
        (value = ch_gap_decode(data)) < 0)
        return;
    ch_gap_encode(record, (unsigned int)value);
    native_settings_lock();
    if (read_setting() != value) {
        /* Erase via the native save, never attempt a NOR 0->1 overwrite.
         * Copy input before this call: no dependency on the protocol buffer.
         * The standard 0x22 ACK is not a persistence guarantee; PC reads back.
         */
        native_save(1);
        native_flash_write(record, CH_USER_FLASH + CH_GAP_FLASH_OFFSET, CH_GAP_BYTES);
    }
    native_settings_unlock();
}
