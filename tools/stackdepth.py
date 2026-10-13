"""Worst-case stack depth from the call graph in fw.sqlite.

    python tools/stackdepth.py [FUNCTION ...]     default: main_loop and the vectors

A function's frame is what it pushes plus what it subtracts from sp,
read from its disassembly. The depth of a function is its frame plus the
deepest callee. Calls through a pointer (virtual methods, callbacks) are not
in the graph, so the result is a floor on the true worst case, not a bound.
Recursion is cut where it closes.
"""
import glob
import os
import re
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUSH = re.compile(r"\s(?:push|stmdb sp!,)\s*\{([^}]*)\}")
SUB = re.compile(r"\ssub(?:\.w|w)?\s+sp,(?:sp,)?#(0x[0-9a-f]+|\d+)")
VPUSH = re.compile(r"\svpush\s*\{([^}]*)\}")


def frame(path):
    """-> bytes of stack one activation of the function uses."""
    size = 0
    with open(path) as f:
        for line in f:
            m = PUSH.search(line)
            if m:
                regs = 0
                for part in m.group(1).split(","):
                    lo, _, hi = part.strip().partition("-")
                    regs += int(hi[1:]) - int(lo[1:]) + 1 if hi else 1
                size += 4 * regs
            m = SUB.search(line)
            if m:
                size += int(m.group(1), 0)
    return size


def main():
    dump = sorted(glob.glob(os.path.join(REPO, "out", "*", "fw.sqlite")), key=os.path.getmtime)[-1]
    db = sqlite3.connect(dump)
    names, frames = {}, {}
    for entry, name, path in db.execute("SELECT entry, name, disasm_path FROM functions"):
        names[entry] = name
        frames[entry] = frame(os.path.join(os.path.dirname(dump), path))
    callees = {}
    for a, b in db.execute("SELECT DISTINCT from_func, to_func FROM edges "
                           "WHERE from_func IS NOT NULL AND to_func IS NOT NULL"):
        callees.setdefault(a, set()).add(b)

    depth, path_of = {}, {}

    def walk(fn, active):
        if fn in depth:
            return depth[fn]
        best, via = 0, None
        for callee in callees.get(fn, ()):
            if callee in active:
                continue
            d = walk(callee, active | {callee})
            if d > best:
                best, via = d, callee
        depth[fn], path_of[fn] = frames[fn] + best, via
        return depth[fn]

    sys.setrecursionlimit(10000)
    by_name = {v: k for k, v in names.items()}
    roots = [by_name[a] for a in sys.argv[1:]] or (
        [by_name["main_loop"]] + [h for (h,) in db.execute("SELECT DISTINCT handler FROM vectors")])
    rows = []
    for root in roots:
        if root not in names:
            continue
        total = walk(root, {root})
        chain, fn = [], root
        while fn is not None:
            chain.append("%s(%d)" % (names[fn], frames[fn]))
            fn = path_of[fn]
        rows.append((total, names[root], " > ".join(chain)))
    for total, name, chain in sorted(rows, reverse=True)[:8]:
        print("%6d  %s\n        %s" % (total, name, chain))


if __name__ == "__main__":
    main()
