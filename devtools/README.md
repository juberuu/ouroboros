# Ouroboros Devtools

`devtools/` contains operator-side and benchmark support code that should be
versioned with Ouroboros without becoming part of the runtime core.

Rules:

- Generated logs, datasets, run outputs, Docker layers, and secrets do not live
  here.
- Default benchmark outputs go under `/Users/anton/Ouroboros/bench_runs/`.
- Runtime modules must not import `devtools`.
- This is not an immune-system bypass: touched files are reviewed normally.
- Promote code out of `devtools` only through a separate reviewed runtime plan.
