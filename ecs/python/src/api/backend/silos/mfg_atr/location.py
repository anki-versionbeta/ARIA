"""Where this silo's objects live in the shared bucket.

Ported from `api/s3.py:32-34`. A location, not a credential — the bucket and the
credential chain stay with the platform.

The app being replaced archives every run under

    <base>/{ATR|MFGR}_<processId>_<identifier>/
        pdf/   <id>_preview.pdf    <id>_edited.pdf
        docx/  <id>_preview.docx   <id>_edited.docx

and that layout is a compliance artefact: the preview copy is the clean report and the
edited copy is what the author signed off, both retained. It is preserved exactly, so the
same objects serve the old app and this platform during migration. The `pdf/` and `docx/`
split is part of it, which is why `output` is not a single folder here.

Note `S3_BASE_PREFIX` defaulted to "ATR_MFG/Downloads" in code while the module docstring
claimed "ATR_MFG/extracts/downloads". The code wins: the default in force is the former.
"""

STORAGE_PREFIX = "ATR_MFG"

# The run folder is built per report type and process id by `run_prefix()` in ids.py, so
# the folder map here only names the top-level kinds the platform itself writes.
STORAGE_FOLDERS = {
    "input": "requests",
    "output": "Downloads",
    "media": "extracts",
}
