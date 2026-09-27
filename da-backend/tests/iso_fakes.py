"""Hand-written stubs shared by the ISO port's tests.

Hand-written because nothing can be installed: the corporate proxy denies pypi, so
`responses` and `moto` are not available. That turns out to be an advantage — these
stubs record exactly the three things a verbatim port must not drift on (the model, the
token budget, and the per-call timeout) and fail loudly when a call is made in a way the
source would not have made it.
"""

from __future__ import annotations

import threading
from typing import Any


class StubLlmError(RuntimeError):
    """Stands in for `da_platform.llm.client.LlmError`."""


class StubEmptyCompletion(StubLlmError):
    """Stands in for `EmptyCompletion`: a 200 with no usable content."""


class FakeLlm:
    """Records calls and returns canned replies.

    Every reply queue is popped in order. An `Exception` *instance* in a queue is raised
    rather than returned, which is how the empty-answer and hard-failure paths are told
    apart in tests.
    """

    def __init__(
        self,
        *,
        chat: list[Any] | None = None,
        chat_vision: list[Any] | None = None,
        chat_vision_multi: list[Any] | None = None,
        rag: list[Any] | None = None,
        documents: list[Any] | None = None,
    ) -> None:
        self._chat = list(chat or [])
        self._chat_vision = list(chat_vision or [])
        self._chat_vision_multi = list(chat_vision_multi or [])
        self._rag = list(rag or [])
        self._documents = list(documents or [])
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.ensured: list[tuple[str, str]] = []
        self.uploaded: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

    # ── helpers ───────────────────────────────────────────────────────────────

    def _pop(self, queue: list[Any], what: str) -> Any:
        # Locked because the ported code fans out over a thread pool: without this two
        # workers can pop the same index, or drop one. Note the lock makes the stub
        # safe, not deterministic — which page receives which reply still depends on
        # thread scheduling, so a test that cares must pin max_workers=1.
        with self._lock:
            if not queue:
                raise AssertionError(f"FakeLlm has no {what} reply left to give")
            reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def calls_of(self, kind: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["kind"] == kind]

    # ── chat ──────────────────────────────────────────────────────────────────

    def chat(
        self,
        *,
        user: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 8000,
        temperature: float | None = None,
        timeouts: tuple[int, ...] = (120, 240),
    ) -> str:
        self.calls.append(
            {
                "kind": "chat",
                "user": user,
                "system": system,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeouts": timeouts,
            }
        )
        return self._pop(self._chat, "chat")

    def chat_vision(
        self,
        *,
        image_b64: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 4096,
        media_type: str = "image/png",
        temperature: float | None = None,
        timeouts: tuple[int, ...] = (120, 240),
    ) -> str:
        self.calls.append(
            {
                "kind": "chat_vision",
                "prompt": prompt,
                "images": 1,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeouts": timeouts,
            }
        )
        return self._pop(self._chat_vision, "chat_vision")

    def chat_vision_multi(
        self,
        *,
        images: list[str],
        prompt: str,
        model: str | None = None,
        max_tokens: int = 4096,
        media_type: str = "image/png",
        temperature: float | None = None,
        timeouts: tuple[int, ...] = (120, 240),
    ) -> str:
        if not images:
            raise ValueError("chat_vision_multi needs at least one image")
        self.calls.append(
            {
                "kind": "chat_vision_multi",
                "prompt": prompt,
                "images": len(images),
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeouts": timeouts,
            }
        )
        return self._pop(self._chat_vision_multi, "chat_vision_multi")

    # ── RAG ───────────────────────────────────────────────────────────────────

    def ensure_source(self, source: str, description: str = "") -> None:
        self.ensured.append((source, description))

    def upload_document(
        self,
        source: str,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
        timeouts: tuple[int, ...] = (120, 240),
    ) -> Any:
        self.uploaded.append(
            {
                "source": source,
                "filename": filename,
                "bytes": len(content),
                "timeouts": timeouts,
            }
        )
        return {"id": "doc-1"}

    def rag_query(self, source: str, question: str, k: int = 3) -> str:
        self.calls.append({"kind": "rag", "source": source, "question": question, "k": k})
        return self._pop(self._rag, "rag")

    def list_documents(self, source: str) -> Any:
        if not self._documents:
            return []
        return self._pop(self._documents, "list_documents")

    def delete_document(self, source: str, document_id: str) -> None:
        self.deleted.append((source, document_id))


def _bounding_box(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {
        "Geometry": {
            "BoundingBox": {"Left": x0, "Top": y0, "Width": x1 - x0, "Height": y1 - y0}
        }
    }


class FakeTextract:
    """Stands in for `ctx.textract`, which analyses one rendered page.

    Returns a **raw** Textract-shaped response — `{"Blocks": [...]}` — because that is
    what the real client returns and what `_call_textract_page` parses. Returning the
    already-parsed `{"tables": ..., "figures": ...}` shape would make every test pass
    against a payload the production code never sees.

    Tables are emitted as a TABLE block with CELL children, since the parser computes a
    table's envelope from its cells rather than trusting a single box. Figures are
    LAYOUT_FIGURE blocks.

    `calls` is the number of page analyses, and therefore the bill: this runs once per
    page of a garbled document, so asserting the count is what stops a refactor quietly
    doubling the cost.
    """

    def __init__(
        self,
        tables: list[list[float]] | tuple = (),
        figures: list[list[float]] | tuple = (),
        *,
        layout_tables: list[list[float]] | tuple = (),
    ) -> None:
        # Normalised [x0, y0, x1, y1], origin top-left, as Textract reports them.
        self.tables = [list(box) for box in tables]
        self.figures = [list(box) for box in figures]
        self.layout_tables = [list(box) for box in layout_tables]
        self.calls = 0
        self.pages: list[bytes] = []

    def blocks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for index, (x0, y0, x1, y1) in enumerate(self.tables):
            cell_id = f"cell-{index}"
            out.append(
                {
                    "Id": f"table-{index}",
                    "BlockType": "TABLE",
                    "Relationships": [{"Type": "CHILD", "Ids": [cell_id]}],
                }
            )
            out.append(
                {"Id": cell_id, "BlockType": "CELL", **_bounding_box(x0, y0, x1, y1)}
            )
        for index, (x0, y0, x1, y1) in enumerate(self.layout_tables):
            out.append(
                {
                    "Id": f"layout-table-{index}",
                    "BlockType": "LAYOUT_TABLE",
                    **_bounding_box(x0, y0, x1, y1),
                }
            )
        for index, (x0, y0, x1, y1) in enumerate(self.figures):
            out.append(
                {
                    "Id": f"figure-{index}",
                    "BlockType": "LAYOUT_FIGURE",
                    **_bounding_box(x0, y0, x1, y1),
                }
            )
        return out

    def __call__(self, png_bytes: bytes, feature_types: list[str] | None = None) -> Any:
        self.calls += 1
        self.pages.append(png_bytes)
        return {"Blocks": self.blocks()}
