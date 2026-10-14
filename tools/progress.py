"""Live progress of long jobs (Ghidra dumps, patch builds, tests), for the pane.

A job writes one small JSON file under out/progress/ and keeps it current:

    import progress
    with progress.Job("ghidra dump", total=4) as job:
        for stage in stages:
            job.advance(step=stage)         # done += 1, step = the stage's name
            ...

or, inside a loop that already counts, `job.update(done=k, total=n, step=..., detail=...)`.
A job started by another job's process finds its parent through BSTP_PROGRESS_PARENT,
which Job sets for its children, so the pane shows the tree. Writes are atomic (a
temporary file, then a rename) and at most a few a second, whatever the caller does; the
last one on leaving the `with` is always written, as "done" or, after an exception,
"failed".

For a command that knows nothing of this module:

    python tools/progress.py run --name "tests" -- python -u -m pytest

runs it, passes its output through, keeps the last line as the job's detail and the exit
code as its state.

For work with no process to wrap:

    python tools/progress.py begin --name "callers" [--total 5]    # prints the job id
    python tools/progress.py step ID [--done 2] [--step TEXT]
    python tools/progress.py end ID [--failed] [--detail TEXT]

Such a job has pid 0: nothing can tell whether it is still going, so it shows as lost
once it has not been updated for an hour.

The reader (`tools/jobs.py`) calls `jobs()`: every job file, newest first, with `alive`
(its process is still running) and `elapsed`. A running job whose process has gone is
shown as lost. `prune()` removes finished jobs' files older than a day.
"""

from __future__ import annotations

import argparse
import ctypes
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIR = pathlib.Path(os.environ.get("BSTP_PROGRESS_DIR", ROOT / "out" / "progress"))
PARENT_ENV = "BSTP_PROGRESS_PARENT"
MIN_INTERVAL = 0.25                     # seconds between writes of one job
MANUAL_TIMEOUT = 3600.0                 # a pid 0 job silent this long is lost
_counter = itertools.count()


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40] or "job"


class Job:
    """One job's progress file. DONE / TOTAL are counts (TOTAL None: no percentage)."""

    def __init__(self, name: str, total: int | None = None, step: str = "", parent: str | None = None):
        self.id = f"{os.getpid()}-{next(_counter)}-{_slug(name)}"
        self.data = {"id": self.id, "name": name, "pid": os.getpid(),
                     "parent": parent if parent is not None else os.environ.get(PARENT_ENV),
                     "started": time.time(), "updated": time.time(), "ended": None,
                     "done": 0, "total": total, "step": step, "detail": "", "state": "running"}
        self.path = DIR / f"{self.id}.json"
        self._last = 0.0
        self._env_before = None

    # -- the context: children see this job as their parent
    def __enter__(self) -> "Job":
        self._env_before = os.environ.get(PARENT_ENV)
        os.environ[PARENT_ENV] = self.id
        self._write(force=True)
        return self

    def __exit__(self, kind, value, tb) -> bool:
        if self._env_before is None:
            os.environ.pop(PARENT_ENV, None)
        else:
            os.environ[PARENT_ENV] = self._env_before
        if self.data["state"] == "running":
            self.data["state"] = "failed" if kind else "done"
        if kind and not self.data["detail"]:
            self.data["detail"] = f"{kind.__name__}: {value}"[:200]
        self.data["ended"] = time.time()
        self._write(force=True)
        return False

    # -- updates
    def update(self, done: int | None = None, total: int | None = None, step: str | None = None,
               detail: str | None = None, state: str | None = None, force: bool = False) -> None:
        changed_shape = False                   # a new step, total or state is always written
        for key, value in (("done", done), ("total", total), ("step", step), ("detail", detail), ("state", state)):
            if value is not None:
                changed_shape |= key in ("step", "total", "state") and self.data[key] != value
                self.data[key] = value
        self._write(force=force or changed_shape)

    def advance(self, n: int = 1, step: str | None = None, detail: str | None = None) -> None:
        self.update(done=self.data["done"] + n, step=step, detail=detail)

    def _write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last < MIN_INTERVAL:
            return
        self._last = now
        self.data["updated"] = now
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        for _ in range(5):                      # a reader may hold the file open for a moment
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.02)


# -- readers -------------------------------------------------------------------------------

def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def jobs() -> list[dict]:
    """Every job, newest first, with `alive`, `elapsed` (s), `percent` (or None), and
    state 'lost' for a running job whose process has gone."""
    out = []
    if not DIR.exists():
        return out
    now = time.time()
    for path in DIR.glob("*.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(d.get("pid", 0))
        d["alive"] = d.get("state") == "running" and (
            _alive(pid) if pid else now - d.get("updated", 0) < MANUAL_TIMEOUT)
        if d.get("state") == "running" and not d["alive"]:
            d["state"] = "lost"
        d["elapsed"] = (d.get("ended") or now) - d.get("started", now)
        total = d.get("total")
        d["percent"] = None if not total else max(0.0, min(100.0, 100.0 * d.get("done", 0) / total))
        out.append(d)
    out.sort(key=lambda d: d.get("started", 0), reverse=True)
    return out


def prune(older_than: float = 86400.0) -> int:
    """Remove the files of jobs that ended (or were lost) more than OLDER_THAN seconds ago."""
    n, now = 0, time.time()
    for d in jobs():
        if d["state"] != "running" and now - (d.get("ended") or d.get("updated", now)) > older_than:
            try:
                (DIR / f"{d['id']}.json").unlink()
                n += 1
            except OSError:
                pass
    return n


def clock(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


# -- the command wrapper --------------------------------------------------------------------

_COUNT = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")


def run_command(name: str, argv: list[str], total: int | None = None) -> int:
    """Run ARGV as job NAME: its output passes through; the last line becomes the detail,
    and a 'k/n' in it (when TOTAL is not given) the progress."""
    with Job(name, total=total, step=" ".join(argv)[:80]) as job:
        argv = [shutil.which(argv[0]) or argv[0]] + argv[1:]    # Windows: a relative path with /
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
        for line in p.stdout:
            sys.stdout.write(line)
            text = line.strip()
            if not text:
                continue
            m = _COUNT.search(text) if total is None else None
            if m and int(m.group(2)) > 0:
                job.update(done=int(m.group(1)), total=int(m.group(2)), detail=text[:200])
            else:
                job.update(detail=text[:200])
        code = p.wait()
        job.update(state="done" if code == 0 else "failed", detail=f"exit {code}: {job.data['detail']}"[:200])
        return code


def manual(a) -> int:
    """begin / step / end: a job kept by separate commands, so its file is its only state."""
    if a.cmd == "begin":
        job = Job(a.name, total=a.total, step=a.step or "")
        job.data["pid"] = 0
        job._write(force=True)
        print(job.id)
        return 0
    path = DIR / f"{a.id}.json"
    job = Job("")
    job.path, job.data = path, json.loads(path.read_text(encoding="utf-8"))
    if a.cmd == "step":
        job.update(done=a.done, step=a.step, detail=a.detail, force=True)
    else:
        job.data["ended"] = time.time()
        job.update(state="failed" if a.failed else "done", detail=a.detail, force=True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a command as a job")
    r.add_argument("--name", required=True)
    r.add_argument("--total", type=int)
    r.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("prune", help="remove finished jobs older than a day")
    b = sub.add_parser("begin", help="start a job with no process; prints its id")
    b.add_argument("--name", required=True)
    b.add_argument("--total", type=int)
    b.add_argument("--step")
    s = sub.add_parser("step", help="update a job started with begin")
    s.add_argument("id")
    s.add_argument("--done", type=int)
    s.add_argument("--step")
    s.add_argument("--detail")
    e = sub.add_parser("end", help="finish a job started with begin")
    e.add_argument("id")
    e.add_argument("--failed", action="store_true")
    e.add_argument("--detail")
    a = p.parse_args(argv)
    if a.cmd in ("begin", "step", "end"):
        return manual(a)
    if a.cmd == "prune":
        print(f"removed {prune()} finished job(s)")
        return 0
    argv = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
    if not argv:
        p.error("run needs a command after --")
    return run_command(a.name, argv, a.total)


if __name__ == "__main__":
    raise SystemExit(main())
