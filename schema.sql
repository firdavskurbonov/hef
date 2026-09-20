-- Health Expenditure Harmonisation prototype - SQLite schema
--
-- Design intent
-- -------------
-- 1. Raw before canonical. Every source row is stored verbatim as JSON in
--    stg_raw_record with its file, sheet and row number, before any cleaning.
--    Nothing is discarded, so any harmonised figure can be traced to the exact
--    cell it came from and a mapping error can be re-run without re-extraction.
-- 2. One fact table, country-agnostic. Country differences are resolved during
--    harmonisation, not carried into the model as per-country columns.
-- 3. Per-country chart of accounts. Countries use different code lengths and
--    structures (A: 7-digit, B: 6-digit, C: 7-digit) with no canonical chart of accounts, so
--    dim_account is keyed by (country_code, account_code) and the account mapping to
--    SHA/SRHR lives in classification, not in the account dimension.
-- 4. Classification is a separate table, not a column. A record can be
--    classified against several schemes (SHA, SRHR), by several methods, and
--    re-classified later without touching the financial fact. History is kept.
-- 5. Data quality is recorded, not silently corrected. dq_issue is a register
--    attached to the raw row, so "how dirty was this extract?" is answerable.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- reference --

CREATE TABLE IF NOT EXISTS dim_country (
    country_code      TEXT PRIMARY KEY,
    country_name      TEXT NOT NULL,
    primary_currency  TEXT NOT NULL,
    language          TEXT
);

CREATE TABLE IF NOT EXISTS dim_ministry (
    ministry_sk    INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code   TEXT NOT NULL REFERENCES dim_country(country_code),
    ministry_code  TEXT,
    ministry_name  TEXT,
    UNIQUE (country_code, ministry_code)
);

-- Per-country chart of accounts. `source` records whether the label came from
-- the country's own chart of accounts sheet (Country B supplies one) or was observed in the
-- transaction extract itself (A and C do not supply one).
CREATE TABLE IF NOT EXISTS dim_account (
    account_sk     INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code   TEXT NOT NULL REFERENCES dim_country(country_code),
    account_code   TEXT NOT NULL,
    account_label  TEXT,
    source         TEXT,
    UNIQUE (country_code, account_code)
);

-- The two analytical classifications. `source` says whether a code came from
-- the supplied reference CSV or was added / edited through the References tab
-- (config/references.yml) - the supplied files are never modified.
CREATE TABLE IF NOT EXISTS dim_sha (
    sha_code         TEXT PRIMARY KEY,
    sha_description  TEXT NOT NULL,
    notes            TEXT,
    source           TEXT NOT NULL DEFAULT 'supplied'    -- supplied | edited | added
);

CREATE TABLE IF NOT EXISTS dim_srhr (
    srhr_code         TEXT PRIMARY KEY,
    srhr_description  TEXT NOT NULL,
    notes             TEXT,
    source            TEXT NOT NULL DEFAULT 'supplied'
);

-- ------------------------------------------------------------------ staging --

-- One row per ingestion run per file: row counts and the country's own control
-- total (Country B prints a TOTAL line) so load completeness is provable.
CREATE TABLE IF NOT EXISTS ingestion_batch (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code    TEXT NOT NULL REFERENCES dim_country(country_code),
    source_file     TEXT NOT NULL,
    source_system   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    rows_read       INTEGER,
    rows_loaded     INTEGER,
    rows_rejected   INTEGER,
    control_total   REAL,      -- as printed by the source, where supplied
    loaded_total    REAL,      -- sum actually loaded, in source currency
    source_metadata TEXT,      -- verbatim metadata block (Country C JSON)
    notes           TEXT
);

-- Immutable landing zone. payload_json is the untouched source record.
CREATE TABLE IF NOT EXISTS stg_raw_record (
    raw_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES ingestion_batch(batch_id),
    country_code      TEXT NOT NULL REFERENCES dim_country(country_code),
    source_file       TEXT NOT NULL,
    source_sheet      TEXT,
    source_row_no     INTEGER,      -- 1-based row in the source file
    source_record_id  TEXT,         -- the country's own transaction id
    payload_json      TEXT NOT NULL,
    ingested_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_country ON stg_raw_record(country_code);
CREATE INDEX IF NOT EXISTS ix_raw_srcid   ON stg_raw_record(source_record_id);

-- --------------------------------------------------------------------- fact --

-- record_level:
--   STANDALONE  ordinary transaction
--   PARENT      transaction that carries sub-transactions
--   CHILD       a sub-transaction of a PARENT
--
-- is_countable exists because Country C's splits would otherwise be
-- double-counted: where children reconcile to the parent, the children are
-- countable and the parent is not; where they do not reconcile (7 of 59
-- parents), the parent is kept countable, the children are flagged, and the
-- discrepancy is raised as a DQ issue rather than quietly resolved.
CREATE TABLE IF NOT EXISTS fact_expenditure (
    expenditure_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id                INTEGER NOT NULL REFERENCES stg_raw_record(raw_id),
    country_code          TEXT NOT NULL REFERENCES dim_country(country_code),
    source_record_id      TEXT,
    parent_expenditure_id INTEGER REFERENCES fact_expenditure(expenditure_id),
    record_level          TEXT NOT NULL CHECK (record_level IN ('STANDALONE','PARENT','CHILD')),

    txn_date              TEXT,          -- ISO yyyy-mm-dd, NULL if unparseable
    fiscal_year           TEXT,
    ministry_sk           INTEGER REFERENCES dim_ministry(ministry_sk),
    account_sk            INTEGER REFERENCES dim_account(account_sk),
    description           TEXT,          -- cleaned (trimmed, case-normalised)
    description_raw       TEXT,          -- exactly as supplied
    supplier              TEXT,
    payment_method        TEXT,

    amount_original       REAL,
    currency_original     TEXT,
    amount_usd            REAL,
    fx_rate               REAL,
    fx_rate_source        TEXT,

    is_countable          INTEGER NOT NULL DEFAULT 1,
    dq_status             TEXT NOT NULL DEFAULT 'OK',   -- OK | WARN | REJECT
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fact_country ON fact_expenditure(country_code);
CREATE INDEX IF NOT EXISTS ix_fact_account ON fact_expenditure(account_sk);
CREATE INDEX IF NOT EXISTS ix_fact_parent  ON fact_expenditure(parent_expenditure_id);
CREATE INDEX IF NOT EXISTS ix_fact_date    ON fact_expenditure(txn_date);

-- --------------------------------------------------------- classification ----

-- outcome distinguishes "we know this is not an HC code" from "we could not
-- tell", which a single nullable code column cannot express:
--   CLASSIFIED    mapped to a code in the scheme
--   CAPITAL       capital formation - SHA HK, deliberately outside HC
--   ADMIN_INPUT   economic input with no determinable function
--   UNCLASSIFIED  insufficient information - needs a human
CREATE TABLE IF NOT EXISTS classification (
    classification_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    expenditure_id     INTEGER NOT NULL REFERENCES fact_expenditure(expenditure_id),
    scheme             TEXT NOT NULL CHECK (scheme IN ('SHA','SRHR')),
    code               TEXT,
    outcome            TEXT NOT NULL,
    method             TEXT NOT NULL,   -- account_mapping | keyword | fuzzy | manual
    rule_id            TEXT,
    confidence         REAL,
    needs_review       INTEGER NOT NULL DEFAULT 0,
    -- NOT_REQUIRED unless the record was flagged for a human; then PENDING until
    -- an analyst ACCEPTED the automated result or CORRECTED it.
    review_status      TEXT NOT NULL DEFAULT 'PENDING', -- NOT_REQUIRED|PENDING|ACCEPTED|CORRECTED
    reviewed_by        TEXT,
    reviewed_at        TEXT,
    review_note        TEXT,
    classified_at      TEXT NOT NULL,
    is_current         INTEGER NOT NULL DEFAULT 1,      -- history kept on re-run
    explanation        TEXT
);
CREATE INDEX IF NOT EXISTS ix_cls_exp    ON classification(expenditure_id);
CREATE INDEX IF NOT EXISTS ix_cls_scheme ON classification(scheme, code);
CREATE INDEX IF NOT EXISTS ix_cls_review ON classification(needs_review, review_status);

-- ------------------------------------------------------------ data quality ---

CREATE TABLE IF NOT EXISTS dq_issue (
    issue_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER REFERENCES ingestion_batch(batch_id),
    raw_id          INTEGER REFERENCES stg_raw_record(raw_id),
    expenditure_id  INTEGER REFERENCES fact_expenditure(expenditure_id),
    country_code    TEXT,
    rule_code       TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('INFO','WARN','ERROR')),
    field           TEXT,
    raw_value       TEXT,
    message         TEXT NOT NULL,
    detected_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dq_rule    ON dq_issue(rule_code);
CREATE INDEX IF NOT EXISTS ix_dq_country ON dq_issue(country_code, severity);

-- --------------------------------------------------------------- analyst views

-- Countable, current-classification view: the one an analyst should query.
CREATE VIEW IF NOT EXISTS v_expenditure_classified AS
SELECT
    f.expenditure_id,
    f.country_code,
    c.country_name,
    f.source_record_id,
    f.record_level,
    f.txn_date,
    f.fiscal_year,
    m.ministry_name,
    a.account_code,
    a.account_label,
    f.description,
    f.supplier,
    f.amount_original,
    f.currency_original,
    f.amount_usd,
    f.fx_rate,
    f.fx_rate_source,
    f.dq_status,
    sha.code          AS sha_code,
    dsha.sha_description,
    sha.outcome       AS sha_outcome,
    sha.method        AS sha_method,
    sha.confidence    AS sha_confidence,
    sha.needs_review  AS sha_needs_review,
    sha.review_status AS sha_review_status,
    srhr.code         AS srhr_code,
    dsrhr.srhr_description,
    srhr.outcome      AS srhr_outcome,
    srhr.confidence   AS srhr_confidence
FROM fact_expenditure f
JOIN dim_country  c    ON c.country_code = f.country_code
LEFT JOIN dim_ministry m ON m.ministry_sk = f.ministry_sk
LEFT JOIN dim_account  a ON a.account_sk  = f.account_sk
LEFT JOIN classification sha
       ON sha.expenditure_id = f.expenditure_id
      AND sha.scheme = 'SHA' AND sha.is_current = 1
LEFT JOIN classification srhr
       ON srhr.expenditure_id = f.expenditure_id
      AND srhr.scheme = 'SRHR' AND srhr.is_current = 1
LEFT JOIN dim_sha  dsha  ON dsha.sha_code   = sha.code
LEFT JOIN dim_srhr dsrhr ON dsrhr.srhr_code = srhr.code
WHERE f.is_countable = 1;

-- The review queue: everything a human still has to look at, worst first.
CREATE VIEW IF NOT EXISTS v_review_queue AS
SELECT
    f.expenditure_id,
    f.country_code,
    f.source_record_id,
    f.description,
    a.account_code,
    a.account_label,
    f.amount_original,
    f.currency_original,
    f.amount_usd,
    cl.scheme,
    cl.code,
    cl.outcome,
    cl.method,
    cl.confidence,
    cl.explanation,
    cl.review_status
FROM classification cl
JOIN fact_expenditure f ON f.expenditure_id = cl.expenditure_id
LEFT JOIN dim_account a ON a.account_sk = f.account_sk
WHERE cl.is_current = 1
  AND cl.review_status = 'PENDING'
  AND (cl.needs_review = 1 OR cl.outcome = 'UNCLASSIFIED')
ORDER BY f.amount_usd DESC;
