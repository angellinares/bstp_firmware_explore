"""The patch page: its data is current, and its JavaScript rebuilds the tested files."""
import json
import os
import shutil
import subprocess

import pytest

import gen_site
import led
from build_patch import mods, stamp

NODE = """
const fs = require("fs");
const P = require("./site/patches.js");
const { fromHex, toHex, sha256, readStamp, patch } = require("./site/patch.js");
(async () => {
  const stock = fromHex(fs.readFileSync(process.argv[1], "latin1"));
  const out = {};
  for (const mod of P.mods) {
    const built = await patch(stock, P, mod);
    out[mod.id] = { text: toHex(built), stamp: readStamp(built, P) };
    out[mod.id].again = await patch(built, P, mod).catch((e) => e.message);
  }
  out.stock_stamp = readStamp(stock, P);
  console.log(JSON.stringify(out));
})();
"""


def test_every_image_is_stamped_with_its_version():
    for name, mod in mods().items():
        image = led.image_of(led.read_led(gen_site.BUILT % name))
        assert image[-34:-2] == stamp(name)
        assert stamp(name).rstrip(b"\0").decode() == "bstp-fw %s %s" % (name, mod["changes"][0][0])


def test_the_page_data_is_current():
    with open(gen_site.OUT, encoding="utf-8") as f:
        assert f.read() == gen_site.build(), "run tools/gen_site.py"


def test_the_page_holds_no_long_run_of_arturia_bytes():
    """Every edit is bytes that differ from stock; a whole frame is only sent where stock has none."""
    stock = b"".join(f.pack() for f in led.read_led(gen_site.STOCK))
    text = gen_site.build()
    for mod in json.loads(text[text.index("{"):text.rindex("}") + 1])["mods"]:
        for at, drop, new in mod["edits"]:
            new = bytes.fromhex(new)
            if drop == len(new):
                assert all(a != b for a, b in zip(stock[at:at + drop], new))
            else:
                assert stock[at:at + 2] == b"\x00\x10"  # a fill frame: the block is empty in stock


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the page's JavaScript")
def test_the_page_script_rebuilds_the_tested_files():
    run = subprocess.run(["node", "-e", NODE, gen_site.STOCK], cwd=gen_site.REPO,
                         capture_output=True, text=True, check=True)
    out = json.loads(run.stdout)
    assert out["stock_stamp"] is None
    for name, mod in mods().items():
        if not mod["page"]:
            continue
        with open(gen_site.BUILT % name) as f:
            assert out[name]["text"] == f.read()
        assert out[name]["stamp"] == "bstp-fw %s %s" % (name, mod["changes"][0][0])
        assert "already patched (bstp-fw %s" % name in out[name]["again"]
