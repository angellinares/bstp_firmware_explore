"""Claude Code hooks for this project (.claude/settings.local.json).

    hooks.py prompt     UserPromptSubmit: a prompt about tracing a value gets a note
                        to use REA's value trace.
    hooks.py tested     PostToolUse on Bash/PowerShell: after a command that builds
                        or tests a patched image, each image REA has not yet checked
                        is wrapped as an ELF, copied into WSL, and a note asks for
                        the REA second opinion on it.
    hooks.py checked FEATURE...
                        Record that the second opinion was done for the current
                        image of each feature, so it is not asked for again.

Both hooks read the event JSON on stdin and answer with JSON that adds context for
Claude. They decide by fixed patterns, not by understanding the request. A hook must
never break the session: any error ends quietly.
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
STATE = OUT / "rea" / "checked.json"
WSL_DIR = "/root/bsp"

TRACE = re.compile(
    r"value[- ]trac|trace (the |a |this |that )?(value|constant|argument|parameter|address)"
    r"|where (does|do|is|are) .{1,60} (come|came|computed|calculated|derived|set) ?(from)?"
    r"|how (is|are) .{1,60} (computed|calculated|derived)"
    r"|(data|value)[- ]flow|def-?use"
    r"|which (constants|values|addresses) (does|do|is|are)",
    re.I)
TESTED = re.compile(r"python\S*\s+(-\S+\s+)*(-m\s+pytest|\S*build_patch\.py)")

TRACE_NOTE = (
    "This request looks like value tracing. Use REA for it: open the ELF with "
    "mcp__rea-wsl__open_binary (stock: /root/bsp/bsp_2.0.1.6.elf), run "
    "mcp__rea-wsl__trace_native_values on the function, then close_binary. Its result "
    "is large: read the saved file with a script and list the constants per opcode. "
    "REA does not see seq_play_event or the code around 0x0801D5FC..0x0801E9EC; for "
    "functions there, read out/bsp_2.0.1.6/decomp instead and say so. Show the run in "
    "the pane with tools/progress.py begin/step/end.")


def reply(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def images():
    """feature -> its patched image, for the images that exist."""
    found = {}
    for path in sorted(OUT.glob("bsp_2.0.1.6-*.bin")):
        found[path.stem.split("-", 1)[1]] = path
    return found


def expectations(feature):
    """What REA should show for this image: written to a file the note points at."""
    sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tests")]
    from build_patch import FEATURES
    from emu import patch_symbols
    symbols = patch_symbols(feature)
    by_symbol = {}
    for site, target, symbol in FEATURES[feature][2]:
        by_symbol.setdefault((symbol, target), []).append(site)
    lines = ["# %s: xrefs REA should report in the patched image" % feature,
             "# REA cannot see sites in 0x0801D5FC..0x0801E9EC; their absence is expected.", ""]
    for (symbol, target), sites in sorted(by_symbol.items()):
        lines.append("%s at 0x%08x replaces 0x%08x at %d site(s):" % (
            symbol, symbols[symbol], target, len(sites)))
        lines.append("  " + " ".join("0x%08x" % s for s in sorted(sites)))
    path = OUT / "rea" / (feature + ".expect.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def prepare(feature, image):
    elf = image.with_suffix(".elf")
    subprocess.run([sys.executable, str(ROOT / "tools" / "bin2elf.py"), str(image), str(elf)],
                   check=True, capture_output=True, timeout=60)
    drive, rest = elf.as_posix().split(":", 1)
    subprocess.run(["wsl.exe", "--", "cp", "/mnt/%s%s" % (drive.lower(), rest), WSL_DIR + "/"],
                   check=True, capture_output=True, timeout=60)
    return "%s/%s" % (WSL_DIR, elf.name)


def tested(event):
    command = event.get("tool_input", {}).get("command", "")
    if not TESTED.search(command) or "hooks.py" in command:
        return
    done = state()
    notes = []
    for feature, image in images().items():
        if done.get(feature) == digest(image):
            continue
        STATE.parent.mkdir(parents=True, exist_ok=True)
        notes.append("- %s: ELF at %s, expected xrefs in %s" % (
            feature, prepare(feature, image), expectations(feature).relative_to(ROOT).as_posix()))
    if notes:
        reply("PostToolUse",
              "A patched image was built or tested and REA has not checked this build yet. "
              "Before reporting the result, get REA's second opinion on each image below: "
              "open it with mcp__rea-wsl__open_binary; for every patch function in the "
              "expected file, call mcp__rea-wsl__xrefs and check the sites it reports are all "
              "in the expected list; call xrefs on each replaced stock function and check no "
              "expected site still refers to it; decompile any patch function whose source "
              "changed since the last check and compare it with patches/*.c; close_binary. "
              "Show the run in the pane (tools/progress.py begin/step/end). Then run "
              "`.venv/Scripts/python tools/hooks.py checked FEATURE...` and tell the user what "
              "REA confirmed and what it could not see.\n" + "\n".join(notes))


def main():
    mode = sys.argv[1]
    if mode == "checked":
        done, found = state(), images()
        for feature in sys.argv[2:]:
            done[feature] = digest(found[feature])
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
        return
    try:
        event = json.load(sys.stdin)
        if mode == "prompt":
            if TRACE.search(event.get("prompt", "")):
                reply("UserPromptSubmit", TRACE_NOTE)
        elif mode == "tested":
            tested(event)
    except Exception:  # noqa: BLE001 -- a hook must never fail
        pass


if __name__ == "__main__":
    main()
