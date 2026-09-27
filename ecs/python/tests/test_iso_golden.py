"""Golden comparison against a real captured ISO session.

The design spec claims `_test_output_baseline.docx` and `_test_output_fixed.docx` exist in
the ISO folder and become fixtures. They do not. What does exist is better: a matched
triple from one real session — the input PDF, the DOCX it produced, and the eight media
PNGs extracted along the way.

Those files are copyrighted ISO standards and **must not** enter this repository. They are
read from `ISO_CORPUS_DIR`, so this runs on a developer machine and skips everywhere else:

    ISO_CORPUS_DIR="C:/Python_files/ISO/iso_doc_latest" pytest tests/test_iso_golden.py

Byte comparison of two .docx files always fails — zip timestamps and document metadata —
so the comparison is structural. See `tests/docx_projection.py`.

The two cheap checks run with only the corpus present. The full reproduction additionally
needs live Iliad and AWS credentials and is gated behind `ISO_GOLDEN_LIVE`, because it
costs one Textract page analysis per page of the document.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.docx_projection import project_docx

_CORPUS = os.environ.get("ISO_CORPUS_DIR", "").strip()
CORPUS = Path(_CORPUS) if _CORPUS else None

SESSION = "04ad4a14-d4ac-46bc-aaa0-9d49298effb0"
INPUT_PDF = f"folders/uploads/{SESSION}_ISO 20417-2021 3.pdf"
EXPECTED_DOCX = (
    "folders/downloads/ANSIAAMIISO_204172021_Medical_devicesInf_Assessment.docx"
)
MEDIA_DIR = f"folders/memry/{SESSION}"

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = [
    pytest.mark.skipif(
        CORPUS is None,
        reason=(
            "set ISO_CORPUS_DIR to the folder holding the ISO corpus; the standards are "
            "copyrighted and are deliberately not committed"
        ),
    ),
    pytest.mark.skipif(
        not (ISO_DIR / "silo.py").is_file(), reason="the ISO silo is not present"
    ),
]


@pytest.fixture(scope="module")
def corpus():
    assert CORPUS is not None
    pdf = CORPUS / INPUT_PDF
    docx = CORPUS / EXPECTED_DOCX
    media = CORPUS / MEDIA_DIR
    missing = [str(path) for path in (pdf, docx, media) if not path.exists()]
    if missing:
        pytest.skip(f"ISO_CORPUS_DIR is set but incomplete: {missing}")
    return {"pdf": pdf, "docx": docx, "media": media}


def test_the_captured_output_projects_to_something_non_trivial(corpus):
    """Guards against a projector change making every comparison below pass vacuously —
    the failure mode that would render this whole file worthless."""
    projection = project_docx(corpus["docx"])

    assert len(projection.headings) >= 3, projection.headings
    assert len(projection.tables) >= 1
    # The assessment table is two columns: section number and description.
    assert projection.tables[0].grid_columns == 2
    assert len(projection.tables[0].row_cell_counts) > 5, "expected many assessment rows"
    assert projection.image_count >= 1, "the captured report embeds figures and tables"


def test_the_captured_media_filenames_survive_sanitisation(corpus):
    """Media filenames are caption-derived, so a change to `_sanitize_filename` silently
    orphans every image. Cheap, deterministic, and needs no credentials."""
    _load_module("iso", ISO_DIR / "silo.py")
    mediastore = _load_module("iso", ISO_DIR / "mediastore.py", name="mediastore")

    on_disk = sorted(path.name for path in corpus["media"].glob("*.png"))

    assert len(on_disk) == 8, on_disk
    for name in on_disk:
        caption = name[: -len(".png")]
        assert mediastore._sanitize_filename(caption) + ".png" == name, name


def test_the_expected_output_names_the_standard(corpus):
    """The output filename comes from the RAG-derived title, sanitised. This pins the
    convention the port must reproduce."""
    import re

    stem = corpus["docx"].name[: -len(".docx")]
    assert stem.endswith("_Assessment")
    # Same rule as the port: word characters, whitespace and hyphens only.
    assert re.fullmatch(r"[\w\-]+", stem), stem


@pytest.mark.skipif(
    os.environ.get("ISO_GOLDEN_LIVE", "").strip().lower() not in {"1", "true", "yes"},
    reason=(
        "set ISO_GOLDEN_LIVE=1 with live Iliad and AWS credentials; this run costs one "
        "Textract page analysis per page"
    ),
)
def test_the_port_reproduces_the_captured_output_structurally(corpus, tmp_path):
    """The acceptance gate for the port.

    Deliberately not asserted for byte equality, and cell text is compared with a
    similarity floor: any page routed through garbled-text repair has its body produced by
    a live model, so exact wording is not reproducible. Structural drift — a missing
    section, a lost table, a dropped figure — moves the projection far more than wording
    jitter does.
    """
    import shutil

    from tests.docx_projection import compare_projections

    _load_module("iso", ISO_DIR / "silo.py")
    for name in (
        "patterns",
        "sections",
        "mediastore",
        "geometry",
        "vision",
        "textract",
        "toc",
        "rag",
        "garble",
        "equations",
        "prescan",
        "docxstyle",
        "rows",
        "body",
        "generate",
    ):
        _load_module("iso", ISO_DIR / f"{name}.py", name=name)

    from api.backend.da_platform.aws import get_clients
    from api.backend.da_platform.llm.client import get_client
    from da_silos.iso import garble, generate, toc as toc_module

    # A workspace over a local copy of the corpus PDF, mirroring what the stage builds.
    scratch = tmp_path / "ws"
    scratch.mkdir()
    local_pdf = scratch / corpus["pdf"].name
    shutil.copyfile(corpus["pdf"], local_pdf)

    class LiveWorkspace:
        pdf_path = str(local_pdf)
        source_name = corpus["pdf"].name

        def __init__(self):
            self._media = scratch / "memry"
            self._media.mkdir(exist_ok=True)

        @property
        def media_dir(self) -> str:
            return str(self._media)

        def put_media(self, filename: str, data: bytes) -> str:
            return filename

        @staticmethod
        def progress(message: str) -> None:
            print(message)

    class LiveContext:
        llm = get_client()
        textract = staticmethod(get_clients().analyze_document)

        @staticmethod
        def progress(message: str, pct: int | None = None) -> None:
            print(f"[{pct}] {message}")

    workspace = LiveWorkspace()
    expected = project_docx(corpus["docx"])

    use_vision = garble.is_pdf_garbled(workspace.pdf_path, llm=LiveContext.llm)
    entries = toc_module.deduplicate_toc(
        toc_module.extract_toc(workspace.pdf_path, llm=LiveContext.llm)
    )
    assert entries, "the outline must not be empty for the corpus document"

    # Match the captured report's shape: as many assessment sections as it has rows.
    result = generate.build_document(
        LiveContext(),
        workspace,
        toc=entries,
        doc_title=corpus["docx"].name[: -len("_Assessment.docx")],
        start_idx=0,
        end_idx=len(entries) - 1,
        use_vision=use_vision,
        llm_pages=None,
        total_pages=0,
    )

    diff = compare_projections(expected, project_docx(result["docx"]))
    if diff.soft:
        print(diff.report())
    assert not diff.hard, diff.report()
