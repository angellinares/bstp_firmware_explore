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
 */
#include "fw.h"

u32 fw_step_on(struct seq *seq, int track, int step, int pad);
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
#define DRUM_NOTE  0xBD9  /* seq + this + pad * 0x40 + step */
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

static void set_code(struct seq *seq, u8 *on, u32 code)
{
    *on = 1 | code << 1;
    ((u8 *)seq)[DIRTY + CUR_TRACK] = 1;
    /* As the stock hold-a-step-and-press-a-pad gesture does, so that
     * letting go of the step leaves it on. */
    HOLD_USED[CUR_TRACK] = 1;
    if (HELD_PAD[CUR_TRACK] >= 0)
        fw_button_flag(BUTTONS, HELD_PAD[CUR_TRACK] + STEP_BUTTON, 0);
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
    set_code(seq, on, value ? CODES + value : 0);
}

/* Display glyphs: 0..9 are the digits. */
#define G_DASH 0x20
#define G_N    0x25
#define G_S    0x1C
#define G_T    0x1B

/* Replaces the number display where knob_display_value shows a knob's value
 * (0x08009C96, 0x08009CCC, 0x08009D9E, 0x08009E02). */
void show_value(void *display, int field, int value)
{
    u32 k = value & 0x7f;

    if (value >= 0 || k > CONDS) {
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
 *          page's notes and on bits, then for q 0..3 a check byte of its codes
 *   0xC40  codes of page 0        0xD00, 0xE00, 0xF00  codes of pages 1, 2, 3
 *
 * The hash ties the codes to the steps they were saved with: a pattern that
 * was since written by stock firmware or MIDI Control Center loads without
 * them. Writing part of a 256-byte page leaves the rest alone on this chip;
 * stock relies on the same for its 16-byte pattern settings. */
#define D_HEAD    0x10
#define D_HEAD_SIZE 0x30
#define D_MAGIC   0xD7
#define D_CODES   (PADS * 16 * 6 / 8)             /* 192 */
#define D_BUF     ((u8 *)0x20006500)              /* patch RAM, D_HEAD_SIZE + D_CODES bytes */
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
            u32 note = ((u8 *)seq)[DRUM_NOTE + pad * 0x40 + step] & 0x7f;

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
    u32 any = 0;

    for (u32 i = 0; i < D_HEAD_SIZE; i++)
        head[i] = 0;
    for (u32 q = 0; q < 4; q++)
        any |= pack_page(seq, q, codes);
    if (any) {
        head[0] = D_MAGIC;
        for (u32 q = 4; q-- > 0;) {
            u32 hash = page_hash(seq, q);

            pack_page(seq, q, codes);
            head[1 + q * 2] = hash;
            head[2 + q * 2] = hash >> 8;
            head[9 + q] = drum_check(codes);
            if (q)
                fw_storage_write(storage, address + q * 0x100, codes, D_CODES);
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

    if (in_project < 0x2000 || in_project >= 0x12000)
        return;                                   /* the last read was not of a drum pattern */
    address = (address & ~0xFFFu) + 0xC00;
    do {
        BUSY = 0;
        fw_storage_read(storage, address, 0x400);
        if (head[0] != D_MAGIC)
            continue;
        for (u32 q = 0; q < 4; q++) {
            const u8 *in = q ? LOADED + q * 0x100 : head + D_HEAD_SIZE;

            if (head[9 + q] != drum_check(in) || (u32)(head[1 + q * 2] | head[2 + q * 2] << 8) != page_hash(seq, q))
                continue;
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
        fw_storage_read(storage, from + q * 0x100, D_CODES);
        fw_storage_write(storage, address + q * 0x100, (u8 *)LOADED, D_CODES);
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
