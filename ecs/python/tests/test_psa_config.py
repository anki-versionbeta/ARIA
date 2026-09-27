"""Configuration, paths, the download registry, and the response models.

Small modules, but three of them guard something that matters more than their size suggests:

  * `config.py` — the Smartsheet token must never reach a log. `PsaConfig` is an argument to
    `set_config` and a local in several call chains, so it reaches a traceback easily, and the
    token authenticates as a real person who can read every sheet they can see. A token in a log
    is a reportable incident, so the redacting `__repr__` is tested as a security property.

  * `config.installed()` — the defaults are repo-relative and therefore WRONG in a deployed
    silo: `_REPO_ROOT` is the parent of the package, which inside ARIA is `silos/`. A caller that
    was never configured would quietly create `silos/Database/psa.db` in the source tree and
    report `smartsheet_live: false` forever, leaving the Product picker permanently empty.

  * `downloads.py` — handing the client a generated file's absolute path would be a path-traversal
    hole over HTTP: any file the server process can read would become downloadable. The opaque id
    is what stops that, so the properties tested here are that the path never round-trips and that
    the map cannot grow without bound.

Every test restores the process configuration afterwards. `set_config` is a module-level global,
so a leaked config would send the next test's paths somewhere unexpected.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)

TOKEN = REDACTED


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import config, downloads, models, paths

    return {"config": config, "paths": paths, "downloads": downloads, "models": models}


@pytest.fixture(autouse=True)
def restore_config(psa):
    """Put the process configuration back, whatever a test does to it."""
    before = psa["config"]._INJECTED
    yield
    psa["config"]._INJECTED = before


@pytest.fixture
def configured(psa, tmp_path):
    """A config rooted in a temp directory, so nothing is written to the source tree."""
    cfg = dataclasses.replace(
        psa["config"].defaults(),
        work_dir=str(tmp_path),
        db_path=str(tmp_path / "Database" / "psa.db"),
        output_dir=str(tmp_path / "Output_Files"),
        uploads_dir=str(tmp_path / "uploads"),
        storage_dir=str(tmp_path / "store"),
        smartsheet_token=REDACTED
        smartsheet_sheet_id="SHEET-1",
    )
    psa["config"].set_config(cfg)
    return cfg


# ── the token must never be printable ─────────────────────────────────────────


def test_the_repr_redacts_the_token(psa, configured):
    """The default dataclass repr would print it. This object reaches a traceback easily."""
    text = repr(configured)

    assert TOKEN not in text
    assert "<redacted>" in text


def test_the_repr_still_says_whether_a_token_is_present(psa):
    """"Is a token configured?" is the first diagnostic question, and redaction must not
    make it unanswerable."""
    without = dataclasses.replace(psa["config"].defaults(), smartsheet_token=None)

    assert "token=None" in repr(without)


def test_the_repr_keeps_the_sheet_id(psa, configured):
    """The sheet id names a sheet rather than being a secret, and it is what you need to know
    when the wrong catalogue comes back."""
    assert "SHEET-1" in repr(configured)


def test_the_token_is_not_leaked_by_formatting_the_config(psa, configured):
    """f-strings and `str()` both route through `__repr__` for a dataclass, and logging
    calls use them far more often than `repr()` directly."""
    assert TOKEN not in f"{configured}"
    assert TOKEN not in str(configured)


def test_the_token_is_not_leaked_when_a_config_is_inside_a_container(psa, configured):
    """`logger.info("...%s", {"cfg": cfg})` is the realistic shape of the accident: a
    container's repr calls its members' repr, not their str."""
    assert TOKEN not in repr({"cfg": configured})
    assert TOKEN not in repr([configured])


# ── knowing whether a host has configured the process ─────────────────────────


def test_a_fresh_process_reports_nothing_installed(psa):
    """Which is what makes `router.py` install the ARIA config on its first request."""
    psa["config"]._INJECTED = None

    assert psa["config"].installed() is False


def test_setting_a_config_marks_the_process_installed(psa, configured):
    assert psa["config"].installed() is True


def test_the_installed_config_is_what_config_returns(psa, configured):
    assert psa["config"].config().smartsheet_sheet_id == "SHEET-1"


def test_an_unconfigured_process_answers_with_the_repo_defaults(psa):
    """Deliberately not an error: someone importing this module directly should get a
    working answer. The danger is only that the answer is repo-relative, which is what
    `installed()` exists to let a host detect."""
    psa["config"]._INJECTED = None

    assert psa["config"].config() == psa["config"].defaults()


def test_the_defaults_carry_no_credentials(psa):
    """A default token would be a credential in source control."""
    defaults = psa["config"].defaults()

    assert defaults.smartsheet_token is None
    assert defaults.smartsheet_sheet_id is None


def test_the_config_is_not_cached_between_calls(psa):
    """A cache would reintroduce the import-ordering trap the module exists to remove: a
    value read before the host finished configuring would win for the whole process."""
    psa["config"]._INJECTED = None
    first = psa["config"].config()
    psa["config"].set_config(
        dataclasses.replace(first, smartsheet_sheet_id="LATER")
    )

    assert psa["config"].config().smartsheet_sheet_id == "LATER"


def test_a_host_can_build_on_the_defaults(psa, tmp_path):
    """`dataclasses.replace(defaults(), ...)` is the documented way to configure, so that a
    new field does not have to be added in three places."""
    cfg = dataclasses.replace(psa["config"].defaults(), work_dir=str(tmp_path))

    assert cfg.work_dir == str(tmp_path)
    assert cfg.db_path == psa["config"].defaults().db_path, "untouched fields survive"


# ── whether the data is live ──────────────────────────────────────────────────


def test_a_configured_token_and_sheet_mean_live(psa, configured):
    assert configured.smartsheet_live is True


@pytest.mark.parametrize("token,sheet", [
    (None, "SHEET-1"), (TOKEN, None), (None, None), ("", "SHEET-1"),
])
def test_a_missing_credential_means_not_live(psa, token, sheet):
    """`/health` reports this rather than failing, so the screen can say why the picker is
    empty instead of showing a 500."""
    cfg = dataclasses.replace(
        psa["config"].defaults(), smartsheet_token=token, smartsheet_sheet_id=sheet
    )

    assert cfg.smartsheet_live is False


def test_an_explicit_xlsx_source_is_never_live(psa, configured):
    """The stale .xlsx export is a deliberate fallback, and a token being present must not
    override the operator's choice to use it."""
    cfg = dataclasses.replace(configured, ingest_source="xlsx")

    assert cfg.smartsheet_live is False


def test_the_image_directory_sits_beside_the_reports(psa, configured):
    """Product images are run artefacts, so they belong with the output rather than in a
    separate tree that has to be provisioned."""
    assert configured.image_dir == os.path.join(configured.output_dir, "assets")


# ── paths, which every write goes through ─────────────────────────────────────


def test_the_paths_follow_the_installed_config(psa, configured):
    """A path resolved from the defaults inside ARIA would write into the source tree."""
    assert psa["paths"].db_path() == configured.db_path
    assert psa["paths"].output_dir() == configured.output_dir


def test_ensuring_the_directories_creates_them(psa, configured):
    """`build_db.main()` calls this first, so a run must not fail on a missing output dir."""
    psa["paths"].ensure_dirs()

    assert os.path.isdir(configured.output_dir)
    assert os.path.isdir(os.path.dirname(configured.db_path))


def test_ensuring_the_directories_twice_is_harmless(psa, configured):
    """Every run calls it, so it has to be idempotent."""
    psa["paths"].ensure_dirs()
    psa["paths"].ensure_dirs()

    assert os.path.isdir(configured.output_dir)


def test_the_image_directory_is_created_too(psa, configured):
    """The photo capture writes here, and it runs before anything else would create it."""
    psa["paths"].ensure_dirs()

    assert os.path.isdir(psa["paths"].image_dir())


# ── the download registry ─────────────────────────────────────────────────────


def test_a_registered_file_resolves_back(psa, tmp_path):
    document = tmp_path / "report.docx"
    document.write_bytes(b"X")

    token = REDACTED

    assert psa["downloads"].resolve(token) == os.path.abspath(str(document))


def test_the_id_is_opaque_and_carries_no_path(psa, tmp_path):
    """The whole point: handing the client the path would make any file the server can read
    downloadable."""
    document = tmp_path / "report.docx"
    document.write_bytes(b"X")

    token = REDACTED

    assert "report" not in token
    assert os.sep not in token
    assert token.isalnum()


def test_two_registrations_get_different_ids(psa, tmp_path):
    """A reused id would let one user fetch another's report."""
    document = tmp_path / "report.docx"
    document.write_bytes(b"X")

    assert psa["downloads"].register(str(document)) != \
        psa["downloads"].register(str(document))


def test_an_unknown_id_resolves_to_nothing(psa):
    """Which the router turns into a 404 rather than a stack trace."""
    assert psa["downloads"].resolve("not-a-real-id") is None


def test_an_id_for_a_deleted_file_resolves_to_nothing(psa, tmp_path):
    """The scratch directory is temporary, so a file can vanish between generating and
    downloading — and streaming a missing path would be a 500."""
    document = tmp_path / "report.docx"
    document.write_bytes(b"X")
    token = REDACTED
    document.unlink()

    assert psa["downloads"].resolve(token) is None


def test_the_registry_is_bounded(psa, tmp_path):
    """A long-running server must not grow the map without limit."""
    document = tmp_path / "report.docx"
    document.write_bytes(b"X")

    tokens = REDACTED
              for _ in range(psa["downloads"]._MAX_ENTRIES + 10)]

    assert len(psa["downloads"]._FILES) <= psa["downloads"]._MAX_ENTRIES
    assert psa["downloads"].resolve(tokens[-1]) is not None, "the newest survives"


def test_the_oldest_id_is_the_one_dropped(psa, tmp_path):
    """dicts keep insertion order, so eviction is oldest-first — the id a user is most
    likely to have finished with."""
    document = tmp_path / "report.docx"
    document.write_bytes(b"X")
    first = psa["downloads"].register(str(document))

    for _ in range(psa["downloads"]._MAX_ENTRIES):
        psa["downloads"].register(str(document))

    assert psa["downloads"].resolve(first) is None


def test_a_relative_path_is_stored_absolute(psa, tmp_path, monkeypatch):
    """The worker's working directory is not the API's, and a relative path would resolve
    differently in the process that serves the download."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.docx").write_bytes(b"X")

    token = REDACTED

    assert os.path.isabs(psa["downloads"].resolve(token))


# ── the response models the screen is typed against ───────────────────────────


def test_the_health_model_reports_the_four_facts(psa):
    """Each one changes how the rest of the screen should be read."""
    out = psa["models"].HealthOut(
        ok=True, smartsheet_live=False, db_built=True, palette_is_override=False
    )

    assert (out.ok, out.smartsheet_live, out.db_built) == (True, False, True)


def test_a_failed_export_needs_no_download_id(psa):
    """The failure path has no file, and requiring one would make the model unusable for
    exactly the case it has to describe."""
    out = psa["models"].ExportOut(ok=False, message="nothing to export")

    assert out.download_id is None
    assert out.filename is None


def test_a_report_result_tolerates_a_validation_answer(psa):
    """`status` is 'ok', 'message' or 'needs_override', and only the first has a file."""
    out = psa["models"].ReportOut(status="message", message="pick a presentation")

    assert out.status == "message"
    assert out.download_id is None


def test_a_recommendation_can_carry_only_an_error(psa):
    """A presentation missing from the database is answered, not raised — the screen shows
    the message and offers a refresh."""
    out = psa["models"].RecommendOut(error="not in the analysis database yet")

    assert out.error
    assert not out.recommended
