# fixture sources and licenses

## KiCad fabrication-output rotation ground truth

The rotation convention is checked against two public KiCad projects that commit
the board and KiCad-generated Excellon output together. Excellon uses absolute
metric decimal coordinates in these extracts. Its Y values have the opposite
sign from KiCad board-file Y values. Both files state 0.001 mm coordinate
precision, so tests use a 0.001 mm absolute tolerance.

| source | commit | license | side / rotation | local pad center | parser board center | fabrication evidence |
|---|---|---|---|---|---|---|
| [noahensley/kicad-probe-scope](https://github.com/noahensley/kicad-probe-scope) | `017a53c5bd6f3ac6296dbdbfea2f9a3618e3d4e3` | MIT, notice in `LICENSE-kicad-probe-scope.txt` | front / +90° | `(0, 2.54)` from X3 | `(73.55, 99.635)` | `X73.55Y-99.635` |
| [noahensley/kicad-probe-scope](https://github.com/noahensley/kicad-probe-scope) | `017a53c5bd6f3ac6296dbdbfea2f9a3618e3d4e3` | MIT | front / +90° | `(1.27, 2.54)` from X3 | `(73.55, 98.365)` | `X73.55Y-98.365` |
| [Ryzee119/hawk](https://github.com/Ryzee119/hawk) | `5ab6f63b280425edd9ee121f5b5520dd8e891990` | MIT, notice in `LICENSE-hawk.txt` | back / 180° | `(-2.949999, -2.9)` from J3 | `(151.449999, 85.4)` | routed slot `X151.45Y-85.812` to `X151.45Y-84.988`, center `(151.45, 85.4)` |

`probe_scope_rotation_trimmed.kicad_pcb` retains the rotated X3 footprint and
its six off-origin drilled pads. `probe_scope_rotation_trimmed.drl` retains the
six corresponding drill coordinates and the format header.
`hawk_back_rotation_trimmed.kicad_pcb` retains one off-origin slotted pad from
the rotated back-side J3 footprint. `hawk_back_rotation_trimmed.drl` retains
that slot's routed endpoints and format header. Unrelated electrical, graphic,
and fabrication data was removed.

The front output proves that KiCad positive 90° maps `(x, y)` to `(y, -x)`.
The back output also proves that child coordinates serialized in a back-side
footprint are already flipped: applying another parser-side mirror gives the
wrong hole center.

The M5Stack repository was also checked at the source commit recorded below.
It contains no committed `.drl`, `.pos`, or Gerber output, so it cannot provide
independent fabrication-coordinate ground truth.

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
