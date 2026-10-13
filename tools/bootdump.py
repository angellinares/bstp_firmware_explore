"""Receive the bootloader that the dump build sends over USB MIDI (patches/dump.c).

    .venv/Scripts/python tools/bootdump.py                 list MIDI inputs
    .venv/Scripts/python tools/bootdump.py PORT [OUT_DIR]   capture, PORT = number or part of the name

Start this, then turn the Probability knob on the BeatStep Pro. It writes
out/bootloader/bootloader.bin (0x08000000, 12 KB), option_bytes.bin and
device_id.bin, and refuses a capture with a missing byte or a wrong sum. Run it
twice: it compares the new capture with the one already there.
"""
import os
import sys
import time

REGIONS = [("bootloader.bin", 0x3000), ("option_bytes.bin", 0x10), ("device_id.bin", 0x20)]
TOTAL = sum(length for _, length in REGIONS)
TIMEOUT = 5.0   # seconds without a message before a started capture is given up


class Decoder:
    """Feed it 3-byte MIDI messages; .data is the dump once .done is true."""

    def __init__(self):
        self.reset()
        self.done = False

    def reset(self):
        self.data = bytearray()
        self.sum_bits = {}
        self.started = False
        self.error = None

    def feed(self, status, d1, d2):
        if status == 0xBF and d1 == 0x70 and d2 == 1:
            self.reset()
            self.started = True
        elif not self.started or self.done:
            return
        elif status & 0xF0 == 0xE0:
            tag = (status & 0xF) << 6 | d2 >> 1
            if tag != len(self.data) & 0x3FF:
                self.error = "lost data at offset 0x%X" % len(self.data)
                self.started = False
                return
            self.data.append(d1 | (d2 & 1) << 7)
        elif status == 0xBF and 0x71 <= d1 <= 0x75:
            self.sum_bits[d1 - 0x71] = d2
        elif status == 0xBF and d1 == 0x70 and d2 == 2:
            self.started = False
            total = sum(v << (7 * k) for k, v in self.sum_bits.items())
            if len(self.data) != TOTAL:
                self.error = "got %d of %d bytes" % (len(self.data), TOTAL)
            elif len(self.sum_bits) != 5 or total != sum(self.data) & 0xFFFFFFFF:
                self.error = "the sum does not match"
            else:
                self.done = True


def split(data):
    at = 0
    for name, length in REGIONS:
        yield name, bytes(data[at:at + length])
        at += length


def describe(parts):
    boot, option, ident = parts["bootloader.bin"], parts["option_bytes.bin"], parts["device_id.bin"]
    sp, reset = int.from_bytes(boot[0:4], "little"), int.from_bytes(boot[4:8], "little")
    print("vectors  sp 0x%08X reset 0x%08X" % (sp, reset))
    print("used     %d of %d bytes are not 0xFF" % (sum(b != 0xFF for b in boot), len(boot)))
    rdp = option[0]
    print("read protection byte 0x%02X: %s" % (
        rdp, "off, a debug probe can read the flash" if rdp == 0xA5 else "on, a debug probe cannot read the flash"))
    print("flash size %d KB" % int.from_bytes(ident[0:2], "little"))


def main():
    import rtmidi
    midi = rtmidi.MidiIn()
    ports = midi.get_ports()
    if len(sys.argv) < 2:
        for number, name in enumerate(ports):
            print(number, name)
        return
    want = sys.argv[1]
    number = int(want) if want.isdigit() else next(
        (n for n, name in enumerate(ports) if want.lower() in name.lower()), None)
    if number is None:
        sys.exit("no MIDI input matches %r" % want)
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join("out", "bootloader")
    midi.open_port(number)
    print("listening on %s; turn the Probability knob" % ports[number])
    decoder, last = Decoder(), time.time()
    while not decoder.done:
        message = midi.get_message()
        if message is None:
            if decoder.started and time.time() - last > TIMEOUT:
                sys.exit("the dump stopped at offset 0x%X" % len(decoder.data))
            time.sleep(0.001)
            continue
        last = time.time()
        if len(message[0]) == 3:
            decoder.feed(*message[0])
            if decoder.error:
                print("discarded: %s; turn the knob again" % decoder.error)
                decoder.error = None
            elif decoder.started and len(decoder.data) % 0x400 == 0 and decoder.data:
                print("%d/%d" % (len(decoder.data), TOTAL), flush=True)
    parts = dict(split(decoder.data))
    os.makedirs(out_dir, exist_ok=True)
    for name, data in parts.items():
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                same = f.read() == data
            print("%-17s %s the earlier capture" % (name, "matches" if same else "DIFFERS FROM"))
            if not same:
                path += ".new"
        with open(path, "wb") as f:
            f.write(data)
        print("wrote    %s (%d bytes)" % (path, len(data)))
    describe(parts)


if __name__ == "__main__":
    main()
