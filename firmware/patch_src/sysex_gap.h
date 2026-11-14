#ifndef CH_SYSEX_GAP_H
#define CH_SYSEX_GAP_H

/* One setting for every HOST bank/switch. No per-bank overrides. */
#define CH_GAP_DEFAULT_MS 20u
#define CH_GAP_STEP_MS 10u
#define CH_GAP_MAX_MS 1000u
#define CH_GAP_BYTES 16u
#define CH_GAP_OFFSET 0x53580000u
#define CH_CONFIG_BYTES 23652u
#define CH_GAP_FLASH_OFFSET (CH_CONFIG_BYTES + 8u)
#define CH_USER_FLASH 0x39000u
#define CH_HOST_CALLBACK 0x1e119aeu

int ch_gap_decode(const unsigned char *record);
void ch_gap_encode(unsigned char *record, unsigned int milliseconds);
int ch_host_boundary(const unsigned char *chunk, unsigned int length);
void ch_host_after_send(unsigned int port, unsigned int length);
void ch_config_save(unsigned int mode);
void ch_config_read(void *data, unsigned int offset, unsigned int length);
void ch_config_write(const void *data, unsigned int offset, unsigned int length);

#endif
