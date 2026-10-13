"""Query the database tools/ghidra_dump.py wrote, without starting Ghidra.

    python tools/fwq.py info
    python tools/fwq.py func    ADDR|NAME        the function containing it
    python tools/fwq.py callers ADDR|NAME        calls, tail jumps, pointers, vectors
    python tools/fwq.py callees ADDR|NAME
    python tools/fwq.py refs    ADDR [END]       who reads or writes the range
    python tools/fwq.py strings [TEXT]
    python tools/fwq.py sql     "select ..."

--db picks another dump (default: the newest out/*/fw.sqlite). Addresses are
hex. No caller listed is not proof of dead code: Ghidra only records
references from code it disassembled.
"""
import argparse
import glob
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_function(db, what):
    """-> (entry, end, name, decomp_path) for an address inside it or a name."""
    try:
        addr = int(what, 16)
        row = db.execute("SELECT entry, end, name, decomp_path FROM functions "
                         "WHERE entry <= ? AND end >= ? ORDER BY entry DESC", (addr, addr)).fetchone()
    except ValueError:
        row = db.execute("SELECT entry, end, name, decomp_path FROM functions WHERE name = ?",
                         (what,)).fetchone()
    if row is None:
        sys.exit("no function for %s" % what)
    return row


def show(rows):
    for row in rows:
        print("  ".join("%08x" % v if isinstance(v, int) and v > 0xFFFF else str(v) for v in row))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db")
    ap.add_argument("cmd", choices=["info", "func", "callers", "callees", "refs", "strings", "sql"])
    ap.add_argument("args", nargs="*")
    opts = ap.parse_args()
    dumps = sorted(glob.glob(os.path.join(REPO, "out", "*", "fw.sqlite")), key=os.path.getmtime)
    path = opts.db or (dumps[-1] if dumps else sys.exit("no out/*/fw.sqlite; run tools/ghidra_dump.py"))
    if not opts.db and len(dumps) > 1:
        print("using %s (newest of %d dumps; --db picks another)" % (path, len(dumps)), file=sys.stderr)
    db = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    cmd, args = opts.cmd, opts.args

    if cmd == "info":
        show(db.execute("SELECT key, value FROM meta"))
    elif cmd == "func":
        entry, end, name, decomp = find_function(db, args[0])
        print("%08x..%08x  %s  %s" % (entry, end, name, os.path.join(os.path.dirname(path), decomp or "")))
    elif cmd == "callers":
        entry = find_function(db, args[0])[0]
        show(db.execute(
            "SELECT e.kind, e.site, coalesce(f.name, '(no function)') FROM edges e "
            "LEFT JOIN functions f ON f.entry = e.from_func WHERE e.to_func = ? "
            "UNION ALL SELECT 'pointer', d.site, coalesce(f.name, '(data)') FROM datarefs d "
            "LEFT JOIN functions f ON f.entry = d.func WHERE d.to_addr IN (?, ?) "
            "UNION ALL SELECT 'vector', idx, name FROM vectors WHERE handler = ?",
            # A stored function pointer has the Thumb bit set.
            (entry, entry, entry + 1, entry)))
    elif cmd == "callees":
        entry = find_function(db, args[0])[0]
        show(db.execute(
            "SELECT DISTINCT e.kind, e.to_addr, coalesce(f.name, '(no function)') FROM edges e "
            "LEFT JOIN functions f ON f.entry = e.to_func WHERE e.from_func = ? ORDER BY e.to_addr",
            (entry,)))
    elif cmd == "refs":
        lo = int(args[0], 16)
        hi = int(args[1], 16) if len(args) > 1 else lo
        show(db.execute(
            "SELECT d.to_addr, d.kind, d.site, coalesce(f.name, '(data)') FROM datarefs d "
            "LEFT JOIN functions f ON f.entry = d.func WHERE d.to_addr BETWEEN ? AND ? "
            "ORDER BY d.to_addr, d.site", (lo, hi)))
    elif cmd == "strings":
        show(db.execute("SELECT addr, text FROM strings WHERE text LIKE ?",
                        ("%" + (args[0] if args else "") + "%",)))
    else:
        show(db.execute(args[0]))


if __name__ == "__main__":
    main()
