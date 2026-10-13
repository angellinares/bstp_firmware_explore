"""Find every Thumb BL in the raw image that targets an address, by bytes.

    python tools/blscan.py out/bsp_2.0.1.6.bin ADDR [ADDR ...]

Ghidra only records calls from code it disassembled; this reads every
halfword pair, so it also finds callers in code Ghidra never reached. A hit
can be data that happens to look like a BL: check the site.
"""
import struct
import sys

BASE = 0x08003000


def bl_targets(image, base=BASE):
    """-> {target: [site, ...]} for every halfword pair that decodes as BL."""
    found = {}
    for off in range(0, len(image) - 3, 2):
        h1, h2 = struct.unpack_from("<HH", image, off)
        if h1 & 0xF800 != 0xF000 or h2 & 0xD000 != 0xD000:
            continue
        s, j1, j2 = h1 >> 10 & 1, h2 >> 13 & 1, h2 >> 11 & 1
        imm = (s << 24 | (~(j1 ^ s) & 1) << 23 | (~(j2 ^ s) & 1) << 22
               | (h1 & 0x3FF) << 12 | (h2 & 0x7FF) << 1)
        if s:
            imm -= 1 << 25
        found.setdefault(base + off + 4 + imm, []).append(base + off)
    return found


if __name__ == "__main__":
    with open(sys.argv[1], "rb") as f:
        targets = bl_targets(f.read())
    for arg in sys.argv[2:]:
        addr = int(arg, 16)
        print("%08x: %s" % (addr, " ".join("%08x" % s for s in targets.get(addr, [])) or "none"))
