# eval runner

run one task with either supported harness:

```bash
uv run python -m evals.run --task plate-holes --model <model> --harness claude --out <new-output-directory>
uv run python -m evals.run --task plate-holes --model <model> --harness zeta --zeta-project <project> --out <new-output-directory>
```

`--task` accepts a task id, a tier (`easy`, `medium`, or `hard`), or `all`. the output directory must not contain an existing directory for the same task and model.

## task files

tasks are json files under `evals/tasks/`. each task requires `id`, `tier`, `prompt`, `fixtures`, and a non-empty `checks` list. optional `parts`, `documentParts`, and `beforeState` fields add artifact-count, saved-document equivalence, and staged-edit requirements. checks contain a `kind` and the measurements required by that kind; see the existing task files for examples.

## scoring and trust

scoring is fail closed: a run passes only when the harness succeeds, its transcript parses, and every configured check passes. missing, unknown, malformed, extra, or unrecognized evidence fails the run. the runner inventories its complete `steps/` directory, accepts only `.step` and `.stp` files case-insensitively, and rejects all other entries.

hole checks match every detected through-hole one-to-one with the expected positions and diameter range. the solid must also equal its bounding box minus the matched holes in volume (`rel_tol=1e-6`, `abs_tol=1e-3`), so an extra cavity, blind hole, pocket, or boss fails the check. this is an aggregate-volume invariant: a deliberate addition and removal of equal volume (for example a partly blocked hole plus a compensating pocket) can spoof it. move to an exact expected-shape or symmetric-difference comparison when a task needs resistance to deliberate spoofing.

saved document parts are matched to exported solids approximately by volume, bounding box, and centre of mass, using `rel_tol=1e-7` and `abs_tol=1e-6`. a property-equivalent different solid can spoof this comparison. move to exact topology or shape-distance comparison when a task needs stronger identity proof.

scoring assumes no external process mutates the run artifacts while scoring is in progress; external mutation is not guarded.
