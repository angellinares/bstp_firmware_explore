/* Chords on the melodic sequencers, BeatStep Pro 2.0.1.6. Built with -DPOLY.
 *
 * Stock firmware keeps one note per step and that stays as it is: the first
 * note of a step is the stock one, with its velocity, gate and CV. Each step
 * gains three more notes with their own velocity. They share the step's gate
 * and go to MIDI and USB only.
 *
 * Panel: hold a step button and press pads. The first pad sets the step's
 * note as before and clears its chord; each further pad while the step is
 * still held adds a note.
 *
 * Storage: two pages per pattern in the part of the storage chip the stock
 * firmware never addresses (0x140000 up), tied to the pattern record by a
 * random tag so that a record copied or restored without its pages plays
 * without a chord instead of with someone else's.
 *
 * Limits: real-time recording, step shift and step copy do not carry the
 * extra notes; MIDI Control Center does not see them.
 */
#include "fw.h"

#define EXTRA 3

struct note {
    u8 pitch;      /* stock step note value + 1; 0: none */
    u8 velocity;
};

/* In the gap between heap and stack, zeroed at start-up like all RAM above
 * the initialised data. See docs/firmware-map.md. */
struct poly {
    struct note notes[2][STEPS][EXTRA];
    u8 sounding[2][EXTRA];   /* MIDI note + 1; 0: silent */
    u8 channel[2];
    u8 dirty[2];             /* notes changed since the pages were written */
    u8 pending[2];           /* where the next pad velocity goes: 0 stock, slot + 1, 0xFF nowhere */
    u8 hold_step[2];         /* step + 1 whose chord is being entered */
    u16 tag[2];              /* tag of the pages that hold these notes; 0: none */
    u32 hold_time[2];
    u32 home[2];             /* address of those pages */
};
#define P ((struct poly *)0x20006000)

#define NOTE_TABLE  ((const u8 *)0x20001796)       /* + track * 0x80 + step note value */
#define PRESS_TIME  (*(volatile u32 *)0x20000124)  /* tick when the held step went down */
#define LAST_READ   ((const u8 *)0x20000752)       /* command and address of the last storage read */
#define STEP_NOTE   0x6F5                          /* seq + track * 0x230 + this + step */

#define PAGES       0x140000   /* + project * 0x4000 + track * 0x2000 + pattern * 0x200 */
#define PAGE_STEPS  (STEPS / 2)
#define PAGE_SIZE   (2 + PAGE_STEPS * EXTRA * 2)
#define REC_TAG     0x3D       /* in the pattern record: tag, then a check byte */
#define TAG_CHECK   0xA7

u32 fw_seq_note_on(void *player, u32 channel, u32 note, u32 velocity,
                   int track, u32 step, int pad, u32 tie);
void fw_seq_note_off(void *player, int track, u32 step, int pad);
void fw_track_notes_off(struct seq *seq, int track, int pad, u32 x);
void fw_pattern_clear(int track, int pad);
void fw_track_reset(u32 track, u32 a, u32 b, u32 c);
void fw_step_param_set(struct seq *seq, int track, int field, u32 value, u32 step, int pad);
void fw_step_velocity_set(struct seq *seq, int track, u32 velocity, u32 step, int pad);
void *fw_out(void);
u32 fw_midi_usb(void *out, u32 message, u32 x);
void fw_midi_din(void *out, u32 message);
u32 fw_transpose_get(struct seq *seq, int track);
u32 fw_note_fit(struct seq *seq, u32 note, u32 transpose, u32 x);
void fw_storage_read(void *storage, u32 address, u32 length);

static void send(u32 status, u32 channel, u32 note, u32 velocity)
{
    void *out = fw_out();
    u32 message = status | (channel & 0xf) | note << 8 | velocity << 16;

    fw_midi_usb(out, message, 0);
    fw_midi_din(out, message);
}

__attribute__((noinline)) static void release(int track)
{
    for (int k = 0; k < EXTRA; k++) {
        if (P->sounding[track][k])
            send(0x80, P->channel[track], P->sounding[track][k] - 1, 0x40);
        P->sounding[track][k] = 0;
    }
}

__attribute__((noinline)) static void copy(u8 *to, const u8 *from, u32 size)
{
    while (size--)
        *to++ = *from++;
}

static int melodic(int track)
{
    return track == 0 || track == 1;
}

/* Replaces fw_seq_note_on at its eight calls in seq_play_event. */
u32 poly_note_on(void *player, u32 channel, u32 note, u32 velocity,
                 int track, u32 step, int pad, u32 tie)
{
    u32 r = fw_seq_note_on(player, channel, note, velocity, track, step, pad, tie);

    if (!melodic(track) || step >= STEPS)
        return r;
    release(track);
    P->channel[track] = channel;
    for (int k = 0; k < EXTRA; k++) {
        struct note n = P->notes[track][step][k];

        if (!n.pitch)
            continue;
        /* As seq_play_event turns the step's own note into a MIDI note. */
        note = fw_note_fit(SEQ, NOTE_TABLE[track * 0x80 + n.pitch - 1],
                           fw_transpose_get(SEQ, track), 0);
        send(0x90, channel, note, n.velocity ? n.velocity : 1);
        P->sounding[track][k] = note + 1;
    }
    return r;
}

/* Replaces fw_seq_note_off at its five calls in seq_play_event. */
void poly_note_off(void *player, int track, u32 step, int pad)
{
    fw_seq_note_off(player, track, step, pad);
    if (melodic(track))
        release(track);
}

/* Replaces fw_track_notes_off at all nineteen of its calls. */
void poly_track_notes_off(struct seq *seq, int track, int pad, u32 x)
{
    fw_track_notes_off(seq, track, pad, x);
    if (melodic(track))
        release(track);
}

/* Kept out of line: the patch block is 2 KB. */
__attribute__((noinline)) static void clear_step(int track, u32 step)
{
    for (int k = 0; k < EXTRA; k++) {
        if (P->notes[track][step][k].pitch)
            P->dirty[track] = 1;
        P->notes[track][step][k].pitch = 0;
    }
}

__attribute__((noinline)) static void clear_track(int track)
{
    for (u32 step = 0; step < STEPS; step++)
        clear_step(track, step);
}

/* Replaces fw_pattern_clear at 0x0800B980. */
void poly_pattern_clear(int track, int pad)
{
    fw_pattern_clear(track, pad);
    if (melodic(track))
        clear_track(track);
}

/* Replaces fw_track_reset at its four calls, two of which are how a pattern
 * load fills in an empty pattern. */
void poly_track_reset(u32 track, u32 a, u32 b, u32 c)
{
    fw_track_reset(track, a, b, c);
    if (melodic(track))
        clear_track(track);
}

/* Replaces fw_step_param_set where a pad pressed while a step is held sets
 * that step's note (0x0801BF18). */
void poly_step_note(struct seq *seq, int track, int field, u32 value, u32 step, int pad)
{
    struct note *notes;
    int slot = -1;

    if (!melodic(track) || field != 0 || step >= STEPS) {
        fw_step_param_set(seq, track, field, value, step, pad);
        return;
    }
    notes = P->notes[track][step];
    if (P->hold_time[track] != PRESS_TIME || P->hold_step[track] != step + 1) {
        /* First pad of this hold: the stock note, and a fresh chord. */
        fw_step_param_set(seq, track, field, value, step, pad);
        clear_step(track, step);
        P->hold_time[track] = PRESS_TIME;
        P->hold_step[track] = step + 1;
        P->pending[track] = 0;
        return;
    }
    P->pending[track] = 0xFF;
    if ((value & 0xff) == ((u8 *)seq)[track * 0x230 + STEP_NOTE + step])
        return;
    for (int k = 0; k < EXTRA; k++)
        if (notes[k].pitch == (value & 0xff) + 1)
            slot = k;
    for (int k = 0; slot < 0 && k < EXTRA; k++)
        if (!notes[k].pitch)
            slot = k;
    if (slot < 0)
        return;
    notes[slot].pitch = (value & 0xff) + 1;
    P->pending[track] = slot + 1;
    P->dirty[track] = 1;
    ((u8 *)seq)[DIRTY + track] = 1;
}

/* Replaces fw_step_velocity_set right after it (0x0801BF46). */
void poly_step_velocity(struct seq *seq, int track, u32 velocity, u32 step, int pad)
{
    u32 pending = melodic(track) && step < STEPS && P->hold_step[track] == step + 1
                      ? P->pending[track] : 0;

    if (pending == 0)
        fw_step_velocity_set(seq, track, velocity, step, pad);
    else if (pending != 0xFF)
        P->notes[track][step][pending - 1].velocity = velocity;
}

/* -> where the pages of the pattern record at this address live. */
static u32 pages_of(u32 address)
{
    u32 project = address / 0x13000, rest = address % 0x13000;

    return PAGES + project * 0x4000 + (rest >> 12 & 1) * 0x2000 + (rest >> 8 & 0xf) * 0x200;
}

/* Called before a melodic pattern record is written: write the pages if
 * they are out of date and put their tag in the record. */
void poly_save(void *storage, u32 address, u8 *rec)
{
    int track = SAVE_TRACK;
    u32 home = pages_of(address);
    u8 page[PAGE_SIZE];

    if (!melodic(track))
        return;
    if (P->dirty[track] || P->home[track] != home) {
        int any = 0;

        for (u32 step = 0; step < STEPS; step++)
            for (int k = 0; k < EXTRA; k++)
                any |= P->notes[track][step][k].pitch;
        if (any) {
            fw_rng_next(SEQ);
            P->tag[track] = (SEQ->rng >> 8) | 1;
        } else {
            P->tag[track] = 0;
        }
        for (u32 half = 0; any && half < 2; half++) {
            page[0] = P->tag[track];
            page[1] = P->tag[track] >> 8;
            copy(page + 2, (const u8 *)P->notes[track][half * PAGE_STEPS], PAGE_SIZE - 2);
            fw_storage_write(storage, home + half * 0x100, page, PAGE_SIZE);
        }
        P->home[track] = home;
        P->dirty[track] = 0;
    }
    rec[REC_TAG] = P->tag[track];
    rec[REC_TAG + 1] = P->tag[track] >> 8;
    rec[REC_TAG + 2] = rec[REC_TAG] ^ rec[REC_TAG + 1] ^ TAG_CHECK;
}

/* Called when a melodic pattern load has set the steps: fetch the chord
 * notes that belong to the record in LOADED, or none. */
void poly_load(void *storage, int track)
{
    u32 address = LAST_READ[1] << 16 | LAST_READ[2] << 8 | LAST_READ[3];
    u32 tag = LOADED[REC_TAG] | LOADED[REC_TAG + 1] << 8;
    int valid = tag && (LOADED[REC_TAG] ^ LOADED[REC_TAG + 1] ^ TAG_CHECK) == LOADED[REC_TAG + 2];

    if (!melodic(track))
        return;
    release(track);
    clear_track(track);
    P->tag[track] = 0;
    P->home[track] = pages_of(address);
    P->dirty[track] = 0;
    P->hold_step[track] = 0;
    if (!valid)
        return;
    fw_storage_read(storage, P->home[track], 0x200);
    if ((u32)(LOADED[0] | LOADED[1] << 8) == tag && (u32)(LOADED[0x100] | LOADED[0x101] << 8) == tag) {
        for (u32 half = 0; half < 2; half++)
            copy((u8 *)P->notes[track][half * PAGE_STEPS], LOADED + half * 0x100 + 2, PAGE_SIZE - 2);
        P->tag[track] = tag;
    }
    /* The stock loader may still look at the record. */
    fw_storage_read(storage, address, 0x100);
}
