"""Verify the shared Iliad client against the live gateway.

Makes three cheap calls: a text chat, a vision call on a generated image, and a RAG
source listing. Secret values are never printed — only their length and last four
characters, which is enough to tell two keys apart.

Run:  python -m scripts.check_iliad
      python -m scripts.check_iliad --env-file <path to a .env>
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_env_file(path: Path) -> list[str]:
    """Load KEY=VALUE pairs into os.environ. Returns the names loaded, not values."""
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def mask(value: str | None) -> str:
    if not value:
        return "(unset)"
    return f"set, {len(value)} chars, ends …{value[-4:]}"


def text_png_base64() -> str:
    """A small rendered PNG so the vision reply can be checked for known content."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=320, height=120)
    page.insert_text((20, 60), "PLATFORM VISION OK", fontsize=22)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    return base64.b64encode(pixmap.tobytes("png")).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--source", default=None, help="RAG source to query, optional")
    args = parser.parse_args()

    if args.env_file:
        names = load_env_file(args.env_file)
        print(f"Loaded {len(names)} names from {args.env_file}: {', '.join(sorted(names))}")
        print()

    from da_platform.credentials import iliad_credentials
    from da_platform.llm.client import IliadClient, LlmError

    try:
        credentials = iliad_credentials()
    except Exception as exc:
        print(f"Credentials unavailable: {exc}")
        return 2

    print(f"base_url      : {credentials.base_url}")
    print(f"text_model    : {credentials.text_model}")
    print(f"vision_model  : {credentials.vision_model}")
    print(f"verify        : {credentials.verify}")
    print(f"ILIAD_API_KEY : {mask(credentials.api_key)}")
    print(f"ILIAD_USER_TOKEN: {mask(credentials.user_token)}")
    print()

    client = IliadClient()
    failures = 0

    print("[1] text chat")
    try:
        reply = client.chat(
            system="Reply with exactly one word.",
            user="Reply with the single word: READY",
            max_tokens=REDACTED
        )
        print(f"    OK -> {reply.strip()[:80]!r}")
    except LlmError as exc:
        failures += 1
        print(f"    FAILED -> {exc}")

    print("[2] vision chat")
    try:
        reply = client.chat_vision(
            image_b64=text_png_base64(),
            prompt="Transcribe the text in this image exactly. Return only the text.",
            max_tokens=REDACTED
        )
        got = reply.strip()
        print(f"    OK -> {got[:80]!r}")
        if "VISION OK" not in got.upper():
            print("    NOTE: reply did not contain the expected words")
    except LlmError as exc:
        failures += 1
        print(f"    FAILED -> {exc}")

    print("[3] RAG sources")
    try:
        sources = client.list_sources()
        print(f"    OK -> {len(sources)} source(s)")
        if args.source:
            answer = client.rag_query(args.source, "What is this document?", k=3)
            print(f"    rag_query -> {answer[:100]!r}")
    except LlmError as exc:
        failures += 1
        print(f"    FAILED -> {exc}")
    except Exception as exc:
        failures += 1
        print(f"    FAILED -> {type(exc).__name__}: {exc}")

    print()
    print("RESULT:", "all checks passed" if failures == 0 else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
