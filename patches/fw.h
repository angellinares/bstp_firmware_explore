/* Stock BeatStep Pro 2.0.1.6 state and functions shared by the patches.
 * Function addresses are in fw.ld; the layout is in docs/firmware-map.md. */
typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef unsigned int u32;

struct seq {
    u8 rng_seeded;
    u32 rng;
};

#define SEQ        (*(struct seq **)0x20000278)
#define SAVE_TRACK (*(volatile s8 *)0x20000023)   /* the track whose record is being built */
#define LOADED     ((const u8 *)0x20000B56 + 4)   /* what the last storage read returned */

#define DIRTY      0x2450 /* seq + this + track: pattern needs saving */
#define STEPS      64

void fw_rng_next(struct seq *seq);
u32 fw_storage_write(void *storage, u32 address, u8 *data, u32 length);

void poly_save(void *storage, u32 address, u8 *rec);
void poly_load(void *storage, int track);
