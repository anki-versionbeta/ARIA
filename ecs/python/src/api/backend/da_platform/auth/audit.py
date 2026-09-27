"""Authentication audit trail.

Not in the design spec, but section 1's second stated problem is that nobody can
answer "who did what, when". The reference Flask app logged these events and it
costs nothing to keep.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("api.backend.da_platform.audit")


def record(event: str, username: str, detail: str = "") -> None:
    logger.info("audit event=%s user=%s %s", event, username, detail)
