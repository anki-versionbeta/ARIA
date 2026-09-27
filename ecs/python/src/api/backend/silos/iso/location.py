"""Where this silo's objects live in the shared bucket.

Its own module so both `silo.py` and `router.py` can use it without importing each
other. A location, not a credential — the bucket, the region and the credential chain
stay with the platform.

The app being replaced hardcodes bucket "ir-doc-authoring", prefix
"ISO_Doc_Generation" and region "us-east-1" in source (backend.py:44-46). Only the
prefix and the managed-folder names stay with the silo, so the same S3 objects serve
the old app and this platform during migration:

    ISO_Doc_Generation/uploads/<run_id>_SOP.pdf
    ISO_Doc_Generation/downloads/<run_id>_<title>_Assessment.docx
    ISO_Doc_Generation/memry/<run_id>_llm.json
    ISO_Doc_Generation/memry/<run_id>_Figure 1 — ....png
"""

STORAGE_PREFIX = "ISO_Doc_Generation"

# The run id is part of the object name because these folders are flat and shared.
# The old app already prefixed its session id for exactly that reason.
STORAGE_FOLDERS = {
    "input": "uploads",
    "output": "downloads",
    "media": "memry",
}
