# eval runner

run one task with either supported harness:

```bash
uv run python -m evals.run --task plate-holes --model <model> --harness claude --out <new-output-directory>
uv run python -m evals.run --task plate-holes --model <model> --harness zeta --zeta-project <project> --coalesce-path <checkout> --out <new-output-directory>

```

Install the runner and constrained-sketch solver before staged sketch tasks:

```bash
uv sync --extra test --extra sketch
```

`--coalesce-path` is the revision under test. the zeta harness reads the coalesce server's `--directory` or `--project` from `<zeta-project>/.zeta/mcp.json`; it does not generate this config. The `coalesce` entry must use `"transport": "stdio"`. The runner fails closed unless the configured checkout has the same `HEAD`. it never changes that checkout; run the checkout command in the error message, then retry. both the server checkout and the checkout under test must be clean; commit or clean all listed files before running. every run records the revision and clean-tree state in its metadata and scorecard.

`--task` accepts a task id, a tier (`easy`, `medium`, or `hard`), or `all`. the output directory must not contain an existing directory for the same task and model.

## task files

tasks are json files under `evals/tasks/`. each task requires `id`, `tier`, `prompt`, `fixtures`, and a non-empty `checks` list. optional `parts`, `documentParts`, and `beforeState` fields add artifact-count, saved-document equivalence, and staged-edit requirements. checks contain a `kind` and the measurements required by that kind; see the existing task files for examples.

## scoring and trust

scoring is fail closed: a run passes only when the harness succeeds, its transcript parses, and every configured check passes. missing, unknown, malformed, extra, or unrecognized evidence fails the run. the runner inventories its complete `steps/` directory, accepts only `.step` and `.stp` files case-insensitively, and rejects all other entries.

hole checks match every detected through-hole one-to-one with the expected positions and diameter range. the solid must also equal its bounding box minus the matched holes in volume (`rel_tol=1e-6`, `abs_tol=1e-3`), so an extra cavity, blind hole, pocket, or boss fails the check. this is an aggregate-volume invariant: a deliberate addition and removal of equal volume (for example a partly blocked hole plus a compensating pocket) can spoof it. move to an exact expected-shape or symmetric-difference comparison when a task needs resistance to deliberate spoofing.

saved document parts are matched to exported solids approximately by volume, bounding box, and centre of mass, using `rel_tol=1e-7` and `abs_tol=1e-6`. a property-equivalent different solid can spoof this comparison. move to exact topology or shape-distance comparison when a task needs stronger identity proof.

scoring assumes no external process mutates the run artifacts while scoring is in progress; external mutation is not guarded.

## hard tier admission

the `edge_treatment` check compares the complete set of fillet and chamfer features (kind, target part, amount, edge roles) with the task's expected set. a wrong edge, a missing treatment, or any extra treatment fails.


Each hard task is admitted only with a passing golden artifact generated through the real MCP path. The hard tier is admitted on this branch with golden scripts under `evals/golden/` and artifacts generated through the stdio MCP server. Base fillets use the public `outer_vertical` edge role; lid top chamfers use `between(edge,top)`. `pcb-refresh-treatments` uses the replacement absolute `path` on import refresh and checks that stable feature identities, moved board features, and treatments survive. The three goldens run in CI with `uv run python -m evals.golden.pcb_change <out>`, `uv run python -m evals.golden.pcb_edge_treatments <out>`, and `uv run python -m evals.golden.pcb_refresh_treatments <out>`; each command must exit successfully with every recorded check passing. Goldens use fixtures from `evals/fixtures`.

The merged product surface adds three hard tasks: `screw-closure-pattern-edit`,
`constrained-inner-loop`, and `constrained-sketch-update-after-extrusion`. Their real-MCP goldens run in CI with:

```bash
uv run python -m evals.golden.screw_closure_pattern_edit <out>
uv run --extra sketch python -m evals.golden.constrained_inner_loop <out>
uv run --extra sketch python -m evals.golden.constrained_sketch_update_after_extrusion <out>
uv run python -m evals.golden.revolve_shaft_edit <out>
```

These tasks bind every STEP solid back to one persisted manufacturing part. The closure task
starts from a runner-built screw-closed enclosure with a three-member, 6 mm lid pattern. It
checks that the agent updates the runner's `lid_pattern` feature exactly once, plus the complete
reimported lid hole set and final document state. The constrained tasks check the reimported
bounding box and inner-loop hole plus the exact saved radius dimension. The update task starts
from a runner-built 3 mm sketch and extrusion, then requires one saved update of that same
`profile` to 4 mm; it does not claim that a rejected update occurred. `revolve-shaft-edit`
starts from a runner-built full-360 stepped shaft and shoulder fillet, then requires an in-place
12 to 16 mm profile edit. It preserves the profile point ids and downstream feature identities,
checks the exact feature set and fillet intent, and reads exact `GeomType.CYLINDER` radii from
the exported STEP topology without curvature sampling.
