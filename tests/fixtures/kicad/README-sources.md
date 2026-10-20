# fixture sources and license findings

these committed fixtures are minimal, hand-made files. every asserted coordinate, diameter, outline primitive, and thickness is present in the fixture source text; tests check those source snippets before parsing.

## public project checks

- [SL2.1A-Type-C-Hub](https://github.com/Hugoyhu/SL2.1A-Type-C-Hub): no `LICENSE` file exists in the inspected default branch (`main`). redistribution is not assumed, so no project board is committed. observed source numbers: thickness 1.6 mm; 1 closed loop; bbox `53.086..110.49 x 80.772..108.712`; 32 footprints; 32 drilled holes; M3 NPTH holes at `(106.68, 84.582)` and `(56.896, 104.902)`.
- [M5_Hardware](https://github.com/m5stack/M5_Hardware): `MIT License`, copyright 2021 M5Stack (the actual repository `LICENSE` file was inspected). redistribution is permitted with attribution, but no full project board is committed because the test uses a minimal fixture. observed source numbers: thickness 1.6 mm; 1 closed loop; bbox `0..50 x 0..50 mm`; 20 footprints; 37 drilled holes; M2 holes at `(7,3)`, `(7,47)`, `(43,3)`, `(43,47)`; M3 holes at `(3,3)`, `(47,3)`, `(3,47)`, `(47,47)`, plus one additional M3; four 1.1 mm CN6 NPTH holes at approximately `(44.049862, 11.030137)`, `(44.049862, 41.510137)`, `(44.049862, 8.490137)`, `(44.049878, 38.970121)`.

both source-project tests are explicit skips because the source boards are not redistributed. this keeps the observed values documented without silently depending on `/tmp` files in ci.
