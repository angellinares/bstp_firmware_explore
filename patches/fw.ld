/* The stock 2.0.1.6 functions patch code calls, and how it is laid out.
 *
 * The code goes in flash the update image leaves empty (0x080317B8 to
 * 0x0803AFFD), below the 0x0803F800 page the firmware writes at run time.
 * The last two bytes of the image are its checksum.
 *
 * Firmware addresses carry the Thumb bit, or the linker emits BLX.
 */
fw_step_on       = 0x080113df;  /* (seq, track, step, pad) -> the on byte of a step */
fw_rng_next      = 0x0801d1d9;  /* (seq): advance the LCG at seq+4 */
fw_rand_get      = 0x080113b1;  /* (seq, track or -1) -> Randomness knob value */
fw_rand_set      = 0x08011577;  /* (seq, track or -1, value) */
fw_prob_get      = 0x080118f1;  /* (seq, track or -1) -> Probability knob value */
fw_prob_set      = 0x08011c49;  /* (seq, track or -1, value) */
fw_storage_write = 0x08022b39;  /* (storage, address, data, length) */
fw_set_length    = 0x08011191;  /* (seq, track, length, -1, 0, 0) */
fw_button_flag   = 0x0801f8b9;  /* (buttons, button id, 0): release will not toggle it */

/* Used by poly.c */
fw_seq_note_on       = 0x0801d489;  /* (player, channel, note, velocity, track, step, pad, tie) */
fw_seq_note_off      = 0x0801e8cd;  /* (player, track, step, pad) */
fw_track_notes_off   = 0x080159fd;  /* (seq, track, pad, x) */
fw_pattern_clear     = 0x08007b39;  /* (track, pad) */
fw_track_reset       = 0x08008161;  /* (track, ...) */
fw_step_param_set    = 0x08013b49;  /* (seq, track, field, value, step, pad) */
fw_step_velocity_set = 0x08010eeb;  /* (seq, track, velocity, step, pad) */
fw_encoders_poll = 0x0802bee5;
fw_ticks         = 0x080105cd;  /* (seq, track) -> ticks the track has played since it started */
fw_step_ticks    = 0x08011ad9;  /* (seq, step size, track) -> ticks in a step */
fw_drum_loaded   = 0x08014229;  /* (seq, 0): the call that ends a drum pattern load */
fw_pos_set       = 0x080103e7;  /* (seq, track, position, pad or -1): the step a track is at */
fw_show_number   = 0x08023beb;  /* (display, field, value); field 4 is the 3-character display */
fw_show_glyphs   = 0x08023ba3;  /* (display, field, glyph, glyph, glyph) */
fw_out               = 0x0803022d;  /* () -> the MIDI output object */
fw_midi_usb          = 0x0802a785;  /* (out, message, 0) -> 1, or 0 when its 64-message queue is full */
fw_midi_din          = 0x0802a79d;  /* (out, message): the 5-pin output, USART1 */
fw_transpose_get     = 0x0801296d;  /* (seq, track) */
fw_note_fit          = 0x080144c9;  /* (seq, note, transpose, 0) -> MIDI note */
fw_storage_read      = 0x08022a51;  /* (storage, address, length); data lands at 0x20000B5A */

/* MEMORY { PATCH } comes from tools/build_patch.py: the block depends on the feature. */

SECTIONS {
    .text : { KEEP(*(.text*)) KEEP(*(.rodata*)) } > PATCH
    /* The firmware gave us no RAM: writable statics cannot work. */
    .bad : { *(.data*) *(.bss*) *(COMMON) } > PATCH
    ASSERT(SIZEOF(.bad) == 0, "patch code must not use writable statics")
    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) *(.note*) }
}
