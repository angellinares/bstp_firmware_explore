"""Boot the firmware in Unicorn and measure what RAM it really uses.

    .venv/Scripts/python tools/ramscan.py out/bsp_2.0.1.6.bin [--steps N]

Runs from the reset vector with no hardware: every peripheral read
alternates between all ones and zero, so "wait for this bit" loops end
whichever way they test. No interrupt ever fires. It records every malloc
(size, caller, block) and the lowest stack pointer, and fills the heap and
stack area with a marker after start-up zeroes it, to show what is touched.

What this is good for: the heap after start-up and the stack depth on the
paths it reaches. What it is not: proof of the worst case. Code that only
runs on an interrupt, a button, a MIDI message or a pattern load is not
exercised, and the fake peripherals can send start-up down a path the real
device does not take.
"""
import argparse
import collections
import struct

from unicorn import UC_ARCH_ARM, UC_HOOK_BLOCK, UC_HOOK_MEM_READ, UC_MODE_MCLASS, UC_MODE_THUMB, Uc, UcError
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_SP

BASE = 0x08003000
RAM, RAM_SIZE = 0x20000000, 0xC000
MALLOC = 0x080033A8
NEW = 0x0802ED74        # operator new; its callers are in the result as "new"
MAIN_LOOP = 0x0800A61C
RT_ENTRY = 0x080031FE   # where __main goes once RAM is initialised
HEAP = 0x20002820       # end of .data and .bss; the heap starts here
FILL = 0xA5
# Functions that wait on a timer interrupt, which never comes here.
STUBS = [0x0800C9A4]    # delay(ms)
PERIPHERALS = [(0x40000000, 0x30000), (0x42000000, 0x2000000),  # and their bit-band alias
               (0xE0000000, 0x100000), (0x1FFFF000, 0x1000)]


def scan(image, steps):
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    uc.mem_map(0x08000000, 0x40000)
    uc.mem_write(BASE, image)
    uc.mem_map(RAM, 0x10000)
    for start, size in PERIPHERALS:
        uc.mem_map(start, size)
    sp, reset = struct.unpack_from("<II", image)
    for addr in STUBS:
        uc.mem_write(addr, bytes.fromhex("7047"))  # bx lr

    state = {"sp": sp, "pending": [], "reads": collections.Counter(), "main": None,
             "blocks": 0, "filled": False, "new": collections.Counter(), "last": collections.deque(maxlen=64)}
    mallocs = []

    def on_block(uc, addr, size, _):
        state["blocks"] += 1
        state["last"].append(addr)
        cur = uc.reg_read(UC_ARM_REG_SP)
        # The C library runs on a temporary stack before main.
        if cur < state["sp"] and state["main"] is not None:
            state["sp"] = cur
        if state["pending"] and state["pending"][-1][0] == addr:
            _, want, caller = state["pending"].pop()
            mallocs.append((want, caller, uc.reg_read(UC_ARM_REG_R0), state["main"] is not None))
        if addr == RT_ENTRY and not state["filled"]:
            # Start-up has just zeroed all of RAM above .data. Mark what the
            # C library then hands out as heap and stack, to see what is used.
            state["filled"] = True
            uc.mem_write(HEAP, bytes([FILL]) * (cur - 64 - HEAP))
        if addr == NEW:
            state["new"][(uc.reg_read(UC_ARM_REG_LR) & ~1) - 4] += 1
        if addr == MALLOC:
            lr = uc.reg_read(UC_ARM_REG_LR) & ~1
            state["pending"].append((lr, uc.reg_read(UC_ARM_REG_R0), lr - 4))
        elif addr == MAIN_LOOP and state["main"] is None:
            state["main"] = state["blocks"]
        # Not emu_start(count=): single-stepping breaks Thumb IT blocks.
        if state["blocks"] >= steps:
            uc.emu_stop()

    def on_peripheral_read(uc, access, addr, size, value, _):
        state["reads"][addr] += 1
        uc.mem_write(addr, (b"\xff" if state["reads"][addr] & 1 else b"\0") * size)

    uc.hook_add(UC_HOOK_BLOCK, on_block)
    for start, size in PERIPHERALS:
        uc.hook_add(UC_HOOK_MEM_READ, on_peripheral_read, begin=start, end=start + size - 1)
    uc.reg_write(UC_ARM_REG_SP, sp)
    error = None
    try:
        uc.emu_start(reset | 1, 0)
    except UcError as e:
        error = "%s at pc 0x%08X" % (e, uc.reg_read(UC_ARM_REG_PC))
    ram = bytes(uc.mem_read(RAM, RAM_SIZE))
    written = bytes(off < HEAP - RAM or b != FILL for off, b in enumerate(ram))
    return {"mallocs": mallocs, "written": written, "sp_min": state["sp"], "sp_top": sp,
            "reached_main": state["main"] is not None, "error": error,
            "pc": uc.reg_read(UC_ARM_REG_PC), "blocks": state["blocks"],
            "last": list(state["last"]), "ram": ram, "new": state["new"]}


def runs(flags):
    """-> [(start, end, flag)] for runs of equal bytes."""
    out, start = [], 0
    for i in range(1, len(flags) + 1):
        if i == len(flags) or flags[i] != flags[start]:
            out.append((start, i, flags[start]))
            start = i
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("--steps", type=int, default=5_000_000, help="basic blocks to run")
    ap.add_argument("--gaps", type=int, default=256, help="smallest untouched run to list")
    args = ap.parse_args()
    with open(args.image, "rb") as f:
        r = scan(f.read(), args.steps)

    print("stopped at pc 0x%08X after %d blocks%s; main loop %s"
          % (r["pc"], r["blocks"], " (%s)" % r["error"] if r["error"] else "",
             "reached" if r["reached_main"] else "NOT reached"))
    blocks = [(p, s) for s, _, p, _ in r["mallocs"] if p]
    print("malloc   %d calls (%d after the main loop started), %d bytes asked, %d failed"
          % (len(r["mallocs"]), sum(m[3] for m in r["mallocs"]), sum(s for _, s in blocks),
             sum(1 for m in r["mallocs"] if not m[2])))
    if blocks:
        print("heap     0x%08X..0x%08X in use" % (min(p for p, _ in blocks), max(p + s for p, s in blocks)))
    print("stack    lowest sp 0x%08X (%d bytes below 0x%08X)"
          % (r["sp_min"], r["sp_top"] - r["sp_min"], r["sp_top"]))
    print("heap and stack area never written, runs of %d bytes or more:" % args.gaps)
    for start, end, flag in runs(r["written"]):
        if not flag and end - start >= args.gaps:
            print("  0x%08X..0x%08X  %6d bytes" % (RAM + start, RAM + end, end - start))
    print("last blocks: " + " ".join("%08x" % a for a in r["last"][-8:]))


if __name__ == "__main__":
    main()
