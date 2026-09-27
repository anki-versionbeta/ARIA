"""Which AWS credentials is this process actually using, and do they work?

Run it on the box that is failing, with the same user and working directory as the
service:

    python scripts/diagnose_aws.py

`ExpiredToken` from an EC2 instance role should be impossible: botocore refreshes
role credentials from the instance metadata service automatically. Seeing it means
something is supplying *frozen* temporary credentials that win over the role —
almost always environment variables left in the service's environment, or a stale
`~/.aws/credentials`. `credentials.py` only passes explicit keys when both an access
key and a secret are present, so the culprit is the environment rather than the app.

Prints no secret values: key ids are truncated and secrets are reported only as
set/unset.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    # Import settings first so `.env` is loaded exactly as the app loads it. Without
    # this the report would describe a different environment from the one the service
    # runs in — and `_load_dotenv` never overrides a variable already set, so the
    # distinction below between "from .env" and "already in the environment" is the
    # whole point: an inherited stale value silently wins over `.env`.
    before = {k for k in os.environ if k.startswith("AWS_")}
    import da_platform.settings  # noqa: F401  (loads .env as a side effect)

    from_dotenv = {k for k in os.environ if k.startswith("AWS_")} - before

    print("=== AWS_* in this process's environment ===")
    if from_dotenv:
        print(f"  (came from .env: {', '.join(sorted(from_dotenv))})")
    else:
        print("  (.env contributed no AWS_* variables)")
    interesting = [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_EC2_METADATA_DISABLED",
    ]
    for name in interesting:
        raw = os.environ.get(name)
        if raw is None:
            print(f"  {name:42} unset")
        elif "SECRET" in name or "TOKEN" in name:
            print(f"  {name:42} SET ({len(raw)} chars)")
        else:
            print(f"  {name:42} {raw[:12]}{'...' if len(raw) > 12 else ''}")

    print()
    print("=== credential files on disk ===")
    for path in (Path.home() / ".aws" / "credentials", Path.home() / ".aws" / "config"):
        print(f"  {str(path):42} {'exists' if path.is_file() else 'absent'}")

    print()
    print("=== what botocore resolves ===")
    try:
        import botocore.session
    except ImportError:
        print("  botocore is not installed")
        return 1

    creds = botocore.session.get_session().get_credentials()
    if creds is None:
        print("  no credentials found at all")
        return 1

    frozen = creds.get_frozen_credentials()
    # `method` names the provider that won: iam-role means the instance role (good,
    # auto-refreshing); env or shared-credentials-file means frozen values (the bug).
    print(f"  provider (method) : {creds.method}")
    print(f"  access key id     : {frozen.access_key[:8]}...")
    print(f"  key type          : {_key_type(frozen.access_key)}")
    print(f"  session token     : {'present' if frozen.token else 'none'}")
    print(f"  auto-refreshing   : {hasattr(creds, 'refresh_needed')}")

    print()
    print("=== does a real call work? ===")
    try:
        from da_platform.aws import get_clients
        from da_platform.credentials import aws_credentials

        bucket = aws_credentials().s3_bucket
        get_clients().s3().list_objects_v2(Bucket=bucket, MaxKeys=1)
        print(f"  s3:ListObjectsV2 on {bucket}: OK")
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic
        print(f"  FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    return 0


def _key_type(access_key: str) -> str:
    if access_key.startswith("ASIA"):
        return "ASIA = temporary STS credential (expires)"
    if access_key.startswith("AKIA"):
        return "AKIA = long-lived IAM user key"
    return "unrecognised prefix"


if __name__ == "__main__":
    raise SystemExit(main())
