"""Chords on the melodic sequencers, run on the patched image in an emulator.

    .venv/Scripts/python tools/build_patch.py out/bsp_2.0.1.6.bin 00_Resources/*.led poly
    .venv/Scripts/python -m pytest

The stock functions the patch calls are replaced by spies, and the storage
chip by a dict. So these prove what the patch code does with the arguments
it is given and the state the tests set up, not that the device hands it
those arguments: nothing here runs the sequencer, the panel or the hardware.
"""
import struct

import pytest

from emu import STACK, Firmware, patch_symbols

SYM = patch_symbols("poly")
IMAGE = "out/bsp_2.0.1.6-poly.bin"

SEQ = 0x20001000
SEQ_PTR = 0x20000278
SAVE_TRACK = 0x20000023
PRESS_TIME = 0x20000124
LAST_READ = 0x20000752
LOADED = 0x20000B56 + 4
NOTE_TABLE = 0x20001796
NOTES = 0x20006000       # + track * 384 + step * 6 + slot * 2: pitch + 1, velocity
REC = 0x20008000
OUT = 0x20009000
STORAGE = 0x20009100
PLAYER = 0x20009200

SEQ_NOTE_ON = 0x0801D488
SEQ_NOTE_OFF = 0x0801E8CC
TRACK_NOTES_OFF = 0x080159FC
PATTERN_CLEAR = 0x08007B38
TRACK_RESET = 0x08008160
STEP_PARAM_SET = 0x08013B48
STEP_VELOCITY_SET = 0x08010EEA
FW_OUT = 0x0803022C
MIDI_USB = 0x0802A784
MIDI_DIN = 0x0802A79C      # the 5-pin output
STORAGE_READ = 0x08022A50
STORAGE_WRITE = 0x08022B38
SET_LENGTH = 0x08011190
STEP_NOTE = 0x6F5        # + track * 0x230 + step
TRANSPOSE = 0x8F8        # + track * 0x230
MINUS_ONE = 0xFFFFFFFF
RECORD_ON = 0x0802D2F0
STEP_ON_SET = 0x080115B8
MS = 0x20000574
REC_PENDING = 0x200001C8   # + track
REC_NOTE = 0x20000558      # + track


class Device:
    """The patched firmware with its surroundings faked."""

    def __init__(self):
        fw = self.fw = Firmware(IMAGE)
        fw.write(SEQ_PTR, struct.pack("<I", SEQ))
        for track in (0, 1):
            fw.write(NOTE_TABLE + track * 0x80, range(128))
            fw.write(SEQ + track * 0x230 + TRANSPOSE, [0x3C])  # no transposition
        fw.write(PRESS_TIME, struct.pack("<I", 1000))
        self.flash = {}
        self.stock_note = fw.spy(STEP_PARAM_SET, count=6)
        self.stock_velocity = fw.spy(STEP_VELOCITY_SET, count=5)
        self.stock_note_on = fw.spy(SEQ_NOTE_ON, count=8, result=0x55)
        self.stock_note_off = fw.spy(SEQ_NOTE_OFF)
        self.stock_notes_off = fw.spy(TRACK_NOTES_OFF)
        self.stock_clear = fw.spy(PATTERN_CLEAR)
        self.stock_reset = fw.spy(TRACK_RESET)
        self.set_length = fw.spy(SET_LENGTH, count=6)
        fw.spy(FW_OUT, result=OUT)
        self.usb = fw.spy(MIDI_USB, count=3)
        self.din = fw.spy(MIDI_DIN, count=2)
        self.writes = fw.spy(STORAGE_WRITE, then=self._write)
        self.reads = fw.spy(STORAGE_READ, count=3, then=self._read)

    def _write(self, args):
        _, address, data, length = args
        for i, byte in enumerate(self.fw.read(data, length)):
            self.flash[address + i] = byte

    def _read(self, args):
        _, address, length = args
        self.fw.write(LAST_READ, [3, address >> 16 & 0xFF, address >> 8 & 0xFF, address & 0xFF])
        self.fw.write(LOADED, [self.flash.get(address + i, 0xFF) for i in range(length)])

    def call(self, name, *args):
        return self.fw.call(SYM[name], *args)

    def new_hold(self):
        self.fw.write(PRESS_TIME, struct.pack("<I", self.fw.u32(PRESS_TIME) + 500))

    def pad(self, track, step, note, velocity=100):
        """A pad pressed while the step is held, as the pad handler reports it."""
        self.call("poly_step_note", SEQ, track, 0, note, step, MINUS_ONE)
        if self.stock_note and self.stock_note[-1][3] == note:
            # The stub does not store the note the stock setter was given.
            self.fw.write(SEQ + track * 0x230 + STEP_NOTE + step, [note])
        self.call("poly_step_velocity", SEQ, track, velocity, step, MINUS_ONE)

    def chord(self, track, step):
        raw = self.fw.read(NOTES + track * 384 + step * 6, 6)
        return [(raw[i] - 1, raw[i + 1]) for i in (0, 2, 4) if raw[i]]

    def messages(self):
        """-> MIDI sent since the last call, as (status, note, velocity)."""
        assert [m[1] for m in self.din] == [m[1] for m in self.usb]
        assert all(m[0] == OUT for m in self.din + self.usb) and all(m[2] == 0 for m in self.usb)
        out = [(m[1] & 0xFF, m[1] >> 8 & 0xFF, m[1] >> 16 & 0xFF) for m in self.din]
        del self.din[:], self.usb[:]
        return out

    def save(self, track, address):
        self.fw.write(SAVE_TRACK, [track])
        self.fw.write(REC, [0x11] * 0x100)
        del self.writes[:]
        self.call("save_melodic", STORAGE, address, REC, 0x100)
        return [(a[1], a[3]) for a in self.writes]

    def load(self, track, address):
        """What patterns_load does around the hook, for a pattern that holds data."""
        self._read((STORAGE, address, 0x100))
        self.fw.write(STACK + 0x68, struct.pack("<I", STORAGE))
        del self.reads[:]
        self.call("load_melodic", SEQ, track, 16, MINUS_ONE, 0, 0)
        return [(a[1], a[2]) for a in self.reads]


@pytest.fixture
def dev():
    return Device()


def record(project, track, pattern):
    return project * 0x13000 + track * 0x1000 + pattern * 0x100


def pages(project, track, pattern):
    return 0x140000 + project * 0x4000 + track * 0x2000 + pattern * 0x200


# Entering a chord

def test_first_pad_is_the_stock_note(dev):
    dev.pad(0, 5, 60, 100)
    assert dev.stock_note == [[SEQ, 0, 0, 60, 5, MINUS_ONE]]
    assert dev.stock_velocity == [[SEQ, 0, 100, 5, MINUS_ONE]]
    assert dev.chord(0, 5) == []


@pytest.mark.parametrize("track", [0, 1])
def test_more_pads_in_the_same_hold_build_the_chord(dev, track):
    dev.pad(track, 5, 60, 100)
    dev.pad(track, 5, 64, 90)
    dev.pad(track, 5, 67, 80)
    dev.pad(track, 5, 71, 70)
    assert len(dev.stock_note) == len(dev.stock_velocity) == 1
    assert dev.chord(track, 5) == [(64, 90), (67, 80), (71, 70)]
    assert dev.chord(1 - track, 5) == []
    assert dev.fw.read(SEQ + 0x2450, 2)[track] == 1


def test_a_fifth_note_is_dropped(dev):
    for note in (60, 64, 67, 71, 74):
        dev.pad(0, 5, note)
    assert dev.chord(0, 5) == [(64, 100), (67, 100), (71, 100)]
    assert len(dev.stock_velocity) == 1


def test_repeating_a_note_only_changes_its_velocity(dev):
    dev.pad(0, 5, 60, 100)
    dev.pad(0, 5, 64, 90)
    dev.pad(0, 5, 64, 30)
    dev.pad(0, 5, 60, 20)       # the step's own note: nothing to add
    assert dev.chord(0, 5) == [(64, 30)]
    assert len(dev.stock_note) == len(dev.stock_velocity) == 1


def test_a_new_hold_starts_over(dev):
    dev.pad(0, 5, 60)
    dev.pad(0, 5, 64)
    dev.new_hold()
    dev.pad(0, 5, 62, 50)
    assert dev.chord(0, 5) == []
    assert dev.stock_note[-1][3] == 62 and dev.stock_velocity[-1][2] == 50


def test_another_step_is_another_chord(dev):
    dev.pad(0, 5, 60)
    dev.pad(0, 5, 64)
    dev.pad(0, 6, 62)            # held together with step 5: its own first note
    dev.pad(0, 6, 65)
    assert dev.chord(0, 5) == [(64, 100)] and dev.chord(0, 6) == [(65, 100)]
    assert [n[3] for n in dev.stock_note] == [60, 62]


def test_drum_and_control_tracks_are_stock(dev):
    for track in (2, 3):
        dev.pad(track, 5, 60)
        dev.pad(track, 5, 64)
    assert len(dev.stock_note) == len(dev.stock_velocity) == 4
    assert dev.fw.read(NOTES, 768) == bytes(768)


# Recording

class Recorder:
    """The stock recording calls: a note waits for its release, then record_note_off writes its steps."""

    def __init__(self, dev):
        self.dev = dev
        self.on = dev.fw.spy(RECORD_ON, then=self.pending)
        self.step_on = dev.fw.spy(STEP_ON_SET, count=5)

    def pending(self, args):
        self.dev.fw.write(REC_PENDING + args[0], [1])
        self.dev.fw.write(REC_NOTE + args[0], [args[1]])

    def press(self, track, note, ms, velocity=100, pad=3):
        self.dev.fw.write(MS, struct.pack("<I", ms))
        self.dev.call("rec_on", track, note, velocity, pad)

    def write(self, track, *steps):
        """record_note_off writing the note into these steps (more than one for a long note)."""
        for step in steps:
            self.dev.call("poly_rec_step", SEQ, track, 1, step, MINUS_ONE)
        self.dev.fw.write(REC_PENDING + track, [0])


@pytest.fixture
def rec(dev):
    return Recorder(dev)


@pytest.mark.parametrize("track", [0, 1])
def test_pads_pressed_together_are_recorded_as_a_chord(dev, rec, track):
    rec.press(track, 60, 1000)
    rec.press(track, 64, 1020, velocity=90)
    rec.press(track, 67, 1059, velocity=80)
    assert rec.on == [[track, 60, 100, 3]]
    rec.write(track, 7)
    assert rec.step_on == [[SEQ, track, 1, 7, MINUS_ONE]]
    assert dev.chord(track, 7) == [(64, 90), (67, 80)]


def test_a_long_chord_fills_every_step_of_its_note(dev, rec):
    rec.press(0, 60, 1000)
    rec.press(0, 64, 1010)
    rec.write(0, 7, 8, 9)
    assert [dev.chord(0, step) for step in (7, 8, 9)] == [[(64, 100)]] * 3


def test_a_pad_pressed_later_is_a_new_note(dev, rec):
    rec.press(0, 60, 1000)
    rec.press(0, 64, 1060)
    assert rec.on == [[0, 60, 100, 3], [0, 64, 100, 3]]


def test_a_chord_pad_pressed_again_by_a_release_is_ignored(dev, rec):
    rec.press(0, 60, 1000)
    rec.press(0, 64, 1010)
    rec.press(0, 67, 1020)
    rec.press(0, 64, 1400)      # 67 let go: stock presses 64 again
    rec.press(0, 60, 1500)      # then 64: stock presses 60 again
    assert len(rec.on) == 1
    rec.write(0, 7)
    assert dev.chord(0, 7) == [(64, 100), (67, 100)]


def test_a_fifth_recorded_note_is_dropped(dev, rec):
    for i, note in enumerate((60, 62, 64, 65, 67)):
        rec.press(0, note, 1000 + i)
    rec.write(0, 7)
    assert dev.chord(0, 7) == [(62, 100), (64, 100), (65, 100)]


def test_a_note_recorded_alone_clears_the_chord_of_its_step(dev, rec):
    chord_on(dev, step=7)
    rec.press(0, 60, 1000)
    rec.write(0, 7)
    assert dev.chord(0, 7) == []
    assert dev.save(0, record(0, 0, 0)) == [(record(0, 0, 0), 0x100)]   # no pages left to write


def test_the_next_note_starts_without_a_chord(dev, rec):
    rec.press(0, 60, 1000)
    rec.press(0, 64, 1010)
    rec.write(0, 7)
    rec.press(0, 62, 1200)
    rec.write(0, 9)
    assert dev.chord(0, 7) == [(64, 100)] and dev.chord(0, 9) == []


def test_a_recorded_chord_is_saved(dev, rec):
    rec.press(0, 60, 1000)
    rec.press(0, 64, 1010)
    rec.write(0, 7)
    assert len(dev.save(0, record(0, 0, 0))) == 3


def test_drum_recording_is_stock(dev, rec):
    rec.press(2, 36, 1000, pad=4)
    rec.press(2, 38, 1005, pad=5)
    assert rec.on == [[2, 36, 100, 4], [2, 38, 100, 5]]
    dev.call("poly_rec_step", SEQ, 2, 1, 7, 4)
    assert dev.fw.read(NOTES, 768) == bytes(768)


# Playback

def chord_on(dev, track=0, step=5):
    dev.pad(track, step, 60, 100)
    dev.pad(track, step, 64, 90)
    dev.pad(track, step, 67, 80)


def test_a_step_plays_its_chord_after_the_stock_note(dev):
    chord_on(dev)
    assert dev.call("poly_note_on", PLAYER, 3, 60, 100, 0, 5, MINUS_ONE, 1) == 0x55
    assert dev.stock_note_on == [[PLAYER, 3, 60, 100, 0, 5, MINUS_ONE, 1]]
    assert dev.messages() == [(0x93, 64, 90), (0x93, 67, 80)]


def test_note_off_releases_the_chord_once(dev):
    chord_on(dev)
    dev.call("poly_note_on", PLAYER, 3, 60, 100, 0, 5, MINUS_ONE, 0)
    dev.messages()
    dev.call("poly_note_off", PLAYER, 0, 5, MINUS_ONE)
    assert dev.stock_note_off == [[PLAYER, 0, 5, MINUS_ONE]]
    assert dev.messages() == [(0x83, 64, 0x40), (0x83, 67, 0x40)]
    dev.call("poly_note_off", PLAYER, 0, 5, MINUS_ONE)
    assert dev.messages() == []


def test_the_next_step_releases_a_chord_still_sounding(dev):
    chord_on(dev, step=5)
    dev.new_hold()
    dev.pad(0, 6, 62)
    dev.pad(0, 6, 65)
    dev.call("poly_note_on", PLAYER, 0, 60, 100, 0, 5, MINUS_ONE, 0)
    dev.messages()
    dev.call("poly_note_on", PLAYER, 0, 62, 100, 0, 6, MINUS_ONE, 0)
    assert dev.messages() == [(0x80, 64, 0x40), (0x80, 67, 0x40), (0x90, 65, 100)]


def test_stopping_a_track_releases_its_chord_only(dev):
    chord_on(dev, track=0)
    dev.new_hold()
    chord_on(dev, track=1)
    dev.call("poly_note_on", PLAYER, 0, 60, 100, 0, 5, MINUS_ONE, 0)
    dev.call("poly_note_on", PLAYER, 1, 60, 100, 1, 5, MINUS_ONE, 0)
    dev.messages()
    dev.call("poly_track_notes_off", SEQ, 1, MINUS_ONE, 7)
    assert dev.stock_notes_off == [[SEQ, 1, MINUS_ONE, 7]]
    assert dev.messages() == [(0x81, 64, 0x40), (0x81, 67, 0x40)]


def test_the_chord_follows_the_track_transposition(dev):
    chord_on(dev)
    dev.fw.write(SEQ + TRANSPOSE, [0x3C + 5])
    dev.call("poly_note_on", PLAYER, 0, 65, 100, 0, 5, MINUS_ONE, 0)
    assert dev.messages() == [(0x90, 69, 90), (0x90, 72, 80)]


def test_drum_steps_are_stock(dev):
    chord_on(dev)
    dev.call("poly_note_on", PLAYER, 9, 36, 100, 2, 5, 3, 0)
    dev.call("poly_note_off", PLAYER, 2, 5, 3)
    assert dev.stock_note_on == [[PLAYER, 9, 36, 100, 2, 5, 3, 0]]
    assert dev.messages() == []


# Clearing

@pytest.mark.parametrize("name, args, spy", [
    ("poly_pattern_clear", (0, MINUS_ONE), "stock_clear"),
    ("poly_track_reset", (0, 1, 2, 3), "stock_reset"),
])
def test_clearing_a_pattern_clears_its_chords(dev, name, args, spy):
    chord_on(dev, track=0)
    dev.new_hold()
    chord_on(dev, track=1)
    dev.call(name, *args)
    assert [c[:len(args)] for c in getattr(dev, spy)] == [list(args)]
    assert dev.chord(0, 5) == [] and dev.chord(1, 5) == [(64, 90), (67, 80)]


# Storage

@pytest.mark.parametrize("track", [0, 1])
def test_chords_survive_save_and_load(dev, track):
    chord_on(dev, track, step=5)
    dev.new_hold()
    chord_on(dev, track, step=40)
    address = record(2, track, 3)
    assert dev.save(track, address) == [
        (pages(2, track, 3), 194), (pages(2, track, 3) + 0x100, 194), (address, 0x100)]

    dev.call("poly_track_reset", track, 0, 0, 0)     # another pattern in between
    assert dev.load(track, address) == [(pages(2, track, 3), 0x200), (address, 0x100)]
    assert dev.set_length[-1] == [SEQ, track, 16, MINUS_ONE, 0, 0]
    assert dev.chord(track, 5) == dev.chord(track, 40) == [(64, 90), (67, 80)]
    # The stock loader finds the record it read still in its buffer.
    assert dev.fw.read(LOADED, 0x100) == bytes(dev.flash[address + i] for i in range(0x100))

    assert dev.save(track, address) == [(address, 0x100)]   # nothing changed: pages stay


def test_a_pattern_without_chords_writes_no_pages(dev):
    dev.pad(0, 5, 60)
    assert dev.save(0, record(0, 0, 0)) == [(0, 0x100)]
    assert dev.load(0, record(0, 0, 0)) == []


def test_removing_the_last_chord_drops_the_pages(dev):
    chord_on(dev)
    dev.save(0, record(0, 0, 1))
    dev.new_hold()
    dev.pad(0, 5, 60)
    assert dev.save(0, record(0, 0, 1)) == [(record(0, 0, 1), 0x100)]
    chord_on(dev, step=9)                             # to see that the load clears it
    assert dev.load(0, record(0, 0, 1)) == []
    assert dev.chord(0, 5) == dev.chord(0, 9) == []


def test_saving_to_another_slot_takes_the_chords_along(dev):
    chord_on(dev)
    dev.save(0, record(0, 0, 1))
    assert dev.save(0, record(4, 1, 7))[:2] == [(pages(4, 1, 7), 194), (pages(4, 1, 7) + 0x100, 194)]
    dev.call("poly_track_reset", 1, 0, 0, 0)
    dev.load(1, record(4, 1, 7))
    assert dev.chord(1, 5) == [(64, 90), (67, 80)]


def test_a_record_copied_without_its_pages_has_no_chords(dev):
    chord_on(dev)
    dev.save(0, record(0, 0, 1))
    for i in range(0x100):                            # as a stock pattern copy would
        dev.flash[record(0, 0, 2) + i] = dev.flash[record(0, 0, 1) + i]
    dev.load(0, record(0, 0, 2))
    assert dev.chord(0, 5) == []


def test_pages_of_another_record_are_ignored(dev):
    chord_on(dev)
    dev.save(0, record(0, 0, 1))
    dev.flash[pages(0, 0, 1) + 0x100] ^= 1            # second page: someone else's tag
    dev.load(0, record(0, 0, 1))
    assert dev.chord(0, 5) == []


def test_records_from_the_trig_build_load_without_chords(dev):
    """They hold stack garbage where the page tag goes."""
    chord_on(dev)
    dev.flash.update({record(0, 0, 1) + i: 0xAB for i in range(0x100)})
    assert dev.load(0, record(0, 0, 1)) == []
    assert dev.chord(0, 5) == []
