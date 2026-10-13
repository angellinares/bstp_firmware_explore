"""The bootloader dump build, run in an emulator against made-up memory.

These prove the patch sends what tools/bootdump.py decodes, and that it waits
for room in the send queue. They do not prove the device's USB side behaves.
"""
import random
import struct

import pytest

from bootdump import TOTAL, Decoder, split
from unicorn.arm_const import UC_ARM_REG_R0

from emu import Firmware, patch_symbols

SEQ = 0x20001000
STATE = 0x20006000       # struct dump: state, next, sum
OUT = 0x0803022C
MIDI_USB = 0x0802A784      # (out, message, 0) -> 1 when queued
PROB_SET = 0x08011C48
ENCODERS_POLL = 0x0802BEE4
RUNNING = 0xD0B007ED

SYM = patch_symbols("dump")


@pytest.fixture
def fw():
    fw = Firmware("out/bsp_2.0.1.6-dump.bin")
    rnd = random.Random(7)
    fw.boot = bytes(rnd.randrange(256) for _ in range(0x3000))
    fw.write(0x08000000, fw.boot)
    fw.uc.mem_map(0x1FFFF000, 0x1000)
    fw.system = bytes(rnd.randrange(256) for _ in range(0x1000))
    fw.write(0x1FFFF000, fw.system)
    fw.write(STATE, struct.pack("<III", 0x12345678, 0x9999, 0x7777))   # start-up garbage
    fw.spy(OUT, result=0x20009800)
    fw.room = True                   # a test clears this to play a full queue
    fw.sent = []

    def queue(args):
        if fw.room:
            fw.sent.append(args)
        fw.uc.reg_write(UC_ARM_REG_R0, int(fw.room))

    fw.spy(MIDI_USB, count=3, then=queue)
    return fw


def messages(fw):
    return [(m & 0xFF, m >> 8 & 0xFF, m >> 16 & 0xFF) for _, m, _ in fw.sent]


def run(fw, passes=4000):
    for _ in range(passes):
        fw.call(SYM["dump_step"])
        if fw.u32(STATE) != RUNNING:
            break


def test_nothing_is_sent_until_the_knob_is_turned(fw):
    run(fw, 20)
    assert fw.sent == []


def test_the_knob_still_sets_the_probability(fw):
    calls = fw.spy(PROB_SET, count=3)
    fw.call(SYM["dump_start"], SEQ, 1, 42)
    assert calls == [[SEQ, 1, 42]]
    assert fw.u32(STATE) == RUNNING and fw.u32(STATE + 4) == 0 and fw.u32(STATE + 8) == 0


def test_the_dump_decodes_to_the_memory(fw):
    fw.stub(PROB_SET)
    fw.call(SYM["dump_start"], SEQ, 0, 50)
    run(fw)
    decoder = Decoder()
    for message in messages(fw):
        decoder.feed(*message)
    assert decoder.done and len(decoder.data) == TOTAL
    parts = dict(split(decoder.data))
    assert parts["bootloader.bin"] == fw.boot
    assert parts["option_bytes.bin"] == fw.system[0x800:0x810]
    assert parts["device_id.bin"] == fw.system[0x7E0:0x800]
    assert fw.u32(STATE) != RUNNING
    count = len(fw.sent)
    run(fw, 20)
    assert len(fw.sent) == count


def test_turning_the_knob_again_does_not_restart_a_running_dump(fw):
    fw.stub(PROB_SET)
    fw.call(SYM["dump_start"], SEQ, 0, 50)
    run(fw, 10)
    offset = fw.u32(STATE + 4)
    fw.call(SYM["dump_start"], SEQ, 0, 51)
    assert offset > 0 and fw.u32(STATE + 4) == offset


def test_a_refused_message_is_sent_again(fw):
    """The queue is full on every other pass: the dump still arrives whole."""
    fw.stub(PROB_SET)
    fw.call(SYM["dump_start"], SEQ, 0, 50)
    for n in range(8000):
        fw.room = n % 2 == 0
        fw.call(SYM["dump_step"])
        if fw.u32(STATE) != RUNNING:
            break
    decoder = Decoder()
    for message in messages(fw):
        decoder.feed(*message)
    assert decoder.done and dict(split(decoder.data))["bootloader.bin"] == fw.boot


def test_a_pass_sends_at_most_eight_messages_with_a_zero_third_argument(fw):
    fw.stub(PROB_SET)
    fw.call(SYM["dump_start"], SEQ, 0, 50)
    fw.call(SYM["dump_step"])
    assert len(fw.sent) == 8 and all(out == 0x20009800 and x == 0 for out, _, x in fw.sent)


def test_the_main_loop_hook_keeps_the_arguments(fw):
    calls = fw.spy(ENCODERS_POLL, count=4)
    fw.call(SYM["dump_poll"], 0x2000A094, 2, 3, 4)
    assert calls == [[0x2000A094, 2, 3, 4]]


def test_a_lost_message_is_noticed():
    decoder = Decoder()
    decoder.feed(0xBF, 0x70, 1)
    decoder.feed(0xE0, 0x11, 0 << 1)
    decoder.feed(0xE0, 0x22, 2 << 1)
    assert decoder.error and not decoder.done
