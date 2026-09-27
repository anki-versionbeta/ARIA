"""Where this silo's objects live in the shared store.

A location, not a credential — the bucket and the credential chain stay with the platform, exactly as in
`silos/mfg_atr/location.py`. Kept in its own module because `silo.py` re-exports these two names for the
registry to read, and `palette.py` needs the prefix to key its override object.

PSA writes three kinds of object:

  reports/   the generated PSA assessment (.docx) and the cap-recommendation export
  images/    product photos pulled from the Smartsheet row
  palette/   the cap-palette override — an ASSET, not a run artefact, so it is addressed with
             `asset_key(STORAGE_PREFIX, ...)` rather than a run folder (see `palette.py`)

There is no `input` folder: `ACCEPTS = []`, so nothing is ever uploaded. A run starts from a program
code and a Smartsheet row.
"""

STORAGE_PREFIX = "psa"

STORAGE_FOLDERS = {
    "output": "reports",
    "media": "images",
}
