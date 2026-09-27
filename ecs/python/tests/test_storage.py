from __future__ import annotations

import pytest

from api.backend.da_platform.storage import LocalObjectStore
from api.backend.da_platform.storage.base import PayloadTooLarge, asset_key, run_key


class TestKeys:
    def test_run_key_is_prefixed_per_silo(self):
        """BOP already owns `BOP/` in the shared bucket and ISO owns
        `ISO_Doc_Generation/`, so one global prefix could not serve both."""
        assert run_key("BOP", "r1", "input", "manual.pdf") == "BOP/runs/r1/input/manual.pdf"
        assert (
            run_key("ISO_Doc_Generation", "r2", "output", "report.docx")
            == "ISO_Doc_Generation/runs/r2/output/report.docx"
        )

    def test_run_key_without_a_prefix(self):
        assert run_key("", "r1", "media", "page-1.png") == "runs/r1/media/page-1.png"

    def test_run_key_uses_only_the_filename(self):
        """A silo-supplied name must not be able to redirect the key."""
        assert run_key("BOP", "r1", "input", "../../etc/passwd") == (
            "BOP/runs/r1/input/passwd"
        )
        assert run_key("BOP", "r1", "input", "C:\\temp\\x.pdf") == "BOP/runs/r1/input/x.pdf"

    def test_a_silo_can_map_kinds_onto_its_own_folder_names(self):
        """BOP keeps its Dataiku managed-folder layout: uploads/ and downloads/."""
        folders = {"input": "uploads", "output": "downloads", "media": "memry"}
        assert (
            run_key("BOP", "r1", "input", "manual.pdf", folders)
            == "BOP/uploads/r1_manual.pdf"
        )
        assert (
            run_key("BOP", "r1", "output", "BOP_manual.docx", folders)
            == "BOP/downloads/r1_BOP_manual.docx"
        )
        assert (
            run_key("BOP", "r1", "media", "extracted.txt", folders)
            == "BOP/memry/r1_extracted.txt"
        )

    def test_the_run_id_is_kept_so_shared_folders_do_not_collide(self):
        """The old app wrote the bare basename into a shared folder, so two people
        uploading the same filename overwrote each other."""
        folders = {"input": "uploads"}
        first = run_key("BOP", "run-aaa", "input", "Helix_Manual.pdf", folders)
        second = run_key("BOP", "run-bbb", "input", "Helix_Manual.pdf", folders)
        assert first != second

    def test_kinds_without_a_mapping_keep_the_default_layout(self):
        folders = {"input": "uploads"}
        assert (
            run_key("BOP", "r1", "output", "x.docx", folders)
            == "BOP/runs/r1/output/x.docx"
        )

    def test_asset_key_mirrors_the_managed_folder_names(self):
        assert asset_key("BOP", "prompts/petra.txt") == "BOP/prompts/petra.txt"
        assert asset_key("BOP", "/templates/SOP template.docx") == (
            "BOP/templates/SOP template.docx"
        )


class TestLocalObjectStore:
    def test_round_trip(self, tmp_path):
        store = LocalObjectStore(tmp_path)
        assert store.put("BOP/runs/r1/output/report.docx", b"docx bytes") == 10
        assert store.exists("BOP/runs/r1/output/report.docx")
        with store.open("BOP/runs/r1/output/report.docx") as handle:
            assert handle.read() == b"docx bytes"

    def test_local_layout_mirrors_the_object_keys(self, tmp_path):
        """Local development should be inspectable the same way S3 is."""
        store = LocalObjectStore(tmp_path)
        store.put("BOP/prompts/petra.txt", b"prompt")
        assert (tmp_path / "BOP" / "prompts" / "petra.txt").read_bytes() == b"prompt"

    def test_missing_key_reports_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            LocalObjectStore(tmp_path).open("nope/missing.txt")

    def test_exists_is_false_for_a_missing_key(self, tmp_path):
        assert LocalObjectStore(tmp_path).exists("nope/missing.txt") is False

    def test_delete_is_idempotent(self, tmp_path):
        store = LocalObjectStore(tmp_path)
        store.put("a/b.txt", b"x")
        store.delete("a/b.txt")
        store.delete("a/b.txt")
        assert not store.exists("a/b.txt")

    def test_put_stream_writes_chunks(self, tmp_path):
        store = LocalObjectStore(tmp_path)
        written = store.put_stream("a/b.bin", iter([b"12345", b"67890"]))
        assert written == 10
        with store.open("a/b.bin") as handle:
            assert handle.read() == b"1234567890"

    def test_put_stream_enforces_the_ceiling_and_cleans_up(self, tmp_path):
        """A rejected upload must not leave a partial object behind."""
        store = LocalObjectStore(tmp_path)
        with pytest.raises(PayloadTooLarge):
            store.put_stream("a/big.bin", iter([b"x" * 10] * 10), max_bytes=25)
        assert not store.exists("a/big.bin")

    def test_a_key_cannot_escape_the_storage_root(self, tmp_path):
        store = LocalObjectStore(tmp_path)
        with pytest.raises(ValueError):
            store.put("../escaped.txt", b"x")
