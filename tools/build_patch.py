"""Compile patches/*.c into the free flash block and redirect calls to it.

    .venv/Scripts/python tools/build_patch.py out/bsp_2.0.1.6.bin         00_Resources/BeatStepPro_Firmware_Update_2_0_1_6.led [trig|poly|dump]

Writes out/NAME-FEATURE.bin and .led, and prints each call site it changed.
Every version is also kept in out/releases/ and listed in patches/releases.txt.
`trig` is the per-step trigger chance; `poly` is that plus chords. The image is
stamped with the mod and its version from patches/mods.json. A site is
only rewritten if it holds a BL to the stock function listed for it, so
running this on another firmware version fails instead of producing a broken
image.

Needs the ziglang package (a bundled clang and lld).
"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = os.path.join(REPO, "patches")
BASE = 0x08003000
IMAGE_END = 0x0803AFFE  # the two checksum bytes follow
STAMP = 32             # bytes before the checksum: which mod and version this image is
STAMP_MARK = b"bstp-fw "
# (call site, stock target, replacement symbol)
TRIG = [
    (0x0801DC24, 0x080113DE, "trig_step_on"),
    (0x0801E128, 0x080113DE, "trig_step_on"),
    (0x0802C15A, 0x080113B0, "ui_chance_get"),
    (0x0802C3F4, 0x080113B0, "ui_chance_get"),
    (0x08009A28, 0x080113B0, "ui_chance_get"),
    (0x0802C18A, 0x08011576, "ui_chance_set"),
    (0x0802C424, 0x08011576, "ui_chance_set"),
    (0x0800CBBA, 0x08022B38, "save_melodic"),
    (0x0800CF84, 0x08022B38, "save_melodic"),
    (0x0800DDF2, 0x08011190, "load_melodic"),
    (0x0800E0F6, 0x08011190, "load_melodic"),
    (0x0802C1DC, 0x080118F0, "ui_cond_get"),
    (0x0802C476, 0x080118F0, "ui_cond_get"),
    (0x08009A58, 0x080118F0, "ui_cond_shown"),
    (0x0802C20C, 0x08011C48, "ui_cond_set"),
    (0x0802C4A6, 0x08011C48, "ui_cond_set"),
    (0x08009C96, 0x08023BEA, "show_value"),
    (0x08009CCC, 0x08023BEA, "show_value"),
    (0x08009D9E, 0x08023BEA, "show_value"),
    (0x08009E02, 0x08023BEA, "show_value"),
    (0x08012A10, 0x080103E6, "pos_advance"),
    (0x08014ACE, 0x080103E6, "pos_reset"),
    (0x08015182, 0x080103E6, "pos_reset"),
    (0x08015194, 0x080103E6, "pos_reset"),
    (0x080157F8, 0x080103E6, "pos_reset"),
    (0x0800D51A, 0x08022B38, "save_drum"),
    (0x0800E8DA, 0x08014228, "load_drum"),
    (0x0800D984, 0x08022B38, "copy_drum"),
]
POLY = (
    [(site, 0x0801D488, "poly_note_on") for site in (
        0x0801DDE8, 0x0801DE3E, 0x0801DE6E, 0x0801DF24, 0x0801DF4C, 0x0801DF8C,
        0x0801E266, 0x0801E2F0)]
    + [(site, 0x0801E8CC, "poly_note_off") for site in (
        0x0801DE8A, 0x0801DF66, 0x0801E2B2, 0x0801E3F6, 0x0801E4A2)]
    + [(site, 0x080159FC, "poly_track_notes_off") for site in (
        0x0800B93E, 0x0800B952, 0x0800B99E, 0x0800EA3E, 0x0800EA4C, 0x0800EA5C,
        0x08010748, 0x080107D0, 0x08014AD8, 0x08014B7A, 0x080170A8, 0x080170C4,
        0x08017116, 0x0801BA7C, 0x0801E708, 0x0801E750, 0x0801E7A4, 0x0802D33A,
        0x0802D3B8)]
    + [(0x0800B980, 0x08007B38, "poly_pattern_clear")]
    + [(site, 0x08008160, "poly_track_reset") for site in (
        0x0800B91E, 0x0800DE12, 0x0800E116, 0x0800E8FA)]
    + [(0x0801BF18, 0x08013B48, "poly_step_note"),
       (0x0801BF46, 0x08010EEA, "poly_step_velocity")]
)
# feature -> (sources, compiler defines, redirects, first address of its code)
# dump fits the image's last block, which the stock update already sends as
# data. trig (since 1.2.1) and poly need the block below as well, which the
# stock update only erases: their .led carries one more data frame than Arturia's.
DUMP = [
    (0x0800C196, 0x0802BEE4, "dump_poll"),
    (0x0802C20C, 0x08011C48, "dump_start"),
    (0x0802C4A6, 0x08011C48, "dump_start"),
]

FEATURES = {
    "dump": (["dump.c"], [], DUMP, 0x0803A800),
    "trig": (["trig.c"], [], TRIG, 0x0803A000),
    "poly": (["trig.c", "poly.c"], ["-DPOLY"], TRIG + POLY, 0x0803A000),
}


def mods():
    """-> patches/mods.json: each mod's name and changelog; the newest change is its version."""
    with open(os.path.join(PATCHES, "mods.json"), encoding="utf-8") as f:
        return json.load(f)


def stamp(feature):
    """-> the STAMP bytes for a mod: b"bstp-fw trig 1.0.0", zero-padded."""
    text = STAMP_MARK + ("%s %s" % (feature, mods()[feature]["changes"][0][0])).encode("ascii")
    if len(text) >= STAMP:
        raise ValueError("stamp %r does not fit %d bytes" % (text, STAMP))
    return text.ljust(STAMP, b"\0")


def archive(stem, feature):
    """Keep this version for good: out/releases/NAME-FEATURE-VERSION.bin and .led.

    A version is never overwritten. Building other contents under a version
    that is already kept fails: the change needs its own changelog entry. Each
    new version gets a line in patches/releases.txt, and NAME-FEATURE-VERSION-src.zip
    holds the sources it was built from.
    """
    version, date, _ = mods()[feature]["changes"][0]
    folder = os.path.join(os.path.dirname(stem), "releases")
    kept = os.path.join(folder, "%s-%s" % (os.path.basename(stem), version))
    built = {}
    for ext in (".bin", ".led"):
        with open(stem + ext, "rb") as f:
            built[ext] = f.read()
        if os.path.exists(kept + ext):
            with open(kept + ext, "rb") as f:
                if f.read() != built[ext]:
                    sys.exit("%s %s is already kept in %s with other contents: this change needs a new "
                             "changelog entry in patches/mods.json" % (feature, version, folder))
    if os.path.exists(kept + ".led"):
        return
    os.makedirs(folder, exist_ok=True)
    for ext, data in built.items():
        with open(kept + ext, "wb") as f:
            f.write(data)
    sources(kept + "-src.zip")
    with open(os.path.join(PATCHES, "releases.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("%s  %-5s %-7s sha256 %s  %s\n" % (
            date, feature, version, hashlib.sha256(built[".led"]).hexdigest(), os.path.basename(kept) + ".led"))
    print("kept     %s.led" % kept)


def sources(path):
    """Zip what a build is made from: patches/ and the two tools that build it."""
    tools = os.path.join(REPO, "tools")
    files = [os.path.join(PATCHES, name) for name in sorted(os.listdir(PATCHES))]
    files += [os.path.join(tools, name) for name in ("build_patch.py", "led.py")]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            if os.path.isfile(name):
                z.write(name, os.path.relpath(name, REPO).replace(os.sep, "/"))


def zig(*args):
    subprocess.run([sys.executable, "-m", "ziglang", *args], check=True)


def bl(site, target):
    """-> the four bytes of a Thumb BL at site to target."""
    off = target - (site + 4)
    if not -(1 << 24) <= off < 1 << 24 or off & 1:
        raise ValueError("BL from %08x cannot reach %08x" % (site, target))
    s = off >> 24 & 1
    j1, j2 = (~(off >> 23) & 1) ^ s, (~(off >> 22) & 1) ^ s
    return struct.pack("<HH", 0xF000 | s << 10 | off >> 12 & 0x3FF,
                       0xD000 | j1 << 13 | j2 << 11 | off >> 1 & 0x7FF)


def compile_patch(feature):
    """-> (code bytes, {symbol: address})."""
    sources, defines, _, base = FEATURES[feature]
    build = os.path.join(REPO, "out", "patch")
    os.makedirs(build, exist_ok=True)
    elf = os.path.join(build, feature + ".elf")
    script = os.path.join(build, feature + ".ld")
    with open(os.path.join(PATCHES, "fw.ld")) as f, open(script, "w") as out:
        out.write("MEMORY { PATCH (rx) : ORIGIN = %#x, LENGTH = %#x }\n" % (base, IMAGE_END - STAMP - base))
        out.write(f.read())
    zig("cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m3", "-Os",
        "-g",  # keeps the symbol table; debug sections never reach the image
        "-ffreestanding", "-fno-builtin", "-nostdlib", "-Wall", "-Wextra", "-Werror", *defines,
        "-Wl,-T," + script, "-Wl,--no-entry", "-Wl,--no-gc-sections",
        "-o", elf, *(os.path.join(PATCHES, s) for s in sources))
    raw = os.path.join(build, feature + ".bin")
    zig("objcopy", "-O", "binary", elf, raw)
    with open(raw, "rb") as f:
        code = f.read()
    with open(elf, "rb") as f:
        symbols = elf_symbols(f.read())
    return code, symbols


def elf_symbols(elf):
    """-> {name: value} from a 32-bit little-endian ELF's .symtab."""
    shoff, = struct.unpack_from("<I", elf, 0x20)
    shentsize, shnum = struct.unpack_from("<HH", elf, 0x2E)
    sections = [struct.unpack_from("<10I", elf, shoff + i * shentsize) for i in range(shnum)]
    symbols = {}
    for sec in sections:
        if sec[1] != 2:  # SHT_SYMTAB
            continue
        strtab = sections[sec[6]][4]
        for off in range(sec[4], sec[4] + sec[5], 16):
            name, value = struct.unpack_from("<II", elf, off)
            end = elf.index(b"\0", strtab + name)
            symbols[elf[strtab + name:end].decode()] = value
    return symbols


def main():
    image_path, led_path = sys.argv[1], sys.argv[2]
    feature = sys.argv[3] if len(sys.argv) > 3 else "trig"
    stem = "%s-%s" % (os.path.splitext(image_path)[0], feature)
    with open(image_path, "rb") as f:
        image = bytearray(f.read())
    code, symbols = compile_patch(feature)
    base = FEATURES[feature][3]
    start = base - BASE
    if image[start:IMAGE_END - BASE].strip(bytes([0xFF])):
        sys.exit("the patch area is not empty in %s" % image_path)
    image[start:start + len(code)] = code
    image[IMAGE_END - STAMP - BASE:IMAGE_END - BASE] = stamp(feature)
    print("patch    %d of %d bytes at 0x%08X" % (len(code), IMAGE_END - STAMP - base, base))
    print("stamp    %s" % stamp(feature).rstrip(b"\0").decode())
    for site, stock, symbol in FEATURES[feature][2]:
        at = site - BASE
        if bytes(image[at:at + 4]) != bl(site, stock):
            sys.exit("0x%08X does not call 0x%08X: wrong firmware version?" % (site, stock))
        target = symbols[symbol] & ~1
        image[at:at + 4] = bl(site, target)
        print("redirect 0x%08X: 0x%08X -> %s at 0x%08X" % (site, stock, symbol, target))
    with open(stem + ".bin", "wb") as f:
        f.write(image)
    subprocess.run([sys.executable, os.path.join(REPO, "tools", "led.py"), "pack",
                    stem + ".bin", led_path, stem + ".led"], check=True)
    archive(stem, feature)


if __name__ == "__main__":
    main()
