"""Pipeline orchestration: ingest -> harmonise -> reconcile -> classify.

Two entry points share the same per-country unit of work:

    run()           the command line - every country in sources.yml, fresh DB
    load_country()  one country, one file - what the Ingest tab calls

A country is the unit of replacement. Re-loading a country discards its
previous batch (facts, raw rows, issues and classifications - including any
review decisions), because a re-delivered extract supersedes the old one and
mixing two deliveries of the same period would double-count.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import classify, db, harmonise, ingest


def ensure_countries(conn, sources: dict) -> None:
    """Every country in sources.yml exists in dim_country.

    ref_countries.csv seeds the three supplied countries; a country added
    through the Ingest tab lives only in sources.yml, so the dimension is
    completed from there. Existing rows are left alone.
    """
    for code, spec in sources.items():
        conn.execute(
            "INSERT OR IGNORE INTO dim_country (country_code, country_name, primary_currency,"
            " language) VALUES (?,?,?,?)",
            (code, spec.get("name") or code, spec.get("currency") or "", spec.get("language")),
        )
    conn.commit()


def delete_country_data(conn, country_code: str) -> None:
    """Remove every batch for a country so a fresh load replaces it.

    Order follows the foreign keys: issues and classifications first, then
    child facts before parents (self-reference), then raw rows and batches.
    Dimension rows (ministries, accounts) are kept - they are reference data
    the next load will reuse.
    """
    conn.execute(
        "DELETE FROM dq_issue WHERE country_code=?"
        " OR batch_id IN (SELECT batch_id FROM ingestion_batch WHERE country_code=?)"
        " OR expenditure_id IN (SELECT expenditure_id FROM fact_expenditure WHERE country_code=?)",
        (country_code, country_code, country_code))
    conn.execute(
        "DELETE FROM classification WHERE expenditure_id IN"
        " (SELECT expenditure_id FROM fact_expenditure WHERE country_code=?)", (country_code,))
    conn.execute("DELETE FROM fact_expenditure WHERE country_code=? AND record_level='CHILD'",
                 (country_code,))
    conn.execute("DELETE FROM fact_expenditure WHERE country_code=?", (country_code,))
    conn.execute("DELETE FROM stg_raw_record WHERE country_code=?", (country_code,))
    conn.execute("DELETE FROM ingestion_batch WHERE country_code=?", (country_code,))
    conn.commit()


def load_country(conn, country_code: str, spec: dict, data_dir: Path, fx: dict,
                 replace: bool = True, say: Callable[[str], None] | None = None) -> dict:
    """Ingest, harmonise and reconcile one country's extract.

    Returns the batch figures (rows read/staged, facts, control and loaded
    totals, difference). Classification is a separate step, classify_country,
    so an account mapping change never needs the file re-read. `say` is an
    optional progress printer; the command line passes print, the app nothing.
    """
    if replace:
        delete_country_data(conn, country_code)

    batch_id = ingest.ingest_country(conn, country_code, spec, data_dir)
    batch = conn.execute(
        "SELECT rows_read, rows_loaded, control_total, source_file FROM ingestion_batch"
        " WHERE batch_id=?", (batch_id,)).fetchone()
    if say:
        say(f"  staged {batch['rows_loaded']} of {batch['rows_read']} rows read"
            + (f"; source control total {batch['control_total']:,.2f}"
               if batch["control_total"] else ""))

    stats = harmonise.harmonise_country(conn, country_code, spec, batch_id, fx)
    harmonise.reconcile_batch(conn, batch_id)
    loaded = conn.execute(
        "SELECT loaded_total FROM ingestion_batch WHERE batch_id=?", (batch_id,)
    ).fetchone()["loaded_total"]
    if say:
        say(f"  facts {stats['facts']} (+{stats['children']} sub-transactions), "
            f"countable total {loaded:,.2f} {spec.get('currency')}"
            if loaded else f"  facts {stats['facts']}")

    return {
        "batch_id": batch_id,
        "source_file": batch["source_file"],
        "rows_read": batch["rows_read"],
        "rows_staged": batch["rows_loaded"],
        "facts": stats["facts"],
        "children": stats["children"],
        "control_total": batch["control_total"],
        "loaded_total": loaded,
        "difference": (round(loaded - batch["control_total"], 2)
                       if loaded is not None and batch["control_total"] else None),
    }


def classify_country(conn, country_code: str | None = None,
                     account_mapping: dict | None = None, rules: dict | None = None) -> dict:
    """(Re-)classify one country, or everything when country_code is None.
    Previous results are superseded, never deleted."""
    return classify.classify_all(conn, account_mapping or db.load_account_mapping(), rules or db.load_rules(),
                                 country_code=country_code)


def run(data_dir: Path, db_path: Path, rebuild: bool = True, verbose: bool = True) -> dict:
    """Build the whole warehouse from the command line.

    Fresh schema (unless rebuild=False), reference lists, every country in
    sources.yml in order, then one classification pass over everything.
    A country whose file is missing is skipped, not fatal. Returns a summary
    dict: per-country batch figures, classification stats and the DQ register
    counts.
    """
    def say(msg: str) -> None:
        if verbose:
            print(msg)

    if rebuild:
        db.reset(db_path)

    conn = db.connect(db_path)
    db.init_schema(conn)

    ref = db.load_reference_data(conn, data_dir)
    say(f"reference loaded: {ref}")

    sources = db.load_sources()
    account_mapping = db.load_account_mapping()
    rules = db.load_rules()
    fx = db.load_fx()
    ensure_countries(conn, sources)

    harmonise.register_fx_assumption(conn, fx)

    summary: dict = {"countries": {}, "skipped": [], "reference": ref,
                     "fx_rate_source": fx.get("rate_source")}

    for country_code, spec in sources.items():
        say(f"\n--- {country_code}: {spec.get('name')} ({spec['path']}) ---")
        if not ingest.source_path(spec, data_dir).exists():
            # A country added through the Ingest tab keeps its file under
            # uploads/, which is not versioned. Say so and carry on rather
            # than fail the whole rebuild.
            say(f"  SKIPPED: file not found at {ingest.source_path(spec, data_dir)}")
            summary["skipped"].append(country_code)
            continue
        summary["countries"][country_code] = load_country(
            conn, country_code, spec, data_dir, fx, replace=not rebuild, say=say)

    say("\n--- classification ---")
    cls = classify.classify_all(conn, account_mapping, rules)
    say(f"  {cls}")
    summary["classification"] = cls

    dq = conn.execute(
        "SELECT rule_code, severity, COUNT(*) n FROM dq_issue"
        " GROUP BY rule_code, severity ORDER BY n DESC").fetchall()
    summary["dq"] = [dict(r) for r in dq]
    say("\n--- data quality register ---")
    for r in dq:
        say(f"  {r['severity']:<5} {r['rule_code']:<32} {r['n']}")

    conn.close()
    return summary
