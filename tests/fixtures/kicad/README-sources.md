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

## generated USB-C hub regression

`usb_c_hub_generated.kicad_pcb` is original, generated KiCad 8 syntax. It does
not copy board content from the unlicensed upstream repository. Its mechanical
facts were independently recorded from
[`Hugoyhu/SL2.1A-Type-C-Hub`](https://github.com/Hugoyhu/SL2.1A-Type-C-Hub):
1.6 mm thickness; one four-line, four-arc rounded outline bounded by x 53.086 to
110.49 mm and y 80.772 to 108.712 mm; 32 footprints and holes; and 3.2 mm
mounting holes at `(106.68, 84.582)` and `(56.896, 104.902)`. The remaining
fixture footprint names, positions, and one-millimetre drills are generated test
data and do not represent upstream content.

The upstream repository has no license file, so none of its file content is
redistributed here.
