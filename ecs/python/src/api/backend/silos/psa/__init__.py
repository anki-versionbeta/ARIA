"""Product Similarity Assessment — one silo package, shaped to drop into ARIA.

Laid out like `silos/mfg_atr/`: the silo contract, an HTTP router, and the domain modules as flat
siblings. The port into ARIA is done — this IS the ported copy, so `dev/` and `deps.py` (the standalone
uvicorn host and its `CurrentUser` stub) are gone and `router.py` uses the platform's own auth dependency.

    silo.py         LABEL / ACCEPTS / STAGES — what ARIA's folder scan discovers
    router.py       the endpoints (paths are mount-relative, so the prefix is the platform's business)
    runs.py         POST /runs and the run result — the only routes that need da_platform
    models.py       pydantic request/response models
    engines.py      UI-agnostic data layer over the pipeline + the run lock
    config.py       the ONLY place configuration is read  (see its docstring for why)
    paths.py        named accessors for writable locations
    asset_store.py  read-only access to assets/ — see the note below
    palette.py      the cap palette: shipped default + override object
    storage/        object-store port + a local implementation
    downloads.py    opaque id → generated file, so no filesystem path reaches a client
    aria.py         config built from the ARIA platform — imported lazily, needs da_platform
    snapshot.py     capture the sheet once, rebuild deterministically (the core design)

    build_db · ingest_smartsheet · ingest_smartsheet_api · extract_docs · ingest_assessment ·
    populate_template · verify · workflow · scope · risk · cap_colors · cap_recommend ·
    cap_queries · cap_export

Nothing is imported here on purpose: `import da_silos.psa.cap_colors` must not drag in fastapi.

⚠️ `asset_store.py` still reads from this package's `assets/` directory on local disk rather than
delegating to `ctx.assets`, which is what every other silo uses for its templates. The consequence is
that `PSA_template.docx` and `cap_palette.csv` are baked into the image and cannot be swapped per
environment. Recorded as an open item in
`docs/superpowers/plans/2026-09-09-psa-silo-parity.md`.
"""
