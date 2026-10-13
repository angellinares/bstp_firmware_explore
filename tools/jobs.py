"""The live progress pane: every running job (Ghidra dumps, builds, tests, REA runs), redrawn each second.

    python tools/jobs.py [--keep 10] [--interval 1]

Meant for a Windows Terminal split pane beside Claude Code:

    wt -w 0 split-pane -V --size 0.35 -d D:\\01_Code\\Z_Personal\\bstp_custom_fw D:\\01_Code\\Z_Personal\\bstp_custom_fw\\.venv\\Scripts\\python.exe tools/jobs.py

Reads out/progress/ (tools/progress.py). Each job: a bar and its percentage (when it knows
its total), the time it has run, its current step and its last detail line; a job a
job another job started sits under it. Finished jobs stay for KEEP minutes, marked done,
failed or lost (its process went away while it said running).

Keys: c collapses the pane to one line a job (and back), f shows or hides finished jobs,
p prunes finished jobs older than a day, q quits.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import progress  # noqa: E402

COL = {"running": "\x1b[36m", "done": "\x1b[32m", "failed": "\x1b[31m", "lost": "\x1b[33m"}
DIM, BOLD, OFF = "\x1b[90m", "\x1b[1m", "\x1b[0m"
MARK = {"running": "▶", "done": "✔", "failed": "✘", "lost": "?"}


def bar(percent: float | None, width: int) -> str:
    if percent is None:
        return DIM + "·" * width + OFF
    filled = int(round(width * percent / 100))
    return "█" * filled + DIM + "░" * (width - filled) + OFF


def tree(items: list[dict]) -> list[tuple[int, dict]]:
    """Jobs in display order with their depth: each parent followed by its children."""
    by_id = {d["id"]: d for d in items}
    kids: dict[str | None, list[dict]] = {}
    for d in items:
        parent = d.get("parent") if d.get("parent") in by_id else None
        kids.setdefault(parent, []).append(d)
    out: list[tuple[int, dict]] = []

    def walk(parent, depth):
        for d in sorted(kids.get(parent, []), key=lambda d: d.get("started", 0)):
            out.append((depth, d))
            walk(d["id"], depth + 1)
    walk(None, 0)
    return out


def render(compact: bool, show_finished: bool, keep_min: float) -> str:
    cols = shutil.get_terminal_size((100, 30)).columns
    now = time.time()
    items = [d for d in progress.jobs()
             if d["state"] == "running" or (show_finished and now - (d.get("ended") or d.get("updated", now)) < keep_min * 60)]
    running = sum(d["state"] == "running" for d in items)
    keys = "c compact · f finished · p prune · q quit" if cols >= 72 else "c f p q"
    lines = [f"{BOLD}bstp jobs{OFF}  {running} running  {DIM}{time.strftime('%H:%M:%S')}   {keys}{OFF}", ""]
    if not items:
        lines.append(DIM + "  no jobs" + OFF)
    for depth, d in tree(items):
        state = d["state"]
        pct = d["percent"]
        head = "  " * depth + f"{COL.get(state, '')}{MARK.get(state, '·')}{OFF} {BOLD}{d['name']}{OFF}"
        count = f"{d.get('done', 0)}/{d['total']}" if d.get("total") else ""
        pct_text = f"{pct:5.1f}%" if pct is not None else "     "
        info = f"{pct_text} {count:>7} {progress.clock(d['elapsed']):>7}"
        if compact:
            step = d.get("step") or ""
            room = max(0, cols - len(d["name"]) - 2 * depth - len(info) - 6)
            lines.append(f"{head}  {info}  {DIM}{step[:room]}{OFF}")
            continue
        lines.append(head)
        width = max(10, min(40, cols - 2 * depth - 32))
        lines.append("  " * depth + f"  {bar(pct, width)} {info}")
        if d.get("step"):
            lines.append("  " * depth + f"  step  {d['step'][:max(0, cols - 2 * depth - 8)]}")
        if d.get("detail"):
            lines.append("  " * depth + f"  {DIM}{d['detail'][:max(0, cols - 2 * depth - 4)]}{OFF}")
    return "\n".join(lines)


def key() -> str | None:
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415
        if msvcrt.kbhit():
            return msvcrt.getwch().lower()
        return None
    import select  # noqa: PLC0415
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1).lower()
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--keep", type=float, default=10.0, help="minutes a finished job stays shown")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    a = p.parse_args(argv)
    if sys.platform == "win32":
        os.system("")                                   # turn on VT escape sequences
        sys.stdout.reconfigure(encoding="utf-8")
    compact, show_finished = False, True
    if a.once:
        print(render(compact, show_finished, a.keep))
        return 0
    sys.stdout.write("\x1b[?25l")                       # hide the cursor
    try:
        while True:
            sys.stdout.write("\x1b[H\x1b[2J" + render(compact, show_finished, a.keep) + "\n")
            sys.stdout.flush()
            end = time.time() + a.interval
            while time.time() < end:
                k = key()
                if k == "q":
                    return 0
                if k == "c":
                    compact = not compact
                    break
                if k == "f":
                    show_finished = not show_finished
                    break
                if k == "p":
                    progress.prune()
                    break
                time.sleep(0.05)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\x1b[?25h")


if __name__ == "__main__":
    raise SystemExit(main())
