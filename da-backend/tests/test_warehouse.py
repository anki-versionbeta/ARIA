"""The CMC warehouse layer (spec phase 5).

Offline: the Oracle driver is never called. `oracledb` is installed, but a unit test that
needed a reachable database would be a test of the network rather than of this code, so
the connector is injected and the one genuinely live check is skipped unless
`CMCDW_DSN` is set.

This layer exists because `tests/test_silo_isolation.py` forbids a silo from reading
`os.environ`. Credentials live in the platform's `.env`; the silo receives a connection.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from da_platform.credentials import MissingCredential, warehouse_credentials
from da_platform.warehouse import Warehouse


@pytest.fixture
def env(monkeypatch):
    """A complete set of warehouse variables, as the platform .env would supply."""
    for key, value in {
        "CMCDW_USER": "svc_reader",
        "CMCDW_PASSWORD": "secret",
        "CMCDW_DSN": "uq00604p:1521/DSDMUNP1",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("CMCDW_SCHEMA", "CMCDW_RESULTS_OBJECT"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.queries: list[str] = []

    def close(self):
        self.closed = True


def fake_connector(recorder: list):
    def connect(config):
        recorder.append(config)
        return FakeConnection()

    return connect


# ── credentials ───────────────────────────────────────────────────────────────


def test_credentials_come_from_the_environment(env):
    creds = warehouse_credentials()

    assert creds.user == "svc_reader"
    assert creds.password == "secret"
    assert creds.dsn == "uq00604p:1521/DSDMUNP1"


def test_the_database_object_names_have_the_source_defaults(env):
    """`DEVSCI_DM.mv_combined_results` is what the app being replaced queries."""
    creds = warehouse_credentials()

    assert creds.schema == "DEVSCI_DM"
    assert creds.results_object == "mv_combined_results"
    assert creds.results_view == "DEVSCI_DM.mv_combined_results"


def test_the_object_names_can_be_overridden(env):
    env.setenv("CMCDW_SCHEMA", "PROD_DM")
    env.setenv("CMCDW_RESULTS_OBJECT", "combined_results_vw")

    creds = warehouse_credentials()

    assert creds.results_view == "PROD_DM.combined_results_vw"


@pytest.mark.parametrize("missing", ["CMCDW_USER", "CMCDW_PASSWORD", "CMCDW_DSN"])
def test_a_missing_credential_names_itself(env, missing):
    """The original raised a RuntimeError naming the variable. Keeping that is the
    difference between a five-second fix and a confusing 500."""
    env.delenv(missing)

    with pytest.raises(MissingCredential, match=missing):
        warehouse_credentials()


def test_credentials_are_not_repr_leaked(env):
    """A connection error that dumps the config must not print the password."""
    assert "secret" not in repr(warehouse_credentials())


# ── connecting ────────────────────────────────────────────────────────────────


def test_connect_yields_a_connection_and_always_closes_it(env):
    seen: list = []
    warehouse = Warehouse(connector=fake_connector(seen))

    with warehouse.connect() as conn:
        assert isinstance(conn, FakeConnection)
        held = conn

    assert held.closed, "the connection must be closed on the way out"
    assert seen and seen[0].dsn == "uq00604p:1521/DSDMUNP1"


def test_connect_closes_the_connection_even_when_the_caller_raises(env):
    seen: list = []
    warehouse = Warehouse(connector=fake_connector(seen))

    held = None
    with pytest.raises(RuntimeError, match="stage blew up"):
        with warehouse.connect() as conn:
            held = conn
            raise RuntimeError("stage blew up")

    assert held is not None and held.closed


def test_the_real_connector_fetches_lobs_eagerly_and_passes_the_credentials(
    env, monkeypatch
):
    """ATR's Q4 returns `scope` and `summary_conclusion` as CLOBs. Row shaping happens
    after the connection closes, and a lazy LOB cannot be read by then, so the driver has
    to materialise them.

    Exercises the real connector with `oracledb.connect` stubbed, rather than the injected
    fake — the flag is driver state, so a fake connector would prove nothing.
    """
    import oracledb

    from da_platform.warehouse import _oracle_connector

    monkeypatch.setattr(oracledb.defaults, "fetch_lobs", True, raising=False)
    captured: dict = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(oracledb, "connect", fake_connect)

    _oracle_connector(warehouse_credentials())

    assert oracledb.defaults.fetch_lobs is False
    assert captured == {
        "user": "svc_reader",
        "password": "secret",
        "dsn": "uq00604p:1521/DSDMUNP1",
    }


def test_the_object_names_are_readable_without_connecting(env):
    """The silo builds its SQL from these, so reaching them must not open a session."""
    def explode(config):
        raise AssertionError("connected when only the object names were needed")

    warehouse = Warehouse(connector=explode)

    assert warehouse.schema == "DEVSCI_DM"
    assert warehouse.results_view == "DEVSCI_DM.mv_combined_results"


def test_reachable_is_false_rather_than_raising(env):
    """Used by the silo's status endpoint, which must render rather than 500 when the
    warehouse is down."""
    def explode(config):
        raise OSError("ORA-12541: TNS:no listener")

    assert Warehouse(connector=explode).reachable() is False


def test_reachable_is_true_when_a_connection_opens(env):
    assert Warehouse(connector=fake_connector([])).reachable() is True


def test_reachable_is_false_when_credentials_are_absent(env):
    env.delenv("CMCDW_DSN")
    assert Warehouse(connector=fake_connector([])).reachable() is False


# ── the stage context capability ───────────────────────────────────────────────


def test_stage_context_offers_warehouse_and_allows_injection(client, session, probe):
    from da_platform.engine.context import StageContext
    from tests.test_engine import queued_run

    run = queued_run(client, session)
    sentinel = object()

    assert StageContext(session, run, warehouse=sentinel).warehouse is sentinel


def test_stage_context_does_not_touch_the_warehouse_until_used(
    client, session, probe, monkeypatch
):
    """Every run builds a context, and most silos have no database at all."""
    from da_platform.engine import context as context_module
    from tests.test_engine import queued_run

    def explode():
        raise AssertionError("the warehouse was resolved during construction")

    monkeypatch.setattr(context_module, "get_warehouse", explode)
    run = queued_run(client, session)

    assert context_module.StageContext(session, run) is not None


# ── the one live check ────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("CMCDW_DSN"),
    reason="set CMCDW_USER / CMCDW_PASSWORD / CMCDW_DSN to check the real warehouse",
)
def test_the_real_warehouse_is_reachable():
    assert Warehouse().reachable() is True, (
        "CMCDW_* are set but the warehouse did not accept a connection"
    )
