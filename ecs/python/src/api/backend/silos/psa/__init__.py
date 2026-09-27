"""Product Similarity Assessment — one silo package, shaped to drop into ARIA.

Laid out like `da-backend/silos/mfg_atr/`: the silo contract, an HTTP router, and the domain modules as
flat siblings. The Stage C port copies this folder to `da-backend/silos/psa/`, deletes `dev/` and
`deps.py`, and points `config._resolve()` at the stage context.

    silo.py         LABEL / ACCEPTS / STAGES — what ARIA's folder scan discovers
    router.py       the endpoints (paths are mount-relative, so the prefix is the platform's business)
    models.py       pydantic request/response models
    engines.py      UI-agnostic data layer over the pipeline + the run lock
    deps.py         CurrentUser seam — a dev stub here, ARIA's real dependency there
    config.py       the ONLY place configuration is read  (see its docstring for why)
    paths.py        named accessors for writable locations
    asset_store.py  read-only access to assets/ — the port ARIA fills with ctx.assets
    palette.py      the cap palette: shipped default + override object
    storage/        object-store port + a local implementation
    downloads.py    opaque id → generated file, so no filesystem path reaches a client
    aria.py         config built from the ARIA platform — imported lazily, needs da_platform
    dev/            the local host: uvicorn app, headless runner, environment reader. Present only in
                    the standalone checkout — the port deletes this one directory and nothing else.

    build_db · ingest_smartsheet · ingest_smartsheet_api · extract_docs · ingest_assessment ·
    populate_template · verify · workflow · scope · risk · cap_colors · cap_recommend ·
    cap_queries · cap_export

Nothing is imported here on purpose: `import psa.cap_colors` must not drag in fastapi.
"""
