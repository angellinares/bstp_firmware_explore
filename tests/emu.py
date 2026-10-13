"""Run single firmware functions in Unicorn, on the stock or a patched image."""
import os
import struct

from unicorn import UC_ARCH_ARM, UC_HOOK_BLOCK, UC_MODE_MCLASS, UC_MODE_THUMB, Uc
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP)

from build_patch import elf_symbols

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = 0x08003000
STOP = 0x08000100  # below the image: a return lands here
STACK = 0x2000B000
ARGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)


def patch_symbols(feature):
    """-> {name: address} of the functions in a built patch."""
    with open(os.path.join(REPO, "out", "patch", feature + ".elf"), "rb") as f:
        return {name: value & ~1 for name, value in elf_symbols(f.read()).items()}


class Firmware:
    def __init__(self, image="out/bsp_2.0.1.6.bin"):
        with open(os.path.join(REPO, image), "rb") as f:
            self.image = f.read()
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.uc.mem_map(0x08000000, 0x40000)
        self.uc.mem_write(BASE, self.image)
        self.uc.mem_map(0x20000000, 0x10000)
        self.uc.mem_map(0x40000000, 0x24000)  # peripherals read as zero

    def call(self, addr, *args):
        """Call a function, AAPCS: four register arguments, the rest on the stack -> r0."""
        for reg, value in zip(ARGS, args):
            self.uc.reg_write(reg, value & 0xFFFFFFFF)
        self.write(STACK, b"".join(struct.pack("<I", a & 0xFFFFFFFF) for a in args[4:]))
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.uc.emu_start(addr | 1, STOP, count=200000)
        assert self.uc.reg_read(UC_ARM_REG_PC) == STOP, "function did not return"
        assert self.uc.reg_read(UC_ARM_REG_SP) == STACK, "stack not restored"
        return self.uc.reg_read(UC_ARM_REG_R0)

    def args(self, count=4):
        """-> the arguments as a callee sees them right now."""
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        regs = [self.uc.reg_read(reg) for reg in ARGS]
        return (regs + [self.u32(sp + 4 * i) for i in range(count - 4)])[:count]

    def stub(self, addr):
        """Make a firmware function return at once, arguments untouched."""
        self.uc.mem_write(addr & ~1, b"\x70\x47")  # bx lr

    def spy(self, addr, count=4, result=None, then=None):
        """Stub a function and -> the list its calls' arguments are appended to.

        result replaces r0; then(args) runs at each call, to play the part of
        the function.
        """
        calls = []

        def on_entry(uc, address, size, _):
            calls.append(self.args(count))
            if then:
                then(calls[-1])
            if result is not None:
                uc.reg_write(UC_ARM_REG_R0, result)

        self.stub(addr)
        self.uc.hook_add(UC_HOOK_BLOCK, on_entry, begin=addr & ~1, end=addr & ~1)
        return calls

    def write(self, addr, data):
        self.uc.mem_write(addr, bytes(data))

    def read(self, addr, size):
        return bytes(self.uc.mem_read(addr, size))

    def u32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]
