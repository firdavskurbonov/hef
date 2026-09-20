"""Staging -> canonical facts.

All country-specific cleaning lives here, driven by the `conventions` block in
sources.yml. The rule throughout: never silently repair a value. Either it
parses, or it is loaded as NULL/flagged and an entry is written to the
data-quality register so the problem stays visible.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from typing import Iterable

from . import db

# ---------------------------------------------------------------- cleaning ----


def normalise_text(value: str | None) -> str | None:
    """Collapse whitespace; blank becomes NULL. Case is never changed here.

    Case is deliberately left alone at row level: any per-value rule
    ("title-case ALL-CAPS") destroys acronyms (HIV -> Hiv) and cannot see that
    "medical drugs" and "MEDICAL DRUGS" are the same thing. The case-variant
    problem is solved per batch instead, in `canonicalise_descriptions`, once
    all spellings are known.
    """
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def clean_code(value) -> str | None:
    """Identifiers (ministry codes, payment methods) are trimmed, never re-cased.

    MOE must stay MOE and BANK_TRANSFER must stay BANK_TRANSFER: a code is a
    key into somebody else's system, and altering it is a silent repair.
    """
    return normalise_text(value)


def canonical_spellings(values: Iterable[str]) -> dict[str, str]:
    """Map each case/accent-insensitive key to its most frequent spelling.

    Country A ships every description in three casings - "HIV test kits and
    ARVs" (109 rows), "HIV TEST KITS AND ARVS" (7) and "hiv test kits and arvs"
    (9). The extract itself tells us which spelling is canonical: the one the
    country used most. Choosing it preserves acronyms and makes the 60 raw
    spellings group as the 20 descriptions they are.
    """
    groups: dict[str, Counter] = defaultdict(Counter)
    for v in values:
        if v:
            groups[fold(v)][v] += 1
    return {key: cnt.most_common(1)[0][0] for key, cnt in groups.items()}


def fold(value: str | None) -> str:
    """Accent-stripped, lowercase form used for pattern matching only."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def parse_amount(raw, conv: dict) -> tuple[float | None, str | None]:
    """Parse an amount, deciding each separator's role from the value itself.

    A single declared convention is not enough, because Country B's montant_XOF
    column mixes two mutually contradictory ones:

        11548910,00        comma is the DECIMAL separator (French)
        31,073,710 FCFA    comma is the THOUSANDS separator
        3 246 565 FCFA     space thousands, currency suffix
        "29,998.63"        Country A: comma thousands, dot decimal, quoted

    One declared convention misreads about 130 Country B rows: with "comma is
    the decimal" the load came out 11.7m XOF short of the printed control
    total. So the role is inferred per value from the digits around it:

      * both separators present -> the rightmost is the decimal separator
      * one separator appearing more than once -> thousands
      * one separator with exactly 3 trailing digits -> thousands if the
        leading group is 1-3 digits ("781,311"), otherwise decimal
        ("1234567,890"). When that separator is also the source's declared
        decimal separator, the value still parses as thousands (the structure
        is unambiguous) but is reported as ambiguous, because a French reader
        could dispute it.
      * one separator with 1-2 trailing digits -> decimal

    Returns (value, problem). `problem` is None, "missing", "unparseable:..."
    or "ambiguous:..." - the last one means the value parsed but under an
    assumption that should be on the record.
    """
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return None, "missing"
    if isinstance(raw, (int, float)):
        return float(raw), None

    s = str(raw).strip().strip('"').strip()
    for suffix in conv.get("strip_suffixes", []) or []:
        s = re.sub(rf"\s*{re.escape(suffix)}\s*$", "", s, flags=re.IGNORECASE)
    # ordinary, non-breaking, narrow and thin spaces all turn up as thousands marks
    for sp in (" ", "\u00a0", "\u202f", "\u2009"):
        s = s.replace(sp, "")
    s = s.strip()

    neg = s.startswith("-")
    if neg:
        s = s[1:]

    declared_dec = conv.get("decimal_separator", ".")
    problem = None
    n_dot, n_comma = s.count("."), s.count(",")

    if n_dot and n_comma:
        dec_sep = "." if s.rfind(".") > s.rfind(",") else ","
        thou_sep = "," if dec_sep == "." else "."
        s = s.replace(thou_sep, "").replace(dec_sep, ".")
    elif n_dot or n_comma:
        sep = "." if n_dot else ","
        count = n_dot or n_comma
        tail = len(s) - s.rfind(sep) - 1
        if count > 1:
            s = s.replace(sep, "")                     # certainly thousands
        elif tail == 3:
            # One separator, three digits after it: decided structurally rather
            # than by the declared convention. A thousands group must have a
            # leading group of 1-3 digits ("781,311"), so anything longer
            # before the separator cannot be one and must be a decimal
            # ("1234567,890").
            head = len(s) - tail - 1
            if head <= 3:
                s = s.replace(sep, "")                 # thousands group
                if sep == declared_dec:
                    # A French reader could also read this as a decimal, so the
                    # reading is recorded even though the structure is clear.
                    problem = f"ambiguous:{str(raw)[:40]}"
            else:
                s = s.replace(sep, ".")                # decimal
        elif tail in (1, 2):
            s = s.replace(sep, ".")                    # certainly decimal
        else:
            s = s.replace(sep, "")

    if not re.fullmatch(r"\d*\.?\d+", s):
        return None, f"unparseable:{str(raw)[:40]}"
    try:
        return (-float(s) if neg else float(s)), problem
    except ValueError:
        return None, f"unparseable:{str(raw)[:40]}"


def parse_date(raw, conv: dict) -> tuple[str | None, str | None]:
    """Parse a date using only the formats the source declares.

    Deliberately not a permissive parser: guessing between dd/mm and mm/dd is
    how real financial data silently acquires wrong months. If the declared
    formats do not match, the value is rejected and flagged.
    """
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return None, "missing"
    if isinstance(raw, datetime):
        return raw.date().isoformat(), None
    s = str(raw).strip()
    if "T" in s:
        s = s.split("T")[0]
    for fmt in conv.get("date_formats", []):
        try:
            return datetime.strptime(s, fmt).date().isoformat(), None
        except ValueError:
            continue
    try:  # ISO is unambiguous, so accept it as a last resort
        return datetime.fromisoformat(s).date().isoformat(), None
    except ValueError:
        return None, f"unparseable:{s[:40]}"


# ---------------------------------------------------------------------- FX ----

def convert_to_usd(amount: float | None, currency: str | None, fx: dict):
    """Convert to USD using the declared reference table.

    Returns (amount_usd, rate, rate_source). An unknown currency yields no USD
    amount rather than an assumed one - the record still loads and still counts
    in its own currency, it simply cannot enter a cross-country total.
    """
    if amount is None or not currency:
        return None, None, None
    rate = (fx.get("rates") or {}).get(currency.upper())
    if not rate:
        return None, None, None
    return round(amount / float(rate), 2), float(rate), fx.get("rate_source")


def register_fx_assumption(conn, fx: dict) -> None:
    """One register entry saying whether USD rests on assumed rates.

    Re-created on every load and every rate change, so the register always
    describes the rate table in force."""
    conn.execute("DELETE FROM dq_issue WHERE rule_code='FX_ASSUMED_RATES'")
    if not fx.get("authoritative", False):
        db.add_issue(
            conn, rule_code="FX_ASSUMED_RATES", severity="WARN",
            message=(f"USD figures use assumed reference rates "
                     f"({fx.get('rate_source')}, {fx.get('period')}). "
                     f"Not authoritative - replace with UN Operational Rates "
                     f"before publication."),
        )
    conn.commit()


# --------------------------------------------------------------- harmonise ----

def _mapped(rec: dict, field_map: dict, key: str):
    """Raw value of canonical field `key`, via the source's field_map; None if
    the source has no column for it."""
    src = field_map.get(key)
    return rec.get(src) if src else None


def harmonise_country(conn, country_code: str, spec: dict, batch_id: int, fx: dict) -> dict:
    """Turn one batch's staged rows into fact_expenditure rows.

    Per row: parse amount and date under the source's conventions, resolve
    ministry and account to dimension keys, convert to USD, write the fact,
    then load any nested sub-transactions. Every parse problem becomes a
    dq_issue row rather than a repaired value. Returns counts of facts,
    children, warnings and rejects.
    """
    conv = spec.get("conventions", {}) or {}
    fmap = spec["field_map"]
    default_currency = spec.get("currency")
    stats = {"facts": 0, "children": 0, "warn": 0, "reject": 0}

    # The declared reporting period is asserted, not observed. A posting date
    # outside it - or after the source's own extraction timestamp, where the
    # source records one - cannot be right, whatever the row says.
    period = spec.get("period") or {}
    period_start, period_end = period.get("start"), period.get("end")
    extracted_at = _extraction_date(conn, batch_id, spec)

    rows = conn.execute(
        "SELECT raw_id, source_row_no, source_record_id, payload_json"
        " FROM stg_raw_record WHERE batch_id=? ORDER BY raw_id", (batch_id,)
    ).fetchall()

    # Country A reuses one TXN_ID for two different transactions. Neither is a
    # duplicate of the other, so both are kept - but the collision is recorded,
    # because an id that is not unique cannot be used as a business key.
    seen_ids: dict[str, int] = {}

    for row in rows:
        rec = json.loads(row["payload_json"])
        raw_id = row["raw_id"]

        amount, amt_problem = parse_amount(_mapped(rec, fmap, "amount"), conv)
        txn_date, date_problem = parse_date(_mapped(rec, fmap, "txn_date"), conv)
        desc_raw = _mapped(rec, fmap, "description")
        description = normalise_text(desc_raw)
        currency = (_mapped(rec, fmap, "currency") or default_currency or "").upper() or None

        ministry_sk = db.get_or_create_ministry(
            conn, country_code,
            clean_code(_mapped(rec, fmap, "ministry_code")),
            normalise_text(_mapped(rec, fmap, "ministry_name")),
        )
        account_code = _mapped(rec, fmap, "account_code")
        account_sk = db.get_or_create_account(
            conn, country_code,
            str(account_code).strip() if account_code is not None else None,
            description, "observed_in_extract",
        )

        amount_usd, fx_rate, fx_src = convert_to_usd(amount, currency, fx)

        dq_status = "OK"
        if amt_problem == "missing":
            dq_status = "WARN"
            stats["warn"] += 1
            db.add_issue(conn, rule_code="AMOUNT_MISSING", severity="WARN",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field=fmap.get("amount"), raw_value=_mapped(rec, fmap, "amount"),
                         message="Amount is empty; row loaded with NULL amount and excluded from totals.")
        elif amt_problem and amt_problem.startswith("ambiguous"):
            # Parsed, but a French reader could dispute it: a single comma with
            # three trailing digits in a column whose declared decimal separator
            # is the comma. Structure says thousands (781,311 -> 781311), and
            # that reading is what reconciles to the printed total. The value
            # counts; the assumption is on the record.
            db.add_issue(conn, rule_code="AMOUNT_SEPARATOR_AMBIGUOUS", severity="WARN",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field=fmap.get("amount"), raw_value=_mapped(rec, fmap, "amount"),
                         message=(f"Single '{conv.get('decimal_separator')}' with three trailing "
                                  f"digits in a column that declares it as the decimal separator; "
                                  f"read structurally as thousands -> {amount!r}. "
                                  f"Confirm with the country."))
        elif amt_problem:
            dq_status = "WARN"
            stats["warn"] += 1
            db.add_issue(conn, rule_code="AMOUNT_UNPARSEABLE", severity="ERROR",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field=fmap.get("amount"), raw_value=_mapped(rec, fmap, "amount"),
                         message=f"Amount could not be parsed ({amt_problem}).")
        if date_problem:
            dq_status = "WARN" if dq_status == "OK" else dq_status
            db.add_issue(conn, rule_code="DATE_UNPARSEABLE", severity="WARN",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field=fmap.get("txn_date"), raw_value=_mapped(rec, fmap, "txn_date"),
                         message=f"Posting date could not be parsed ({date_problem}).")
        elif txn_date:
            outside = ((period_start and txn_date < period_start)
                       or (period_end and txn_date > period_end))
            after_extract = extracted_at and txn_date > extracted_at
            if outside or after_extract:
                dq_status = "WARN" if dq_status == "OK" else dq_status
                stats["warn"] += 1
                why = []
                if outside:
                    why.append(f"outside the declared period {period_start}..{period_end}")
                if after_extract:
                    why.append(f"after the source's own extraction date {extracted_at}")
                db.add_issue(conn, rule_code="DATE_OUT_OF_PERIOD", severity="ERROR",
                             country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                             field=fmap.get("txn_date"), raw_value=_mapped(rec, fmap, "txn_date"),
                             message=(f"Posting date {txn_date} is {' and '.join(why)}. "
                                      f"Kept and counted; confirm the date with the country."))
        if amount is not None and amount < 0:
            db.add_issue(conn, rule_code="AMOUNT_NEGATIVE", severity="INFO",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field=fmap.get("amount"), raw_value=amount,
                         message="Negative amount - plausible refund/reversal; kept and counted.")
        if description is None:
            db.add_issue(conn, rule_code="DESCRIPTION_MISSING", severity="INFO",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field=fmap.get("description"),
                         message="No description; classification relies on the account code alone.")
        if amount is not None and currency and not fx_rate:
            db.add_issue(conn, rule_code="FX_RATE_MISSING", severity="WARN",
                         country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                         field="currency", raw_value=currency,
                         message=f"No reference rate for {currency}; USD amount not computed.")

        src_id = row["source_record_id"]
        if src_id:
            if src_id in seen_ids:
                db.add_issue(conn, rule_code="SOURCE_ID_COLLISION", severity="ERROR",
                             country_code=country_code, batch_id=batch_id, raw_id=raw_id,
                             field=fmap.get("source_record_id"), raw_value=src_id,
                             message=f"Transaction id reused by a different record "
                                     f"(also at source row {seen_ids[src_id]}); "
                                     f"both retained, id is not a reliable business key.")
            else:
                seen_ids[src_id] = row["source_row_no"]

        children = rec.get(spec.get("child_path") or "") or []
        level = "PARENT" if children else "STANDALONE"

        cur = conn.execute(
            "INSERT INTO fact_expenditure (raw_id, country_code, source_record_id,"
            " parent_expenditure_id, record_level, txn_date, fiscal_year, ministry_sk,"
            " account_sk, description, description_raw, supplier, payment_method,"
            " amount_original, currency_original, amount_usd, fx_rate, fx_rate_source,"
            " is_countable, dq_status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (raw_id, country_code, src_id, None, level, txn_date,
             normalise_text(_mapped(rec, fmap, "fiscal_year")) or spec.get("fiscal_year"),
             ministry_sk, account_sk, description,
             str(desc_raw) if desc_raw is not None else None,
             normalise_text(_mapped(rec, fmap, "supplier")),
             clean_code(_mapped(rec, fmap, "payment_method")),
             amount, currency, amount_usd, fx_rate, fx_src,
             1, dq_status, db.utc_now()),
        )
        parent_id = cur.lastrowid
        stats["facts"] += 1

        if children:
            _load_children(conn, country_code, spec, batch_id, fx, rec, raw_id,
                           parent_id, amount, currency, txn_date, ministry_sk,
                           account_sk, children, stats)

    canonicalise_descriptions(conn, country_code, batch_id)
    conn.commit()
    return stats


def _extraction_date(conn, batch_id: int, spec: dict) -> str | None:
    """The source's own extraction timestamp (Country C's metadata.extractedAt),
    as an ISO date, if the source declares one."""
    key = spec.get("metadata_extracted_at")
    if not key:
        return None
    row = conn.execute("SELECT source_metadata FROM ingestion_batch WHERE batch_id=?",
                       (batch_id,)).fetchone()
    meta = json.loads(row["source_metadata"]) if row and row["source_metadata"] else {}
    value, _ = parse_date(meta.get(key), {"date_formats": ["%Y-%m-%d"]})
    return value


def canonicalise_descriptions(conn, country_code: str, batch_id: int) -> int:
    """Resolve case variants of the same description to one canonical spelling.

    Done per batch, after every row is loaded, because only then is it known
    which spelling the country used most. `description_raw` keeps the original
    on every row, so this is a grouping decision, not a repair: the variants
    are recorded in the DQ register, and the account label observed for a code
    follows the same canonical spelling so the Overview does not show one
    account under two names.
    """
    rows = conn.execute(
        "SELECT f.expenditure_id, f.description FROM fact_expenditure f"
        " JOIN stg_raw_record s ON s.raw_id = f.raw_id"
        " WHERE s.batch_id = ? AND f.description IS NOT NULL", (batch_id,)
    ).fetchall()
    canonical = canonical_spellings(r["description"] for r in rows)

    variants: dict[str, set] = defaultdict(set)
    for r in rows:
        variants[fold(r["description"])].add(r["description"])
    changed = 0
    for r in rows:
        want = canonical[fold(r["description"])]
        if want != r["description"]:
            conn.execute("UPDATE fact_expenditure SET description=? WHERE expenditure_id=?",
                         (want, r["expenditure_id"]))
            changed += 1

    for key, want in canonical.items():
        conn.execute(
            "UPDATE dim_account SET account_label=? WHERE country_code=? "
            "AND source='observed_in_extract' AND account_label<>? AND lower(account_label)=lower(?)",
            (want, country_code, want, want))

    multi = {k: v for k, v in variants.items() if len(v) > 1}
    if multi:
        example = sorted(multi.values(), key=len, reverse=True)[0]
        db.add_issue(
            conn, rule_code="DESCRIPTION_CASE_VARIANTS", severity="INFO",
            country_code=country_code, batch_id=batch_id, field="description",
            raw_value=" | ".join(sorted(example)),
            message=(f"{len(multi)} descriptions arrive in more than one casing "
                     f"({sum(len(v) for v in multi.values())} spellings in total); "
                     f"{changed} rows re-labelled to the most frequent spelling. "
                     f"Original text kept in description_raw."),
        )
    return changed


def _load_children(conn, country_code, spec, batch_id, fx, parent_rec, raw_id,
                   parent_id, parent_amount, currency, txn_date, ministry_sk,
                   account_sk, children, stats) -> None:
    """Load Country C's sub-transactions without double-counting.

    Where the children sum to the parent, the split is the finer-grained truth:
    children become countable and the parent is switched off. Where they do not
    sum (7 of 59 parents in the supplied file), the split is incomplete or
    wrong, so the parent stays countable, the children are loaded for visibility
    but excluded from totals, and the gap is raised for a human. Either way the
    country total is preserved exactly once.
    """
    conv = spec.get("conventions", {}) or {}
    cmap = spec.get("child_field_map", {}) or {}
    total = 0.0
    parsed: list[tuple[dict, float | None]] = []
    for ch in children:
        amt, _ = parse_amount(ch.get(cmap.get("amount", "amount")), conv)
        parsed.append((ch, amt))
        total += amt or 0.0

    reconciles = (
        parent_amount is not None
        and all(a is not None for _, a in parsed)
        and abs(total - parent_amount) <= 0.01
    )

    if reconciles:
        conn.execute("UPDATE fact_expenditure SET is_countable=0 WHERE expenditure_id=?",
                     (parent_id,))
    else:
        db.add_issue(
            conn, rule_code="SUBTXN_SUM_MISMATCH", severity="ERROR",
            country_code=country_code, batch_id=batch_id, raw_id=raw_id,
            expenditure_id=parent_id, field=spec.get("child_path"),
            raw_value=f"parent={parent_amount} children_sum={round(total, 2)}",
            message=(f"Sub-transactions sum to {round(total, 2)} but the parent is "
                     f"{parent_amount} (difference {round((total - (parent_amount or 0)), 2)}). "
                     f"Parent counted; children retained but excluded from totals."),
        )

    for ch, amt in parsed:
        desc_raw = ch.get(cmap.get("description", "description"))
        amount_usd, fx_rate, fx_src = convert_to_usd(amt, currency, fx)
        conn.execute(
            "INSERT INTO fact_expenditure (raw_id, country_code, source_record_id,"
            " parent_expenditure_id, record_level, txn_date, fiscal_year, ministry_sk,"
            " account_sk, description, description_raw, supplier, payment_method,"
            " amount_original, currency_original, amount_usd, fx_rate, fx_rate_source,"
            " is_countable, dq_status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (raw_id, country_code, str(ch.get(cmap.get("source_record_id", "subId")) or ""),
             parent_id, "CHILD", txn_date,
             normalise_text(parent_rec.get(spec["field_map"].get("fiscal_year", ""))) or spec.get("fiscal_year"),
             ministry_sk, account_sk, normalise_text(desc_raw),
             str(desc_raw) if desc_raw is not None else None,
             None, None, amt, currency, amount_usd, fx_rate, fx_src,
             1 if reconciles else 0, "OK" if reconciles else "WARN", db.utc_now()),
        )
        stats["children"] += 1


def reconcile_batch(conn, batch_id: int) -> None:
    """Close the batch: record the countable total and, where the source printed
    a control total, compare the two. A gap over 0.50 is a CONTROL_TOTAL_MISMATCH."""
    row = conn.execute(
        "SELECT b.country_code, b.control_total,"
        "       (SELECT ROUND(SUM(f.amount_original),2) FROM fact_expenditure f"
        "          JOIN stg_raw_record s ON s.raw_id=f.raw_id"
        "         WHERE s.batch_id=b.batch_id AND f.is_countable=1) AS loaded"
        "  FROM ingestion_batch b WHERE b.batch_id=?", (batch_id,)
    ).fetchone()
    loaded = row["loaded"]
    conn.execute("UPDATE ingestion_batch SET loaded_total=?, finished_at=? WHERE batch_id=?",
                 (loaded, db.utc_now(), batch_id))
    control = row["control_total"]
    if control and loaded is not None:
        diff = round(loaded - control, 2)
        if abs(diff) > 0.5:
            db.add_issue(
                conn, rule_code="CONTROL_TOTAL_MISMATCH", severity="WARN",
                country_code=row["country_code"], batch_id=batch_id,
                raw_value=f"control={control} loaded={loaded}",
                message=(f"Loaded total differs from the source's printed total by {diff:,.2f}. "
                         f"Expected here: rows with unparseable or missing amounts are "
                         f"excluded from the loaded sum."),
            )
    conn.commit()
