"""Draw the figures of docs/step-chance-and-conditions.md as SVG files in docs/figures/.

    python tools/gen_figures.py

The chance values and the conditions come from the tables in patches/trig.c, and
which pass a condition plays on from the same rule as cond_met there, so the
figures follow the firmware.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "figures")
SRC = open(os.path.join(ROOT, "patches", "trig.c"), encoding="utf-8").read()

CHANCE = [int(v) for v in re.search(r"chance\[\] = \{([^}]*)\}", SRC).group(1).split(",")]
RATIO = [tuple(map(int, p)) for p in re.findall(r"\{(\d), (\d)\}", re.search(r"ratio\[\]\[2\] = \{(.*?)\};", SRC, re.S).group(1))]

INK, MUTED, LINE, ON, ACCENT, BG = "#1d2430", "#6b7380", "#c9ced6", "#2f6fdb", "#e0782f", "#ffffff"
FONT = "font-family='Helvetica, Arial, sans-serif'"
MONO = "font-family='Consolas, Menlo, monospace'"


def conditions():
    """-> [(display text, plays(pass from 1))] in the order the Probability knob walks them."""
    out = [("1-1", lambda p: True), ("1St", lambda p: p == 1), ("n1S", lambda p: p != 1)]
    for a, b in RATIO:
        out.append(("%d-%d" % (a, b), lambda p, a=a, b=b: (p - 1) % b == a - 1))
        out.append(("n%d%d" % (a, b), lambda p, a=a, b=b: (p - 1) % b != a - 1))
    return out


def svg(name, width, height, body):
    text = ("<svg xmlns='http://www.w3.org/2000/svg' width='%d' height='%d' viewBox='0 0 %d %d'>\n"
            "<rect width='100%%' height='100%%' fill='%s'/>\n%s\n</svg>\n" % (width, height, width, height, BG, body))
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def label(x, y, text, size=13, colour=INK, anchor="start", font=FONT, weight="normal"):
    return "<text x='%g' y='%g' font-size='%d' fill='%s' text-anchor='%s' font-weight='%s' %s>%s</text>" % (
        x, y, size, colour, anchor, weight, font, text)


def panel():
    """The gesture: hold a step, turn a knob, read the display."""
    p = []
    # display
    p.append("<rect x='30' y='30' width='130' height='60' rx='6' fill='%s'/>" % INK)
    p.append(label(95, 74, "n12", 34, "#ff5a4a", "middle", MONO, "bold"))
    p.append(label(95, 112, "display", 12, MUTED, "middle"))
    # knobs
    for x, name, use in ((260, "RANDOMNESS", "chance"), (400, "PROBABILITY", "condition")):
        p.append("<circle cx='%d' cy='60' r='26' fill='none' stroke='%s' stroke-width='3'/>" % (x, INK))
        p.append("<line x1='%d' y1='60' x2='%d' y2='38' stroke='%s' stroke-width='3'/>" % (x, x + 12, INK))
        p.append("<path d='M %d 24 A 40 40 0 0 1 %d 40' fill='none' stroke='%s' stroke-width='2.5' marker-end='url(#a)'/>"
                 % (x + 16, x + 36, ACCENT))
        p.append(label(x, 106, name, 12, INK, "middle", weight="bold"))
        p.append(label(x, 122, "sets the " + use, 12, MUTED, "middle"))
    # step row
    for i in range(16):
        x = 30 + i * 30
        held = i == 4
        p.append("<rect x='%d' y='160' width='24' height='24' rx='3' fill='%s' stroke='%s'/>"
                 % (x, ON if held or i in (0, 8, 12) else BG, INK))
        p.append(label(x + 12, 200, str(i + 1), 10, MUTED, "middle"))
    p.append("<path d='M 162 240 L 162 192' stroke='%s' stroke-width='2.5' marker-end='url(#a)'/>" % ACCENT)
    p.append(label(172, 238, "1. hold a step that is on", 13, INK))
    p.append(label(310, 160 - 12, "2. turn a knob while you hold it", 13, INK))
    defs = ("<defs><marker id='a' viewBox='0 0 10 10' refX='8' refY='5' markerWidth='7' markerHeight='7' orient='auto'>"
            "<path d='M0 0 L10 5 L0 10 z' fill='%s'/></marker></defs>" % ACCENT)
    svg("panel.svg", 520, 260, defs + "\n".join(p))


def chance_scale():
    """The 21 chance values on a 0..100 line, plus 100 for a normal step."""
    left, width = 30, 640
    x = lambda v: left + v * width / 100
    p = ["<line x1='%g' y1='70' x2='%g' y2='70' stroke='%s' stroke-width='2'/>" % (x(0), x(100), LINE)]
    for i, v in enumerate(CHANCE + [100]):
        last = v == 100
        p.append("<circle cx='%g' cy='70' r='5' fill='%s'/>" % (x(v), INK if last else ON))
        y = 120 if last else 52 if i % 2 else 98
        p.append(label(x(v), y, str(v), 11, INK, "middle", MONO))
    p.append(label(x(100) - 30, 120, "no chance, plays every time:", 12, MUTED, "end"))
    p.append(label(left, 24, "Randomness, turned right one click at a time, with a step held (percent)", 13, INK))
    svg("chance.svg", 700, 136, "\n".join(p))


def condition_grid(passes=12):
    """Which passes each condition plays on."""
    rows = conditions()
    cell, top, left = 24, 56, 70
    p = [label(10, 22, "The passes on which a step plays, for each condition", 13, INK)]
    for n in range(passes):
        p.append(label(left + n * cell + cell / 2, top - 10, str(n + 1), 11, MUTED, "middle"))
    p.append(label(left - 8, top - 10, "pass", 11, MUTED, "end"))
    for r, (name, plays) in enumerate(rows):
        y = top + r * cell
        if r in (1, 3, 7, 13):  # a new group: 1St and n1S, then B = 2, 3, 4
            p.append("<line x1='4' y1='%g' x2='%g' y2='%g' stroke='%s'/>" % (y - 2, left + passes * cell, y - 2, LINE))
        p.append(label(left - 10, y + 16, name, 13, INK, "end", MONO, "bold"))
        for n in range(passes):
            on = plays(n + 1)
            p.append("<rect x='%d' y='%d' width='%d' height='%d' rx='3' fill='%s' stroke='%s'/>"
                     % (left + n * cell + 2, y + 2, cell - 4, cell - 4, ON if on else BG, ON if on else LINE))
    y = top + len(rows) * cell + 22
    p.append("<rect x='%d' y='%d' width='16' height='16' rx='3' fill='%s'/>" % (left, y - 12, ON))
    p.append(label(left + 22, y + 1, "the step plays", 12, INK))
    p.append("<rect x='%d' y='%d' width='16' height='16' rx='3' fill='%s' stroke='%s'/>" % (left + 150, y - 12, BG, LINE))
    p.append(label(left + 172, y + 1, "the step is silent", 12, INK))
    svg("conditions.svg", left + passes * cell + 20, y + 16, "\n".join(p))


def fill_example():
    """A 16-step drum part over 4 passes: a fill on steps 14 to 16 set to 4-4."""
    always, fill = {0, 4, 8, 12}, {13, 14, 15}
    cell, top, left = 26, 40, 76
    p = [label(10, 22, "Kick on steps 1, 5, 9, 13 (no condition). Fill on steps 14, 15, 16 set to 4-4.", 13, INK)]
    for i in range(16):
        p.append(label(left + i * cell + cell / 2, top + 4 * cell + 18, str(i + 1), 10, MUTED, "middle"))
    p.append(label(left - 10, top + 4 * cell + 18, "step", 10, MUTED, "end"))
    for n in range(4):
        y = top + n * cell
        p.append(label(left - 10, y + 17, "pass %d" % (n + 1), 12, INK, "end"))
        for i in range(16):
            if i in always:
                fill_c, edge = ON, ON
            elif i in fill:
                fill_c, edge = (ACCENT, ACCENT) if n == 3 else (BG, ACCENT)
            else:
                fill_c, edge = BG, LINE
            p.append("<rect x='%d' y='%d' width='%d' height='%d' rx='3' fill='%s' stroke='%s'/>"
                     % (left + i * cell + 2, y + 2, cell - 4, cell - 4, fill_c, edge))
    y = top + 4 * cell + 44
    p.append("<rect x='%d' y='%d' width='16' height='16' rx='3' fill='%s'/>" % (left, y - 12, ACCENT))
    p.append(label(left + 22, y + 1, "the 4-4 step plays: pass 4, 8, 12 and so on", 12, INK))
    p.append("<rect x='%d' y='%d' width='16' height='16' rx='3' fill='%s' stroke='%s'/>" % (left + 270, y - 12, BG, ACCENT))
    p.append(label(left + 292, y + 1, "the 4-4 step is silent", 12, INK))
    svg("fill.svg", left + 16 * cell + 20, y + 16, "\n".join(p))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    assert len(CHANCE) == 21 and len(RATIO) == 9
    assert [n for n, _ in conditions()][:5] == ["1-1", "1St", "n1S", "1-2", "n12"]
    panel()
    chance_scale()
    condition_grid()
    fill_example()
    print("figures in", OUT)
