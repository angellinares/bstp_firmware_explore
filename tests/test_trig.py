"""Per-step trigger chance, run on the patched image in an emulator.

    .venv/Scripts/python tools/build_patch.py out/bsp_2.0.1.6.bin 00_Resources/*.led trig
    .venv/Scripts/python -m pytest

These prove what the patch code computes, on firmware state the tests set
up by hand. They do not prove the device behaves: nothing here runs the
sequencer, the panel, the storage chip or the hardware.
"""
import random
import struct

import pytest

from build_patch import BASE, FEATURES, IMAGE_END, bl
from emu import Firmware, patch_symbols

SEQ = 0x20001000         # where the tests put the sequencer object
SEQ_PTR = 0x20000278
CUR_TRACK = 0x200001F0
HELD_STEP = 0x200000E7
HOLD_USED = 0x200001CA
SAVE_TRACK = 0x20000023
HELD_PAD = 0x200000C4
BUTTONS = 0x20000264
LOADED = 0x20000B56 + 4
REC = 0x20008000         # a record buffer, as the save code has on its stack

STEP_ON = 0x080113DE
RNG_NEXT = 0x0801D1D8
STORAGE_WRITE = 0x08022B38
SET_LENGTH = 0x08011190
BUTTON_FLAG = 0x0801F8B8
MELODIC_ON = 0x8B7       # + track * 0x230 + step
DRUM_ON = 0x1FF9         # + pad * 0x40 + step
RANDOMNESS = 0x6D1       # + track * 0x230; Probability is the byte after
DIRTY = 0x2450           # + track
CHANCE = [1, 2, 4, 6, 9, 13, 19, 25, 33, 41, 50, 59, 67, 75, 81, 87, 91, 94, 96, 98, 99]

SYM = patch_symbols("trig")
IMAGE = "out/bsp_2.0.1.6-trig.bin"


@pytest.fixture
def fw():
    fw = Firmware(IMAGE)
    fw.write(SEQ_PTR, struct.pack("<I", SEQ))
    fw.write(HELD_STEP, [0xFF] * 3)
    fw.write(HELD_PAD, [0xFF] * 3)
    return fw


def on_addr(track, step):
    return SEQ + track * 0x230 + MELODIC_ON + step


def seed(fw, state):
    fw.write(SEQ, struct.pack("<II", 1, state))


def hold(fw, track, step, on=1):
    fw.write(CUR_TRACK, struct.pack("<i", track))
    fw.write(HELD_STEP + track, [step])
    fw.write(on_addr(track, step), [on])


# Playback

@pytest.mark.parametrize("value", [0, 1])
def test_steps_without_a_code_play_as_stock(fw, value):
    """Same answer as the stock getter, and no random draw."""
    rnd = random.Random(1)
    for track, step, pad in [(0, 0, -1), (1, 63, -1), (2, 5, 0), (2, 63, 15)]:
        addr = SEQ + (DRUM_ON + pad * 0x40 + step if track == 2 else track * 0x230 + MELODIC_ON + step)
        fw.write(addr, [value])
        state = rnd.getrandbits(32)
        seed(fw, state)
        assert fw.call(SYM["trig_step_on"], SEQ, track, step, pad) == value
        assert fw.call(STEP_ON, SEQ, track, step, pad) == value
        assert fw.u32(SEQ + 4) == state


@pytest.mark.parametrize("code", [1, 8, 11, 14, 21])
def test_chance_matches_the_table(fw, code):
    fw.write(on_addr(0, 7), [1 | code << 1])
    seed(fw, 0x12345678)
    runs = 4000
    played = sum(fw.call(SYM["trig_step_on"], SEQ, 0, 7, -1) != 0 for _ in range(runs))
    assert abs(played / runs * 100 - CHANCE[code - 1]) < 3


@pytest.mark.parametrize("code", [42, 63])
def test_unknown_codes_always_play(fw, code):
    fw.write(on_addr(0, 0), [1 | code << 1])
    seed(fw, 0xFFFFFFFF)
    assert all(fw.call(SYM["trig_step_on"], SEQ, 0, 0, -1) for _ in range(20))


def test_one_draw_per_step_from_the_stock_generator(fw):
    fw.write(on_addr(0, 0), [1 | 11 << 1])
    seed(fw, 42)
    fw.call(SYM["trig_step_on"], SEQ, 0, 0, -1)
    after = fw.u32(SEQ + 4)
    seed(fw, 42)
    fw.call(RNG_NEXT, SEQ)
    assert fw.u32(SEQ + 4) == after


# Panel

@pytest.mark.parametrize("track", [-1, 0, 1, 2])
def test_knob_is_stock_when_no_step_is_held(fw, track):
    fw.write(CUR_TRACK, struct.pack("<i", max(track, 0)))
    fw.call(SYM["ui_chance_set"], SEQ, track, 37)
    assert fw.call(0x080113B0, SEQ, track) == 37
    assert fw.call(SYM["ui_chance_get"], SEQ, track) == 37


@pytest.mark.parametrize("cur", [2, 3])
def test_knob_is_stock_off_the_melodic_tracks(fw, cur):
    fw.write(CUR_TRACK, struct.pack("<i", cur))
    fw.write(HELD_STEP, [4, 4, 4])
    fw.call(SYM["ui_chance_set"], SEQ, -1, 12)
    assert fw.call(SYM["ui_chance_get"], SEQ, -1) == 12


def test_knob_is_stock_when_the_held_step_is_off(fw):
    hold(fw, 0, 9, on=0)
    fw.call(SYM["ui_chance_set"], SEQ, 0, 55)
    assert fw.read(on_addr(0, 9), 1) == b"\0"
    assert fw.read(SEQ + RANDOMNESS, 2) == bytes([55, 0])  # and Probability is untouched


@pytest.mark.parametrize("track", [0, 1])
def test_turning_the_knob_walks_the_table(fw, track):
    """As the knob handler does: read, then write one more or one less."""
    hold(fw, track, 20)
    get = lambda: fw.call(SYM["ui_chance_get"], SEQ, track)
    assert get() == 100

    seen = []
    while get() > 0 and len(seen) < 30:
        fw.call(SYM["ui_chance_set"], SEQ, track, get() - 1)
        seen.append(get())
    assert seen[:21] == CHANCE[::-1] and set(seen[21:]) == {1}

    seen = []
    while get() < 100:
        fw.call(SYM["ui_chance_set"], SEQ, track, get() + 1)
        seen.append(get())
    assert seen == CHANCE[1:] + [100]
    assert fw.read(on_addr(track, 20), 1) == b"\x01"


def test_setting_a_chance_leaves_the_track_alone_and_marks_it(fw):
    hold(fw, 1, 3)
    fw.write(SEQ + 0x230 + RANDOMNESS, [77])
    fw.call(SYM["ui_chance_set"], SEQ, 1, 99)
    assert fw.read(on_addr(1, 3), 1)[0] == 1 | 21 << 1
    assert fw.read(SEQ + 0x230 + RANDOMNESS, 1)[0] == 77
    assert fw.read(HOLD_USED, 3) == b"\0\1\0"
    assert fw.read(SEQ + DIRTY, 3) == b"\0\1\0"


def test_setting_a_chance_cancels_the_toggle_like_the_pad_gesture(fw):
    hold(fw, 0, 16 + 5)                  # step 6 on the second page
    fw.write(HELD_PAD, [5])
    fw.write(BUTTONS, struct.pack("<I", 0x20009000))
    fw.stub(BUTTON_FLAG)
    fw.call(SYM["ui_chance_set"], SEQ, 0, 50)
    assert fw.args()[:3] == [0x20009000, 0x20 + 5, 0]


# Storage

def random_track(fw, track, rnd):
    steps = [rnd.choice([0, 1, 1 | rnd.randrange(1, 64) << 1]) for _ in range(64)]
    fw.write(on_addr(track, 0), steps)
    return steps


@pytest.mark.parametrize("track", [0, 1])
def test_codes_survive_save_and_load(fw, track):
    rnd = random.Random(track)
    steps = random_track(fw, track, rnd)
    other = random_track(fw, 1 - track, rnd)
    record = bytes(rnd.randrange(256) for _ in range(0x100))
    fw.write(REC, record)
    fw.write(SAVE_TRACK, [track])
    fw.stub(STORAGE_WRITE)
    fw.call(SYM["save_melodic"], 0x1111, 0x2222, REC, 0x100)
    assert fw.args()[1:] == [0x2222, REC, 0x100]
    saved = fw.read(REC, 0x100)
    assert saved[:0x0B] == record[:0x0B] and saved[0x3D:] == record[0x3D:]

    # The stock loader sets each step to 0 or 1 before the hook runs.
    fw.write(LOADED, saved)
    fw.write(on_addr(track, 0), [s & 1 for s in steps])
    fw.stub(SET_LENGTH)
    fw.call(SYM["load_melodic"], SEQ, track, 16, 0xFFFFFFFF)
    assert fw.args() == [SEQ, track, 16, 0xFFFFFFFF]
    assert list(fw.read(on_addr(track, 0), 64)) == steps
    assert list(fw.read(on_addr(1 - track, 0), 64)) == other


@pytest.mark.parametrize("damage", [0x0B, 0x0C, 0x3B, 0x3C])
def test_records_without_valid_codes_load_as_stock(fw, damage):
    """Patterns saved by stock firmware hold stack garbage in these bytes."""
    random_track(fw, 0, random.Random(5))
    fw.write(SAVE_TRACK, [0])
    fw.stub(STORAGE_WRITE)
    fw.call(SYM["save_melodic"], 0, 0, REC, 0x100)
    record = bytearray(fw.read(REC, 0x100))
    record[damage] ^= 0x10
    fw.write(LOADED, record)
    fw.write(on_addr(0, 0), [1] * 64)
    fw.call(SYM["load_codes"], SEQ, 0)
    assert fw.read(on_addr(0, 0), 64) == b"\1" * 64


# The image

@pytest.mark.parametrize("feature", sorted(FEATURES))
def test_only_the_patch_area_and_the_call_sites_changed(feature):
    stock = Firmware().image
    patched = bytearray(Firmware("out/bsp_2.0.1.6-%s.bin" % feature).image)
    symbols = patch_symbols(feature)
    _, _, redirects, base = FEATURES[feature]
    for site, target, symbol in redirects:
        at = site - BASE
        assert stock[at:at + 4] == bl(site, target)
        assert patched[at:at + 4] == bl(site, symbols[symbol])
        patched[at:at + 4] = stock[at:at + 4]
    patched[base - BASE:IMAGE_END - BASE] = stock[base - BASE:IMAGE_END - BASE]
    assert patched == stock
