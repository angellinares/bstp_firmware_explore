"""Per-step conditions (play on some passes only), run on the patched image in an emulator.

Like test_trig.py, these prove what the patch code computes on state set up by
hand. They do not prove the firmware calls the hooks when the tests assume: the
pass counting is only as right as the reading of the timer handlers.
"""
import struct

import pytest

from test_trig import (CUR_TRACK, DIRTY, HELD_STEP, HOLD_USED, RANDOMNESS, SEQ, SYM, fw, hold,  # noqa: F401
                       on_addr, seed)

PASS = 0x20006400        # u32 per melodic track
POSITION = 0x6F0         # + track * 0x230
DIRECTION = 0x6EC        # + track * 0x230, int
PROBABILITY = RANDOMNESS + 1
POS_SET = 0x080103E6
SHOW_NUMBER = 0x08023BEA
SHOW_GLYPHS = 0x08023BA2
DISPLAY = 0x20009000

RATIOS = [(1, 2), (2, 2), (1, 3), (2, 3), (3, 3), (1, 4), (2, 4), (3, 4), (4, 4)]
# What the Probability knob walks through, left to right, as the display shows it.
SHOWN = ["1-1", "1St", "n1S"] + [text for a, b in RATIOS for text in ("%d-%d" % (a, b), "n%d%d" % (a, b))]
GLYPH = {"-": 0x20, "n": 0x25, "S": 0x1C, "t": 0x1B, **{str(d): d for d in range(10)}}
FIRST = 22               # the code of condition 1; codes 1..21 are chances


def plays(k, passes):
    """The model: does condition k (1..20) play on this pass, counted from 0?"""
    if k == 1:
        return passes == 0
    if k == 2:
        return passes != 0
    a, b = RATIOS[(k - 3) // 2]
    return (passes % b == a - 1) != bool((k - 3) & 1)


def set_pass(fw, track, value):
    fw.write(PASS + 4 * track, struct.pack("<I", value))


# Playback

@pytest.mark.parametrize("k", range(1, 21))
def test_a_condition_plays_on_its_passes_only(fw, k):
    fw.write(on_addr(1, 9), [1 | (FIRST + k - 1) << 1])
    seed(fw, 0x1234)
    for passes in range(13):
        set_pass(fw, 1, passes)
        assert bool(fw.call(SYM["trig_step_on"], SEQ, 1, 9, -1)) == plays(k, passes), (SHOWN[k], passes)
    assert fw.u32(SEQ + 4) == 0x1234, "a condition must not draw a random number"


def test_tracks_count_their_own_passes(fw):
    for track in (0, 1):
        fw.write(on_addr(track, 0), [1 | FIRST << 1])   # first pass only
    set_pass(fw, 0, 0)
    set_pass(fw, 1, 3)
    assert fw.call(SYM["trig_step_on"], SEQ, 0, 0, -1)
    assert not fw.call(SYM["trig_step_on"], SEQ, 1, 0, -1)


def test_in_alternate_direction_a_pass_is_there_and_back(fw):
    fw.write(SEQ + DIRECTION, struct.pack("<i", 2))
    fw.write(on_addr(0, 0), [1 | (FIRST + 2) << 1])      # 1-2
    played = []
    for sweeps in range(8):
        set_pass(fw, 0, sweeps)
        played.append(bool(fw.call(SYM["trig_step_on"], SEQ, 0, 0, -1)))
    assert played == [True, True, False, False] * 2


# Counting passes

def test_a_pass_is_counted_when_the_position_wraps(fw):
    calls = fw.spy(POS_SET)
    set_pass(fw, 0, 5)
    fw.write(SEQ + POSITION, [14])
    fw.call(SYM["pos_advance"], SEQ, 0, 15, -1)
    assert fw.u32(PASS) == 5
    fw.write(SEQ + POSITION, [15])
    fw.call(SYM["pos_advance"], SEQ, 0, 0, -1)
    assert fw.u32(PASS) == 6
    assert calls == [[SEQ, 0, 15, 0xFFFFFFFF], [SEQ, 0, 0, 0xFFFFFFFF]]


def test_a_one_step_loop_counts_every_step(fw):
    fw.stub(POS_SET)
    set_pass(fw, 1, 0)
    fw.write(SEQ + 0x230 + POSITION, [4])
    for _ in range(3):
        fw.call(SYM["pos_advance"], SEQ, 1, 4, -1)
    assert fw.u32(PASS + 4) == 3


def test_play_stop_and_pattern_change_start_the_count_again(fw):
    calls = fw.spy(POS_SET)
    set_pass(fw, 0, 9)
    set_pass(fw, 1, 9)
    fw.call(SYM["pos_reset"], SEQ, 1, 0, -1)
    assert (fw.u32(PASS), fw.u32(PASS + 4)) == (9, 0)
    assert calls == [[SEQ, 1, 0, 0xFFFFFFFF]]


def test_the_drum_track_leaves_the_counters_alone(fw):
    fw.stub(POS_SET)
    fw.write(PASS, struct.pack("<III", 7, 8, 0xAAAAAAAA))
    fw.call(SYM["pos_advance"], SEQ, 2, 0, 3)
    fw.call(SYM["pos_reset"], SEQ, 2, 0, 3)
    assert fw.read(PASS, 12) == struct.pack("<III", 7, 8, 0xAAAAAAAA)


# Panel

@pytest.mark.parametrize("track", [-1, 0, 1, 2])
def test_probability_is_stock_when_no_step_is_held(fw, track):
    fw.call(SYM["ui_cond_set"], SEQ, track, 37)
    assert fw.call(0x080118F0, SEQ, track) == 37
    assert fw.call(SYM["ui_cond_get"], SEQ, track) == 37
    assert fw.call(SYM["ui_cond_shown"], SEQ, track) == 37


def test_the_knob_walks_the_conditions_and_stops_at_both_ends(fw):
    """As the handler does it: read, then write the value one up or one down."""
    hold(fw, 0, 9)
    get = lambda: fw.call(SYM["ui_cond_get"], SEQ, 0)
    seen = [get()]
    for _ in range(25):
        fw.call(SYM["ui_cond_set"], SEQ, 0, get() + 1)
        seen.append(get())
    assert seen == list(range(21)) + [20] * 5
    assert fw.read(on_addr(0, 9), 1)[0] == 1 | 41 << 1
    for _ in range(20):
        fw.call(SYM["ui_cond_set"], SEQ, 0, get() - 1)
    assert get() == 0 and fw.read(on_addr(0, 9), 1) == b"\1"
    assert fw.read(SEQ + PROBABILITY, 1) == b"\0", "the track's own Probability is untouched"
    assert fw.read(SEQ + DIRTY, 1) == b"\1" and fw.read(HOLD_USED, 1) == b"\1"


def test_a_step_has_a_chance_or_a_condition(fw):
    hold(fw, 1, 3)
    fw.call(SYM["ui_chance_set"], SEQ, 1, 50)
    assert fw.call(SYM["ui_chance_get"], SEQ, 1) == 50
    fw.call(SYM["ui_cond_set"], SEQ, 1, 4)
    assert fw.call(SYM["ui_cond_get"], SEQ, 1) == 4
    assert fw.call(SYM["ui_chance_get"], SEQ, 1) == 100, "the chance is gone"
    fw.call(SYM["ui_chance_set"], SEQ, 1, 99)
    assert fw.call(SYM["ui_cond_get"], SEQ, 1) == 0, "and the condition goes when a chance is set"


def test_a_held_step_that_is_off_gets_no_condition(fw):
    hold(fw, 0, 9, on=0)
    fw.call(SYM["ui_cond_set"], SEQ, 0, 5)
    assert fw.read(on_addr(0, 9), 1) == b"\0"
    assert fw.read(SEQ + PROBABILITY, 1)[0] == 5


# Display

@pytest.mark.parametrize("k", range(21))
def test_the_display_shows_the_condition(fw, k):
    hold(fw, 0, 9)
    fw.call(SYM["ui_cond_set"], SEQ, 0, k)
    shown = fw.call(SYM["ui_cond_shown"], SEQ, 0)
    value = shown - 0x100 if shown & 0x80 else shown     # the caller sign-extends the byte
    assert value < 0
    numbers = fw.spy(SHOW_NUMBER, count=3)
    glyphs = fw.spy(SHOW_GLYPHS, count=5)
    fw.call(SYM["show_value"], DISPLAY, 4, value)
    assert numbers == []
    assert glyphs == [[DISPLAY, 4] + [GLYPH[c] for c in SHOWN[k]]]


@pytest.mark.parametrize("value", [0, 5, 100, 127])
def test_other_values_go_to_the_stock_number_display(fw, value):
    numbers = fw.spy(SHOW_NUMBER, count=3)
    glyphs = fw.spy(SHOW_GLYPHS, count=5)
    fw.call(SYM["show_value"], DISPLAY, 4, value)
    assert numbers == [[DISPLAY, 4, value]] and glyphs == []


def test_the_knob_order_is_the_agreed_one():
    assert SHOWN == ["1-1", "1St", "n1S", "1-2", "n12", "2-2", "n22", "1-3", "n13", "2-3", "n23", "3-3",
                     "n33", "1-4", "n14", "2-4", "n24", "3-4", "n34", "4-4", "n44"]
