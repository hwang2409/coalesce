# fixture sources and licenses

## M5Stack Module_ExtPort_For_Core2

`m5stack_module_extport_trimmed.kicad_pcb` is a mechanically relevant extract of
[`KiCad/Projects/Module_ExtPort_For_Core2/Module_ExtPort_For_Core2.kicad_pcb`](https://github.com/m5stack/M5_Hardware/blob/60cabaaeae992f23050261e3dc90f8e12e1f277e/KiCad/Projects/Module_ExtPort_For_Core2/Module_ExtPort_For_Core2.kicad_pcb)
at upstream commit `60cabaaeae992f23050261e3dc90f8e12e1f277e`.

The extract retains the board `Edge.Cuts`, one front mounting-hole footprint, two
representative back footprints, front and back courtyards, a 3D model reference,
and plated and non-plated drills. Unrelated electrical and artwork data was
removed. Tests calculate transformed coordinates from the retained source values.

M5_Hardware is distributed under the MIT License, copyright 2021 M5Stack. The
required notice is in `LICENSE-M5Stack.txt`.

## synthetic coverage

The other fixtures are small, hand-written files for isolated syntax and geometry
cases. `kicad6_legacy_text.kicad_pcb` covers KiCad 6 `fp_text reference` and
`fp_text value`. `rounded.kicad_pcb` uses valid three-point arcs.

The USB hub board previously used for manual checks has no license file in its
upstream repository, so none of its source is redistributed here.
