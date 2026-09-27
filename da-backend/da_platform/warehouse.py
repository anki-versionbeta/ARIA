"""Read-only access to the CMC data warehouse (spec section 5, phase 5).

The ATR/MFGR silo builds its reports from Oracle rather than from an uploaded document,
so it needs a connection. It may not open one itself: `tests/test_silo_isolation.py`
fails the build on `os.environ` in a silo file, with the guidance "receive configuration
through the stage context". Credentials therefore live in the platform's `.env` and the
silo receives a connection through `ctx.warehouse`.

Read-only by intent. Nothing here writes, and the queries the silo runs are `SELECT`s
recorded verbatim in its GxP audit trail.

Deliberately thin: no pooling. A report run opens one connection, runs a handful of
queries and closes it, which is what the app being replaced did. Pooling would be a
behaviour change to reach for only if the load test asks for it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

from da_platform.credentials import WarehouseCredentials, warehouse_credentials

logger = logging.getLogger(__name__)

Connector = Callable[[WarehouseCredentials], Any]


def _oracle_connector(credentials: WarehouseCredentials) -> Any:
    """Open a real Oracle connection.

    `oracledb` is imported here rather than at module scope so that importing the
    platform — and every test that never touches a database — does not require the
    driver. It runs in thin mode, so no Oracle Instant Client is needed.
    """
    import oracledb

    # ATR's Q4 selects `scope` and `summary_conclusion`, which are CLOBs. Row shaping
    # happens after the connection closes, and a lazy LOB cannot be read by then, so the
    # driver has to materialise them. Set per connect because it is global driver state
    # and something else may have changed it.
    oracledb.defaults.fetch_lobs = False

    return oracledb.connect(
        user=credentials.user,
        password=REDACTED
        dsn=credentials.dsn,
    )


class Warehouse:
    """A source of read-only warehouse connections, plus the object names to query."""

    def __init__(
        self,
        *,
        credentials: Callable[[], WarehouseCredentials] = warehouse_credentials,
        connector: Connector | None = None,
    ) -> None:
        self._credentials = credentials
        self._connector = connector or _oracle_connector

    # ── the object names the silo builds its SQL from ─────────────────────────
    # Properties rather than a stored config so that reading them resolves the
    # environment but never opens a session.

    @property
    def schema(self) -> str:
        return self._credentials().schema

    @property
    def results_object(self) -> str:
        return self._credentials().results_object

    @property
    def results_view(self) -> str:
        """Fully qualified, e.g. `DEVSCI_DM.mv_combined_results`."""
        return self._credentials().results_view

    # ── connecting ────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def connect(self) -> Iterator[Any]:
        """Yield a connection and always close it.

            with ctx.warehouse.connect() as conn:
                raw = fetch_all(conn, ids["query"])
        """
        credentials = self._credentials()
        connection = self._connector(credentials)
        logger.info("Opened a CMC warehouse connection to %s", credentials.dsn)
        try:
            yield connection
        finally:
            try:
                connection.close()
            except Exception as exc:  # noqa: BLE001 - a failed close must not mask the
                # stage's own outcome, which is what the caller actually cares about.
                logger.warning("Closing the warehouse connection failed: %s", exc)

    def reachable(self) -> bool:
        """Can we open a session right now?

        Answers rather than raises, because the silo's status screen has to render when
        the warehouse is down — and "unreachable" is information, not an error.
        """
        try:
            with self.connect():
                return True
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            logger.info("CMC warehouse is not reachable: %s", exc)
            return False


_warehouse: Warehouse | None = None


def get_warehouse() -> Warehouse:
    """Process-wide instance, so the credential lookup happens in one place."""
    global _warehouse
    if _warehouse is None:
        _warehouse = Warehouse()
    return _warehouse
