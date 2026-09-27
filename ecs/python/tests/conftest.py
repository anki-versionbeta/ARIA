"""Test fixtures.

Environment variables are set before importing `api.backend.da_platform` because settings and
the engine are resolved at import time.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from pathlib import Path

_tmp_dir = Path(tempfile.mkdtemp(prefix="da_tests_"))

os.environ["DA_ENV"] = "local"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_tmp_dir / "storage")
os.environ["STORAGE_BACKEND"] = "local"
# Point the registry at the fixture silo, so the engine can be exercised without a
# fake silo shipping in the deployed image.
os.environ["SILOS_DIR"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "silos")
os.environ["AUTH_PROVIDER"] = "dev"
os.environ["DEV_AUTH_USERS"] = "asha.rao,ben.carter"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ["STALE_CLAIM_TIMEOUT_S"] = "900"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.backend.da_platform.db.models import Base, Run, RunFile  # noqa: E402
from api.backend.da_platform.db.session import SessionLocal, engine  # noqa: E402
from api.app import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def probe():
    """The fixture silo, with its recorded calls cleared."""
    from api.backend.da_platform.silo_registry import get_silo

    silo = get_silo("probe")
    assert silo is not None, "fixture silo not discovered; check SILOS_DIR"
    silo.module.reset()
    yield silo
    silo.module.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def grant_every_module(username: str) -> None:
    """Give `username` a grant row for every discovered silo.

    Logging in leaves a user on role="user", which holds no modules until an admin grants
    them, so without this every module-gated route answers 403 and the test never reaches
    the behaviour it is about. Granting rather than promoting to admin keeps role="user",
    so the ownership and admin-only checks are still exercised for real.
    """
    from api.backend.da_platform.auth.access import known_module_ids
    from api.backend.da_platform.db.models import User, UserModuleAccess

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            return
        held = {
            module_id
            for (module_id,) in db.query(UserModuleAccess.module_id).filter(
                UserModuleAccess.user_id == user.id
            )
        }
        for module_id in known_module_ids() - held:
            db.add(
                UserModuleAccess(
                    user_id=user.id, module_id=module_id, granted_by="tests"
                )
            )
        db.commit()
    finally:
        db.close()


def login(client: TestClient, username: str = "asha.rao") -> dict:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "anything"}
    )
    assert response.status_code == 200, response.text
    grant_every_module(username)
    return response.json()


def make_run(session, *, owner_id: str, title: str, silo: str = "probe", status: str = "complete") -> Run:
    run = Run(silo_id=silo, user_id=owner_id, title=title, status=status, stage="build")
    session.add(run)
    session.flush()
    session.add(
        RunFile(
            run_id=run.id,
            kind="input",
            filename="source.pdf",
            storage_key=f"runs/{run.id}/input/source.pdf",
            size_bytes=1234,
            content_type="application/pdf",
        )
    )
    if status == "complete":
        session.add(
            RunFile(
                run_id=run.id,
                kind="output",
                filename="report.docx",
                storage_key=f"runs/{run.id}/output/report.docx",
                size_bytes=999,
                content_type="application/octet-stream",
            )
        )
    session.commit()
    session.refresh(run)
    return run
