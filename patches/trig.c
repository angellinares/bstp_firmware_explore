/* Per-step trigger chance and conditions, BeatStep Pro 2.0.1.6.
 *
 * The on byte of a step is 0 or 1 in stock firmware and every reader only
 * tests it for zero. Bits 1..6 now hold a condition code; 0 means "always",
 * so a pattern that never sets one plays exactly as before.
 *
 * Panel: hold a step button and turn the Randomness knob. The display shows
 * the chance in percent, as it does for the knob. On the DRUM track the step
 * buttons belong to the selected pad, and so does the step that is changed.
 *
 * Conditions: codes 22..41 make a step play on some passes of the pattern
 * only. Hold a step and turn the Probability knob; the display shows 1-1 (no
 * condition), 1St, n1S (not the first pass), then A-B (pass A of every B) and
 * nAB (every pass but that one) for B up to 4. A step has a chance or a
 * condition, not both. Passes are counted per track, from 0 at Play from stop
 * and at a pattern change; in alternate direction a pass is there and back.
 * Each drum pad counts the passes of its own length.
 *
 * Storage: the 256-byte pattern record keeps bytes 0x0B..0x3F unused (the
 * stock save leaves stack garbage there), so the codes go in behind a magic
 * byte and a check byte. A drum pattern's codes go in the part of its own
 * storage area that stock firmware never writes (see save_drum). Limit: the
 * stock step setter writes 0 or 1, so turning a step off, shifting or copying
 * steps drops their codes.
 *
 * Ratchets (DRUM track): a step can play 2, 4 or 8 times inside its own
 * length. Hold a step of the selected pad and touch the roller strip: a rate
 * finer than the track's step size sets it, the track's own rate or a coarser
 * one clears it. Recording a roll that is finer than the track marks the
 * steps it fills. Bit 7 of the on byte says the step has a ratchet, so the
 * stock setter drops it with the codes; the rate is in patch RAM (RATE).
 * A ratcheted step shows in cyan.
 *
 * Lights: a step with a chance blinks once a second, a step with a condition
 * blinks twice in a row every two seconds, in the track colour or in cyan. A held step is steady.
 */
#include "fw.h"

u32 fw_step_on(struct seq *seq, int track, int step, int pad);
void fw_step_on_set(struct seq *seq, int track, u32 on, int step, int pad);
u32 fw_rand_get(struct seq *seq, int track);
void fw_rand_set(struct seq *seq, int track, u32 value);
void fw_prob_set(struct seq *seq, int track, u32 value);
u32 fw_prob_get(struct seq *seq, int track);
void fw_pos_set(struct seq *seq, int track, u32 position, int pad);
void fw_show_number(void *display, int field, int value);
void fw_show_glyphs(void *display, int field, u32 a, u32 b, u32 c);
void fw_set_length(void);
void fw_drum_loaded(void);
void fw_storage_read(void *storage, u32 address, u32 length);
u32 fw_ticks(struct seq *seq, int track);
u32 fw_step_ticks(struct seq *seq, int size, int track);
void fw_button_flag(void *buttons, int id, int value);

/* Firmware state in RAM. */
#define CUR_TRACK  (*(volatile int *)0x200001F0)  /* 0, 1 melodic; 2 drum; 3 control */
#define HELD_STEP  ((volatile s8 *)0x200000E7)    /* per track; -1 when none is held */
#define HOLD_USED  ((volatile u8 *)0x200001CA)    /* per track; set: releasing the step does not toggle it */
#define HELD_PAD   ((volatile s8 *)0x200000C4)    /* per track; the held step button, 0..15 */
#define BUTTONS    (*(void **)0x20000264)
#define STEP_BUTTON 0x20                          /* id of the first step button */

#define MELODIC_ON 0x8B7  /* seq + track * 0x230 + this + step */
#define DRUM_ON    0x1FF9 /* seq + this + pad * 0x40 + step */
#define DRUM_SHIFT 0xBD9  /* seq + this + pad * 0x40 + step: 50 is on the beat */
#define DRUM_GATE  0x13D9 /* seq + this + pad * 0x40 + step, percent; 100 ties */
#define DRUM_PAD   0xB50  /* seq + this, int: the pad the step buttons show */
#define DRUM_SIZE  0xB48  /* seq + this, int: step size */
#define DRUM_DIR   0xB4C  /* seq + this, int: 2 is alternate */
#define DRUM_LEN   0xB89  /* seq + this + pad: the pad's length in steps */
#define DRUM_FIRST 0xB99  /* seq + this + pad: first and last step of the range played */
#define DRUM_LAST  0xBA9
#define DRUM_FWD   0x2409 /* seq + this + pad: 0 while the pad plays backwards */
#define PADS       16
#define POSITION   0x6F0  /* seq + track * 0x230 + this: the step being played, before direction */
#define DIRECTION  0x6EC  /* seq + track * 0x230 + this, int: 2 is alternate */

/* Passes of the pattern played so far, per melodic track. Patch RAM, above
 * what poly.c uses; not cleared at start-up, set by pos_reset. */
#define PASS ((volatile u32 *)0x20006400)

#define REC_MAGIC  0x0B
#define REC_CODES  0x0C   /* 48 bytes: 64 codes of 6 bits */
#define REC_CHECK  0x3C
#define MAGIC      0xC7

/* Codes 1..21: chance in percent that the step plays. */
static const u8 chance[] = {1, 2, 4, 6, 9, 13, 19, 25, 33, 41, 50,
                            59, 67, 75, 81, 87, 91, 94, 96, 98, 99};
#define CODES ((u32)sizeof chance)

/* Codes CODES + 1 .. CODES + CONDS: condition k = code - CODES, in the order
 * the Probability knob walks them. k 1 is the first pass only, 2 is every
 * pass but the first; from 3 on they come in pairs, pass A of every B and
 * its opposite. */
static const u8 ratio[][2] = {{1, 2}, {2, 2}, {1, 3}, {2, 3}, {3, 3},
                              {1, 4}, {2, 4}, {3, 4}, {4, 4}};
#define CONDS (2 + 2 * (u32)(sizeof ratio / sizeof *ratio))
#define N12   4   /* the conditions the knob steps over (see ui_cond_set) */
#define N22   6

/* -> the condition of an on byte, 1..CONDS, or 0 when it has none. */
static u32 cond_of(u32 on)
{
    u32 code = on >> 1 & 0x3f;

    return code > CODES && code <= CODES + CONDS ? code - CODES : 0;
}

static u32 cond_met(u32 k, u32 pass)
{
    const u8 *r;

    if (k <= 2)
        return (pass == 0) ^ (k == 2);
    r = ratio[(k - 3) / 2];
    return (pass % r[1] == r[0] - 1u) ^ ((k - 3) & 1);
}

static u8 *step_on(struct seq *seq, int track)
{
    return (u8 *)seq + track * 0x230 + MELODIC_ON;
}

/* Ratchets. Patch RAM is not cleared at start-up: ready() does it once. */
#define R_VALID  0x80                              /* in the on byte: the step has a ratchet */
#define R_MAX    96                                /* ratchets a drum pattern can store */
#define RATE     ((u8 *)0x20006800)                /* [pad * 64 + step]: bits 0..1 hits (1: 2, 2: 4, 3: 8); bits 2..5 velocity ramp, not used yet */
#define R_LIST   ((u8 *)0x20006C00)                /* R_MAX * 2 bytes: the ratchets as stored */
#define SHOW_RATCHET 0xC0                          /* shown value: this | hits code, or | 4 for "full" */

/* What is left of the ratchet a pad is playing. */
struct pend {
    u16 time;       /* timer value of the next hit */
    u16 gap;        /* ticks between hits */
    u8 step, left, queued;
};
#define PEND ((struct pend *)0x20006640)           /* per pad */

/* The step a recorded roll is filling. */
struct rec {
    u8 step, code, count;
};
#define REC ((struct rec *)0x200066C0)             /* per pad */

struct misc {
    u32 magic;
    u8 led;         /* L_ bits of the step whose light is being chosen */
    u8 swallow;     /* the strip touch set a ratchet: it must not start the roller */
    u8 full;        /* the last strip touch found the pattern full */
};
#define MISC ((struct misc *)0x20006700)
#define MISC_MAGIC 0x52415443

#define EVENT    (*(volatile u16 *)0x20000618)     /* timer value of the event being handled */
#define TIM4_DIER (*(volatile u16 *)0x4000080C)
#define TIM4_SR  (*(volatile u16 *)0x40000810)
#define TIM4_CNT (*(volatile u16 *)0x40000824)
#define TIM4_CCR4 (*(volatile u16 *)0x40000840)
#define CC4      0x10
#define LOOPER   0xB35  /* seq + this: the looper is repeating steps */
#define ROLL_ZONE 0x2444 /* seq + this, int: the strip zone, 0 or 1, 2, 4, 8 for 1/4 to 1/32 */
#define ROLL_ON  0x244D /* seq + this: the roller is running */
#define ROLL_PERIOD 0x6B0 /* seq + this, u16: ticks between roller hits */
#define RECORDED_STEP ((volatile u8 *)0x200018A7)  /* per pad: the step the hit being recorded falls in */

void fw_note_off_at(struct seq *seq, int step, int pad, int live);
void fw_merge(void *events, int node);
void fw_drum_events(struct seq *seq, u32 a, u32 b, u32 c);
u32 fw_step_map(struct seq *seq, int track, u32 step, int pad);
void fw_led(void *leds, int index, int colour);
void fw_strip_set(struct seq *seq, u32 zone, int send);
void fw_knob_show(void *buttons, int knob, int on);
u32 fw_swing_get(struct seq *seq, int track);
void fw_swing_set(struct seq *seq, int track, u32 value);
u32 fw_swing_shown(struct seq *seq, int track);
void fw_record_on(int track, u32 note, u32 velocity, int pad);
void fw_record_off(int track, int pad, int tie);

static u32 irq_off(void)
{
    u32 mask;

    __asm__ volatile("mrs %0, primask\n" "cpsid i" : "=r"(mask));
    return mask;
}

static void irq_restore(u32 mask)
{
    __asm__ volatile("msr primask, %0" : : "r"(mask));
}

static void ready(void)
{
    if (MISC->magic == MISC_MAGIC)
        return;
    for (u32 i = 0; i < PADS; i++) {
        PEND[i].left = 0;
        REC[i].count = 0;
    }
    MISC->led = MISC->swallow = MISC->full = 0;
    MISC->magic = MISC_MAGIC;
}

/* -> the hits code of a drum step, 1..3, or 0 when it has no ratchet. */
static u32 ratchet_of(struct seq *seq, u32 pad, u32 step)
{
    if (pad >= PADS || step >= STEPS || !(((u8 *)seq)[DRUM_ON + pad * 0x40 + step] & R_VALID))
        return 0;
    return RATE[pad * 0x40 + step] & 3;
}

/* -> 1 if the step-on event being handled is a later hit of the pad's ratchet. */
static u32 repeating(int pad, int step)
{
    const struct pend *p = &PEND[pad & 15];

    ready();
    return (u32)pad < PADS && p->left && p->step == step && (u16)(EVENT - p->time) < p->gap;
}

/* Replaces the call that schedules the end of a drum step's note, the last
 * thing the step-on path does when the step really played (0x0801E2FE). On a
 * ratcheted step it also asks for the next hit, half, a quarter or an eighth
 * of a step later; flush() puts that in the firmware's own queue of timed
 * step-ons, so the hit plays through the stock path. The gate is taken of
 * the gap between hits and capped, so that each hit ends before the next: the
 * drum gate outputs need the gap to fire again. */
void drum_played(struct seq *seq, int step, int pad, int live)
{
    u8 *s = (u8 *)seq;
    struct pend *p = &PEND[pad & 15];
    u32 code, gate, mask;
    u16 period;

    if (!repeating(pad, step)) {
        code = ratchet_of(seq, pad, step);
        /* Not while an earlier ratchet of this pad still has hits to come. */
        if (!code || s[LOOPER] || (p->left && (s16)(EVENT - p->time) < 0)) {
            fw_note_off_at(seq, step, pad, live);
            return;
        }
        p->gap = fw_step_ticks(seq, *(const int *)(s + DRUM_SIZE), 2) >> code;
        p->step = step;
        p->left = 1 << code;
    }
    if (--p->left) {
        p->time = EVENT + p->gap;
        p->queued = 0;
    }
    gate = s[DRUM_GATE + pad * 0x40 + step];
    if (gate > 90)
        gate = 90;
    /* The stock scheduler takes a live note's length from the roller period. */
    mask = irq_off();
    period = *(u16 *)(s + ROLL_PERIOD);
    *(u16 *)(s + ROLL_PERIOD) = 2 * (p->gap * gate / 100 + 1);
    fw_note_off_at(seq, step, pad, 1);
    *(u16 *)(s + ROLL_PERIOD) = period;
    irq_restore(mask);
}

struct event {
    u32 time;       /* 16-bit timer value; all ones in an empty entry */
    s8 pad, step;   /* the step before the direction mapping */
    u8 unused[2];
};
#define QUEUE(seq) ((struct event *)((u8 *)(seq) + 8))
#define QUEUE_SIZE 32

/* Put the hits asked for into the step-on queue -> 1 if there were any. The
 * handler plays the entries that are due from the front and drops what it
 * finds overdue, so the queue is kept in time order. */
static u32 flush(struct seq *seq)
{
    struct event *queue = QUEUE(seq);
    u32 n = 0, added = 0, mask;
    u16 now;

    ready();
    mask = irq_off();
    while (n < QUEUE_SIZE && queue[n].time != 0xFFFFFFFF)
        n++;
    for (u32 pad = 0; pad < PADS; pad++) {
        struct pend *p = &PEND[pad];

        if (!p->left || p->queued)
            continue;
        if (n >= QUEUE_SIZE - 1) {
            p->left = 0;                           /* queue full: the hit is skipped */
            continue;
        }
        queue[n].time = p->time;
        queue[n].pad = pad;
        queue[n].step = fw_step_map(seq, 2, p->step, pad);
        n++;
        p->queued = added = 1;
    }
    if (added) {
        queue[n].time = 0xFFFFFFFF;
        queue[n].pad = 0x10;
        queue[n].step = -1;
        /* Soonest first; an entry up to 0x200 ticks overdue still counts as soonest. */
        now = TIM4_CNT - 0x200;
        for (u32 i = 1; i < n; i++) {
            struct event e = queue[i];
            u32 j = i;

            for (; j && (u16)(queue[j - 1].time - now) > (u16)(e.time - now); j--)
                queue[j] = queue[j - 1];
            queue[j] = e;
        }
    }
    irq_restore(mask);
    return added;
}

/* Replaces the call that appends a step boundary's timed step-ons to the
 * queue (0x0801691A); the stock code then sets the timer for the first entry. */
void drum_merge(void *events, int node)
{
    fw_merge(events, node);
    flush(SEQ);
}

/* Replaces the timer handler that plays the queued step-ons (0x0800692C).
 * It has set the timer for what was left; the hits asked for while it ran
 * may come sooner. */
void drum_events(struct seq *seq, u32 a, u32 b, u32 c)
{
    struct event *queue = QUEUE(seq);
    u16 at, now;

    fw_drum_events(seq, a, b, c);
    if (!flush(seq))
        return;
    at = queue[0].time;
    now = TIM4_CNT;
    if ((u16)(at - now - 3) >= 0x8000) {           /* too close to catch: as stock does, three ticks from now */
        for (u32 i = 0; i < QUEUE_SIZE && queue[i].time == at; i++)
            queue[i].time = (u16)(now + 3);
        at = now + 3;
    }
    TIM4_SR = (u16)~CC4;
    TIM4_CCR4 = at;
    TIM4_DIER |= CC4;
}

/* -> how many times a drum pad has played its pattern through, for the step
 * about to play. A drum pad's position is not stored and stepped: the
 * firmware works it out from the track's tick count, so the pass is worked
 * out the same way. A step can be played up to half a step early or late
 * (shift, swing), so the step given says which side of a wrap this is. */
static u32 drum_pass(struct seq *seq, u32 step, u32 pad)
{
    const u8 *s = (const u8 *)seq;
    u32 len = s[DRUM_LEN + pad], first = s[DRUM_FIRST + pad], last = s[DRUM_LAST + pad];
    u32 ticks = fw_step_ticks(seq, *(const int *)(s + DRUM_SIZE), 2);
    u32 index, n, at, pass;

    if (len == 0 || ticks == 0 || first >= len || step >= len)
        return 0;
    index = fw_ticks(seq, 2) / ticks;
    if (!s[DRUM_FWD + pad])
        step = len - 1 - step;
    n = last >= first ? last - first + 1 : len - first + last + 1;
    at = (step + len - first) % len % n;
    /* at - index % n, taken as the nearest of early and late. */
    at = (at + n - index % n + n / 2) % n;
    pass = index + at < n / 2 ? 0 : (index + at - n / 2) / n;
    return *(const int *)(s + DRUM_DIR) == 2 ? pass / 2 : pass;
}

/* Replaces fw_step_on at the first step test of the melodic (0x0801DC24)
 * and drum (0x0801E128) playback paths. Returning 0 there skips the step;
 * the second test of each path still reads the raw byte, which keeps a
 * skipped step from being played by the Randomness knob instead. */
u32 trig_step_on(struct seq *seq, int track, int step, int pad)
{
    u32 on = fw_step_on(seq, track, step, pad);
    u32 code = on >> 1 & 0x3f;
    u32 k = cond_of(on);

    /* The later hits of a ratchet: the step was decided at its first. */
    if (track == 2 && on && repeating(pad, step))
        return on;
    if (k && (u32)track < 2) {
        u32 pass = PASS[track];

        if (*(int *)((u8 *)seq + track * 0x230 + DIRECTION) == 2)
            pass /= 2;
        return cond_met(k, pass) ? on : 0;
    }
    if (k && track == 2 && (u32)pad < PADS)
        return cond_met(k, drum_pass(seq, step, pad)) ? on : 0;
    if (code == 0 || code > CODES)
        return on;
    fw_rng_next(seq);
    /* The stock draws use the top 7 bits of the state too. */
    if ((seq->rng >> 25) * 100 < chance[code - 1] * 128u)
        return on;
    return 0;
}

/* -> the on byte of the step held on the current track, or 0. On the drum
 * track it is the step of the selected pad. */
static u8 *held(struct seq *seq)
{
    int track = CUR_TRACK;

    if ((u32)track > 2)
        return 0;
    if (HELD_STEP[track] < 0 || HELD_STEP[track] >= STEPS)
        return 0;
    if (track == 2) {
        u32 pad = *(u32 *)((u8 *)seq + DRUM_PAD);

        return pad < PADS ? (u8 *)seq + DRUM_ON + pad * 0x40 + HELD_STEP[2] : 0;
    }
    return step_on(seq, track) + HELD_STEP[track];
}

/* Letting go of the held step must leave it on, as after the stock
 * hold-a-step-and-press-a-pad. */
static void keep_held(void)
{
    HOLD_USED[CUR_TRACK] = 1;
    if (HELD_PAD[CUR_TRACK] >= 0)
        fw_button_flag(BUTTONS, HELD_PAD[CUR_TRACK] + STEP_BUTTON, 0);
}

/* The held step was changed: the pattern needs saving, and the step stays on. */
static void touched(struct seq *seq)
{
    ((u8 *)seq)[DIRTY + CUR_TRACK] = 1;
    keep_held();
}

/* Replaces the call that shows a knob's value in the Swing, Randomness and
 * Probability handlers (six sites), which comes before the setter and also
 * when the value is at its end and the setter is not called. A held step
 * with a chance, a condition or a ratchet whose value is only looked at stays
 * on when it is let go. A plain step still goes off, as stock. */
void ui_knob_show(void *buttons, int knob, int on)
{
    u8 *step = held(SEQ);

    if (step && *step & ~1u)
        keep_held();
    fw_knob_show(buttons, knob, on);
}

static void set_code(struct seq *seq, u8 *on, u32 code)
{
    *on = (*on & R_VALID) | 1 | code << 1;
    touched(seq);
}

static u32 percent(u32 on)
{
    u32 code = on >> 1 & 0x3f;

    return code == 0 || code > CODES ? 100 : chance[code - 1];
}

/* Replaces fw_rand_get where the Randomness knob handler (0x0802C15A,
 * 0x0802C3F4) and the display (0x08009A28) read the knob value. */
u32 ui_chance_get(struct seq *seq, int track)
{
    u8 *on = held(seq);

    return on && *on ? percent(*on) : fw_rand_get(seq, track);
}

/* Replaces fw_rand_set in the knob handler (0x0802C18A, 0x0802C424). The
 * handler passes the value it read plus or minus one, inside 0..100. */
void ui_chance_set(struct seq *seq, int track, u32 value)
{
    u8 *on = held(seq);
    u32 code;

    if (!on || !*on) {
        fw_rand_set(seq, track, value);
        return;
    }
    if (value >= 100) {
        code = 0;
    } else if (value > percent(*on)) {
        for (code = 1; chance[code - 1] < value; code++)
            ;
    } else {
        for (code = CODES; code > 1 && chance[code - 1] > value; code--)
            ;
    }
    set_code(seq, on, code);
}

/* Replaces fw_prob_get where the Probability knob handler reads the knob
 * value (0x0802C1DC, 0x0802C476): the condition of the held step, 0..CONDS. */
u32 ui_cond_get(struct seq *seq, int track)
{
    u8 *on = held(seq);

    return on && *on ? cond_of(*on) : fw_prob_get(seq, track);
}

/* The same for the display (0x08009A58). The caller sign-extends the byte and
 * hands it to the number display, which show_value replaces: a negative value
 * is a condition. Nothing stock passes a negative number there. */
u32 ui_cond_shown(struct seq *seq, int track)
{
    u8 *on = held(seq);

    return on && *on ? 0x80 | cond_of(*on) : fw_prob_get(seq, track);
}

/* Replaces fw_prob_set in the knob handler (0x0802C20C, 0x0802C4A6). */
void ui_cond_set(struct seq *seq, int track, u32 value)
{
    u8 *on = held(seq);

    if (!on || !*on) {
        fw_prob_set(seq, track, value);
        return;
    }
    if (value > CONDS)
        value = CONDS;
    /* n12 and n22 play on the same passes as 2-2 and 1-2: the knob steps over
     * them. A step saved with one keeps it and plays as before. */
    if (value == N12 || value == N22)
        value += value > ui_cond_get(seq, track) ? 1 : -1;
    set_code(seq, on, value ? CODES + value : 0);
}


/* -> the on byte of the drum step held, if it is on, else 0. */
static u8 *drum_held(struct seq *seq)
{
    u8 *on = CUR_TRACK == 2 ? held(seq) : 0;

    return on && *on ? on : 0;
}

static u32 ratchets(struct seq *seq)
{
    u32 count = 0;

    for (u32 i = 0; i < PADS * 0x40; i++)
        count += ((u8 *)seq)[DRUM_ON + i] >> 7;
    return count;
}

/* Give a drum step that is on a ratchet, or with code 0 take it away -> 0 if
 * the pattern has no room for another. */
static u32 set_ratchet(struct seq *seq, u32 pad, u32 step, u32 code)
{
    u8 *on = (u8 *)seq + DRUM_ON + pad * 0x40 + step;

    if (!code) {
        *on &= ~R_VALID;
    } else {
        if (!(*on & R_VALID) && ratchets(seq) >= R_MAX)
            return 0;
        RATE[pad * 0x40 + step] = code;
        *on |= R_VALID;
    }
    ((u8 *)seq)[DIRTY + 2] = 1;
    return 1;
}

/* -> the hits code for a strip zone on this track: how many times finer than
 * the step size it is, as a power of two; 0 when it is not finer. */
static u32 zone_code(struct seq *seq, u32 zone)
{
    u32 size = *(const u32 *)((u8 *)seq + DRUM_SIZE) & 3, code = 0;

    for (zone >>= size; zone > 1 && code < 3; zone >>= 1)
        code++;
    return code;
}

/* Replaces the setter the touch strip reader calls with the zone it found
 * (0x08007CC0 and four more). With a drum step held the touch sets that
 * step's ratchet and is kept from the roller and the looper until the finger
 * is lifted. */
void strip_set(struct seq *seq, u32 zone, int send)
{
    ready();
    if (!zone)
        MISC->swallow = 0;
    else if (drum_held(seq)) {
        u32 pad = *(u32 *)((u8 *)seq + DRUM_PAD);

        MISC->full = !set_ratchet(seq, pad, HELD_STEP[2], zone_code(seq, zone));
        MISC->swallow = 1;
        REC[pad].count = 0;
        touched(seq);
        fw_knob_show(BUTTONS, 0x11, 1);            /* the Swing knob's value: ui_swing_shown */
    }
    fw_strip_set(seq, MISC->swallow ? 0 : zone, send);
}

/* Replace the Swing knob's getter (0x0802C0D2, 0x0802C372) and setter
 * (0x0802C108, 0x0802C3A2). With a drum step held the knob leaves the swing
 * alone and the display shows the step's ratchet. The handler only calls the
 * setter inside 50..75, so the getter answers from the middle. */
u32 ui_swing_get(struct seq *seq, int track)
{
    return drum_held(seq) ? 60 : fw_swing_get(seq, track);
}

void ui_swing_set(struct seq *seq, int track, u32 value)
{
    if (drum_held(seq)) {
        MISC->full = 0;
        keep_held();
    } else {
        fw_swing_set(seq, track, value);
    }
}

/* Replaces the read of the swing for the display (0x080099F8); show_value
 * turns the answer into r 1, r 2, r 4, r 8 or FuL. */
u32 ui_swing_shown(struct seq *seq, int track)
{
    if (!drum_held(seq))
        return fw_swing_shown(seq, track);
    ready();
    return SHOW_RATCHET | (MISC->full ? 4 : ratchet_of(seq, *(u32 *)((u8 *)seq + DRUM_PAD), HELD_STEP[2]));
}

/* Step lights. The main loop repaints the step buttons on every pass, so a
 * light can blink by the SysTick count (1 kHz). A cycle is 16 slots of 128 ms;
 * a set bit is a lit slot. */
#define MS        (*(volatile u32 *)0x20000574)
#define L_CYAN    0x01                             /* in MISC->led: the step has a ratchet */
#define L_CHANCE  0x02                             /* it has a chance */
#define L_COND    0x04                             /* it has a condition */
#define LIT_CHANCE 0x7F7F                          /* two short blinks in two seconds */
#define LIT_COND   0xAFFF                          /* a double blink in two seconds */
#define LED_OFF   8

/* -> how the light of an on step differs from the track colour: L_ bits. A
 * held step does not blink, so that what the knobs change stays readable. */
static u32 look_of(struct seq *seq, int track, int step, int pad, u32 on)
{
    u32 code = on >> 1 & 0x3f, look = track == 2 && ratchet_of(seq, pad, step) ? L_CYAN : 0;

    if ((u32)track > 2 || HELD_STEP[track] == step)
        return look;
    if (cond_of(on))
        return look | L_COND;
    return code && code <= CODES ? look | L_CHANCE : look;
}

/* Replace, where the step buttons are lit, the read of each step (0x08008A1A,
 * 0x08008B5A) and the call that gives an on step the track's colour (four
 * sites): a ratcheted step is cyan, a step with a chance or a condition
 * blinks. The play cursor is painted after. */
u32 led_step_on(struct seq *seq, int track, int step, int pad)
{
    u32 on = fw_step_on(seq, track, step, pad);

    MISC->led = on ? look_of(seq, track, step, pad, on) : 0;
    return on;
}

void led_colour(void *leds, int index, int colour)
{
    u32 look = MISC->led, slot = MS >> 7 & 15;

    MISC->led = 0;
    if (look & L_CYAN)
        colour = 4;
    if ((look & L_CHANCE && !(LIT_CHANCE >> slot & 1)) || (look & L_COND && !(LIT_COND >> slot & 1)))
        colour = LED_OFF;
    fw_led(leds, index, colour);
}

/* Replaces the calls that note a played pad for recording (ten sites).
 * While the roller runs finer than the track, count the hits that fall in
 * the same step. */
void rec_on(int track, u32 note, u32 velocity, int pad)
{
    struct seq *seq = SEQ;
    const u8 *s = (const u8 *)seq;
    struct rec *r = &REC[pad & 15];
    u32 code;

    fw_record_on(track, note, velocity, pad);
    if (track != 2 || (u32)pad >= PADS)
        return;
    ready();
    code = s[ROLL_ON] ? zone_code(seq, *(const u32 *)(s + ROLL_ZONE)) : 0;
    if (code && r->count && r->step == RECORDED_STEP[pad] && r->code == code) {
        if (r->count < 2)
            r->count++;
    } else {
        r->step = RECORDED_STEP[pad];
        r->code = code;
        r->count = code != 0;
    }
}

/* Replaces the call that writes a recorded hit into its step (seven sites).
 * The write leaves the step plainly on; a step that got two hits of a finer
 * roll is given the roll's rate after it. */
void rec_off(int track, int pad, int tie)
{
    const struct rec *r = &REC[pad & 15];

    fw_record_off(track, pad, tie);
    if (track != 2 || (u32)pad >= PADS)
        return;
    ready();
    if (r->count >= 2 && r->step < STEPS && ((u8 *)SEQ)[DRUM_ON + pad * 0x40 + r->step])
        set_ratchet(SEQ, pad, r->step, r->code);
}

/* Replaces the setter where a press of a step button switches the step on
 * (0x08018B02). The press comes before the release decides whether to turn
 * it off, so it also hits a step that is already on: that step keeps its
 * chance or condition and its ratchet. The stock setter still runs, for the
 * drum step count it keeps. */
void press_on(struct seq *seq, int track, u32 on, int step, int pad)
{
    u8 *at = (u32)step >= STEPS ? 0 : track == 2 ? ((u32)pad < PADS ? (u8 *)seq + DRUM_ON + pad * 0x40 + step : 0)
             : (u32)track < 2 ? step_on(seq, track) + step : 0;
    u32 was = at ? *at : 0;

    fw_step_on_set(seq, track, on, step, pad);
    if (on && was)
        *at = was;
}

/* Display glyphs: 0..9 are the digits. */
#define G_DASH 0x20
#define G_N    0x25
#define G_S    0x1C
#define G_T    0x1B
#define G_R    0x1E
#define G_L    0x1D
#define G_U    0x28
#define G_F    0x0F
#define G_NONE 0x2C

/* Replaces the number display where knob_display_value shows a knob's value
 * (0x08009C96, 0x08009CCC, 0x08009D9E, 0x08009E02). */
void show_value(void *display, int field, int value)
{
    u32 k = value & 0x7f;

    if (value == (s8)(SHOW_RATCHET | 4)) {
        fw_show_glyphs(display, field, G_F, G_U, G_L);
    } else if (value >= (s8)SHOW_RATCHET && value < (s8)(SHOW_RATCHET | 4)) {
        fw_show_glyphs(display, field, G_R, G_NONE, 1 << (value & 3));
    } else if (value >= 0 || k > CONDS) {
        fw_show_number(display, field, value);
    } else if (k == 0) {
        fw_show_glyphs(display, field, 1, G_DASH, 1);
    } else if (k == 1) {
        fw_show_glyphs(display, field, 1, G_S, G_T);
    } else if (k == 2) {
        fw_show_glyphs(display, field, G_N, 1, G_S);
    } else {
        const u8 *r = ratio[(k - 3) / 2];

        if ((k - 3) & 1)
            fw_show_glyphs(display, field, G_N, r[0], r[1]);
        else
            fw_show_glyphs(display, field, r[0], G_DASH, r[1]);
    }
}

/* Replaces fw_pos_set where the timer handlers move a melodic track to its
 * next step (0x08012A10). The position only goes down, or stays, when the
 * pattern or the looped range starts again. */
void pos_advance(struct seq *seq, int track, u32 position, int pad)
{
    if ((u32)track < 2 && position <= ((u8 *)seq)[track * 0x230 + POSITION])
        PASS[track]++;
    fw_pos_set(seq, track, position, pad);
}

/* Replaces fw_pos_set where a melodic track is put back to step 0: Play from
 * stop (0x08015182, 0x08015194), Stop (0x080157F8) and the restart after a
 * pattern change (0x08014ACE). */
void pos_reset(struct seq *seq, int track, u32 position, int pad)
{
    if ((u32)track < 2)
        PASS[track] = 0;
    for (u32 i = 0; i < PADS; i++)
        PEND[i].left = 0;
    fw_pos_set(seq, track, position, pad);
}

static u8 check(const u8 *codes)
{
    u8 sum = 0x5A;

    for (int i = 0; i < STEPS * 6 / 8; i++)
        sum ^= codes[i];
    return sum;
}

/* Replaces fw_storage_write where the two melodic pattern records are
 * written (0x0800CBBA, 0x0800CF84). */
u32 save_melodic(void *storage, u32 address, u8 *rec, u32 length)
{
    const u8 *on = step_on(SEQ, SAVE_TRACK);
    u8 *out = rec + REC_CODES;

    for (int i = 0; i < STEPS; i += 4) {
        u32 a = on[i] >> 1 & 0x3f, b = on[i + 1] >> 1 & 0x3f;
        u32 c = on[i + 2] >> 1 & 0x3f, d = on[i + 3] >> 1 & 0x3f;

        *out++ = a | b << 6;
        *out++ = b >> 2 | c << 4;
        *out++ = c >> 4 | d << 2;
    }
    rec[REC_MAGIC] = MAGIC;
    rec[REC_CHECK] = check(rec + REC_CODES);
#ifdef POLY
    poly_save(storage, address, rec);
#endif
    return fw_storage_write(storage, address, rec, length);
}

/* Give the steps a pattern load just switched on their saved codes. */
void load_codes(struct seq *seq, int track, void **caller_sp)
{
    const u8 *in = LOADED + REC_CODES;
    u8 *on = step_on(seq, track);

    if (LOADED[REC_MAGIC] == MAGIC && LOADED[REC_CHECK] == check(in)) {
        for (int i = 0; i < STEPS; i += 4, in += 3) {
            u32 codes[4] = {in[0] & 0x3f, (in[0] >> 6 | in[1] << 2) & 0x3f,
                            (in[1] >> 4 | in[2] << 4) & 0x3f, in[2] >> 2};

            for (int k = 0; k < 4; k++)
                if (on[i + k])
                    on[i + k] = 1 | codes[k] << 1;
        }
    }
#ifdef POLY
    /* patterns_load keeps its storage object at sp + 0x68. */
    poly_load(caller_sp[0x68 / 4], track);
#else
    (void)caller_sp;
#endif
}

/* Drum codes on the storage chip. A drum pattern owns 0x1000 bytes and stock
 * firmware writes 0x000..0xC0F of them; its last write is 16 bytes at 0xC00.
 * The codes go behind that, a 16-step page q of all 16 pads (192 bytes) at
 * a time, so that the stock loader's piecewise loads can each be followed:
 *
 *   0xC10  0xD7 if codes are stored; then for q 0..3 a 16-bit hash of the
 *          page's shifts and on bits, then for q 0..3 a check byte of its
 *          codes, then how many ratchets are stored and their check byte
 *   0xC40  codes of page 0        0xD00, 0xE00, 0xF00  codes of pages 1, 2, 3
 *   0xDC0, 0xEC0, 0xFC0  the ratchets, 32 to a block: step | hits code << 6,
 *          then pad | velocity ramp << 4
 *
 * The hash ties the codes to the steps they were saved with: a pattern that
 * was since written by stock firmware or MIDI Control Center loads without
 * them. Writing part of a 256-byte page leaves the rest alone on this chip;
 * stock relies on the same for its 16-byte pattern settings. */
#define D_HEAD    0x10
#define D_HEAD_SIZE 0x30
#define D_MAGIC   0xD7
#define D_CODES   (PADS * 16 * 6 / 8)             /* 192 */
#define D_TAIL    0x40                            /* a block of ratchets behind a page's codes */
#define D_BUF     ((u8 *)0x20006500)              /* patch RAM, D_HEAD_SIZE + D_CODES + D_TAIL bytes */
#define H_COUNT   13                              /* in the head: ratchets stored */
#define H_RCHECK  14
#define BUSY      (*(volatile u8 *)0x2000016D)    /* set when something else used the storage buffers */
#define LAST_ADDR ((const u8 *)0x20000752 + 1)    /* address of the last storage transfer, high byte first */

static u8 *drum_on(struct seq *seq, u32 pad, u32 step)
{
    return (u8 *)seq + DRUM_ON + pad * 0x40 + step;
}

static u32 page_hash(struct seq *seq, u32 q)
{
    u32 hash = 0x1D0F + q;

    for (u32 pad = 0; pad < PADS; pad++)
        for (u32 i = 0; i < 16; i++) {
            u32 step = q * 16 + i;
            u32 note = ((u8 *)seq)[DRUM_SHIFT + pad * 0x40 + step] & 0x7f;

            hash = (hash * 31 + (note | (*drum_on(seq, pad, step) ? 0x80 : 0))) & 0xffff;
        }
    return hash;
}

/* Pack the codes of page q into out, D_CODES bytes -> 1 if any is set. */
static u32 pack_page(struct seq *seq, u32 q, u8 *out)
{
    u32 any = 0;

    for (u32 pad = 0; pad < PADS; pad++)
        for (u32 i = 0; i < 16; i += 4) {
            const u8 *on = drum_on(seq, pad, q * 16 + i);
            u32 a = on[0] >> 1 & 0x3f, b = on[1] >> 1 & 0x3f;
            u32 c = on[2] >> 1 & 0x3f, d = on[3] >> 1 & 0x3f;

            *out++ = a | b << 6;
            *out++ = b >> 2 | c << 4;
            *out++ = c >> 4 | d << 2;
            any |= a | b | c | d;
        }
    return any != 0;
}


/* Write the pattern's ratchets to R_LIST -> how many. */
static u32 list_ratchets(struct seq *seq)
{
    u32 count = 0;

    for (u32 i = 0; i < R_MAX * 2; i++)
        R_LIST[i] = 0;
    for (u32 pad = 0; pad < PADS; pad++)
        for (u32 step = 0; step < STEPS; step++) {
            u32 code = ratchet_of(seq, pad, step);

            if (!code || count >= R_MAX)
                continue;
            R_LIST[count * 2] = step | code << 6;
            R_LIST[count * 2 + 1] = pad | (RATE[pad * 0x40 + step] >> 2 & 15) << 4;
            count++;
        }
    return count;
}

static u8 list_check(const u8 *list)
{
    u8 sum = 0xA5;

    for (u32 i = 0; i < R_MAX * 2; i++)
        sum ^= list[i];
    return sum;
}

/* Give the steps of the pages that loaded well (bits of good) their stored
 * ratchets. The stock loader has left those steps plainly on. */
static void load_ratchets(struct seq *seq, const u8 *head, u32 good)
{
    u8 *list = R_LIST;
    u32 count = head[H_COUNT];

    for (u32 i = 0; i < R_MAX * 2; i++)
        list[i] = LOADED[(1 + i / D_TAIL) * 0x100 + D_CODES + i % D_TAIL];
    if (count > R_MAX || head[H_RCHECK] != list_check(list))
        return;
    for (u32 i = 0; i < count; i++) {
        u32 step = list[i * 2] & 0x3f, code = list[i * 2] >> 6, pad = list[i * 2 + 1] & 15;
        u8 *on = drum_on(seq, pad, step);

        if (code && (good >> (step >> 4) & 1) && *on && !(*on & R_VALID)) {
            RATE[pad * 0x40 + step] = code | (list[i * 2 + 1] >> 4) << 2;
            *on |= R_VALID;
        }
    }
}

static u8 drum_check(const u8 *codes)
{
    u8 sum = 0x5A;

    for (u32 i = 0; i < D_CODES; i++)
        sum ^= codes[i];
    return sum;
}

/* Replaces fw_storage_write where a drum pattern's last block is written
 * (0x0800D51A): address is the pattern's area + 0xC00. */
u32 save_drum(void *storage, u32 address, u8 *data, u32 length)
{
    struct seq *seq = SEQ;
    u8 *head = D_BUF, *codes = D_BUF + D_HEAD_SIZE;
    u32 any = 0, count = list_ratchets(seq);

    for (u32 i = 0; i < D_HEAD_SIZE; i++)
        head[i] = 0;
    for (u32 q = 0; q < 4; q++)
        any |= pack_page(seq, q, codes);
    if (any | count) {
        head[0] = D_MAGIC;
        head[H_COUNT] = count;
        head[H_RCHECK] = list_check(R_LIST);
        for (u32 q = 4; q-- > 0;) {
            u32 hash = page_hash(seq, q);

            pack_page(seq, q, codes);
            head[1 + q * 2] = hash;
            head[2 + q * 2] = hash >> 8;
            head[9 + q] = drum_check(codes);
            if (q) {
                for (u32 i = 0; i < D_TAIL; i++)
                    codes[D_CODES + i] = R_LIST[(q - 1) * D_TAIL + i];
                fw_storage_write(storage, address + q * 0x100, codes, D_CODES + D_TAIL);
            }
        }
        /* Page 0 is still in codes, right behind the head. */
        fw_storage_write(storage, address + D_HEAD, head, D_HEAD_SIZE + D_CODES);
    } else {
        fw_storage_write(storage, address + D_HEAD, head, D_HEAD_SIZE);
    }
    return fw_storage_write(storage, address, data, length);
}

/* Give the drum steps a load just switched on their saved codes. Only steps
 * that are exactly 1 are touched: those are the ones the stock loader set. */
void load_drum_codes(struct seq *seq, void **caller_sp)
{
    void *storage = caller_sp[0x68 / 4];          /* patterns_load keeps it at sp + 0x68 */
    u32 address = LAST_ADDR[0] << 16 | LAST_ADDR[1] << 8 | LAST_ADDR[2];
    u32 in_project = address % 0x13000;
    const u8 *head = LOADED + D_HEAD;
    u32 good;

    ready();
    if (in_project < 0x2000 || in_project >= 0x12000)
        return;                                   /* the last read was not of a drum pattern */
    address = (address & ~0xFFFu) + 0xC00;
    do {
        BUSY = 0;
        fw_storage_read(storage, address, 0x400);
        if (head[0] != D_MAGIC)
            continue;
        good = 0;
        for (u32 q = 0; q < 4; q++) {
            const u8 *in = q ? LOADED + q * 0x100 : head + D_HEAD_SIZE;

            if (head[9 + q] != drum_check(in) || (u32)(head[1 + q * 2] | head[2 + q * 2] << 8) != page_hash(seq, q))
                continue;
            good |= 1u << q;
            for (u32 pad = 0; pad < PADS; pad++)
                for (u32 i = 0; i < 16; i += 4, in += 3) {
                    u32 codes[4] = {in[0] & 0x3f, (in[0] >> 6 | in[1] << 2) & 0x3f,
                                    (in[1] >> 4 | in[2] << 4) & 0x3f, in[2] >> 2};

                    for (int j = 0; j < 4; j++) {
                        u8 *on = drum_on(seq, pad, q * 16 + i + j);

                        if (*on == 1 && codes[j] <= CODES + CONDS)
                            *on = 1 | codes[j] << 1;
                    }
                }
        }
        load_ratchets(seq, head, good);
    } while (BUSY);
    BUSY = 1;
}

/* Replaces the call that ends a drum pattern load (0x0800E8DA), keeping its
 * arguments. */
__attribute__((naked)) void load_drum(void)
{
    __asm__("push {r0-r3, r4, lr}\n"
            "add r1, sp, #24\n"
            "bl load_drum_codes\n"
            "pop {r0-r3, r4, lr}\n"
            "b.w fw_drum_loaded\n");
}

/* Replaces fw_storage_write where copy project writes a drum pattern's last
 * block to the other project (0x0800D984): the codes go with it. data is in
 * the read buffer, which the reads here overwrite. */
u32 copy_drum(void *storage, u32 address, u8 *data, u32 length)
{
    u32 from = LAST_ADDR[0] << 16 | LAST_ADDR[1] << 8 | LAST_ADDR[2];
    u8 keep[16];
    u32 stored;

    for (u32 i = 0; i < sizeof keep && i < length; i++)
        keep[i] = data[i];
    fw_storage_read(storage, from + D_HEAD, D_HEAD_SIZE + D_CODES);
    stored = LOADED[0] == D_MAGIC;
    fw_storage_write(storage, address + D_HEAD, (u8 *)LOADED, stored ? D_HEAD_SIZE + D_CODES : D_HEAD_SIZE);
    for (u32 q = 1; stored && q < 4; q++) {
        fw_storage_read(storage, from + q * 0x100, D_CODES + D_TAIL);
        fw_storage_write(storage, address + q * 0x100, (u8 *)LOADED, D_CODES + D_TAIL);
    }
    return fw_storage_write(storage, address, keep, length);
}

/* Replaces fw_set_length where a melodic pattern load finishes (0x0800DDF2,
 * 0x0800E0F6): the last call after the steps are set, with seq in r0, the
 * track in r1 and two more arguments on the stack that must stay put. */
__attribute__((naked)) void load_melodic(void)
{
    __asm__("push {r0-r3, r4, lr}\n"
            "add r2, sp, #24\n"
            "bl load_codes\n"
            "pop {r0-r3, r4, lr}\n"
            "b.w fw_set_length\n");
}
