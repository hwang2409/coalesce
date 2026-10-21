# constrained sketch solver

Coalesce keeps constrained-sketch solving behind `src/coalesce/sketch_solver.py`. The base package does not import or require a solver.

## install

Install the optional solver extra:

```sh
pip install 'coalesce[sketch]'
```

PlaneGCS publishes a Python 3.13 Linux wheel. On macOS, install its build prerequisites once before pip builds it from source:

```sh
brew install eigen boost
pip install 'coalesce[sketch]'
```

## seam guarantees

Call `solve_sketch()` with coalesce-owned points, lines, arcs, circles, dimensions, constraints, initial values, and branch landmarks. Each item has a stable caller-supplied id. The result:

- returns solved geometry keyed by those ids;
- reports degrees of freedom and under-, fully-, over-, or redundantly constrained state;
- maps redundant and conflicting solver diagnostics back to stable constraint ids;
- rebuilds solver input in stable id order for deterministic results;
- rejects a branch flip or ambiguous landmark without returning partial geometry; and
- does not expose or persist PlaneGCS handles.

A successful numeric solve is not enough to accept geometry. Callers that edit a sketch must provide branch landmarks from the last accepted result. The seam fails closed if a landmark changes sign or becomes too close to zero.

## replace the solver

PlaneGCS is a separate optional dependency. To use a different compatible solver, replace only the private implementation in `src/coalesce/sketch_solver.py` while keeping its coalesce-owned request and result types stable. No other coalesce module imports PlaneGCS.
