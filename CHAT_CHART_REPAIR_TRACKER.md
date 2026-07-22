# Chat Chart Repair Tracker

> Dedicated tracker for the chat-native K-line/candlestick feature repair.
> This document is the source of truth for scope, priority, progress, evidence,
> and acceptance decisions until the repair is complete.

## Status

- Overall: **All required P0/P1 repair items complete; final real-data smoke tests and review remain**
- Priority order: **P0 blockers first, then P1 correctness/performance**
- Opened: 2026-07-21
- Baseline commit: `d5f8297`
- Repair batch: implementation, tests, and documentation are tracked; 14 local `.orig` patch
  backups remain untracked and are excluded from commits
- Last updated: 2026-07-22

### Status legend

- `[ ]` Not started
- `[-]` In progress
- `[x]` Complete and verified
- `[!]` Blocked; blocker must be recorded in the progress log

## Repair principles

1. Accuracy and bounded resource use take precedence over adding more intervals.
2. Never relabel daily/hourly data as a finer or different interval.
3. Preserve the user's requested range in metadata, even when the retained data is bounded.
4. Keep the latest retained bars contiguous; do not use even-stride sampling for charts.
5. Record the actual provider, adjustment mode, timezone, requested range, and actual range.
6. Add a failing regression test before each behavioral fix where practical.
7. Do not mark an item complete until its acceptance checks and relevant regression tests pass.
8. Keep fixes in small, independently reviewable batches; do not mix unrelated feature expansion into this repair.

## Reference and reuse gate

The project documents already define which repositories may inform this work. They are the
authority for reuse scope:

- [`EXTERNAL_QUANT_PROJECTS.md`](../docs/EXTERNAL_QUANT_PROJECTS.md)
- [`NATURAL_LANGUAGE_QUANT_PLATFORM_DEVELOPMENT_PLAN.md`](../docs/NATURAL_LANGUAGE_QUANT_PLATFORM_DEVELOPMENT_PLAN.md)
- [`QUANT_FRAMEWORK_GLOBAL_AUDIT.md`](../docs/QUANT_FRAMEWORK_GLOBAL_AUDIT.md)
- [`UPSTREAM_ROADMAP_OVERLAP.md`](../docs/UPSTREAM_ROADMAP_OVERLAP.md)

Scope note: the earlier P11 plan limited the first release to daily bars. The user's later
direction expands the active scope to minute bars and multi-year ranges. That newer scope
supersedes the old feature limit. Resource, accuracy, and security gates still apply.

Reuse decision (confirmed by the user on 2026-07-21): this is a private, self-use,
non-commercial project and is not intended for commercialization. Direct code reuse from
the documented reference repositories is therefore allowed for this work when it is the
fastest accurate path. License review is not a repair blocker; provenance is still recorded
so copied or closely ported code remains traceable and maintainable.

Rules before implementation:

1. Reuse the current Vibe code and upstream-compatible abstractions before importing an external pattern.
2. Inspect external projects at the pinned revision below, not at a moving default branch.
3. Treat a README feature claim as orientation only; verify the exact source file and behavior.
4. Direct reuse is allowed. Record the repository, pinned revision, source file, reuse type,
   and local changes when code is copied or closely ported.
5. `tickflow-stock-panel` may be reused directly for this private non-commercial project;
   note its MIT/non-commercial wording in provenance without blocking the repair.
6. Do not add a runtime dependency or transplant a subsystem merely to obtain one small pattern.
7. Independently verify every adopted pattern against this project's tests, schema, security boundary,
   provider behavior, and golden fixtures.

### Pinned reference set

| Project | Pinned revision | Permitted role in this repair |
| --- | --- | --- |
| Current Vibe/upstream | Local baseline `d5f8297` plus documented upstream PRs/issues | Direct reuse and local repair |
| `shy3130/tickflow-stock-panel` | `0cb0b38437d813edeb0964e0eba1913245faccb6` | K-line, indicators, loading/sync and workbench UX reference; direct reuse allowed with provenance |
| `ZhuLinsen/daily_stock_analysis` | `d13721e8174c487763a1ca3b63fff611f8356026` | Narrow task/history/report recovery and multi-source diagnostic patterns |
| AlphaSift | `9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf` | Provider health, fallback, stale-data and source-error semantics |
| `yutiansut/QUANTAXIS` | `a69e978a2e38d045a64c380cc3b5c9fa08fa4903` | Market-data/account semantic reference; not a chat-UI dependency |
| `vnpy/vnpy` | `1b78494979deb4c4996f6b864f234d9839f2f239` | Event/order lifecycle semantic reference; not a P0/P1 dependency |

AlphaEvo remains relevant to later strategy-DSL work, but not to the current chart repair.

### Repair-to-reference map

| Repair item | First source to reuse | External behavior to study | Boundary |
| --- | --- | --- | --- |
| P0-01 bounded intraday fetch | Existing loaders, cache and interval utilities | TickFlow data-flow and sync-state behavior | Keep provider limits local; do not transplant its pipeline |
| P0-02 interval contract | Current tool schema, prompt and upstream resampling work | None required | Fix the contract locally; do not improvise `4H` |
| P0-03 tooltip/API security | Current API validation and chart component | None required | Security behavior must be locally specified and tested |
| P0-04 chart isolation | Existing `CandlestickChart` and Run Detail integration | TickFlow interaction behavior; upstream stale-response issue `#589` | Preserve Run Detail compatibility |
| P1-01 provider fallback | Current loader interfaces; upstream issue `#681` | AlphaSift health/fallback/stale semantics; DSA source diagnostics | Never disguise interval/source degradation |
| P1-02 run write boundary | Current session, attempt and path-security code | None required | Do not borrow filesystem behavior from external projects |
| P1-03 index routing | Current symbol routing and providers | None required | Support only identifiers verified by the actual provider |
| P1-04 OHLCV validation | Existing local validation utilities | QUANTAXIS/vn.py market-data semantics | Use as a semantic oracle, not as a dependency |
| P1-05 lazy load/cache | Existing Run Detail K-line and message/SSE flow | DSA history/recovery; TickFlow loading/sync UX; upstream issue `#589` | One local request lifecycle and cache contract |
| P1-06/P1-07 defaults/colors | Existing chart props and chart theme | None required | Preserve existing consumers and locale behavior |

For any code or close structural port, add a provenance entry before marking the repair complete:

| Repair item | Repository + commit | Source file(s) | License note | Reuse type | Local changes | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| _Not yet applicable_ |  |  |  |  |  |  |

## Baseline evidence

- [x] Frontend focused tests: 16 passed.
- [x] Frontend full suite: 30 test files, 252 tests passed.
- [x] Frontend production build passed.
- [x] `git diff --check` passed.
- [x] Changed/new Python files passed syntax compilation.
- [x] Runtime registry exposed `show_price_chart` with intervals `1m/5m/15m/30m/1H/1D`.
- [x] Visualization API smoke test returned a sanitized payload with HTTP 200.
- [x] Chart artifact creation smoke test succeeded in a temporary run directory.
- [x] Focused backend chart/API/loop/market-data/Yahoo tests passed: 119 tests.
- [x] The broad non-live backend suite passed 5,297 tests and skipped 9 live/environment-gated
  tests when run with the documented explicit Tushare placeholder.

## P0 — release blockers

### P0-01 Bound intraday fetches before materialization

Status: `[x]` — implemented and focused backend pytest verified

Problem:

- The chart tool disables the shared row cap, fetches and materializes the full provider result,
  then sorts and truncates to 5,000 bars.
- A multi-year minute request across several symbols can consume excessive memory, CPU, network,
  and wall-clock time even though only the latest 5,000 bars are persisted.

Planned repair:

- Separate `requested_start` from the effective provider `fetch_start`.
- Introduce a provider/market/interval-aware fetch budget with a hard target of at most 5,000
  contiguous retained bars per symbol.
- Stop paginated providers once the budget is satisfied.
- Preserve the requested range and expose the effective fetch range and retention policy.
- Keep the final defensive 5,000-bar slice.

Acceptance:

- [x] A years-long `1m` request does not make any loader materialize an unbounded history.
- [x] Returned bars are the latest contiguous bars, not sampled bars.
- [x] Five-symbol requests remain bounded to about 25,000 retained bars total.
- [x] Tool output clearly reports truncation and effective range.
- [x] Added tests cover historical/current ranges, crypto 24/7 data, and exchange sessions.

### P0-02 Make the interval contract consistent

Status: `[x]` — implemented and focused backend pytest verified

Problem:

- The system prompt advertises `4H`, while the tool schema and implementation reject it.

Decision:

- Remove `4H` from the system prompt for this repair.
- Do not add an improvised `1H -> 4H` resampler in this batch.
- Native/provider-specific `4H` support may be designed separately after this repair.

Acceptance:

- [x] System prompt, JSON schema, runtime normalization, and descriptions list the same intervals.
- [x] A regression test compares the advertised intervals with the tool enum.
- [x] Unsupported-interval regression test passes.

### P0-03 Eliminate tooltip HTML injection

Status: `[x]` — frontend and focused API regressions verified

Problem:

- ECharts HTML tooltips interpolate unescaped timestamps, series names, and marker text.
- The visualization API currently accepts arbitrary strings for bar times.

Planned repair:

- Validate bar time strings and OHLCV semantics at the API boundary.
- Escape every dynamic string inserted into an HTML tooltip, including marker/reason text.
- Prefer non-HTML/rich-text rendering where it does not regress usability.

Acceptance:

- [x] Series-name, marker, and malicious API timestamp fixtures pass.
- [x] Normal tooltips retain OHLC, change percentage, volume, and indicator values.
- [x] API returns HTTP 422 for invalid time/OHLCV/order.

### P0-04 Isolate chat chart interactions

Status: `[x]`

Problem:

- Every candlestick instance joins the same global ECharts group.
- Zoom/crosshair actions can propagate between unrelated chat messages, symbols, and inline/expanded charts.

Planned repair:

- Add explicit chart-linking configuration to `CandlestickChart`.
- Keep existing Run Detail behavior unchanged.
- Disable global linking for chat charts or assign an isolated group per visualization.
- Render only one active ECharts instance while a chart is expanded.

Acceptance:

- [x] Zooming one chat chart does not change another chart.
- [x] Expanding a chart does not leave a second active chart instance behind.
- [x] Existing Run Detail chart behavior remains unchanged.
- [x] Component tests cover at least two simultaneous chat visualizations.

## P1 — correctness and integrity

### P1-01 Implement real capability-aware provider fallback

Status: `[x]` — implemented and verified

Planned repair:

- Define a market × interval × provider capability matrix.
- Try only providers that genuinely support the requested interval and symbol class.
- Track the actual provider per symbol.
- Never silently fall back to an incompatible interval or adjustment mode.

Implementation evidence:

- Added a verified market × provider × interval capability matrix for A-share, US, HK,
  India, and crypto chart sources.
- Automatic requests try providers per symbol in fallback order; failed or empty providers
  fall through only to sources that support the exact requested interval.
- Daily-only providers are skipped for intraday requests and reported as
  `unsupported_interval`; provider resolution/fetch failures and empty results are reported as
  `unavailable` or `no_data`.
- The successful provider is persisted as the chart source, and `source_attempts` exposes the
  complete per-symbol diagnostic path without changing successful partial-result behavior.

Acceptance:

- [x] A failed primary provider falls through to a compatible secondary provider.
- [x] An incompatible fallback is skipped and reported.
- [x] Partial multi-symbol success returns the successful charts plus explicit unresolved symbols.

### P1-02 Restrict writes to the current attempt run

Status: `[x]` — implementation, focused tests, and cross-run verification complete

Planned repair:

- Prevent model-supplied arguments from overriding the current attempt's injected `run_dir`.
- Add a reusable tool capability/property rather than relying on a fragile tool-name special case.

Implementation evidence:

- Added `BaseTool.requires_current_run_dir`, enabled it for `PriceChartTool`, and applied it in
  both parallel and sequential `AgentLoop` tool execution paths.
- A model-supplied `run_dir` is replaced by the active attempt directory; without an active
  attempt directory the argument is removed so the tool fails closed.
- Visualization and manifest paths now use containment-aware `safe_path` resolution, including
  protection against symlink escapes.
- Unit regressions cover forced current-run injection, missing-current-run failure, the default
  capability value, and a symlinked artifact escape. The focused backend tests pass.
- An AgentLoop-to-PriceChartTool regression supplies another run's absolute path, preserves that
  run's sentinel manifest byte-for-byte, and verifies artifacts are written only to the active run.

Acceptance:

- [x] A supplied absolute path to another run is overwritten or rejected.
- [x] The tool can write only beneath the current run's visualization artifact directory.
- [x] Cross-run overwrite regression test passes.

### P1-03 Support the index symbols that are advertised

Status: `[x]` — implemented and focused backend pytest verified for the supported Yahoo identifiers

Planned repair:

- Route common Yahoo index identifiers such as `^GSPC`, `^IXIC`, and `^DJI`.
- Align the tool description with the symbol formats actually supported.

Implementation evidence:

- Added verified support for `^GSPC`, `^IXIC`, and `^DJI` in source detection, Yahoo loader
  gating, market classification, and the chart-tool description.
- The original Yahoo identifier/display symbol is preserved through fetch and chart creation.
- Other caret-prefixed index identifiers are rejected with a supported-symbol error instead of
  being routed to an unrelated provider.
- Regression tests pass for routing, loader acceptance, identifier preservation, and
  unsupported-index errors.

Acceptance:

- [x] Common US index symbols route to Yahoo and preserve the original display symbol.
- [x] A-share index codes continue to route correctly.
- [x] Unsupported index formats produce a useful error instead of a misleading source choice.

### P1-04 Validate OHLCV semantics and time ordering

Status: `[x]` — implemented with a reported drop policy and focused backend pytest verified

Implemented policy:

- Invalid and duplicate provider rows are dropped before persistence rather than rejecting the
  whole symbol. Duplicate normalized timestamps keep the last provider row.
- The retained rows are normalized and sorted into strict ascending order.
- `dropped_bar_count` is persisted in the visualization payload and manifest, sanitized by the
  API/session boundaries, and shown in the chat chart metadata when non-zero.
- The focused normalization regression test passes.

Acceptance:

- [x] Prices are finite and strictly positive.
- [x] `high` brackets open/close/low and `low` brackets open/close/high.
- [x] Volume is finite and non-negative.
- [x] Timestamps are parseable, normalized, de-duplicated, and strictly ascending.
- [x] Invalid rows follow one documented reject/drop policy and are reported.

## P1 — frontend performance and compatibility

### P1-05 Lazy-load and cache historical visualizations

Status: `[x]` — implemented and verified

Planned repair:

- Fetch visualization payloads only when their panels approach the viewport.
- Cache by `runId + visualizationId`.
- Cancel in-flight requests on unmount/session change.
- Reuse cached data when switching symbol tabs or reopening an expanded chart.

Implementation evidence:

- `IntersectionObserver` starts chart requests only when a panel approaches the viewport, with a
  400px prefetch margin and a safe immediate-load fallback when the API is unavailable.
- A bounded 100-entry cache is keyed by `runId + visualizationId`, reuses completed responses,
  and shares in-flight requests with subscriber reference counting.
- The API accepts an `AbortSignal`; unmount/session changes release subscribers and abort an
  orphaned request, while stale promises cannot update the replacement component.
- Retry explicitly invalidates failed/cached state and starts a fresh request.

Acceptance:

- [x] Loading a long session does not immediately fetch every historical chart.
- [x] Returning to a previously loaded chart causes no duplicate network request.
- [x] Session switches do not update an unmounted chart.

### P1-06 Preserve existing Run Detail defaults

Status: `[x]` — implemented and full frontend verification passed

Planned repair:

- Restore the reusable chart's default range to `ALL`.
- Add an explicit initial-range prop.
- Use `1Y` daily and `5D` intraday defaults only for chat visualizations.

Implementation evidence:

- The reusable chart resolves an omitted or invalid initial range to `ALL` and accepts a valid
  explicit `initialRange` without changing existing Run Detail callers.
- Chat visualizations explicitly request `1Y` for daily data and `5D` for intraday data.
- A component-level ECharts regression test records a user zoom, toggles an overlay indicator,
  and verifies the next chart option preserves the selected start and end.

Acceptance:

- [x] Existing Run Detail charts initially show their full range.
- [x] Chat daily and intraday charts use their intended defaults.
- [x] Changing indicators does not reset a user-selected zoom.

### P1-07 Use locale-aware gain/loss colors

Status: `[x]` — implemented and full frontend verification passed

Planned repair:

- Reuse the existing chart theme rather than hardcoding red-up/green-down classes.

Implementation evidence:

- The chat chart header now uses the same locale-aware `upColor` and `downColor` values as the
  ECharts candlesticks, trade markers, MACD bars, and volume series.
- Regression tests verify that Chinese swaps the international green-up/red-down convention to
  red-up/green-down, including the matching translucent volume colors.
- Component tests verify the header, candle body/border, and per-bar volume colors resolve from
  the same locale theme.

Acceptance:

- [x] Chinese UI uses red-up/green-down consistently.
- [x] Other locales use green-up/red-down consistently.
- [x] Header change color, candles, and volume colors agree.

## Deferred low-priority work

- [ ] Complete dialog focus trapping, focus restoration, and background scroll locking.
- [ ] Add accessible chart fallback/summary content and pressed/tab semantics.
- [ ] Add real locale entries for all visualization UI text.
- [ ] Replace fixed 1,825-day wording with calendar-aware five-year calculation if needed.

These items remain visible but must not delay P0/P1 correctness work unless a change in the
same component makes them effectively free to include.

## Validation matrix

### Backend

- [x] Focused chart tool tests.
- [x] Visualization API validation/security tests.
- [x] SessionService persistence and historical replay tests.
- [x] AgentLoop current-run injection test.
- [x] Provider fallback and interval capability tests.
- [x] Runtime registry smoke test.
- [ ] Real-data smoke tests for A-share daily/minute, US daily/minute, index, and crypto.

### Frontend

- [x] Tooltip injection regression tests.
- [x] Independent multi-chart isolation tests.
- [x] Failure/retry and request-cancellation tests.
- [x] Multi-symbol tab caching tests.
- [x] Historical message restoration tests.
- [x] Full Vitest suite.
- [x] TypeScript production build.

## Intended repair batches

1. P0 backend resource bound and interval contract.
2. P0 frontend tooltip security and chart isolation.
3. P1 backend fallback, run boundary, index routing, and semantic validation.
4. P1 frontend lazy loading, caching, default-range compatibility, and locale colors.
5. Full regression, real-data smoke tests, documentation cleanup, and final review.

Each batch must be reviewable and reversible without depending on unfinished later batches.

## Progress log

### 2026-07-21

- Completed a read-only review of the current uncommitted implementation.
- Confirmed four P0 issues and the P1 issues listed above.
- Verified frontend tests/build, runtime tool registration, API serving, and temporary artifact creation.
- Recorded that backend pytest is not currently available in the execution environment.
- Opened this tracker. No repair item has been marked started or complete.
- Reconciled the repair plan with the project's external-project audit and pinned the permitted
  reference/reuse boundaries above. Business-code repair remains not started.
- User confirmed this is a permanently private, non-commercial project. License review was
  changed from a repair gate to a provenance note, and direct reuse from the documented pinned
  repositories was authorized when it improves delivery speed or accuracy.
- Implemented P0-01 provider/market/interval-aware intraday fetch windows before loader calls,
  preserved requested/effective/actual ranges, and kept the latest contiguous 5,000-bar cap.
- Implemented P0-02 by removing unsupported `4H` from the chat-chart prompt and adding a
  prompt/schema contract regression test.
- Implemented P0-03 with escaped ECharts rich-text tooltips plus API-side ISO time, strict ordering,
  finite/positive OHLC, bracket, and non-negative volume validation.
- Completed P0-04: chat charts opt out of the global group, Run Detail retains the default group,
  expanded charts replace rather than duplicate the inline ECharts instance.
- Verification: P0 frontend focused tests 8/8, full Vitest 30 files/255 tests, production build,
  Python syntax checks, `git diff --check`, and isolated P0 backend behavior smoke passed.
- Backend pytest still cannot run because the host and current production image do not contain
  the project runtime/test dependencies. P0-01/P0-02/P0-03 remain in progress until those tests run.
- Reconciled the tracker after the interrupted P1 work. P1 is no longer "not started": P1-02,
  P1-03, and P1-04 have implementations and regression tests in the working tree.
- P1-02 now forces the active attempt directory for scoped tools and contains visualization paths;
  formal pytest and an end-to-end cross-run overwrite check remain pending.
- P1-03 now routes and preserves `^GSPC`, `^IXIC`, and `^DJI`, and rejects unsupported caret-prefixed
  indices with an explicit supported-symbol error.
- P1-04 now drops invalid/duplicate rows, normalizes and sorts timestamps, and reports the dropped
  count through persistence, API sanitization, types, and chat metadata.
- Refreshed verification after the P1 changes: full Vitest passed 30 files/255 tests, the production
  frontend build passed, changed Python files passed syntax compilation, and `git diff --check` passed.
- Backend pytest remains blocked with `/usr/bin/python3: No module named pytest`; the new P1 backend
  regressions therefore remain unexecuted.
- Worktree hygiene remains open: 14 untracked `.orig` patch backups still need removal.

### 2026-07-22

- Installed the host's `python3.12-venv` support and created a repository-local, ignored `.venv`.
- Installed the project in editable mode with its declared `.[dev]` dependencies, including
  pytest 9.1.1, pytest-cov 7.1.0, and pytest-socket 0.8.0.
- Focused chart/API/loop/market-data/Yahoo verification passed: 119 tests.
- The final broad non-live backend run passed 5,297 tests and skipped 9 live/environment-gated
  tests. Frontend Vitest passed 30 files/255 tests, and the production build passed.
- Completed P1-01 capability-aware provider fallback with exact-interval filtering, per-symbol
  retry, actual-source persistence, and structured source-attempt diagnostics.
- P1-01 focused chart/API/loop/market-data/Yahoo verification passed 122 tests; the updated broad
  non-live backend suite passed 5,300 tests and skipped 9 live/environment-gated tests.
- Closed P1-02 with a real AgentLoop-to-PriceChartTool cross-run overwrite regression; the
  malicious target run remains untouched and the active run receives the visualization.
- Completed P1-05 with viewport-triggered loading, a bounded run/visualization cache, shared
  in-flight requests, AbortSignal cancellation, explicit retry invalidation, and run-scoped keys.
- P1-05 focused frontend verification passed 7 tests; full Vitest passed 30 files/260 tests,
  and the production TypeScript/Vite build passed.
- Completed P1-06 by restoring the reusable K-line default to `ALL`, adding a validated explicit
  initial-range prop, and limiting the daily `1Y` / intraday `5D` defaults to chat charts.
- P1-06 focused frontend verification passed 3 files/14 tests; full Vitest passed 31 files/263
  tests, and the production TypeScript/Vite build passed.
- Completed P1-07 by replacing the chat header's hardcoded red/green classes with the shared
  locale-aware chart theme used by candlesticks and volume.
- P1-07 focused frontend verification passed 3 files/12 tests; full Vitest passed 32 files/266
  tests, and the production TypeScript/Vite build passed.

## Completion record

Fill this section only after all required P0/P1 items are complete.

- Completion date:
- Final commit(s):
- Tests executed:
- Known residual risks:
- Reviewer decision:
