"""ATR constants and the request-id normaliser. Ported from `src/config.py:1-95`.

Only the identifier contract lands here for now — the ATR unit maps, `ATTACHMENT_MARKER`
and the query-pack constants arrive with the ATR pipeline.

Two deliberate differences from the source, both forced by the platform:

* `DWH_SCHEMA` and `RESULTS_OBJECT` are **gone**. The source read them from
  `CMCDW_SCHEMA` / `CMCDW_RESULTS_OBJECT`, and a silo may not read the environment
  (`tests/test_silo_isolation.py`). They now arrive as `ctx.warehouse.schema` and
  `.results_object`, which means the SQL is assembled at call time rather than at import
  time. The SQL text is unchanged.
* `REQUEST_ID_QUERY_PREFIX` was read from a `CMC_REQUEST_PREFIX` variable, defaulting to
  "PEGA-PROD-CMC-", and is now a plain constant at that default. **This drops the
  environment override.** If anyone actually sets `CMC_REQUEST_PREFIX` in a deployed
  environment, it needs a home in the warehouse config instead — worth asking before
  release rather than discovering it when a query silently returns nothing.
"""

from __future__ import annotations

import re

# Request-id forms:  input "10352"/"CMC-10352"  → query "PEGA-PROD-CMC-10352"  → display "CMC-10352"
REQUEST_ID_QUERY_PREFIX = "PEGA-PROD-CMC-"
_DIGITS_RE = re.compile(r"(\d{3,})")

# 3rd-party (files ending _A01) results are out of scope → shown as this marker.
ATTACHMENT_MARKER = "Attachment A01"


def normalize_request_id(raw: str) -> dict[str, str]:
    """Normalize any accepted request-id form to its three representations.

    >>> normalize_request_id("CMC-10352")
    {'short': '10352', 'display': 'CMC-10352', 'query': 'PEGA-PROD-CMC-10352'}
    """
    if raw is None:
        raise ValueError("request id is required")
    m = _DIGITS_RE.search(str(raw))
    if not m:
        raise ValueError(f"could not find a numeric CMC request id in {raw!r}")
    short = m.group(1)
    return {
        "short": short,
        "display": f"CMC-{short}",
        "query": f"{REQUEST_ID_QUERY_PREFIX}{short}",
    }
