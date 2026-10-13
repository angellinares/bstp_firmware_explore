"""Analyse a raw Cortex-M image in Ghidra once, then dump it for grep and SQL.

    .venv/Scripts/python tools/ghidra_dump.py out/bsp_2.0.1.6.bin

Every Ghidra query starts a JVM, so this writes what those queries return,
once, and tools/fwq.py answers from the files:

  out/ghidra/bsp.gpr       the project; the image is imported and analysed
                           the first time and reused after that
  out/NAME/fw.sqlite       meta, vectors, functions, edges, datarefs,
                           strings, symbols. Addresses are integers.
  out/NAME/decomp/*.c      decompiled C, one file per function
  out/NAME/disasm/*.s      address, bytes, instruction

Functions listed in symbols/NAME.txt are named in the dump.

The image is loaded as ARM:LE:32:Cortex at --base, with RAM and peripheral
blocks added and every vector table handler made a function before analysis.

Limits: Ghidra's references miss code it never made a function of, and an
interrupt handler has no caller, only a vector slot. So no row in `edges` is
not proof that a function is dead; check `vectors` and `datarefs` as well.
Decompiled C is Ghidra's interpretation: check claims against the bytes.

Needs GHIDRA_INSTALL_DIR (falls back to C:\\Tools\\ghidra_*) and a JDK.
"""
import argparse
import datetime
import glob
import hashlib
import os
import re
import shutil
import sqlite3
import struct
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGUAGE = "ARM:LE:32:Cortex"
# RAM runs from 0x20000000 to the image's initial stack pointer (48 KB on the
# BeatStep Pro's STM32F103). The other two are the peripherals and the
# Cortex-M private peripheral bus.
BLOCKS = [("PERIPH", 0x40000000, 0x20000000), ("PPB", 0xE0000000, 0x100000)]
EXCEPTIONS = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
              6: "UsageFault", 11: "SVCall", 12: "DebugMon", 14: "PendSV", 15: "SysTick"}
MAX_VECTORS = 16 + 112
DECOMPILE_TIMEOUT = 60

TABLES = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE vectors(idx INTEGER PRIMARY KEY, name TEXT, handler INTEGER);
CREATE TABLE functions(entry INTEGER PRIMARY KEY, end INTEGER, name TEXT, namespace TEXT,
    size INTEGER, signature TEXT, decomp_path TEXT, disasm_path TEXT, decomp_error TEXT);
CREATE TABLE edges(site INTEGER, from_func INTEGER, to_func INTEGER, to_addr INTEGER, kind TEXT);
CREATE TABLE datarefs(site INTEGER, func INTEGER, to_addr INTEGER, kind TEXT, label TEXT, block TEXT);
CREATE TABLE strings(addr INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE symbols(addr INTEGER, name TEXT, namespace TEXT, type TEXT, is_primary INTEGER);
CREATE INDEX functions_range ON functions(entry, end);
CREATE INDEX functions_name ON functions(name);
CREATE INDEX edges_to_func ON edges(to_func);
CREATE INDEX edges_to_addr ON edges(to_addr);
CREATE INDEX edges_from_func ON edges(from_func);
CREATE INDEX datarefs_to ON datarefs(to_addr);
CREATE INDEX datarefs_func ON datarefs(func);
CREATE INDEX symbols_addr ON symbols(addr);
CREATE INDEX symbols_name ON symbols(name);
"""


def log(message):
    print(message, file=sys.stderr, flush=True)


def read_vectors(image, base):
    """-> [(index, name, handler)] for the vector table at the image start.

    The table ends at the first word that is neither zero nor a Thumb address
    inside the image. Handlers shared by several vectors are the default one.
    """
    slots = []
    for idx in range(1, MAX_VECTORS):
        (word,) = struct.unpack_from("<I", image, idx * 4)
        if word and not (word & 1 and base <= word < base + len(image)):
            break
        if word:
            slots.append((idx, word & ~1))
    shared = {h for h in {h for _, h in slots} if sum(1 for _, x in slots if x == h) > 1}
    vectors = []
    for idx, handler in slots:
        own = EXCEPTIONS.get(idx, "IRQ%d" % (idx - 16) if idx >= 16 else "Vector%d" % idx)
        vectors.append((idx, own, handler))
    names = {h: "Default_Handler" for h in shared}
    for idx, own, handler in vectors:
        names.setdefault(handler, own + "_Handler")
    return vectors, names


def prepare(program, image, base, vectors, names):
    """Rebase, add memory blocks and seed the vector table handlers."""
    from ghidra.app.cmd.disassemble import DisassembleCommand
    from ghidra.app.cmd.function import CreateFunctionCmd
    from ghidra.program.model.data import PointerDataType
    from ghidra.program.model.symbol import SourceType
    from ghidra.util.task import TaskMonitor

    space = program.getAddressFactory().getDefaultAddressSpace()
    memory = program.getMemory()
    program.setImageBase(space.getAddress(base), True)
    flash = memory.getBlocks()[0]
    flash.setName("FLASH")
    flash.setPermissions(True, False, True)
    (stack,) = struct.unpack_from("<I", image)
    for name, start, size in [("RAM", 0x20000000, stack - 0x20000000)] + BLOCKS:
        block = memory.createUninitializedBlock(name, space.getAddress(start), size, False)
        block.setPermissions(True, True, False)
        block.setVolatile(name != "RAM")

    listing = program.getListing()
    symbols = program.getSymbolTable()
    for idx in range(len(vectors) and vectors[-1][0] + 1):
        listing.createData(space.getAddress(base + idx * 4), PointerDataType())
    for handler, name in names.items():
        addr = space.getAddress(handler)
        DisassembleCommand(addr, None, True).applyTo(program, TaskMonitor.DUMMY)
        CreateFunctionCmd(addr).applyTo(program, TaskMonitor.DUMMY)
        symbols.createLabel(addr, name, SourceType.IMPORTED)
        symbols.addExternalEntryPoint(addr)


def seed_gaps(program):
    """Make a function at every PUSH {..., lr} still undefined in flash.

    Analysis only follows references it can see, so code reached through an
    untyped pointer table stays undefined (the sequencer playback, 5 KB at
    0x0801D5FC, was). -> the number of functions created.
    """
    from ghidra.app.cmd.disassemble import DisassembleCommand
    from ghidra.app.cmd.function import CreateFunctionCmd
    from ghidra.util.task import TaskMonitor

    listing, memory = program.getListing(), program.getMemory()
    flash = memory.getBlock("FLASH")
    made, addr, last = 0, flash.getStart(), flash.getEnd().subtract(3)
    while addr.compareTo(last) <= 0:
        if listing.isUndefined(addr, addr.add(1)):
            word = memory.getShort(addr) & 0xFFFF
            wide = word == 0xE92D and memory.getShort(addr.add(2)) & 0x4000
            if word & 0xFF00 == 0xB500 or wide:
                DisassembleCommand(addr, None, True).applyTo(program, TaskMonitor.DUMMY)
                made += CreateFunctionCmd(addr).applyTo(program, TaskMonitor.DUMMY)
        addr = addr.add(2)
    return made


def open_or_import(pyghidra, project, name, path, image, base, vectors, names):
    """Import and analyse the image unless the project already has it."""
    if project.getProjectData().getFile("/" + name) is not None:
        log("reusing analysed program /%s" % name)
        return
    log("importing %s as %s at 0x%08X" % (path, LANGUAGE, base))
    loader = (pyghidra.program_loader().project(project).source(path).name(name)
              .language(LANGUAGE).loaders("BinaryLoader"))
    with loader.load() as results:
        results.save(pyghidra.task_monitor())
    try:
        with pyghidra.program_context(project, "/" + name) as program:
            with pyghidra.transaction(program, "prepare"):
                prepare(program, image, base, vectors, names)
            # Off by default, and without it Ghidra never reaches code that
            # is only called through a pointer it did not type, such as the
            # sequencer playback at 0x0801D618.
            with pyghidra.transaction(program, "options"):
                pyghidra.analysis_properties(program).setBoolean(
                    "ARM Aggressive Instruction Finder", True)
            started = time.time()
            pyghidra.analyze(program)
            while True:
                with pyghidra.transaction(program, "seed gaps"):
                    made = seed_gaps(program)
                if not made:
                    break
                log("seeded %d functions in undefined code" % made)
                pyghidra.analyze(program)
            log("analysed in %.0f s" % (time.time() - started))
            program.save("import and analyse", pyghidra.task_monitor())
    except BaseException:
        # A half-prepared program must not be reused by the next run.
        project.getProjectData().getFile("/" + name).delete()
        raise


def apply_names(program, path):
    """Name functions from a symbols file; not saved to the project."""
    from ghidra.program.model.symbol import SourceType

    space = program.getAddressFactory().getDefaultAddressSpace()
    manager = program.getFunctionManager()
    with open(path) as f:
        for line in f:
            fields = line.split("#")[0].split()
            if not fields:
                continue
            fn = manager.getFunctionAt(space.getAddress(int(fields[0], 16)))
            if fn is None:
                sys.exit("%s: no function at %s" % (path, fields[0]))
            fn.setName(fields[1], SourceType.USER_DEFINED)


def file_stem(entry, name):
    return ("%08x_%s" % (entry, re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "unnamed"))[:160]


def dump(pyghidra, program, out, meta, vectors):
    from ghidra.app.decompiler import DecompInterface

    for sub in ("decomp", "disasm"):
        shutil.rmtree(os.path.join(out, sub), ignore_errors=True)
        os.makedirs(os.path.join(out, sub))
    listing, memory = program.getListing(), program.getMemory()
    manager, symbols = program.getFunctionManager(), program.getSymbolTable()
    monitor = pyghidra.task_monitor()

    decompiler = DecompInterface()
    decompiler.openProgram(program)
    functions, code_bytes = [], 0
    started = time.time()
    for fn in manager.getFunctions(True):
        entry, body = fn.getEntryPoint().getOffset(), fn.getBody()
        stem = file_stem(entry, str(fn.getName()))
        lines = []
        for ins in listing.getInstructions(body, True):
            raw = bytes(b & 0xFF for b in ins.getBytes())
            code_bytes += len(raw)
            lines.append("%08x  %-12s  %s" % (ins.getAddress().getOffset(), raw.hex(), ins))
        with open(os.path.join(out, "disasm", stem + ".s"), "w") as f:
            f.write("\n".join(lines) + "\n")
        result = decompiler.decompileFunction(fn, DECOMPILE_TIMEOUT, monitor)
        decomp, error = None, None
        if result.decompileCompleted():
            decomp = "decomp/%s.c" % stem
            with open(os.path.join(out, decomp), "w", encoding="utf-8") as f:
                f.write(str(result.getDecompiledFunction().getC()))
        else:
            error = str(result.getErrorMessage()) or "decompile failed"
        functions.append((entry, body.getMaxAddress().getOffset(), str(fn.getName()),
                          str(fn.getParentNamespace().getName()), body.getNumAddresses(),
                          str(fn.getPrototypeString(False, False)), decomp,
                          "disasm/%s.s" % stem, error))
    decompiler.dispose()
    log("%d functions dumped in %.0f s" % (len(functions), time.time() - started))

    entries = {f[0] for f in functions}
    edges, datarefs = [], []
    for ref in program.getReferenceManager().getReferenceIterator(program.getMinAddress()):
        to = ref.getToAddress()
        if not to.isMemoryAddress():
            continue
        kind, site, target = ref.getReferenceType(), ref.getFromAddress(), to.getOffset()
        owner = manager.getFunctionContaining(ref.getFromAddress())
        owner = owner.getEntryPoint().getOffset() if owner is not None else None
        if kind.isCall() or (kind.isJump() and target in entries and target != owner):
            # A jump to another function's entry is a tail call.
            name = "call" if kind.isCall() else "cond_jump" if kind.isConditional() else "jump"
            edges.append((site.getOffset(), owner, target if target in entries else None, target, name))
        elif not kind.isFlow():
            symbol = symbols.getPrimarySymbol(to)
            block = memory.getBlock(to)
            datarefs.append((site.getOffset(), owner, target, str(kind.getName()).lower(),
                             str(symbol.getName()) if symbol is not None else None,
                             str(block.getName()) if block is not None else None))

    strings = [(d.getAddress().getOffset(), str(d.getValue()))
               for d in listing.getDefinedData(True) if d.hasStringValue()]
    names = [(s.getAddress().getOffset(), str(s.getName()), str(s.getParentNamespace().getName()),
              str(s.getSymbolType()), int(s.isPrimary()))
             for s in symbols.getAllSymbols(False) if s.getAddress().isMemoryAddress()]

    meta = dict(meta, functions=len(functions), code_bytes=code_bytes, edges=len(edges),
                datarefs=len(datarefs), strings=len(strings),
                decompile_failures=sum(1 for f in functions if f[8]))
    path = os.path.join(out, "fw.sqlite")
    if os.path.exists(path + ".tmp"):
        os.remove(path + ".tmp")
    db = sqlite3.connect(path + ".tmp")
    db.executescript(TABLES)
    db.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    db.executemany("INSERT INTO vectors VALUES (?,?,?)", vectors)
    db.executemany("INSERT INTO functions VALUES (?,?,?,?,?,?,?,?,?)", functions)
    db.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", edges)
    db.executemany("INSERT INTO datarefs VALUES (?,?,?,?,?,?)", datarefs)
    db.executemany("INSERT INTO strings VALUES (?,?)", strings)
    db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?)", names)
    db.commit()
    db.close()
    os.replace(path + ".tmp", path)
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image", help="raw image written by tools/led.py unpack")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=0x08003000)
    ap.add_argument("--project", default=os.path.join(REPO, "out", "ghidra"))
    ap.add_argument("--out", help="dump directory (default out/IMAGE_NAME)")
    args = ap.parse_args()

    if not os.environ.get("GHIDRA_INSTALL_DIR"):
        found = sorted(glob.glob(r"C:\Tools\ghidra_*"))
        if not found:
            sys.exit("Ghidra not found: set GHIDRA_INSTALL_DIR")
        os.environ["GHIDRA_INSTALL_DIR"] = found[-1]
    import pyghidra
    pyghidra.start()

    path = os.path.abspath(args.image)
    name = os.path.splitext(os.path.basename(path))[0]
    out = args.out or os.path.join(REPO, "out", name)
    with open(path, "rb") as f:
        image = f.read()
    vectors, names = read_vectors(image, args.base)
    os.makedirs(args.project, exist_ok=True)
    os.makedirs(out, exist_ok=True)

    with pyghidra.open_project(args.project, "bsp", create=True) as project:
        open_or_import(pyghidra, project, name, path, image, args.base, vectors, names)
        with pyghidra.program_context(project, "/" + name) as program:
            meta = {
                "image": name, "image_sha256": hashlib.sha256(image).hexdigest(),
                "base": "0x%08X" % args.base, "size": len(image),
                "language": str(program.getLanguageID().getIdAsString()),
                "ghidra": os.path.basename(os.environ["GHIDRA_INSTALL_DIR"]),
                "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            }
            symbols = os.path.join(REPO, "symbols", name + ".txt")
            if os.path.exists(symbols):
                with pyghidra.transaction(program, "names"):
                    apply_names(program, symbols)
            meta = dump(pyghidra, program, out, meta, vectors)
    used = len(image) - image.count(b"\xff")
    log("%s: %d functions, %d instruction bytes of %d non-FF image bytes (%.0f%%), "
        "%d edges, %d datarefs, %d strings, %d decompile failures"
        % (os.path.join(out, "fw.sqlite"), meta["functions"], meta["code_bytes"], used,
           100.0 * meta["code_bytes"] / used, meta["edges"], meta["datarefs"],
           meta["strings"], meta["decompile_failures"]))


if __name__ == "__main__":
    main()
