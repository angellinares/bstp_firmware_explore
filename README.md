# BeatStep Pro custom firmware

Unofficial changes to the firmware of the Arturia BeatStep Pro, version 2.0.1.6.
Not made or supported by Arturia.

**Patch page: <https://angellinares.github.io/bstp_firmware_explore/>**

The page turns Arturia's own update file into a modified one, in your browser.
You supply `BeatStepPro_Firmware_Update_2_0_1_6.led` from Arturia's downloads;
nothing is uploaded, and the page only offers a file that is byte for byte a
build tested here. You install it with MIDI Control Center, like any update.

> Modified firmware can leave your unit unusable, and you install it at your own
> risk. **Back up all your projects with MIDI Control Center first**: a mod
> changes how patterns are stored, and a fault could damage or erase them. Then
> install Arturia's own 2.0.1.6 file once before anything from here, so you know
> the way back works on your unit.

## The mods

| Mod | What it does | Status |
|---|---|---|
| Step chance and conditions | Hold a step and turn **Randomness** to give it a chance to play, or **Probability** to give it a condition: `1-2` plays on pass 1 of every 2, `n12` on every pass but that one, `1St` on the first pass only, up to `4-4`. On SEQ1, SEQ2 and, since 1.2.1, on DRUM steps of the selected pad. Saved with the pattern. | Runs on one unit |
| Ratchets (version 1.3.2 of the mod above) | A DRUM step plays 2, 4 or 8 times inside its own length: hold the step and touch the roller strip, or record a roll finer than the track. Ratcheted steps light cyan. | Built, not yet run on a device; the page offers 1.2.1 until it is |
| Chords | Up to four notes on a SEQ1 or SEQ2 step, with chance and conditions. | Built, not yet run on a device; not on the page until it is |

How to use each mod, with what every condition plays, is under "How to use it"
on the patch page; the same text is the `guide` in
[`patches/mods.json`](patches/mods.json).

Versions and their changes are in [`patches/mods.json`](patches/mods.json); every
version built, with the SHA-256 of its update file, is in
[`patches/releases.txt`](patches/releases.txt). The mod and version are also
written into the firmware itself: `tools/led.py info FILE` prints them.

Going back: install Arturia's original file the same way. If the unit does not
start, hold PLAY + STOP + RECORD while switching it on to reach the updater.

## What is in this repository

| | |
|---|---|
| `patches/` | The C code of the mods, and the stock functions it calls (`fw.ld`) |
| `tools/` | `led.py` unpacks and repacks `.led` update files; `build_patch.py` compiles a mod into an image; `gen_site.py` writes the page's data |
| `tests/` | The mods run in an emulator against state set up by hand |
| `site/` | The patch page |

No Arturia material is here: no firmware, original or modified, and nothing
derived from it. To build, you need your own copy of the update file:

```
python tools/led.py unpack BeatStepPro_Firmware_Update_2_0_1_6.led out/bsp_2.0.1.6.bin
python tools/build_patch.py out/bsp_2.0.1.6.bin BeatStepPro_Firmware_Update_2_0_1_6.led trig
python -m pytest
```

Building needs Python with `ziglang` (a bundled C compiler); the tests need
`unicorn` and `pytest`.

## How far to trust it

The tests prove what the patch code computes. They do not prove the device
behaves: that is only known for the versions marked as run on a unit, and only
on that one unit.
