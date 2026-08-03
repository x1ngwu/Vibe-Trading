# ADR-QE6: keep vn.py as an isolated CI/diagnostic oracle

- Status: accepted
- Date: 2026-08-03
- Scope: QE6 final architecture boundary

## Decision

vn.py remains an opt-in, fixed-release CI and diagnostic oracle. It is not a
user-visible backtest truth, is not imported into the Vibe process, and is not
registered as an Agent tool, API route, gateway, paper-trading, or live-trading
entry point.

The supported path is:

```text
immutable QE5 BacktestRun + exact snapshot/input lineage
  -> isolated fixed-commit vn.py worker
  -> per-event independent account comparison
  -> content-addressed ReconciliationArtifact
  -> immutable OracleReplayAudit / StrategyValidationDecision
```

The hand-calculated integer-fen accounting fixtures remain the first oracle.
QUANTAXIS remains the product backtest engine. vn.py provides an independent
second implementation and cannot overrule a first divergence by voting or by
similar final returns.

## Release and replay policy

Every vn.py release change must use an isolated environment and record all of
the following before promotion:

1. previous, expected, and actually observed version/commit/source closure;
2. the immutable old BacktestRun identity and its strategy, request, plan,
   snapshot, input, and ledger hashes;
3. a reconciliation artifact, or an explicit `not_available` result when the
   capability was not executed;
4. an `OracleReplayAudit` result of `matched`, `diverged`, `blocked`, or
   `not_available`.

A worker release mismatch and any old-run lineage mismatch are blocked. An
upgrade does not replace the prior environment until historical fixtures match.
A rollback uses the same audit path and exact prior release; it is not a mutable
configuration shortcut. Old BacktestRun records are read-only and are never
rewritten with results from the current worker.

## Queue and cancellation boundary

Oracle jobs reuse the bounded worker/runtime assumptions already proven by
PR-04: explicit queue capacity, backpressure before durable submission,
queued/running cancellation, whole-process-group termination, stable terminal
diagnostics, storage quota/retention, and consistent backup/restore. QE6 does
not add an unbounded alternate scheduler.

## Why no gateway in QE6

Adding a gateway would combine market connectivity, credentials, broker order
state, trading-session recovery, and user authorization with an oracle whose
only accepted purpose is independent replay. That is a different risk and
product surface. The current source and production dependency sets therefore
continue to exclude vn.py, and the QE6 adapter remains unregistered internal
code.

A future gateway requires a separately authorized phase with its own threat
model, broker sandbox, order/position reconciliation, mandate and consent
rules, secret isolation, kill switch, disaster recovery, paper-trading UAT,
and an explicit production go/no-go. This ADR does not pre-approve that work.

## Consequences

- CI may install the audited vn.py wheelhouse in a separate interpreter and run
  the real integration markers.
- Production can store reconciliation/validation evidence but does not need the
  vn.py dependency or expose an engine selector.
- Unsupported scenarios remain `not_available`; skipped tests are never
  reported as passed.
- A first divergence blocks `validated` for that exact strategy version and is
  retained as content-addressed evidence.
