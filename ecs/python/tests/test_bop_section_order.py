"""Section ordering for the review screen.

Sections are stored keyed by dotted path with no ordinal column, so the database can only
hand them back alphabetically. That order bears no relation to the built document:
`purpose` lands in the middle and `description.tools` behind SAFETY. The silo therefore
declares its own sequence and the platform sorts by it.

These cover both halves: the silo-agnostic sort, and the invariants that keep BOP's
declared order in step with what `emit.build_docx` produces.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.db.models import DocumentSection
from api.backend.da_platform.records.sections import in_declared_order
from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

BOP_DIR = BACKEND_ROOT / "silos" / "bop"


# ── the platform's sort, which knows nothing about any silo ────────────────────


def rows(*keys: str) -> list[DocumentSection]:
    return [DocumentSection(run_id="r1", section_key=key) for key in keys]


def keys_of(result: list[DocumentSection]) -> list[str]:
    return [row.section_key for row in result]


def test_rows_follow_the_declared_sequence():
    result = in_declared_order(rows("scope", "purpose"), ["purpose", "scope"])

    assert keys_of(result) == ["purpose", "scope"]


def test_a_silo_that_declares_no_order_is_left_alone():
    stored = rows("b", "a", "c")

    # Not every silo needs to describe itself; saying nothing must not reshuffle anything.
    assert keys_of(in_declared_order(stored, [])) == ["b", "a", "c"]


def test_an_unlisted_section_goes_last_rather_than_disappearing():
    result = in_declared_order(
        rows("surprise", "scope", "purpose"), ["purpose", "scope"]
    )

    # An unlisted section is still the author's content.
    assert keys_of(result) == ["purpose", "scope", "surprise"]


def test_unlisted_sections_keep_a_predictable_order_among_themselves():
    result = in_declared_order(rows("zeta", "alpha", "purpose"), ["purpose"])

    assert keys_of(result) == ["purpose", "alpha", "zeta"]


def test_a_declared_key_that_is_absent_is_simply_skipped():
    result = in_declared_order(rows("scope"), ["purpose", "scope", "safety.ppe"])

    assert keys_of(result) == ["scope"]


# ── BOP's declared order, versus what it actually stores and emits ─────────────

pytestmark_bop = pytest.mark.skipif(
    not (BOP_DIR / "silo.py").is_file(), reason="the BOP silo is not present"
)


@pytest.fixture(scope="module")
def bop():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import editing, schema

    return {"editing": editing, "schema": schema}


@pytestmark_bop
def test_the_silo_exposes_its_order_to_the_platform(bop):
    # The platform reads SECTION_ORDER off the silo module, so re-exporting it from
    # silo.py is what actually wires this up.
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import silo

    assert silo.SECTION_ORDER == bop["editing"].SECTION_ORDER


@pytestmark_bop
def test_the_declared_order_covers_exactly_the_editable_leaves(bop):
    editing = bop["editing"]
    # A leaf added to PROSE_LEAVES or LIST_LEAVES without a place in SECTION_ORDER would
    # otherwise fall silently to the bottom of the review screen.
    assert set(editing.SECTION_ORDER) == set(editing.PROSE_LEAVES) | set(
        editing.LIST_LEAVES
    )


@pytestmark_bop
def test_the_declared_order_lists_nothing_twice(bop):
    order = bop["editing"].SECTION_ORDER

    assert len(order) == len(set(order))


@pytestmark_bop
def test_the_declared_order_describes_exactly_what_gets_stored(bop):
    editing, schema = bop["editing"], bop["schema"]
    # flatten() produces the rows that are written, so the order must match its keys.
    stored = set(editing.flatten(schema.empty_document()))

    assert set(editing.SECTION_ORDER) == stored


@pytestmark_bop
def test_the_document_opens_with_purpose_and_scope(bop):
    # Fixed by emit.build_docx: PURPOSE is the first heading, SCOPE shares its page.
    assert bop["editing"].SECTION_ORDER[:2] == ["purpose", "scope"]


@pytestmark_bop
def test_related_documents_comes_last(bop):
    # RELATED DOCUMENTS is the final section the emitter writes.
    assert bop["editing"].SECTION_ORDER[-1] == "related_documents"


@pytestmark_bop
def test_description_keeps_its_lists_before_safety(bop):
    order = bop["editing"].SECTION_ORDER
    # The emitter writes Tools/Spare Parts/Consumables/Materials under DESCRIPTION, so
    # they belong there and not after SAFETY where the alphabet would put them.
    safety = order.index("safety.hazards")
    for key in (
        "description.tools",
        "description.spare_parts",
        "description.consumables",
        "description.materials",
    ):
        assert order.index(key) < safety, key


@pytestmark_bop
def test_a_phase_keeps_its_equipment_and_software_together(bop):
    order = bop["editing"].SECTION_ORDER
    # The document interleaves the two per phase, where LIST_LEAVES groups all equipment
    # then all software, which would scatter each phase across the list.
    for phase in bop["schema"].OP_PAIRED_PHASES:
        equipment = order.index(f"operating_procedure.{phase}.equipment")
        software = order.index(f"operating_procedure.{phase}.software")
        assert software == equipment + 1, phase


@pytestmark_bop
def test_the_phases_follow_the_documents_running_order(bop):
    order = bop["editing"].SECTION_ORDER
    phases = bop["schema"].OP_PAIRED_PHASES
    positions = [
        order.index(f"operating_procedure.{phase}.equipment") for phase in phases
    ]

    assert positions == sorted(positions)
