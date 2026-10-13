"""Chance and conditions on drum steps, run on the patched image in an emulator.

The storage chip here is a dictionary: the tests prove the patch reads back what
it wrote and refuses what it should, not that the chip or the stock loader behave
as read from the code.
"""
import struct

import pytest

from test_cond import FIRST, PASS, plays
from test_trig import (CUR_TRACK, HELD_PAD, HELD_STEP, HOLD_USED, SEQ, STORAGE_WRITE, SYM, fw, seed)  # noqa: F401

DRUM_ON = 0x1FF9         # + pad * 0x40 + step
DRUM_NOTE = 0xBD9        # + pad * 0x40 + step
DRUM_PAD = 0xB50         # int: the selected pad
DRUM_SIZE = 0xB48
DRUM_DIR = 0xB4C
DRUM_LEN = 0xB89         # + pad
DRUM_FIRST = 0xB99
DRUM_LAST = 0xBA9
DRUM_FWD = 0x2409
DRUM_DIRTY = 0x2452
TICKS = 0x080105CC       # (seq, track)
STEP_TICKS = 0x08011AD8  # (seq, size, track)
STORAGE_READ = 0x08022A50
DRUM_LOADED = 0x08014228
READ_BUFFER = 0x20000B56 + 4
TRANSFER = 0x20000752    # command, then the address high byte first
BUSY = 0x2000016D
STORAGE = 0x20009400
AREA = 3 * 0x13000 + 0x2000 + 5 * 0x1000     # project 3, drum pattern 5
OTHER = 7 * 0x13000 + 0x2000 + 5 * 0x1000    # the same pattern in project 7
TICK = 120


def on_addr(pad, step):
    return SEQ + DRUM_ON + pad * 0x40 + step


def hold(fw, pad, step, on=1):
    fw.write(CUR_TRACK, struct.pack("<i", 2))
    fw.write(SEQ + DRUM_PAD, struct.pack("<i", pad))
    fw.write(HELD_STEP + 2, [step])
    fw.write(on_addr(pad, step), [on])


def pad_setup(fw, pad, length=16, first=0, last=None, forward=1):
    fw.write(SEQ + DRUM_LEN + pad, [length])
    fw.write(SEQ + DRUM_FIRST + pad, [first])
    fw.write(SEQ + DRUM_LAST + pad, [length - 1 if last is None else last])
    fw.write(SEQ + DRUM_FWD + pad, [forward])


class Clock:
    """Plays the two stock functions the pass is worked out from."""

    def __init__(self, fw):
        self.steps = 0
        fw.spy(STEP_TICKS, count=3, result=TICK)
        fw.spy(TICKS, count=2, then=lambda args: fw.uc.reg_write(R0, self.steps * TICK + 7))


class Chip:
    """A storage chip that remembers what was written, 0xFF elsewhere."""

    def __init__(self, fw):
        self.fw, self.data, self.writes, self.reads = fw, {}, [], []
        fw.spy(STORAGE_WRITE, count=4, then=self.write)
        fw.spy(STORAGE_READ, count=3, then=self.read)

    def moved(self, address):
        self.fw.write(TRANSFER + 1, struct.pack(">I", address)[1:])

    def write(self, args):
        storage, address, data, length = args
        assert storage == STORAGE and 0 < length <= 0x400
        self.writes.append((address, length))
        for i, b in enumerate(self.fw.read(data, length)):
            self.data[address + i] = b
        self.moved(address)
        self.fw.uc.reg_write(R0, 1)

    def read(self, args):
        storage, address, length = args
        assert storage == STORAGE and 0 < length <= 0x400
        self.reads.append((address, length))
        self.fw.write(READ_BUFFER, bytes(self.data.get(address + i, 0xFF) for i in range(length)))
        self.moved(address)

    def area(self, base):
        return bytes(self.data.get(base + i, 0xFF) for i in range(0xC10, 0x1000))


from unicorn.arm_const import UC_ARM_REG_R0 as R0  # noqa: E402


# Panel

def test_the_knobs_set_the_held_step_of_the_selected_pad(fw):
    hold(fw, 6, 21)
    fw.write(on_addr(5, 21), [1])
    fw.call(SYM["ui_chance_set"], SEQ, 2, 50)
    assert fw.read(on_addr(6, 21), 1)[0] == 1 | 11 << 1
    assert fw.call(SYM["ui_chance_get"], SEQ, 2) == 50
    fw.call(SYM["ui_cond_set"], SEQ, 2, 3)
    assert fw.read(on_addr(6, 21), 1)[0] == 1 | (FIRST + 2) << 1
    assert fw.call(SYM["ui_cond_shown"], SEQ, 2) == 0x83
    assert fw.read(on_addr(5, 21), 1) == b"\1", "the other pads are left alone"
    assert fw.read(SEQ + DRUM_DIRTY, 1) == b"\1" and fw.read(HOLD_USED + 2, 1) == b"\1"


def test_a_drum_step_that_is_off_leaves_the_knobs_stock(fw):
    hold(fw, 2, 4, on=0)
    fw.call(SYM["ui_chance_set"], SEQ, 2, 55)
    fw.call(SYM["ui_cond_set"], SEQ, 2, 7)
    assert fw.read(on_addr(2, 4), 1) == b"\0"
    assert fw.read(SEQ + 0xB31, 2) == bytes([55, 7])     # drum Randomness, Probability


# Playback

def test_a_drum_chance_draws(fw):
    fw.write(on_addr(9, 30), [1 | 11 << 1])              # 50 percent
    seed(fw, 0x2468ACE1)
    played = sum(fw.call(SYM["trig_step_on"], SEQ, 2, 30, 9) != 0 for _ in range(2000))
    assert 850 < played < 1150


@pytest.mark.parametrize("k", [1, 2, 3, 4, 8, 13, 20])
def test_a_drum_condition_follows_the_passes_of_its_own_pad(fw, k):
    clock = Clock(fw)
    pad_setup(fw, 0, length=16)
    pad_setup(fw, 3, length=12)
    for pad, length in ((0, 16), (3, 12)):
        fw.write(on_addr(pad, 5), [1 | (FIRST + k - 1) << 1])
        for clock.steps in range(5, 5 + length * 9, length):
            assert bool(fw.call(SYM["trig_step_on"], SEQ, 2, 5, pad)) == plays(k, clock.steps // length)


def test_a_step_played_early_or_late_belongs_to_its_own_pass(fw):
    clock = Clock(fw)
    pad_setup(fw, 0, length=16)
    fw.write(on_addr(0, 0), [1 | FIRST << 1])            # first pass only
    fw.write(on_addr(0, 15), [1 | FIRST << 1])
    clock.steps = 15                                     # step 0 of pass 2, sent early
    assert not fw.call(SYM["trig_step_on"], SEQ, 2, 0, 0)
    clock.steps = 16                                     # step 15 of pass 1, sent late
    assert fw.call(SYM["trig_step_on"], SEQ, 2, 15, 0)
    clock.steps = 0
    assert fw.call(SYM["trig_step_on"], SEQ, 2, 0, 0)
    assert fw.call(SYM["trig_step_on"], SEQ, 2, 15, 0), "nothing was played before the start"


def test_backwards_and_alternate(fw):
    clock = Clock(fw)
    pad_setup(fw, 1, length=8, forward=0)
    fw.write(on_addr(1, 7), [1 | (FIRST + 1) << 1])      # not the first pass
    clock.steps = 0                                      # backwards, step 7 comes first
    assert not fw.call(SYM["trig_step_on"], SEQ, 2, 7, 1)
    clock.steps = 8
    assert fw.call(SYM["trig_step_on"], SEQ, 2, 7, 1)
    fw.write(SEQ + DRUM_DIR, struct.pack("<i", 2))       # alternate: a pass is two sweeps
    assert not fw.call(SYM["trig_step_on"], SEQ, 2, 7, 1)
    clock.steps = 16
    assert fw.call(SYM["trig_step_on"], SEQ, 2, 7, 1)


def test_a_looped_range_counts_its_own_passes(fw):
    clock = Clock(fw)
    pad_setup(fw, 2, length=16, first=4, last=7)
    fw.write(on_addr(2, 4), [1 | (FIRST + 2) << 1])      # 1-2
    played = []
    for clock.steps in range(0, 24, 4):
        played.append(bool(fw.call(SYM["trig_step_on"], SEQ, 2, 4, 2)))
    assert played == [True, False] * 3


def test_melodic_passes_are_not_touched_by_the_drum_track(fw):
    Clock(fw)
    pad_setup(fw, 0)
    fw.write(PASS, struct.pack("<II", 3, 4))
    fw.write(on_addr(0, 0), [1 | FIRST << 1])
    fw.call(SYM["trig_step_on"], SEQ, 2, 0, 0)
    assert fw.read(PASS, 8) == struct.pack("<II", 3, 4)


# Saving and loading

def pattern(fw, codes):
    """Give the drum pattern notes, on steps and codes: {(pad, step): code}."""
    for pad in range(16):
        fw.write(SEQ + DRUM_NOTE + pad * 0x40, bytes((pad * 7 + step) % 100 for step in range(64)))
        fw.write(SEQ + DRUM_ON + pad * 0x40, bytes(step % 3 == 0 for step in range(64)))
    for (pad, step), code in codes.items():
        fw.write(on_addr(pad, step), [1 | code << 1])


def reload(fw, chip, base=AREA, last_read=None):
    """What the stock loader leaves, then the hook: on bytes are 0 or 1 again."""
    for pad in range(16):
        raw = fw.read(SEQ + DRUM_ON + pad * 0x40, 64)
        fw.write(SEQ + DRUM_ON + pad * 0x40, bytes(b != 0 for b in raw))
    chip.moved(base + 0xC00 if last_read is None else last_read)
    frame = 0x2000A000
    fw.write(frame + 0x68, struct.pack("<I", STORAGE))
    fw.call(SYM["load_drum_codes"], SEQ, frame)


def codes_now(fw):
    found = {}
    for pad in range(16):
        for step, b in enumerate(fw.read(SEQ + DRUM_ON + pad * 0x40, 64)):
            if b >> 1:
                found[pad, step] = b >> 1
    return found


CODES = {(0, 0): 11, (0, 3): 22, (3, 15): 1, (7, 18): 41, (15, 33): 24, (9, 63): 21, (15, 48): 30}


def test_codes_come_back_after_a_save_and_a_load(fw):
    chip = Chip(fw)
    pattern(fw, CODES)
    data = 0x2000A800
    fw.write(data, bytes(range(16)))
    assert fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, data, 0x10) == 1
    assert chip.writes == [(AREA + 0xF00, 0xC0), (AREA + 0xE00, 0xC0), (AREA + 0xD00, 0xC0),
                           (AREA + 0xC10, 0xF0), (AREA + 0xC00, 0x10)]
    assert bytes(chip.data[AREA + 0xC00 + i] for i in range(16)) == bytes(range(16)), "the stock write still happens"
    reload(fw, chip)
    assert codes_now(fw) == CODES
    assert chip.reads == [(AREA + 0xC00, 0x400)]
    assert fw.read(BUSY, 1) == b"\1"


def test_a_pattern_without_codes_costs_one_small_write(fw):
    chip = Chip(fw)
    pattern(fw, CODES)
    fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    pattern(fw, {})
    fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    assert chip.writes[-2:] == [(AREA + 0xC10, 0x30), (AREA + 0xC00, 0x10)]
    pattern(fw, {})
    reload(fw, chip)
    assert codes_now(fw) == {}, "the codes still on the chip are not used any more"


def test_a_chip_that_was_never_written_gives_no_codes(fw):
    chip = Chip(fw)
    pattern(fw, {})
    reload(fw, chip)
    assert codes_now(fw) == {}


def test_a_page_changed_since_the_save_loses_its_codes_and_only_that_page(fw):
    """As after stock firmware or MIDI Control Center rewrote the pattern."""
    chip = Chip(fw)
    pattern(fw, CODES)
    fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    fw.write(SEQ + DRUM_NOTE + 2 * 0x40 + 20, [99])      # a note in page 1 (steps 16..31)
    reload(fw, chip)
    assert codes_now(fw) == {key: code for key, code in CODES.items() if not 16 <= key[1] < 32}
    pattern(fw, CODES)
    fw.write(on_addr(4, 49), [1])                        # a step switched on in page 3
    reload(fw, chip)
    assert codes_now(fw) == {key: code for key, code in CODES.items() if key[1] < 48}


def test_a_damaged_page_loses_its_codes(fw):
    chip = Chip(fw)
    pattern(fw, CODES)
    fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    chip.data[AREA + 0xE00 + 5] ^= 0x10
    reload(fw, chip)
    assert codes_now(fw) == {key: code for key, code in CODES.items() if not 32 <= key[1] < 48}


def test_steps_the_loader_did_not_touch_keep_what_they_have(fw):
    """The stock loader loads 16 steps first while playing: the rest is still the old pattern."""
    chip = Chip(fw)
    pattern(fw, CODES)
    fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    chip.moved(AREA + 0x800)
    frame = 0x2000A000
    fw.write(frame + 0x68, struct.pack("<I", STORAGE))
    fw.write(on_addr(0, 0), [1])                         # the loader set this one
    fw.write(on_addr(0, 3), [1 | 5 << 1])                # this one has a code already
    fw.call(SYM["load_drum_codes"], SEQ, frame)
    assert fw.read(on_addr(0, 0), 1)[0] == 1 | 11 << 1
    assert fw.read(on_addr(0, 3), 1)[0] == 1 | 5 << 1


def test_nothing_is_read_when_the_last_read_was_not_a_drum_pattern(fw):
    chip = Chip(fw)
    pattern(fw, {})
    reload(fw, chip, last_read=3 * 0x13000 + 0x1200)     # a SEQ2 record
    reload(fw, chip, last_read=3 * 0x13000 + 0x12050)    # drum pattern settings
    assert chip.reads == []


def test_the_load_hook_finds_the_storage_object_and_keeps_the_arguments(fw):
    chip = Chip(fw)
    done = fw.spy(DRUM_LOADED, count=3)
    chip.moved(AREA + 0xC00)
    fw.write(0x2000B000 + 0x68, struct.pack("<I", STORAGE))   # emu.STACK: the caller's frame
    fw.call(SYM["load_drum"], SEQ, 0, 0)
    assert done == [[SEQ, 0, 0]] and chip.reads == [(AREA + 0xC00, 0x400)]


def test_copy_project_takes_the_codes_along(fw):
    chip = Chip(fw)
    pattern(fw, CODES)
    fw.call(SYM["save_drum"], STORAGE, AREA + 0xC00, 0x2000A800, 0x10)
    lengths = bytes(range(16, 32))
    for i, b in enumerate(lengths):
        chip.data[AREA + 0xC00 + i] = b
    fw.call(STORAGE_READ, STORAGE, AREA + 0xC00, 0x10)   # as copy project does before its write
    assert fw.call(SYM["copy_drum"], STORAGE, OTHER + 0xC00, READ_BUFFER, 0x10) == 1
    assert chip.area(OTHER) == chip.area(AREA)
    assert bytes(chip.data[OTHER + 0xC00 + i] for i in range(16)) == lengths
    pattern(fw, CODES)
    reload(fw, chip, base=OTHER)
    assert codes_now(fw) == CODES


def test_copy_project_of_a_pattern_without_codes_clears_the_old_ones(fw):
    chip = Chip(fw)
    pattern(fw, CODES)
    fw.call(SYM["save_drum"], STORAGE, OTHER + 0xC00, 0x2000A800, 0x10)
    fw.call(STORAGE_READ, STORAGE, AREA + 0xC00, 0x10)
    fw.call(SYM["copy_drum"], STORAGE, OTHER + 0xC00, READ_BUFFER, 0x10)
    pattern(fw, CODES)
    reload(fw, chip, base=OTHER)
    assert codes_now(fw) == {}
