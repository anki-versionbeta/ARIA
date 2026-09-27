"""Seed local development data.

Three users, not one: ownership and fork-on-edit (D14/D15) cannot be exercised
with a single identity. Runs cover every status so the history filters and status
badges have something to show.

Run:  python -m scripts.seed_dev
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from da_platform.db.models import Run, RunFile, User, utcnow  # noqa: E402
from da_platform.db.session import SessionLocal, create_all  # noqa: E402

USERS = [
    ("asha.rao", "Asha Rao"),
    ("ben.carter", "Ben Carter"),
    ("mei.lin", "Mei Lin"),
]

# (silo, title, status, stage, pct, message, owner index, age in hours)
RUNS = [
    ("bop", "BOP — Vessel Prep Line 3", "complete", "build", 100, None, 0, 26),
    ("bop", "BOP — Media Hold Tank", "running", "generate", 45, "Generating section 4 of 6", 0, 1),
    ("bop", "BOP — Buffer Prep Skid", "awaiting_user", "review", 70, "Awaiting review", 1, 5),
    ("iso", "ISO 10993-1 Applicability", "complete", "build", 100, None, 1, 50),
    ("iso", "ISO 11137 Sterilisation Review", "failed", "generate", 30, None, 2, 8),
    ("iso", "ISO 10993-5 Cytotoxicity", "queued", None, 0, "Waiting for a worker", 2, 0),
]


def main() -> None:
    create_all()
    session = SessionLocal()
    try:
        users: list[User] = []
        for username, display_name in USERS:
            user = session.query(User).filter(User.username == username).one_or_none()
            if user is None:
                user = User(
                    username=username,
                    display_name=display_name,
                    email=f"{username}@abbvie.com",
                )
                session.add(user)
            users.append(user)
        session.commit()

        if session.query(Run).count() > 0:
            print("Runs already present; leaving them untouched.")
            return

        now = utcnow()
        for silo, title, status, stage, pct, message, owner_index, age_hours in RUNS:
            started = now - timedelta(hours=age_hours)
            finished = started + timedelta(minutes=7) if status in {"complete", "failed"} else None
            run = Run(
                silo_id=silo,
                user_id=users[owner_index].id,
                status=status,
                stage=stage,
                progress_pct=pct,
                progress_message=message,
                title=title,
                started_at=started,
                finished_at=finished,
                duration_ms=int(timedelta(minutes=7).total_seconds() * 1000) if finished else None,
                error_message="Iliad request failed after 3 retries" if status == "failed" else None,
            )
            session.add(run)
            session.flush()

            session.add(
                RunFile(
                    run_id=run.id,
                    kind="input",
                    filename=f"{title.split(' — ')[-1].replace(' ', '_')}.pdf",
                    storage_key=f"runs/{run.id}/input/source.pdf",
                    size_bytes=2_400_000,
                    content_type="application/pdf",
                )
            )
            if status == "complete":
                session.add(
                    RunFile(
                        run_id=run.id,
                        kind="output",
                        filename=f"{title.replace(' ', '_')}.docx",
                        storage_key=f"runs/{run.id}/output/report.docx",
                        size_bytes=180_000,
                        content_type=(
                            "application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"
                        ),
                    )
                )
        session.commit()
        print(f"Seeded {len(USERS)} users and {len(RUNS)} runs.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
