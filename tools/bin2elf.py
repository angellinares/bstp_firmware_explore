"""Wrap the raw image in an ELF so tools that need a file format can load it.

    python tools/bin2elf.py out/bsp_2.0.1.6.bin out/bsp_2.0.1.6.elf

The ELF says: ARM, little-endian, loaded at 0x08003000, entry at
the reset vector, plus an empty RAM segment at 0x20000000.
"""
import struct
import sys

BASE = 0x08003000
RAM, RAM_SIZE = 0x20000000, 0xC000


def main():
    with open(sys.argv[1], "rb") as f:
        image = f.read()
    reset, = struct.unpack_from("<I", image, 4)
    phoff, phnum = 52, 2
    offset = phoff + 32 * phnum
    header = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + bytes(9), 2, 40, 1,
                         reset, phoff, 0, 0x05000200, 52, 32, phnum, 0, 0, 0)
    flash = struct.pack("<8I", 1, offset, BASE, BASE, len(image), len(image), 5, 4)
    ram = struct.pack("<8I", 1, 0, RAM, RAM, 0, RAM_SIZE, 6, 4)
    with open(sys.argv[2], "wb") as f:
        f.write(header + flash + ram + image)


if __name__ == "__main__":
    main()
