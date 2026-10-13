/* Sends the bootloader out over USB MIDI, so it can be read without a debug
 * probe. Read-only: nothing on the device is written except the state below.
 *
 * Start: turn the Probability knob (it still does its job). The dump then runs
 * from the main loop, a few messages a pass. The USB send queue holds 64
 * messages and refuses one when it is full: a refused message is sent again
 * on the next pass, so nothing is lost.
 *
 * Stream, all on USB only (tools/bootdump.py decodes it):
 *   BF 70 01            start
 *   En d1 d2            one byte at offset k: d1 = low 7 bits,
 *                       d2 = top bit | (k & 0x3f) << 1, n = (k >> 6) & 0xf
 *   BF 71..75 v         the sum of all bytes, 7 bits at a time, low first
 *   BF 70 02            end
 */
#include "fw.h"

void *fw_out(void);
u32 fw_midi_usb(void *out, u32 message, u32 x);
void fw_prob_set(struct seq *seq, int track, u32 value);

struct region {
    u32 address, length;
};
static const struct region regions[] = {
    {0x08000000, 0x3000},   /* the bootloader */
    {0x1FFFF800, 0x10},     /* option bytes: read protection */
    {0x1FFFF7E0, 0x20},     /* flash size and device id */
};
#define REGIONS (sizeof regions / sizeof *regions)
#define TOTAL   0x3030
#define LAST    (TOTAL + 6) /* messages: start, TOTAL bytes, 5 for the sum, end */
#define PER_PASS  8         /* messages a main-loop pass */

struct dump {
    u32 state;              /* RUNNING while a dump is in progress; anything else: idle */
    u32 next;               /* the message to send next, 0..LAST */
    u32 sum;
};
#define D ((struct dump *)0x20006000)
#define RUNNING 0xD0B007ED  /* RAM here is not cleared at start-up */

static u8 byte_at(u32 offset)
{
    for (u32 r = 0; r < REGIONS; r++) {
        if (offset < regions[r].length)
            return *(const volatile u8 *)(regions[r].address + offset);
        offset -= regions[r].length;
    }
    return 0;
}

/* -> message number n of the stream; *value is the byte it carries, if any. */
static u32 message(u32 n, u32 *value)
{
    *value = 0;
    if (n == 0)
        return 0xBF | 0x70 << 8 | 1 << 16;
    if (n <= TOTAL) {
        u32 at = n - 1;

        *value = byte_at(at);
        return (0xE0 | (at >> 6 & 0xf)) | (*value & 0x7f) << 8 | (*value >> 7 | (at & 0x3f) << 1) << 16;
    }
    if (n < LAST)
        return 0xBF | (0x71 + n - TOTAL - 1) << 8 | (D->sum >> (7 * (n - TOTAL - 1)) & 0x7f) << 16;
    return 0xBF | 0x70 << 8 | 2 << 16;
}

void dump_step(void)
{
    if (D->state != RUNNING)
        return;
    for (int k = 0; k < PER_PASS; k++) {
        u32 value;

        if (!fw_midi_usb(fw_out(), message(D->next, &value), 0))
            return;         /* the queue is full: this message again next pass */
        D->sum += value;
        if (D->next++ == LAST) {
            D->state = 0;
            return;
        }
    }
}

/* Replaces the call to encoders_poll in the main loop (0x0800C196). */
__attribute__((naked)) void dump_poll(void)
{
    __asm__("push {r0-r3, r4, lr}\n"
            "bl dump_step\n"
            "pop {r0-r3, r4, lr}\n"
            "b.w fw_encoders_poll\n");
}

/* Replaces fw_prob_set where the knob handler calls it (0x0802C20C, 0x0802C4A6). */
void dump_start(struct seq *seq, int track, u32 value)
{
    if (D->state != RUNNING) {
        D->next = 0;
        D->sum = 0;
        D->state = RUNNING;
    }
    fw_prob_set(seq, track, value);
}
