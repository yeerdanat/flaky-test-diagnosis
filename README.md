# Flaky Test Diagnosis

**Finds flaky tests, works out why they're flaky, and proposes fixes it has verified.**

## The problem

A flaky test passes on some runs and fails on others with the same code. The
failure comes from hidden nondeterminism in the environment, so the code under
test is usually fine.

CI retry plugins and `pytest-rerunfailures` tell you which test is flaky. This
tool tells you why. Flakiness has a small number of identifiable causes, and
each one can be isolated by changing exactly one environmental variable and
watching whether the failure rate moves.

## Quickstart

```bash
pip install -e .
whyflaky scan path/to/repo --verify
```

Sample diagnosis, from `examples/flaky_suite`:

```
● test_invoice.py::test_default_currency_is_usd
  kind: order-dependent (victim)
  in suite: failed 5/6 rounds, Wilson CI [44%, 97%]
  alone:    failed 0/24 (0%), CI [0%, 14%] -> stable_pass
  cause: test-order dependence (state pollution)
  polluter(s): test_billing.py::test_eur_invoice_formatting (confidence: high, 2 oracle queries, 3 trials)
    polluted state: os.environ['APP_CURRENCY']
    polluted state: app.state.CURRENCY
    test_billing.py::test_eur_invoice_formatting: os.environ['APP_CURRENCY']: None -> 'EUR'
    test_billing.py::test_eur_invoice_formatting: app.state.CURRENCY: 'USD' -> 'EUR'
  proposed fix (balanced tier): Autouse fixture in conftest.py saves/restores ...
  verification: VERIFIED (replay 0/9 failures, regression ok, semantic ok)
```

The report names the polluted state, so the diagnosis reads as "test_billing
leaves `app.state.CURRENCY` set to `'EUR'`". That is actionable in thirty
seconds.

## How it works

Debugging is run as a controlled experiment.

1. **Detection rounds.** The suite runs several times in shuffled orders
   (iDFlakies-style) so order-dependent failures actually surface.
2. **Isolation baseline.** Every suspect runs alone in a fresh process.
   Consistent passes mean a *victim* of test-order pollution, mixed results mean
   *non-order-dependent* flakiness (seed, time, thread), and consistent failures
   mean a brittle or broken test. Without this baseline, order dependence and
   ambient nondeterminism are confounded and every later conclusion is wrong.
3. **Bisection**, on the order-dependent path. Probabilistic delta debugging
   finds the minimal polluting prefix. State-diff instrumentation then names
   what was polluted: module globals, `os.environ`, cwd, RNG state, and so on.
4. **Screening**, on the non-order-dependent path. One dimension is perturbed
   at a time (`PYTHONHASHSEED`, RNG seed) while everything else is pinned. A
   two-proportion test decides whether the failure rate moved, with
   Benjamini-Hochberg FDR control across all screened hypotheses.
5. **Fix synthesis and verification.** Patches are risk-tiered and emitted as
   `.diff` files for human review. Each one is verified three ways: statistical
   replay of the exact original failing condition, a full-suite regression
   check, and a semantic guard proving the patch didn't weaken any test (no
   assertions deleted, no skip or xfail added).

## The algorithm: ddmin under a noisy oracle

Standard delta debugging assumes a deterministic oracle. A flaky-test oracle is
probabilistic, so a prefix that genuinely triggers the bug can still pass on any
given trial. The bisector handles this by making every oracle query a
**sequential probability ratio test** with **asymmetric error thresholds**:

- If a query wrongly reports that a subset does *not* trigger the failure, the
  real polluter gets pruned away and the rest of the bisection is wrong.
  Discarding a subset therefore requires strong evidence (β = 0.02, about six
  consecutive clean passes).
- If a query wrongly reports that a subset *does* trigger it, the only cost is
  extra trials. Accepting is therefore cheap (α = 0.10, one observed failure is
  usually enough).

SPRT is used everywhere a fixed-N design would be wasteful or misleading:
detection, bisection oracle queries, and fix replay. Failure rates are reported
as Wilson score intervals, because 1/5 and 20/100 are both "20%" with very
different confidence.

## Cost control

- Oracle queries run `[subset..., victim]`, never the whole suite.
- Oracle results are cached per ordered subset.
- Fresh process per trial is the default. Reusing a process introduces the
  state leakage that is being measured.
- `--budget N` caps total trials. On exhaustion it still reports the smallest
  *confirmed* polluting prefix.
- Cost is reported honestly: trials run and wall-clock spent.

## Benchmarks

Measured on 75 generated pytest suites (15 scenario types, 5 generation
seeds) with known injected flakes: order-dependent polluters of module
globals, environment variables, and cwd; hash-seed and unseeded-RNG flakes
at designed failure rates of 20 to 60 percent; an always-failing control and
a fully stable control. Ground truth is recorded at generation time, so every
diagnosis is scored against what was actually injected.

| metric | result |
|---|---|
| Detection precision | 100% (55 flakes reported, 0 false positives) |
| Detection recall | 92% (55/60; all 5 misses are 20 to 40 percent flakes that never failed in 6 detection rounds) |
| Kind classification (OD / NOD / broken) | 98% (59/60) |
| Cause classification | 97% (58/60) |
| Polluter localization, rank-1 | 100% (35/35 order-dependent cases) |
| Proposed fixes passing 3-stage verification | 86% (37/43) |
| Total cost | 2825 trials, 386 s wall |

Trial cost against a fixed-repetition baseline (50 reruns per query,
pytest-flakefinder's default), same scenarios, same conclusions required:

| suite | SPRT trials | fixed-50 trials | saving |
|---|---|---|---|
| 10-test OD scenario | 36 | 282 | 7.8x |
| 30-test OD scenario | 37 to 42 | 332 | 8.0 to 9.0x |
| 80-test OD scenario | 39 | 432 | 11.1x |
| all 15 scenarios | 527 | 2591 | 4.9x |

The two arms reached identical conclusions on 14 of 15 scenarios. The
exception is a 20-percent-rate flake that surfaced in one arm's detection
rounds and not the other's; at that rate a 6-round scan has roughly a 74%
chance of surfacing the flake at all, which is a detection-round limit, and
`--rounds` raises it.

Hardware for wall-clock numbers: Apple M3 Pro, Python 3.13. Trial counts are
hardware-independent. Reproduce with:

```
python -m benchmark.run --seed 1
python -m benchmark.run --baseline --fixed-n 50
```

## CLI

```
whyflaky scan [path]
    --rounds N          detection rounds (default 6; round 0 = collection order)
    --budget N          max total trials, partial results on exhaustion (default 400)
    --screen-trials N   trials per perturbation condition (default 12)
    --fix               synthesize candidate patches (.diff files)
    --verify            verify patches (replay + regression + semantic), implies --fix
    --json PATH         machine-readable report (default <path>/.whyflaky/report.json)
    --fail-on-flake     exit 1 if flakes found (CI mode)
```

Run history is stored in SQLite, at `.whyflaky/whyflaky.db`.
