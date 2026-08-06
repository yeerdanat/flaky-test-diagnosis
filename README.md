# Pinpoint — Flaky Test Detective

**Finds flaky tests, isolates *why* they're flaky, and proposes verified fixes.**

## The problem

A flaky test passes sometimes and fails other times on identical code — it isn't
detecting a real bug, it's reacting to hidden nondeterminism. Existing tooling
(CI retry plugins, `pytest-rerunfailures`) tells you **which** test is flaky.
Pinpoint tells you **why**: flakiness has a small number of identifiable causes,
and each one can be isolated by perturbing exactly one environmental variable
and watching whether the failure rate moves.

## Quickstart

```bash
pip install -e .
pinpoint scan path/to/repo --verify
```

Sample diagnosis (from `examples/flaky_suite`):

```
● test_invoice.py::test_default_currency_is_usd
  kind: order-dependent (victim)
  in suite: failed 5/6 rounds, Wilson CI [44%, 97%]
  alone:    failed 0/24 (0%), CI [0%, 14%] -> stable_pass
  cause: test-order dependence (state pollution)
  polluter(s): test_billing.py::test_eur_invoice_formatting (confidence: high, 2 oracle queries, 3 trials)
    test_billing.py::test_eur_invoice_formatting: os.environ['APP_CURRENCY']: None -> 'EUR'
    test_billing.py::test_eur_invoice_formatting: app.state.CURRENCY: 'USD' -> 'EUR'
  proposed fix (balanced tier): Autouse fixture in conftest.py saves/restores ...
  verification: VERIFIED (replay 0/9 failures, regression ok, semantic ok)
```

Not *"test_billing pollutes test_invoice"* — but *"test_billing leaves
`app.state.CURRENCY` set to `'EUR'`"*. A diagnosis someone can act on in
thirty seconds.

## How it works

Debugging is reframed as a controlled experiment:

1. **Detection rounds** — the suite runs several times in shuffled orders
   (iDFlakies-style) so order-dependent failures actually surface.
2. **Isolation baseline** — every suspect runs alone in a fresh process.
   Passes alone consistently → *victim* of test-order pollution. Mixed alone →
   *non-order-dependent* (seed/time/thread). Fails alone → brittle or broken.
   Without this baseline, order dependence and ambient nondeterminism are
   confounded and every later conclusion is wrong.
3. **Bisection** (order-dependent path) — probabilistic delta debugging finds
   the minimal polluting prefix, then state-diff instrumentation names exactly
   what was polluted (module globals, `os.environ`, cwd, RNG state, …).
4. **Screening** (non-order-dependent path) — one dimension is perturbed at a
   time (`PYTHONHASHSEED`, RNG seed) while everything else is pinned; a
   two-proportion test decides whether the failure rate moved, with
   Benjamini–Hochberg FDR control across all screened hypotheses.
5. **Fix synthesis + verification** — risk-tiered patches (emitted as `.diff`
   for human review, never auto-applied), each verified three ways:
   statistical replay of the exact original failing condition, a full-suite
   regression check, and a semantic guard proving the patch didn't weaken any
   test (no assertions deleted, no skip/xfail added).

## The algorithm: ddmin under a noisy oracle

Standard delta debugging assumes a deterministic oracle; a flaky-test oracle
is probabilistic — a prefix that genuinely triggers the bug may still pass on
a given trial. Pinpoint adapts ddmin by making every oracle query a
**sequential probability ratio test** with **asymmetric error thresholds**:

- A false *"this subset doesn't trigger it"* prunes away the true polluter and
  the whole bisection goes wrong → discarding a subset requires strong
  evidence (β = 0.02, ≈6 consecutive clean passes).
- A false *"triggers"* only costs extra trials → accepting is cheap
  (α = 0.10, one observed failure usually suffices).

SPRT is used everywhere a fixed-N design would be wasteful or wrong: detection,
bisection oracle queries, and fix replay. Failure rates are reported as Wilson
score intervals, never raw fractions.

## Cost control

- Oracle queries run `[subset..., victim]`, never the whole suite.
- Oracle results are cached per ordered subset.
- Fresh process per trial is the default (process reuse *causes* the leakage
  being measured).
- `--budget N` caps total trials; on exhaustion Pinpoint reports the smallest
  *confirmed* polluting prefix rather than nothing.
- Cost is reported honestly: trials run, wall-clock spent.

## CLI

```
pinpoint scan [path]
    --rounds N          detection rounds (default 6; round 0 = collection order)
    --budget N          max total trials, partial results on exhaustion (default 400)
    --screen-trials N   trials per perturbation condition (default 12)
    --fix               synthesize candidate patches (.diff files)
    --verify            verify patches (replay + regression + semantic), implies --fix
    --json PATH         machine-readable report (default <path>/.pinpoint/report.json)
    --fail-on-flake     exit 1 if flakes found (CI mode)
```

Run history is stored in SQLite (`.pinpoint/pinpoint.db`).

## Limitations (v1)

- Python/pytest only; the runner adapter is the single framework-specific component.
- Two perturbation dimensions: test ordering and hash/RNG seed. Clock skew,
  thread scheduling, and network isolation are v2.
- Genuine concurrency bugs can be *identified* (unattributed nondeterminism)
  but not root-caused — the interleaving can't be forced from outside the runtime.
- Interacting multi-cause flakes get flagged as jointly implicated rather than
  resolved (single-factor screening finds main effects only).
- Never auto-commits; patches are proposals for human review.

## Design decisions

- **No LLM in the core loop.** Detection, bisection, and verification are
  entirely algorithmic and reproducible.
- **The semantic guard exists before it's needed** — the trivially "correct"
  fix for any flaky test is deleting its assertions, and a tool that can do
  that silently is dangerous.
- **Patches are applied in a disposable copy of the repo**, never the live tree.

## Roadmap

Clock/timezone skew, thread-scheduling jitter, network isolation ·
parallel trial execution · HTML report · brittle/cleaner detection ·
MCP server wrapper · Jest adapter.
