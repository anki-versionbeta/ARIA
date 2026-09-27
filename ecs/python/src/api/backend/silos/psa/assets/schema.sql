-- PSA consolidated database — canonical schema (Phase 1, Smartsheet-scoped)
-- Source-agnostic: Smartsheet fills the core tables now; PDD/TPP add later with NO structural change
-- (doc_source/doc_attribute/attribute_map are generic; PDD/TPP-destined product columns pre-provisioned).
PRAGMA foreign_keys = ON;

-- ---------- reference / dimension ----------
CREATE TABLE site (
    site_id    INTEGER PRIMARY KEY,
    site_code  TEXT UNIQUE NOT NULL,
    site_name  TEXT,
    location   TEXT,
    site_type  TEXT            -- mfr | pkging | both
);

CREATE TABLE vendor (
    vendor_id   INTEGER PRIMARY KEY,
    vendor_name TEXT UNIQUE NOT NULL,
    vendor_type TEXT
);

-- ---------- core: product-presentation grain ----------
CREATE TABLE product (
    product_id            INTEGER PRIMARY KEY,
    source_row            INTEGER,        -- Smartsheet row (traceability)
    program_no            TEXT,
    program_name          TEXT,
    route_of_admin        TEXT,
    strength              TEXT,
    target_fill_volume_ml TEXT,
    vial_container_size   TEXT,
    product_color         TEXT,
    modality              TEXT,           -- Smartsheet "State" (Biologic/Liquid/Lyo…)
    form                  TEXT,
    status                TEXT,           -- lifecycle: Pipeline/Commercial/Discontinued
    list_number           TEXT,
    launch_year           TEXT,
    shape                 TEXT,
    marking               TEXT,
    batch_type            TEXT,
    cap_vendor_id         INTEGER REFERENCES vendor(vendor_id),
    picture1              TEXT,
    picture2              TEXT,
    contact               TEXT,
    link_dpsa             TEXT,
    link_pdd              TEXT,
    link_tpp              TEXT,
    last_modified         TEXT,
    last_updated_by       TEXT,
    verified_flag         TEXT,
    dp_mfr_site_raw       TEXT,           -- raw multi-valued cell, kept for fidelity
    dp_pkging_site_raw    TEXT,
    -- NOTE: (program_no, strength, vial_container_size) is NOT unique in the real data
    -- (e.g. ABBV-400 has 4 rows differing only by Commercial/Clinical + cap color), so the
    -- grain is one row per Smartsheet data row, keyed by the surrogate id + source_row.
    -- PDD/TPP-destined columns, pre-provisioned (NULL until those sources integrate) --
    inn_name                   TEXT,      -- TPP
    per_vial_strength          TEXT,      -- TPP
    mfr_site_address           TEXT,      -- PDD
    site_location              TEXT,      -- PDD
    dosage_form_type           TEXT,      -- PDD
    strength_range             TEXT,      -- PDD
    shelf_life                 TEXT,      -- PDD
    intended_markets           TEXT,      -- PDD
    regulatory_impact          TEXT,      -- PDD
    excipient_safety_statement TEXT,      -- PDD
    primary_packaging_desc     TEXT,      -- PDD
    UNIQUE (source_row)
);

-- N–N: products have multi-valued mfr/pkging sites ("ABB\nBSP", "AP16\nLU")
CREATE TABLE product_site (
    product_id INTEGER NOT NULL REFERENCES product(product_id),
    site_id    INTEGER NOT NULL REFERENCES site(site_id),
    role       TEXT NOT NULL,            -- mfr | pkging
    PRIMARY KEY (product_id, site_id, role)
);

-- 1–N: a product-presentation can have several batch-type → cap-color rows
CREATE TABLE cap_color (
    cap_color_id   INTEGER PRIMARY KEY,
    product_id     INTEGER NOT NULL REFERENCES product(product_id),
    batch_type     TEXT,
    cap_color_name TEXT,
    color_code     TEXT,
    vendor_id      INTEGER REFERENCES vendor(vendor_id)
);

-- site capability matrix — NOT in Smartsheet; SME-sourced later (blocks F-2). Empty for now.
CREATE TABLE site_capability (
    site_id     INTEGER NOT NULL REFERENCES site(site_id),
    dosage_form TEXT,
    modality    TEXT,
    vial_size   TEXT,
    can_produce INTEGER,
    source      TEXT
);

-- ---------- Phase-2 analysis tables (created now, populated later) ----------
CREATE TABLE risk_assessment (
    assessment_id            INTEGER PRIMARY KEY,
    subject_product_id       INTEGER REFERENCES product(product_id),
    comparator_product_id    INTEGER REFERENCES product(product_id),
    site_id                  INTEGER REFERENCES site(site_id),
    assessed_date            TEXT,
    num_distinguishing_diffs INTEGER,
    risk_level               TEXT,        -- Low | Med | High
    rationale                TEXT,
    mitigation               TEXT,
    status                   TEXT
);

CREATE TABLE risk_attribute_cmp (
    assessment_id     INTEGER REFERENCES risk_assessment(assessment_id),
    attribute         TEXT,
    subject_value     TEXT,
    comparator_value  TEXT,
    is_distinguishing INTEGER,
    weight            REAL
);

-- ---------- source-agnostic document capture (PDD/TPP/other) ----------
CREATE TABLE doc_source (
    doc_id       INTEGER PRIMARY KEY,
    product_id   INTEGER REFERENCES product(product_id),
    doc_role     TEXT,                    -- PDD | TPP | other
    source_type  TEXT,                    -- pdf | docx | pptx
    source_file  TEXT,
    locator_meta TEXT,
    ingested_at  TEXT
);

CREATE TABLE doc_attribute (
    attr_id           INTEGER PRIMARY KEY,
    doc_id            INTEGER REFERENCES doc_source(doc_id),
    row_index         INTEGER,
    attribute_label   TEXT,
    attribute_key     TEXT,
    variant           TEXT,               -- commercial | clinical | both | na
    requirement_text  TEXT,
    value_text        TEXT,
    normalized_target TEXT,
    confidence        REAL,
    source_locator    TEXT,
    status            TEXT                 -- mapped | unmapped | review
);

CREATE TABLE attribute_map (
    attribute_key TEXT PRIMARY KEY,
    target_table  TEXT,
    target_column TEXT,
    transform     TEXT,
    is_list       INTEGER
);

CREATE TABLE formulation_ingredient (
    id                INTEGER PRIMARY KEY,
    product_id        INTEGER REFERENCES product(product_id),
    variant           TEXT,
    ingredient_name   TEXT,
    compendial_status TEXT,
    quantity          TEXT,
    doc_attribute_id  INTEGER REFERENCES doc_attribute(attr_id)
);

-- ---------- provenance & validation (polymorphic side tables) ----------
CREATE TABLE field_provenance (
    prov_id           INTEGER PRIMARY KEY,
    entity            TEXT,                -- product | cap_color | …
    entity_id         INTEGER,
    field_name        TEXT,
    source_doc        TEXT,                -- Smartsheet | PDD | TPP | Derived | None
    source_locator    TEXT,
    match_type        TEXT,                -- Exact | Transformed | Derived | NoSource
    extraction_method TEXT,                -- auto | llm | manual | rule
    confidence        REAL,
    extracted_at      TEXT
);

CREATE TABLE validation_rule (
    rule_id    INTEGER PRIMARY KEY,
    field_name TEXT,
    rule_type  TEXT,                       -- required | reference | range
    params     TEXT,
    defined_by TEXT
);

CREATE TABLE validation_result (
    val_id       INTEGER PRIMARY KEY,
    entity       TEXT,
    entity_id    INTEGER,
    field_name   TEXT,
    rule_id      INTEGER REFERENCES validation_rule(rule_id),
    status       TEXT,
    severity     TEXT,
    message      TEXT,
    validated_at TEXT
);

-- ---------- template population map (drives populate_template.py) ----------
-- Adding PDD/TPP later flips a field's `source` from a placeholder tag to a real resolver
-- with NO populator code change.
CREATE TABLE template_field_map (
    field_id        INTEGER PRIMARY KEY,
    part            TEXT,                  -- A | B | C | D1 | D2 | D3 | E1 | E2 | E3
    field_label     TEXT,
    table_index     INTEGER,               -- docx table index (0-based)
    row_index       INTEGER,               -- row in that table
    value_col       INTEGER,               -- column to write the value into
    source          TEXT,                  -- smartsheet | derived | pdd | tpp | analysis | execution
    resolver        TEXT,                  -- product column name | rule key | NULL
    placeholder_tag TEXT,                  -- e.g. [PENDING: TPP]
    notes           TEXT
);

-- ---------- reporting view (F-5/F-7) ----------
CREATE VIEW v_product_similarity AS
SELECT  p.product_id, p.program_no, p.program_name, p.status, p.form,
        p.vial_container_size, p.product_color, p.marking, p.shape,
        cc.batch_type, cc.cap_color_name, cc.color_code,
        v.vendor_name,
        ps.role AS site_role, s.site_code, s.site_name, s.location
FROM product p
LEFT JOIN cap_color    cc ON cc.product_id = p.product_id
LEFT JOIN vendor       v  ON v.vendor_id   = p.cap_vendor_id
LEFT JOIN product_site ps ON ps.product_id = p.product_id
LEFT JOIN site         s  ON s.site_id     = ps.site_id;
