"""Database access, config files and reference-data loading.

SQLite via the standard library: one file, no server, and plain enough SQL
that the model moves to PostgreSQL without a rewrite. Also home to the YAML
loaders and the writers the interface uses to edit config in place.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_DB = ROOT / "warehouse.db"


def utc_now() -> str:
    """UTC timestamp to the second, for the audit columns."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (or create) the database with dict-style rows and foreign keys on.
    SQLite leaves FK enforcement off unless asked per connection."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql. Everything in it is IF NOT EXISTS, so this is safe to
    run on a database that already has data."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def reset(db_path: Path | str = DEFAULT_DB) -> None:
    """Delete the database file so a run starts clean.

    The pipeline rebuilds rather than upserts: at this size a full rebuild is
    reproducible and there is no partial state to reason about. Incremental
    loads are a production item.
    """
    p = Path(db_path)
    if not p.exists():
        return
    # Windows: a sync client or antivirus can hold a freshly written file for
    # a moment. Retry briefly before giving up with the real error.
    import time
    for attempt in range(10):
        try:
            p.unlink()
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


# ------------------------------------------------------------------ config ----
# Read fresh on every call, not cached: the interface writes these files and
# the next read has to see the change.

def load_yaml(name: str) -> dict:
    """Parse one file under config/."""
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def load_sources() -> dict:
    """sources.yml -> {country_code: spec}."""
    return load_yaml("sources.yml")["countries"]


def load_account_mapping() -> dict:
    """account_mapping.yml -> {country_code: {account_code: entry}}."""
    return load_yaml("account_mapping.yml")["countries"]


def load_rules() -> dict:
    """rules.yml as-is: settings, sha_rules, srhr_rules, injection_patterns."""
    return load_yaml("rules.yml")


def load_fx() -> dict:
    """fx_rates.yml as-is: rates, rate_source, period, authoritative."""
    return load_yaml("fx_rates.yml")


# ------------------------------------------------------------ config writers --
# The YAML files carry the reasoning behind every setting as comments, and a
# round trip through the parser would drop them. So the writers edit the file
# text: a new country is appended, an existing block is replaced in place.
# The block that is written loses any comments it had; everything else keeps
# them. Each write is stamped so the file shows what was done by hand and what
# came from the interface.

_COUNTRY_KEY = re.compile(r"^  ([A-Za-z0-9_]+):\s*(#.*)?$", re.MULTILINE)


def _dump_block(key: str, block: dict) -> str:
    """One `  KEY:` block, indented to sit under a top-level `countries:`."""
    text = yaml.safe_dump({key: block}, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)
    return "".join(("  " + line) if line.strip() else line for line in text.splitlines(True))


def _replace_or_append_block(text: str, key: str, block_text: str, stamp: str) -> str:
    """Replace the `  KEY:` block under `countries:` or append a new one."""
    top = re.search(r"^countries:\s*$", text, re.MULTILINE)
    origin = top.end() if top else 0
    starts = [(m.start(), m.group(1)) for m in _COUNTRY_KEY.finditer(text) if m.start() > origin]
    for i, (pos, k) in enumerate(starts):
        if k == key:
            end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
            # keep a blank line and any comment banner that precedes the next block
            body = text[pos:end].rstrip() + "\n"
            replacement = f"  # ---- {stamp} ----\n{block_text}"
            return text[:pos] + replacement + text[pos + len(body):]
    return text.rstrip("\n") + f"\n\n  # ---- {stamp} ----\n{block_text}"


def append_source(country_code: str, block: dict, path: Path | None = None) -> Path:
    """Add or replace a country's entry in sources.yml."""
    path = path or (CONFIG_DIR / "sources.yml")
    text = path.read_text(encoding="utf-8")
    text = _replace_or_append_block(
        text, country_code, _dump_block(country_code, block),
        f"{country_code} added via the Ingest tab, {utc_now()[:10]}")
    path.write_text(text, encoding="utf-8")
    return path


def upsert_account_mapping(country_code: str, entries: dict, path: Path | None = None) -> Path:
    """Write a country's account mapping entries to account_mapping.yml (whole block)."""
    path = path or (CONFIG_DIR / "account_mapping.yml")
    text = path.read_text(encoding="utf-8")
    text = _replace_or_append_block(
        text, country_code, _dump_block(country_code, entries),
        f"{country_code} account mapping saved via the Account mapping tab, {utc_now()[:10]}")
    path.write_text(text, encoding="utf-8")
    return path


def upsert_fx_rate(currency: str, rate: float, path: Path | None = None,
                   stamp: str = "added via the Ingest tab") -> Path:
    """Set one currency's rate under `rates:` in fx_rates.yml."""
    path = path or (CONFIG_DIR / "fx_rates.yml")
    text = path.read_text(encoding="utf-8")
    currency = currency.upper()
    line = f"  {currency}: {rate:g}      # {stamp}, {utc_now()[:10]}\n"
    existing = re.compile(rf"^  {currency}:.*\n", re.MULTILINE)
    if existing.search(text):
        text = existing.sub(line, text, count=1)
    else:
        m = re.search(r"^rates:\s*\n", text, re.MULTILINE)
        if not m:
            raise ValueError(f"{path.name}: no 'rates:' section")
        text = text[:m.end()] + line + text[m.end():]
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------- reference loading --

def _read_csv(path: Path) -> list[dict]:
    """Rows of a CSV as dicts. utf-8-sig so a BOM does not end up in the first header."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_reference_data(conn: sqlite3.Connection, data_dir: Path) -> dict[str, int]:
    """Load ref_countries, ref_sha_classification and ref_srhr_classification
    from the data directory, then lay config/references.yml over them.
    Returns row counts per table."""
    counts: dict[str, int] = {}

    rows = _read_csv(data_dir / "ref_countries.csv")
    conn.executemany(
        "INSERT OR REPLACE INTO dim_country "
        "(country_code, country_name, primary_currency, language) VALUES (?,?,?,?)",
        [(r["country_code"], r["country_name"], r["primary_currency"], r.get("language"))
         for r in rows],
    )
    counts["dim_country"] = len(rows)

    rows = _read_csv(data_dir / "ref_sha_classification.csv")
    conn.executemany(
        "INSERT OR REPLACE INTO dim_sha (sha_code, sha_description, notes, source)"
        " VALUES (?,?,?,'supplied')",
        [(r["sha_code"], r["sha_description"], r.get("notes") or None) for r in rows],
    )
    counts["dim_sha"] = len(rows)

    rows = _read_csv(data_dir / "ref_srhr_classification.csv")
    conn.executemany(
        "INSERT OR REPLACE INTO dim_srhr (srhr_code, srhr_description, notes, source)"
        " VALUES (?,?,?,'supplied')",
        [(r["srhr_code"], r["srhr_description"], r.get("notes") or None) for r in rows],
    )
    counts["dim_srhr"] = len(rows)

    conn.commit()
    counts["reference_overrides"] = apply_reference_overrides(conn)
    return counts


# ------------------------------------------------------ reference overrides --
# The SHA and SRHR lists come from the supplied CSVs. What an analyst adds or
# edits in the References tab is kept in config/references.yml and laid over
# the CSVs at every load: the supplied files are never modified, a rebuild
# keeps the changes, and the diff against the original list is the file.

REFERENCE_SCHEMES = {
    "sha": ("dim_sha", "sha_code", "sha_description"),
    "srhr": ("dim_srhr", "srhr_code", "srhr_description"),
}


REFERENCE_SECTIONS = (*REFERENCE_SCHEMES, "countries")


def load_reference_overrides(path: Path | None = None) -> dict:
    """references.yml as {sha: {code: entry}, srhr: {...}, countries: {...}}.
    Every section is present, empty if the file has none or does not exist."""
    path = path or (CONFIG_DIR / "references.yml")
    if not path.exists():
        return {section: {} for section in REFERENCE_SECTIONS}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {section: (data.get(section) or {}) for section in REFERENCE_SECTIONS}


def save_reference_overrides(overrides: dict, path: Path | None = None) -> Path:
    """Write the whole overrides file. Unlike the other config writers this one
    regenerates the file, because it is only ever written by the interface."""
    path = path or (CONFIG_DIR / "references.yml")
    body = yaml.safe_dump({k: overrides.get(k) or {} for k in REFERENCE_SECTIONS},
                          sort_keys=True, allow_unicode=True, width=100)
    path.write_text(
        "# Additions and edits to the supplied SHA / SRHR reference lists, written by\n"
        "# the References tab. Applied on top of candidate_data/ref_*.csv at every load;\n"
        "# the supplied files are never modified. A code listed here that also exists in\n"
        "# the CSV is an edit; one that does not is an addition. Countries added here\n"
        "# have no extract yet - the Ingest tab's wizard picks the code up when it arrives.\n"
        f"# Last written {utc_now()}.\n\n" + body, encoding="utf-8")
    return path


def apply_reference_overrides(conn, overrides: dict | None = None) -> int:
    """Upsert the overrides into dim_sha, dim_srhr and dim_country. A code that
    already exists from the CSV is marked source='edited', a new one 'added'.
    Returns rows written."""
    overrides = overrides if overrides is not None else load_reference_overrides()
    n = 0
    for scheme, (table, code_col, desc_col) in REFERENCE_SCHEMES.items():
        for code, entry in (overrides.get(scheme) or {}).items():
            entry = entry or {}
            exists = conn.execute(
                f"SELECT source FROM {table} WHERE {code_col}=?", (code,)).fetchone()
            source = "edited" if exists and exists["source"] in ("supplied", "edited") else "added"
            conn.execute(
                f"INSERT INTO {table} ({code_col}, {desc_col}, notes, source) VALUES (?,?,?,?)"
                f" ON CONFLICT({code_col}) DO UPDATE SET {desc_col}=excluded.{desc_col},"
                f" notes=excluded.notes, source=excluded.source",
                (str(code), entry.get("description") or "", entry.get("notes") or None, source))
            n += 1
    for code, entry in (overrides.get("countries") or {}).items():
        entry = entry or {}
        conn.execute(
            "INSERT INTO dim_country (country_code, country_name, primary_currency, language)"
            " VALUES (?,?,?,?) ON CONFLICT(country_code) DO UPDATE SET country_name=excluded.country_name,"
            " primary_currency=excluded.primary_currency, language=excluded.language",
            (str(code).upper(), entry.get("name") or str(code), (entry.get("currency") or "").upper(),
             entry.get("language")))
        n += 1
    conn.commit()
    return n


def country_has_data(conn, code: str) -> int:
    """Number of facts loaded for the country; 0 means it can be deleted."""
    return conn.execute("SELECT COUNT(*) FROM fact_expenditure WHERE country_code=?", (code,)).fetchone()[0]


def delete_country(conn, code: str) -> None:
    """Remove a country and its ministry/account rows. Only for a country with
    no facts - the References tab checks country_has_data first."""
    conn.execute("DELETE FROM dim_ministry WHERE country_code=?", (code,))
    conn.execute("DELETE FROM dim_account WHERE country_code=?", (code,))
    conn.execute("DELETE FROM dim_country WHERE country_code=?", (code,))
    conn.commit()


def reference_in_use(conn, scheme: str, code: str) -> int:
    """How many current classifications carry this code."""
    return conn.execute(
        "SELECT COUNT(*) FROM classification WHERE is_current=1 AND scheme=? AND code=?",
        (scheme.upper(), code)).fetchone()[0]


def delete_reference(conn, scheme: str, code: str) -> None:
    """Remove one SHA or SRHR code. Callers check reference_in_use first;
    a code with current classifications must not be deleted."""
    table, code_col, _ = REFERENCE_SCHEMES[scheme]
    conn.execute(f"DELETE FROM {table} WHERE {code_col}=?", (code,))
    conn.commit()


# --------------------------------------------------------------- dimensions ---

def get_or_create_ministry(conn, country_code: str, code: str | None, name: str | None) -> int | None:
    """Surrogate key for a ministry, inserted on first sight. Keyed on
    (country, code); `IS ?` so a NULL code still matches its own row.
    None when the record names no ministry at all."""
    if not code and not name:
        return None
    cur = conn.execute(
        "SELECT ministry_sk FROM dim_ministry WHERE country_code=? AND ministry_code IS ?",
        (country_code, code),
    )
    row = cur.fetchone()
    if row:
        return row["ministry_sk"]
    cur = conn.execute(
        "INSERT INTO dim_ministry (country_code, ministry_code, ministry_name) VALUES (?,?,?)",
        (country_code, code, name),
    )
    return cur.lastrowid


def get_or_create_account(conn, country_code: str, code: str | None,
                          label: str | None, source: str) -> int | None:
    """Per-country account dimension.

    No canonical chart of accounts exists across the three countries, so the
    account is keyed by (country, code). Where a country supplies its own chart of accounts
    (Country B's Plan_comptable sheet) the authoritative label is loaded first
    and observed transaction labels do not overwrite it.
    """
    if not code:
        return None
    cur = conn.execute(
        "SELECT account_sk, account_label, source FROM dim_account "
        "WHERE country_code=? AND account_code=?",
        (country_code, code),
    )
    row = cur.fetchone()
    if row:
        # Fill a missing label, but never let a transaction description
        # overwrite a label that came from the country's own chart of accounts.
        if not row["account_label"] and label and row["source"] != "country_chart_of_accounts":
            conn.execute("UPDATE dim_account SET account_label=?, source=? WHERE account_sk=?",
                         (label, source, row["account_sk"]))
        return row["account_sk"]
    cur = conn.execute(
        "INSERT INTO dim_account (country_code, account_code, account_label, source) VALUES (?,?,?,?)",
        (country_code, code, label, source),
    )
    return cur.lastrowid


def load_country_chart_of_accounts(conn, country_code: str, pairs: Iterable[tuple[str, str]]) -> int:
    """Load a country-supplied chart of accounts as authoritative labels."""
    n = 0
    for code, label in pairs:
        if not code:
            continue
        conn.execute(
            "INSERT INTO dim_account (country_code, account_code, account_label, source) "
            "VALUES (?,?,?,'country_chart_of_accounts') "
            "ON CONFLICT(country_code, account_code) "
            "DO UPDATE SET account_label=excluded.account_label, source='country_chart_of_accounts'",
            (country_code, str(code).strip(), str(label).strip() if label else None),
        )
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- dq helper ---

def add_issue(conn, *, rule_code: str, severity: str, message: str,
              country_code: str | None = None, batch_id: int | None = None,
              raw_id: int | None = None, expenditure_id: int | None = None,
              field: str | None = None, raw_value: str | None = None) -> None:
    """Write one row to the data-quality register. The only way anything in the
    pipeline reports a problem. Link it to whatever is known - batch, raw row,
    fact - so the Data quality tab can drill to the source. raw_value is cut
    at 500 chars; the full value is still in the staged payload. No commit:
    the caller's transaction owns it."""
    conn.execute(
        "INSERT INTO dq_issue (batch_id, raw_id, expenditure_id, country_code, rule_code,"
        " severity, field, raw_value, message, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (batch_id, raw_id, expenditure_id, country_code, rule_code, severity,
         field, (str(raw_value)[:500] if raw_value is not None else None), message, utc_now()),
    )
