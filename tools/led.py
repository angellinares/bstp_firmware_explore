"""Unpack and repack Arturia BeatStep Pro .led firmware update files.

    python tools/led.py info   FW.led
    python tools/led.py unpack FW.led OUT.bin
    python tools/led.py pack   IN.bin TEMPLATE.led OUT.led

A .led file is one line of hex. Decoded, it is a run of frames, each
covering 2048 bytes of flash at 0x08000000 + (offset << 8):

  data frame  18-byte header (7s prefix, >H offset, 7s, >H length), payload,
              footer ending in a <H checksum; the last frame's footer is
              24 bytes instead of 12
  fill frame  18 bytes, prefix starting 00 10: the block is all FF; its last
              two bytes are a <H checksum, not a length

A frame checksum is the 16-bit two's complement of the byte sum of the
header from byte 2, the payload and the footer before the checksum. The last
two bytes of the image are the same sum over the whole image before them.

`pack` keeps the template's frame layout and replaces only payloads and
checksums, so the image must be the size `unpack` wrote, and a block the
template fills with FF must stay FF.

Format from https://dsgruss.com/notes/2020/10/02/keystep1.html (KeyStep),
checked against BeatStepPro_Firmware_Update_2_0_1_6.led.
"""
import argparse
import struct
import sys

FLASH = 0x08000000
BLOCK = 2048
HEADER = struct.Struct(">7sH7sH")


def neg16(*parts):
    return -sum(sum(p) for p in parts) & 0xFFFF


class Frame:
    def __init__(self, header, data=None, footer=None):
        self.header, self.data, self.footer = header, data, footer

    @property
    def offset(self):
        return HEADER.unpack(self.header)[1]

    @property
    def fill(self):
        return self.data is None

    def payload(self):
        return b"\xff" * BLOCK if self.fill else self.data

    def checksum(self):
        if self.fill:
            return neg16(self.header[2:16])
        return neg16(self.header[2:], self.data, self.footer[:-2])

    def stored(self):
        return struct.unpack("<H", (self.header if self.fill else self.footer)[-2:])[0]

    def pack(self):
        ck = struct.pack("<H", self.checksum())
        if self.fill:
            return self.header[:16] + ck
        return self.header + self.data + self.footer[:-2] + ck


def read_led(path):
    with open(path) as f:
        raw = bytes.fromhex(f.read().strip())
    frames, i = [], 0
    while i < len(raw):
        header = raw[i:i + 18]
        i += 18
        if header[:2] == b"\x00\x10":
            frames.append(Frame(header))
            continue
        length = HEADER.unpack(header)[3]
        data = raw[i:i + length]
        i += length
        # The last frame's footer has a 0D record after the first six bytes.
        size = len(raw) - i if raw[i + 6] == 0x0D else 12
        frames.append(Frame(header, data, raw[i:i + size]))
        i += size
    return frames


def data_frame(frames, offset):
    """-> (header, footer) for a data frame at offset, modelled on the others.

    Every ordinary data frame in the template must differ only in its offset
    (header bytes 7..8, footer bytes 1..2) for this to be a safe guess.
    """
    ordinary = [f for f in frames if not f.fill and len(f.footer) == 12]
    off = struct.pack(">H", offset)
    header = ordinary[0].header[:7] + off + ordinary[0].header[9:]
    footer = ordinary[0].footer[:1] + off + ordinary[0].footer[3:]
    for f in ordinary:
        own = struct.pack(">H", f.offset)
        if (f.header != header[:7] + own + header[9:]
                or f.footer[:-2] != footer[:1] + own + footer[3:-2]):
            sys.exit("data frames are not uniform: cannot build one for offset %#x" % offset)
    return header, footer


def image_of(frames):
    return b"".join(f.payload() for f in frames)


def check(frames):
    """-> list of problems; empty when every checksum and offset holds."""
    problems = ["frame %d (offset %#x): checksum %04X, stored %04X"
                % (n, f.offset, f.checksum(), f.stored())
                for n, f in enumerate(frames) if f.checksum() != f.stored()]
    offsets = [f.offset for f in frames]
    if any(b - a != BLOCK >> 8 for a, b in zip(offsets, offsets[1:])):
        problems.append("frame offsets are not contiguous")
    image = image_of(frames)
    if neg16(image[:-2]) != struct.unpack("<H", image[-2:])[0]:
        problems.append("image checksum %04X, stored %04X"
                        % (neg16(image[:-2]), struct.unpack("<H", image[-2:])[0]))
    return problems


def info(frames):
    image = image_of(frames)
    base = FLASH + (frames[0].offset << 8)
    sp, reset = struct.unpack("<II", image[:8])
    print("frames   %d (%d data, %d fill)"
          % (len(frames), sum(not f.fill for f in frames), sum(f.fill for f in frames)))
    print("image    0x%08X..0x%08X (%d bytes)" % (base, base + len(image), len(image)))
    print("vectors  sp 0x%08X reset 0x%08X" % (sp, reset))
    print("checksum image %04X" % struct.unpack("<H", image[-2:])[0])
    if image[-34:].startswith(b"bstp-fw "):  # tools/build_patch.py stamps its images
        print("patch    %s" % image[-34:-2].rstrip(b"\0").decode())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").add_argument("led")
    p = sub.add_parser("unpack")
    p.add_argument("led")
    p.add_argument("bin")
    p = sub.add_parser("pack")
    p.add_argument("bin")
    p.add_argument("template")
    p.add_argument("led")
    args = ap.parse_args()

    frames = read_led(args.template if args.cmd == "pack" else args.led)
    problems = check(frames)
    if problems:
        sys.exit("\n".join(problems))

    if args.cmd == "info":
        info(frames)
    elif args.cmd == "unpack":
        with open(args.bin, "wb") as f:
            f.write(image_of(frames))
        info(frames)
    else:
        with open(args.bin, "rb") as f:
            image = bytearray(f.read())
        if len(image) != len(frames) * BLOCK:
            sys.exit("image is %d bytes, template holds %d" % (len(image), len(frames) * BLOCK))
        image[-2:] = struct.pack("<H", neg16(image[:-2]))
        for n, frame in enumerate(frames):
            block = bytes(image[n * BLOCK:(n + 1) * BLOCK])
            if not frame.fill:
                frame.data = block
            elif block.strip(b"\xff"):
                frame.header, frame.footer = data_frame(frames, frame.offset)
                frame.data = block
                print("note     block at 0x%08X sent as data; the template only erases it"
                      % (FLASH + (frame.offset << 8)))
        with open(args.led, "w") as f:
            f.write(b"".join(fr.pack() for fr in frames).hex().upper())
        info(frames)


if __name__ == "__main__":
    main()
