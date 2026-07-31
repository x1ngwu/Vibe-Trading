# QE5 BaoStock transitional materializer

This operator tool turns a small, explicitly reviewed A-share universe into an
immutable QE5 daily-market snapshot:

```text
BaoStock adapter
  -> vibe.qe5-market-capture.v1
  -> vibe.quantaxis-backtest-snapshot.v1
  -> ResearchSpec + DataSnapshotRef
  -> confirmed StrategySpec / BacktestRun
```

BaoStock is only an acquisition adapter. A future database reader should
produce the same `Qe5MarketCapture` contract; it must not bypass the canonical
snapshot, point-in-time validation, content hashes, or research lineage.

## Scope and limits

- Daily bars only; this is not a minute-data store.
- At most 12 explicitly listed Shanghai/Shenzhen main-board stocks.
- The requested window must start on or after 2023-08-28 and contain no more
  than 2,500 open days or 30,000 bars.
- Raw prices drive execution and accounting; qfq close drives signals.
- Trading status, ST status, the exchange calendar, raw preclose and explicit
  corporate actions are captured.
- BaoStock exposes current consolidated history rather than a revision archive.
  The snapshot records this limitation and must not be described as
  vendor-grade point-in-time revision history.
- Cash dividends use the provider's pre-tax amount. Differentiated
  holding-period tax is not modeled. Sub-fen per-share rates are retained as an
  exact rational number, while the total entitlement is explicitly rounded
  half-up to one fen. This is a transitional model policy and is recorded in
  snapshot anomalies.

## Usage

Review and copy `qe5-baostock-universe.example.json`, keeping every board
classification explicit. Then run:

```bash
python agent/scripts/qe5_materialize_baostock.py \
  --universe-manifest agent/scripts/qe5-baostock-universe.example.json \
  --start-date 2024-01-02 \
  --end-date 2025-12-31 \
  --as-of 2025-12-31 \
  --output-dir /absolute/new/audit-bundle
```

To additionally publish the content-addressed snapshot and research objects to
one Vibe home, add:

```bash
  --publish-root /absolute/vibe-home
```

The command refuses to overwrite an audit bundle. Standard output contains one
canonical JSON manifest; BaoStock login diagnostics and errors go to standard
error. The audit directory and files are created with modes `0700` and `0600`.

The bundle contains:

- `capture.json`: provider-neutral source capture and limitations;
- `snapshot.json`: exact worker input;
- `research-spec.json` and `data-snapshot-ref.json`: immutable lineage;
- `manifest.json`: content hashes and object IDs.

Do not hand-edit or replace a published snapshot. Recapture to a new audit
directory and let content addressing produce a new identity.
