# vn.py worker provenance

- Upstream: https://github.com/vnpy/vnpy
- Fixed commit: `1b78494979deb4c4996f6b864f234d9839f2f239`
- Package version at that commit: `4.4.0`
- License: MIT
- Copyright notice: Copyright (c) 2015-present, Xiaoyou Chen
- License source at the fixed commit:
  https://github.com/vnpy/vnpy/blob/1b78494979deb4c4996f6b864f234d9839f2f239/LICENSE

No vn.py source is copied into Vibe. The QE0 worker imports the separately
installed package and exercises EventEngine plus order/trade data objects with
synthetic events. The upstream package and all transitive dependencies remain
in an isolated Python environment. vn.py is a CI/diagnostic oracle candidate,
not a production image dependency in QE0.
