"""Ratchets on drum steps, run on the patched image in an emulator.

The firmware's own queue handler, queue merge and step mapping run for real
here, on a queue the tests fill by hand; the step-on path, the timer, the touch
strip chip and the recording path do not. So these prove the bookkeeping, not
the sound or the timing on a device.
"""
import struct

import pytest

from test_drum import (AREA, DRUM_FWD, DRUM_LEN, DRUM_PAD, DRUM_SIZE, OTHER, READ_BUFFER, STEP_TICKS, STORAGE,
                       STORAGE_READ, Chip, hold, on_addr, pad_setup, pattern, reload)
from test_trig import BUTTON_FLAG, CUR_TRACK, HELD_STEP, HOLD_USED, RNG_NEXT, SEQ, STEP_ON, SYM, fw, seed  # noqa: F401

RATE = 0x20006800        # + pad * 0x40 + step
PEND = 0x20006640        # + pad * 8: time, gap, step, left, queued
REC = 0x200066C0         # + pad * 3: step, code, count
MISC = 0x20006700        # magic, led, swallow, full
EVENT = 0x20000618
TIM4 = 0x40000800
DIER, SR, CNT, CCR4 = TIM4 + 0x0C, TIM4 + 0x10, TIM4 + 0x24, TIM4 + 0x40
QUEUE = SEQ + 8          # 32 entries: u32 time, s8 pad, s8 step, 2 unused
QUEUE_INDEX = 0x20000580
DRUM_GATE = 0x13D9
LOOPER = 0xB35
ROLL_ZONE = 0x2444
ROLL_ON = 0x244D
ROLL_PERIOD = 0x6B0
DRUM_DIRTY = 0x2452
RECORDED_STEP = 0x200018A7
NOTE_OFF_AT = 0x080136A8
POSITION_SET = 0x08012DD0
PAD_STEP = 0x0801013C
STRIP_SET = 0x080125C0
KNOB_SHOW = 0x0801FAC4
SWING_GET = 0x080159BA
SWING_SET = 0x08015B4A
SWING_SHOWN = 0x08012654
LED = 0x0801AF30
RECORD_ON = 0x0802D2F0
RECORD_OFF = 0x0802D48C
SHOW_NUMBER = 0x08023BEA
SHOW_GLYPHS = 0x08023BA2
EMPTY = struct.pack("<IbbH", 0xFFFFFFFF, 0x10, -1, 0)
TICK = 120               # a 1/16 step


def ratchet(fw, pad, step, code, on=1):
    fw.write(on_addr(pad, step), [on | 0x80])
    fw.write(RATE + pad * 0x40 + step, [code])


def pend(fw, pad):
    time, gap, step, left, queued = struct.unpack("<HHBBB", fw.read(PEND + pad * 8, 7))
    return {"time": time, "gap": gap, "step": step, "left": left, "queued": queued}


def queue(fw):
    found = []
    for i in range(32):
        time, pad, step = struct.unpack("<Ibb", fw.read(QUEUE + i * 8, 6))
        if time == 0xFFFFFFFF:
            break
        found.append((time, pad, step))
    return found


def fill(fw, entries=()):
    fw.write(QUEUE, EMPTY * 32)
    for i, (time, pad, step) in enumerate(entries):
        fw.write(QUEUE + i * 8, struct.pack("<Ibb", time, pad, step))


@pytest.fixture
def drum(fw):
    """A forward 16-step pad 0..15 on a 1/16 track, an empty queue, the timer at 1000."""
    fw.spy(STEP_TICKS, count=3, result=TICK)
    for pad in range(16):
        pad_setup(fw, pad)
        fw.write(SEQ + DRUM_GATE + pad * 0x40, [50] * 64)
    fw.write(SEQ + DRUM_SIZE, struct.pack("<i", 2))
    fw.write(SEQ + ROLL_PERIOD, struct.pack("<H", 777))
    fill(fw)
    at(fw, 1000)
    return fw


def at(fw, time):
    for addr in (EVENT, CNT, CCR4):
        fw.write(addr, struct.pack("<H", time & 0xFFFF))


def offs(fw):
    """Spy the stock scheduler of a note's end -> [(step, pad, live, roller period at the call)]."""
    calls = []
    fw.spy(NOTE_OFF_AT, then=lambda a: calls.append(
        (a[1], a[2], a[3], struct.unpack("<H", fw.read(SEQ + ROLL_PERIOD, 2))[0])))
    return calls


# The first hit

def test_a_plain_step_ends_as_stock(drum):
    calls = offs(drum)
    drum.write(on_addr(3, 5), [1])
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    assert calls == [(5, 3, 0, 777)]
    assert pend(drum, 3)["left"] == 0 and queue(drum) == []


@pytest.mark.parametrize("code, hits", [(1, 2), (2, 4), (3, 8)])
def test_a_ratcheted_step_asks_for_its_next_hit(drum, code, hits):
    calls = offs(drum)
    drum.spy(STEP_TICKS, count=3, result=480)             # a 1/4 track: 8 hits are 60 ticks apart
    ratchet(drum, 3, 5, code)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    gap = 480 // hits
    assert pend(drum, 3) == {"time": 1000 + gap, "gap": gap, "step": 5, "left": hits - 1, "queued": 0}
    # The note ends after the step's gate (50 percent) of the gap between hits.
    assert calls == [(5, 3, 1, 2 * (gap * 50 // 100 + 1))]
    assert drum.read(SEQ + ROLL_PERIOD, 2) == struct.pack("<H", 777), "the roller period is put back"


def test_a_tied_step_still_ends_before_its_next_hit(drum):
    calls = offs(drum)
    ratchet(drum, 0, 0, 1)
    drum.write(SEQ + DRUM_GATE, [100])
    drum.call(SYM["drum_played"], SEQ, 0, 0, 0)
    assert calls == [(0, 0, 1, 2 * (60 * 90 // 100 + 1))]


def test_no_ratchets_while_the_looper_repeats(drum):
    calls = offs(drum)
    ratchet(drum, 3, 5, 1)
    drum.write(SEQ + LOOPER, [1])
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    assert calls == [(5, 3, 0, 777)] and pend(drum, 3)["left"] == 0


def test_a_ratchet_bit_without_the_step_being_on_is_nothing(drum):
    """Stock firmware clears the whole byte when it turns a step off."""
    calls = offs(drum)
    drum.write(RATE + 3 * 0x40 + 5, [2])
    drum.write(on_addr(3, 5), [1])                        # on again, as the stock setter writes it
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    assert calls == [(5, 3, 0, 777)]


def test_a_second_ratchet_waits_for_the_first_of_its_pad(drum):
    offs(drum)
    ratchet(drum, 3, 5, 2)
    ratchet(drum, 3, 6, 1)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    at(drum, 1010)                                        # step 6 pulled early, step 5 still has hits to come
    drum.call(SYM["drum_played"], SEQ, 6, 3, 0)
    assert pend(drum, 3)["step"] == 5
    at(drum, 1000 + TICK)                                 # long after: what was left of step 5 never played
    drum.call(SYM["drum_played"], SEQ, 6, 3, 0)
    assert pend(drum, 3) == {"time": 1000 + TICK + 60, "gap": 60, "step": 6, "left": 1, "queued": 0}


# The queue

def test_the_next_hit_joins_the_queue_in_time_order(drum):
    offs(drum)
    fill(drum, [(1020, 7, 2), (1100, 8, 2)])
    merged = drum.spy(0x08027DBC, count=2)
    ratchet(drum, 3, 5, 1)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    drum.call(SYM["drum_merge"], 0x2000A000, 0)
    assert merged == [[0x2000A000, 0]]
    assert queue(drum) == [(1020, 7, 2), (1060, 3, 5), (1100, 8, 2)]
    assert pend(drum, 3)["queued"] == 1
    drum.call(SYM["drum_merge"], 0x2000A000, 0)
    assert len(queue(drum)) == 3, "it is put in once"


def test_what_the_boundary_adds_is_sorted_in_too(drum):
    """The real merge appends the boundary's shifted steps behind what is queued."""
    offs(drum)
    fill(drum, [(1100, 8, 2)])
    tree = 0x2000A000                                     # one node: time, pad, step, no children
    drum.write(tree, struct.pack("<IbbBB", 1030, 9, 4, 0xFF, 0xFF))
    drum.write(QUEUE_INDEX, [1])                          # the stock caller counts the queue first
    ratchet(drum, 3, 5, 1)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    drum.call(SYM["drum_merge"], tree, 0)
    assert queue(drum) == [(1030, 9, 4), (1060, 3, 5), (1100, 8, 2)]


def test_the_order_holds_across_the_timer_wrap(drum):
    offs(drum)
    at(drum, 0xFFF0)
    fill(drum, [(0x0005, 7, 2), (0x0060, 8, 2)])
    drum.spy(0x08027DBC, count=2)
    ratchet(drum, 3, 5, 1)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)           # next hit at 0xFFF0 + 60 = 0x002C
    drum.call(SYM["drum_merge"], 0, 0)
    assert queue(drum) == [(0x0005, 7, 2), (0x002C, 3, 5), (0x0060, 8, 2)]


def test_a_full_queue_skips_the_hit(drum):
    offs(drum)
    fill(drum, [(1010 + i, 7, 2) for i in range(31)])
    drum.spy(0x08027DBC, count=2)
    ratchet(drum, 3, 5, 1)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    drum.call(SYM["drum_merge"], 0, 0)
    assert len(queue(drum)) == 31 and pend(drum, 3)["left"] == 0


# Hit after hit, through the firmware's own queue handler

def player(fw):
    """Stand in for the step-on path: the queue handler's dispatch ends in drum_played -> the steps played."""
    played = []
    fw.stub(POSITION_SET)
    fw.stub(PAD_STEP)
    fw.spy(NOTE_OFF_AT, then=lambda a: played.append((a[1], a[2])))
    code = bytes.fromhex("10b5" "0398" "1946" "029a" "0023" "014c" "a047" "10bd") + struct.pack(
        "<I", SYM["drum_played"] | 1)
    fw.write(0x08000200, code)                            # (obj, event, track, step, pad, seq) -> drum_played(seq, step, pad, 0)
    fw.write(0x2000A100, struct.pack("<III", 0x2000A108, 0, 0x08000201))
    fw.write(SEQ + 0x6B4 + 4, struct.pack("<I", 0x2000A100))   # the handler of event 1, step on
    return played


@pytest.mark.parametrize("forward", [1, 0])
def test_every_hit_plays_and_the_timer_follows(drum, forward):
    played = player(drum)
    drum.spy(STEP_TICKS, count=3, result=240)             # a 1/8 track: 4 hits, 60 apart
    pad_setup(drum, 3, forward=forward)
    ratchet(drum, 3, 5, 2)
    drum.spy(0x08027DBC, count=2)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)           # the step itself, at the boundary
    drum.call(SYM["drum_merge"], 0, 0)
    # The queue holds the step as it is before the direction mapping.
    assert queue(drum) == [(1060, 3, 5 if forward else 10)]
    for hit in (1, 2, 3):
        at(drum, 1000 + 60 * hit)
        drum.write(DIER, struct.pack("<H", 0))
        drum.call(SYM["drum_events"], SEQ, 0, 0, 0)
        assert played[-1] == (5, 3) and len(played) == hit + 1
        if hit < 3:
            assert queue(drum) == [(1000 + 60 * (hit + 1), 3, 5 if forward else 10)]
            assert drum.read(CCR4, 2) == struct.pack("<H", 1000 + 60 * (hit + 1))
            assert drum.read(DIER, 2) == struct.pack("<H", 0x10), "the compare interrupt is on for it"
    assert queue(drum) == [] and pend(drum, 3)["left"] == 0
    assert drum.read(DIER, 2) == struct.pack("<H", 0), "nothing left: stock has switched the interrupt off"


def test_a_hit_too_close_to_catch_is_moved_three_ticks_on(drum):
    player(drum)
    drum.spy(STEP_TICKS, count=3, result=16)              # absurdly short steps: 8 ticks between hits
    ratchet(drum, 3, 5, 1)
    fill(drum, [(1000, 3, 5)])
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    assert pend(drum, 3)["time"] == 1008
    drum.write(CNT, struct.pack("<H", 1007))              # the handler ran late
    fill(drum, [])
    drum.call(SYM["drum_events"], SEQ, 0, 0, 0)
    assert queue(drum) == [(1010, 3, 5)] and drum.read(CCR4, 2) == struct.pack("<H", 1010)
    at(drum, 1010)
    drum.call(SYM["drum_events"], SEQ, 0, 0, 0)
    assert pend(drum, 3)["left"] == 0, "it is still taken for the ratchet's hit"


def test_stock_events_alone_leave_the_timer_to_stock(drum):
    player(drum)
    drum.write(on_addr(7, 2), [1])
    fill(drum, [(1000, 7, 2), (1040, 8, 2)])
    drum.write(SR, struct.pack("<H", 0x1234))
    drum.call(SYM["drum_events"], SEQ, 0, 0, 0)
    assert queue(drum) == [(1040, 8, 2)]
    assert drum.read(SR, 2) == struct.pack("<H", 0x1234), "the patch did not touch the timer"


# Chance and conditions are decided once

def test_the_later_hits_do_not_draw_again(drum):
    offs(drum)
    drawn = drum.spy(RNG_NEXT)
    ratchet(drum, 3, 5, 2, on=1 | 1 << 1)                 # 1 percent chance
    seed(drum, 0x2468ACE1)
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)           # it played: the first hit
    for hit in (1, 2, 3):
        at(drum, 1000 + 30 * hit)
        assert drum.call(SYM["trig_step_on"], SEQ, 2, 5, 3) != 0
        drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    assert drawn == []
    at(drum, 1000 + TICK)                                 # the next pass: decided again
    drum.call(SYM["trig_step_on"], SEQ, 2, 5, 3)
    assert len(drawn) == 1


def test_another_step_of_the_pad_is_not_taken_for_a_hit(drum):
    offs(drum)
    drawn = drum.spy(RNG_NEXT)
    ratchet(drum, 3, 5, 2)
    drum.write(on_addr(3, 6), [1 | 1 << 1])
    drum.call(SYM["drum_played"], SEQ, 5, 3, 0)
    at(drum, 1030)
    drum.call(SYM["trig_step_on"], SEQ, 2, 6, 3)
    assert len(drawn) == 1


# Panel

def strip(fw):
    return fw.spy(STRIP_SET, count=3), fw.spy(KNOB_SHOW, count=3)


def test_the_strip_sets_the_held_steps_ratchet(drum):
    passed, shown = strip(drum)
    drum.spy(BUTTON_FLAG)
    hold(drum, 6, 21)
    drum.call(SYM["strip_set"], SEQ, 8, 1)                # 1/32 on a 1/16 track
    assert drum.read(on_addr(6, 21), 1)[0] == 0x81 and drum.read(RATE + 6 * 0x40 + 21, 1)[0] == 1
    assert passed == [[SEQ, 0, 1]], "the roller does not hear of the touch"
    assert shown == [[0, 0x11, 1]], "the display is asked for the Swing knob's value"
    assert drum.read(SEQ + DRUM_DIRTY, 1) == b"\1" and drum.read(HOLD_USED + 2, 1) == b"\1"
    assert drum.call(SYM["ui_swing_shown"], SEQ, 2) & 0xFF == 0xC1


@pytest.mark.parametrize("size, zone, code", [(0, 1, 0), (0, 2, 1), (0, 4, 2), (0, 8, 3), (1, 2, 0), (1, 4, 1),
                                              (1, 8, 2), (2, 4, 0), (2, 8, 1), (3, 8, 0)])
def test_the_rate_is_the_zone_against_the_step_size(drum, size, zone, code):
    strip(drum)
    drum.spy(BUTTON_FLAG)
    drum.write(SEQ + DRUM_SIZE, struct.pack("<i", size))
    hold(drum, 6, 21)
    ratchet(drum, 6, 21, 3)
    drum.call(SYM["strip_set"], SEQ, zone, 0)
    assert drum.read(on_addr(6, 21), 1)[0] == (0x81 if code else 1)
    if code:
        assert drum.read(RATE + 6 * 0x40 + 21, 1)[0] == code


def test_the_touch_stays_from_the_roller_until_the_finger_lifts(drum):
    passed, _ = strip(drum)
    drum.spy(BUTTON_FLAG)
    hold(drum, 6, 21)
    drum.call(SYM["strip_set"], SEQ, 8, 0)
    drum.write(HELD_STEP + 2, [0xFF])                     # the step is let go first
    drum.call(SYM["strip_set"], SEQ, 8, 0)
    drum.call(SYM["strip_set"], SEQ, 0, 0)
    drum.call(SYM["strip_set"], SEQ, 8, 0)
    assert [call[1] for call in passed] == [0, 0, 0, 8]


def test_the_strip_is_stock_without_a_drum_step_that_is_on(drum):
    passed, shown = strip(drum)
    drum.call(SYM["strip_set"], SEQ, 4, 1)
    hold(drum, 6, 21, on=0)
    drum.call(SYM["strip_set"], SEQ, 2, 1)
    drum.write(CUR_TRACK, struct.pack("<i", 0))
    drum.write(HELD_STEP, [3])
    drum.call(SYM["strip_set"], SEQ, 8, 1)
    assert [call[1] for call in passed] == [4, 2, 8] and shown == []
    assert drum.read(on_addr(6, 21), 1) == b"\0"


def test_a_ratchet_keeps_the_steps_chance_and_the_other_way_round(drum):
    strip(drum)
    drum.spy(BUTTON_FLAG)
    hold(drum, 6, 21, on=1 | 11 << 1)
    drum.call(SYM["strip_set"], SEQ, 8, 0)
    assert drum.read(on_addr(6, 21), 1)[0] == 0x80 | 1 | 11 << 1
    drum.call(SYM["ui_cond_set"], SEQ, 2, 3)
    assert drum.read(on_addr(6, 21), 1)[0] & 0x81 == 0x81
    assert drum.call(SYM["ui_chance_get"], SEQ, 2) == 100


def test_the_pattern_holds_96_ratchets(drum):
    strip(drum)
    drum.spy(BUTTON_FLAG)
    for i in range(96):
        ratchet(drum, i % 16, i // 16, 1)
    hold(drum, 6, 21)
    drum.call(SYM["strip_set"], SEQ, 8, 0)
    assert drum.read(on_addr(6, 21), 1) == b"\1"
    assert drum.call(SYM["ui_swing_shown"], SEQ, 2) & 0xFF == 0xC4
    hold(drum, 0, 0, on=0x81)                             # one that has a ratchet can still be changed
    drum.write(SEQ + DRUM_SIZE, struct.pack("<i", 0))
    drum.call(SYM["strip_set"], SEQ, 8, 0)
    assert drum.read(RATE, 1)[0] == 3 and drum.call(SYM["ui_swing_shown"], SEQ, 2) & 0xFF == 0xC3


def test_swing_is_left_alone_with_a_drum_step_held(drum):
    got, put, shown = drum.spy(SWING_GET, result=55), drum.spy(SWING_SET), drum.spy(SWING_SHOWN, result=57)
    drum.spy(BUTTON_FLAG)
    hold(drum, 6, 21)
    assert drum.call(SYM["ui_swing_get"], SEQ, 2) == 60, "inside the knob handler's limits both ways"
    drum.call(SYM["ui_swing_set"], SEQ, 2, 61)
    assert drum.call(SYM["ui_swing_shown"], SEQ, 2) & 0xFF == 0xC0
    assert got == [] and put == [] and shown == []
    assert drum.read(HOLD_USED + 2, 1) == b"\1" and drum.read(SEQ + DRUM_DIRTY, 1) == b"\0"
    drum.write(HELD_STEP + 2, [0xFF])
    assert drum.call(SYM["ui_swing_get"], SEQ, 2) == 55
    drum.call(SYM["ui_swing_set"], SEQ, 2, 56)
    assert drum.call(SYM["ui_swing_shown"], SEQ, 2) == 57
    assert put == [[SEQ, 2, 56, put[0][3]]]


@pytest.mark.parametrize("value, glyphs", [(0xC0, [0x1E, 0x2C, 1]), (0xC1, [0x1E, 0x2C, 2]), (0xC2, [0x1E, 0x2C, 4]),
                                           (0xC3, [0x1E, 0x2C, 8]), (0xC4, [0x0F, 0x28, 0x1D])])
def test_the_display_reads_r_and_the_hits_or_full(drum, value, glyphs):
    shown, number = drum.spy(SHOW_GLYPHS, count=5), drum.spy(SHOW_NUMBER)
    drum.call(SYM["show_value"], 0x2000A000, 4, value - 256)
    assert shown == [[0x2000A000, 4] + glyphs] and number == []


@pytest.mark.parametrize("value", [-1, -59, -65, -107, 0, 60, 100])
def test_other_numbers_are_shown_as_before(drum, value):
    shown, number = drum.spy(SHOW_GLYPHS, count=5), drum.spy(SHOW_NUMBER, count=3)
    drum.call(SYM["show_value"], 0x2000A000, 4, value)
    assert shown == [] and number == [[0x2000A000, 4, value & 0xFFFFFFFF]]


def test_a_ratcheted_step_lights_cyan(drum):
    lit = drum.spy(LED, count=3)
    drum.spy(STEP_ON, result=1)
    ratchet(drum, 6, 21, 2)
    drum.call(SYM["led_step_on"], SEQ, 2, 21, 6)
    drum.call(SYM["led_colour"], 0x2000A000, 5, 5)
    drum.call(SYM["led_step_on"], SEQ, 2, 22, 6)
    drum.call(SYM["led_colour"], 0x2000A000, 6, 5)
    drum.call(SYM["led_step_on"], SEQ, 0, 21, 6)          # SEQ1 has none
    drum.call(SYM["led_colour"], 0x2000A000, 5, 1)
    drum.call(SYM["led_step_on"], SEQ, 2, 21, 6)
    drum.call(SYM["led_step_on"], SEQ, 2, 22, 6)          # a read that is not followed by a colour
    drum.call(SYM["led_colour"], 0x2000A000, 6, 5)
    assert [call[1:] for call in lit] == [[5, 4], [6, 5], [5, 1], [6, 5]]


# Recording a roll

class Recorder:
    """The stock recording calls: a hit is noted, and written to its step when the next call comes."""

    def __init__(self, fw, zone):
        self.fw, self.pending = fw, {}
        self.zone(zone)
        self.on = fw.spy(RECORD_ON)
        self.off = fw.spy(RECORD_OFF, count=3, then=self.write)

    def zone(self, zone):
        self.fw.write(SEQ + ROLL_ON, [1 if zone else 0])
        self.fw.write(SEQ + ROLL_ZONE, struct.pack("<i", zone))

    def write(self, args):
        if args[0] == 2 and args[1] in self.pending:
            self.fw.write(on_addr(args[1], self.pending.pop(args[1])), [1])   # the stock setter: plainly on

    def roll(self, pad, steps):
        """Roller hits as the recording path sees them: the write of the hit before, then the new hit."""
        for step in steps:
            self.release(pad)
            self.fw.write(RECORDED_STEP + pad, [step])
            self.fw.call(SYM["rec_on"], 2, 36, 100, pad)
            self.pending[pad] = step

    def release(self, pad):
        self.fw.call(SYM["rec_off"], 2, pad, 0)


def marked(fw, pad):
    return {step: fw.read(RATE + pad * 0x40 + step, 1)[0] & 3
            for step, b in enumerate(fw.read(on_addr(pad, 0), 64)) if b & 0x80}


def test_a_finer_roll_marks_the_steps_it_fills(drum):
    rec = Recorder(drum, 8)
    rec.roll(4, [8, 8, 9, 9, 10])                         # two hits in 8 and 9, the roll ends after one in 10
    rec.release(4)                                        # the pad is let go
    assert marked(drum, 4) == {8: 1, 9: 1}
    assert drum.read(on_addr(4, 10), 1) == b""
    assert len(rec.on) == 5 and rec.on[0] == [2, 36, 100, 4] and rec.off[0] == [2, 4, 0]


def test_a_roll_at_the_tracks_own_rate_records_as_stock(drum):
    rec = Recorder(drum, 4)
    rec.roll(4, [8, 9, 10, 10])
    rec.release(4)
    assert marked(drum, 4) == {}


def test_recording_over_a_ratchet_with_single_hits_removes_it(drum):
    rec = Recorder(drum, 8)
    rec.roll(4, [8, 8, 9])
    assert marked(drum, 4) == {8: 1}
    rec.zone(0)
    rec.roll(4, [8])                                      # the stock write of this hit resets the step
    rec.release(4)
    rec.release(4)
    assert marked(drum, 4) == {}


def test_recording_on_the_other_tracks_is_stock(drum):
    rec = Recorder(drum, 8)
    drum.call(SYM["rec_on"], 0, 60, 90, -1)
    drum.call(SYM["rec_off"], 0, -1, 1)
    assert rec.on == [[0, 60, 90, 0xFFFFFFFF]] and rec.off == [[0, 0xFFFFFFFF, 1]]


# Saving and loading

RATCHETS = {(0, 0): 1, (3, 15): 3, (7, 18): 2, (15, 33): 1, (9, 63): 3, (15, 48): 2}
CODES = {(0, 0): 11, (7, 18): 41, (2, 30): 5}


def ratchet_pattern(fw, ratchets, codes=CODES):
    pattern(fw, codes)
    for (pad, step), code in ratchets.items():
        fw.write(on_addr(pad, step), [fw.read(on_addr(pad, step), 1)[0] | 0x81])
        fw.write(RATE + pad * 0x40 + step, [code])


def stored(fw):
    found = {}
    for pad in range(16):
        for step, b in enumerate(fw.read(on_addr(pad, 0), 64)):
            if b & 0x80:
                found[pad, step] = fw.read(RATE + pad * 0x40 + step, 1)[0]
    return found


def stock_reload(fw, chip, base=AREA):
    """As the stock loader leaves the steps, with what was in patch RAM gone, then the load hook."""
    fw.write(RATE, bytes(0x400))
    reload(fw, chip, base)


def test_ratchets_come_back_after_a_save_and_a_load(drum):
    chip = Chip(drum)
    ratchet_pattern(drum, RATCHETS)
    assert drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10) == 1
    assert chip.writes == [(AREA + 0xF00, 0x100), (AREA + 0xE00, 0x100), (AREA + 0xD00, 0x100),
                           (AREA + 0xC10, 0xF0), (AREA + 0xC00, 0x10)]
    stock_reload(drum, chip)
    assert stored(drum) == RATCHETS
    codes = {key: drum.read(on_addr(*key), 1)[0] >> 1 & 0x3F for key in CODES}
    assert codes == CODES, "the chances and conditions too"


def test_ratchets_alone_are_saved(drum):
    chip = Chip(drum)
    ratchet_pattern(drum, RATCHETS, codes={})
    drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    ratchet_pattern(drum, {}, codes={})
    stock_reload(drum, chip)
    assert stored(drum) == RATCHETS


def test_96_ratchets_fit(drum):
    chip = Chip(drum)
    many = {(i % 16, i // 16 * 11): 1 + i % 3 for i in range(96)}
    assert len(many) == 96
    ratchet_pattern(drum, many, codes={})
    drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    stock_reload(drum, chip)
    assert stored(drum) == many


def test_a_step_that_is_off_now_gets_no_ratchet(drum):
    chip = Chip(drum)
    ratchet_pattern(drum, RATCHETS)
    drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    drum.write(on_addr(7, 18), [0])                       # page 1 changed since: its hash no longer fits
    stock_reload(drum, chip)
    assert stored(drum) == {key: code for key, code in RATCHETS.items() if not 16 <= key[1] < 32}


def test_damaged_ratchets_are_dropped_and_the_codes_kept(drum):
    chip = Chip(drum)
    ratchet_pattern(drum, RATCHETS)
    drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    chip.data[AREA + 0xDC0 + 1] ^= 0x10
    stock_reload(drum, chip)
    assert stored(drum) == {}
    assert drum.read(on_addr(2, 30), 1)[0] == 1 | 5 << 1


def test_a_pattern_saved_by_1_2_1_loads_without_ratchets(drum):
    """Its head ends in zeros and the blocks behind the codes were never written."""
    chip = Chip(drum)
    ratchet_pattern(drum, {})
    drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    for q in (1, 2, 3):
        for i in range(0xC0, 0x100):
            chip.data[AREA + 0xC00 + q * 0x100 + i] = 0xFF
    chip.data[AREA + 0xC10 + 13] = chip.data[AREA + 0xC10 + 14] = 0
    stock_reload(drum, chip)
    assert stored(drum) == {}
    assert drum.read(on_addr(2, 30), 1)[0] == 1 | 5 << 1


def test_copy_project_takes_the_ratchets_along(drum):
    chip = Chip(drum)
    ratchet_pattern(drum, RATCHETS)
    drum.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    drum.call(STORAGE_READ, STORAGE, AREA + 0xC00, 0x10)
    assert drum.call(SYM["copy_drum"], STORAGE, OTHER + 0xC00, READ_BUFFER, 0x10) == 1
    assert chip.area(OTHER) == chip.area(AREA)
    ratchet_pattern(drum, {})
    stock_reload(drum, chip, base=OTHER)
    assert stored(drum) == RATCHETS


@pytest.mark.parametrize("knob, value", [("ui_chance_set", 50), ("ui_cond_set", 3)])
def test_a_ratcheted_step_stays_cyan_with_a_chance_or_condition(drum, knob, value):
    drum.spy(BUTTON_FLAG, count=3)
    lit = drum.spy(LED, count=3)
    hold(drum, 6, 21, 0x81)
    drum.write(RATE + 6 * 0x40 + 21, [2])
    drum.call(SYM[knob], SEQ, 2, value)
    assert drum.read(on_addr(6, 21), 1)[0] & 0x80
    drum.call(SYM["led_step_on"], SEQ, 2, 21, 6)
    drum.call(SYM["led_colour"], 0x2000A000, 5, 5)
    assert [call[1:] for call in lit] == [[5, 4]]


# Blinking lights

MS = 0x20000574
CHANCE_ON = 1 | 10 << 1                                   # chance code 10: 41 percent
COND_ON = 1 | (21 + 3) << 1                               # condition 1-2


def light(fw, on, ms, track=2, step=21, pad=6):
    """Paint one step at SysTick count ms -> the colour it got (5 is the track's)."""
    lit = fw.spy(LED, count=3)
    fw.write(on_addr(pad, step) if track == 2 else SEQ + track * 0x230 + 0x8B7 + step, [on])
    fw.write(MS, struct.pack("<I", ms))
    fw.spy(STEP_ON, result=on)
    fw.call(SYM["led_step_on"], SEQ, track, step, pad)
    fw.call(SYM["led_colour"], 0x2000A000, step & 15, 5)
    return lit[0][2]


@pytest.mark.parametrize("on, dark", [(CHANCE_ON, {7, 15}), (COND_ON, {12, 14}),
                                      (CHANCE_ON | 0x80, {7, 15}), (1, set()), (0x81, set())])
def test_a_step_with_a_chance_or_condition_blinks(drum, on, dark):
    drum.write(RATE + 6 * 0x40 + 21, [2])
    colour = 4 if on & 0x80 else 5
    assert [light(drum, on, slot * 128 + 5) for slot in range(16)] == [8 if s in dark else colour for s in range(16)]


def test_melodic_steps_blink_too(drum):
    assert light(drum, COND_ON, 12 * 128, track=0) == 8
    assert light(drum, COND_ON, 0, track=0) == 5


def test_a_held_step_does_not_blink(drum):
    hold(drum, 6, 21, CHANCE_ON)
    assert light(drum, CHANCE_ON, 7 * 128) == 5
    assert light(drum, CHANCE_ON, 7 * 128, step=22) == 8


# Pressing a step that is on

STEP_ON_SET = 0x080115B8
DRUM_COUNT = 0x2456       # seq + this + pad: steps on, kept by the stock setter


@pytest.mark.parametrize("was", [CHANCE_ON | 0x80, COND_ON, 0x81, 1])
def test_pressing_a_step_that_is_on_keeps_it(drum, was):
    drum.write(on_addr(6, 21), [was])
    drum.write(on_addr(6, 22), [1])
    drum.write(SEQ + DRUM_COUNT + 6, [10])
    drum.call(SYM["press_on"], SEQ, 2, 1, 21, 6)
    assert drum.read(on_addr(6, 21), 1)[0] == was
    drum.call(STEP_ON_SET, SEQ, 2, 1, 22, 6)                   # the stock setter, for its count
    count = drum.read(SEQ + DRUM_COUNT + 6, 1)[0]
    assert count == 12


def test_pressing_a_melodic_step_that_is_on_keeps_it(drum):
    addr = SEQ + 0x230 + 0x8B7 + 5
    drum.write(addr, [COND_ON])
    drum.call(SYM["press_on"], SEQ, 1, 1, 5, -1)
    assert drum.read(addr, 1)[0] == COND_ON


def test_pressing_a_step_that_is_off_switches_it_plainly_on(drum):
    drum.write(on_addr(6, 21), [0])
    drum.call(SYM["press_on"], SEQ, 2, 1, 21, 6)
    assert drum.read(on_addr(6, 21), 1)[0] == 1
