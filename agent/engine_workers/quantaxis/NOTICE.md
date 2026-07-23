# QUANTAXIS worker provenance

- Upstream: https://github.com/yutiansut/QUANTAXIS
- Fixed commit: `a69e978a2e38d045a64c380cc3b5c9fa08fa4903`
- Package version at that commit: `2.1.0.alpha2`
- License: MIT
- Copyright notice: Copyright (c) 2016-2021 yutiansut/QUANTAXIS
- License source at the fixed commit:
  https://github.com/yutiansut/QUANTAXIS/blob/a69e978a2e38d045a64c380cc3b5c9fa08fa4903/LICENSE

No QUANTAXIS source is copied into Vibe. The QE0 worker imports the separately
installed package and invokes public or named package functions against
synthetic data. The upstream package and all transitive dependencies remain in
an isolated Python environment.

The exact upstream package currently imports database/Web modules at top level
and performs a MongoDB operation during import. This is recorded as a blocker
in the QE0 ADR; no database service or production deployment was added to work
around it.
