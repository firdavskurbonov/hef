"""Analyst interface.

    streamlit run app.py

Nine tabs, in the pipeline's order - data in, set up, check, decide,
analyse, drill, read:

    Ingest           upload an extract for a known country, or add a new country
                     from its file (columns read, mapping proposed, you confirm);
                     load history and control-total reconciliation per file
    References       the SHA, SRHR and country reference lists - editable, extendable
    Account mapping  map a country's account codes to SHA / SRHR, re-classify
    Data quality     the issue register, by rule, with the rows behind each
    Review queue     what still needs a human, largest value first
    Overview         harmonised totals by country / ministry / SHA / SRHR
    Records          every harmonised record - filter, search, download
    Traceability     one record end to end, down to the raw payload
    Documentation    the repository's markdown files, one sub-tab each

The interface calls the same functions as run_pipeline.py - there is one
pipeline, not a UI copy of it - and writes only through them: a load, a
account mapping save, or a review decision. Everything else is read.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from hef import db, ingest, pipeline  # noqa: E402

DB = Path(os.environ.get("HEF_DB", ROOT / "warehouse.db"))     # override for tests / demos
UPLOADS = ROOT / "uploads"
DATA_DIR = ingest.default_data_dir()

# WHO blue for chrome and single-series charts; pale red/amber/blue behind severity cells.
WHO_BLUE = "#0093d5"
NAVY = "#0b2e4f"
SEV_TINT = {"ERROR": "#f8dede", "WARN": "#fdf0d5", "INFO": "#dcedf8"}
RULE_HELP = {
    "AMOUNT_MISSING": "Amount empty; loaded as NULL and excluded from totals - never zero-filled.",
    "AMOUNT_NEGATIVE": "Negative amount - plausible reversal; kept and counted.",
    "AMOUNT_SEPARATOR_AMBIGUOUS": "One separator, three trailing digits, in a column that declares it decimal; read structurally as thousands.",
    "AMOUNT_UNPARSEABLE": "Amount could not be parsed; loaded as NULL.",
    "DATE_UNPARSEABLE": "Date did not match the source's declared formats; not guessed.",
    "DATE_OUT_OF_PERIOD": "Posting date outside the declared period, or after the source's own extraction date; kept and flagged.",
    "DESCRIPTION_MISSING": "No description; classified from the account code alone.",
    "DESCRIPTION_CASE_VARIANTS": "Same description supplied in several casings; resolved to the majority spelling, originals kept.",
    "SUBTXN_SUM_MISMATCH": "Sub-transactions do not sum to their parent; parent counted, children excluded.",
    "UNTRUSTED_TEXT_IN_DESCRIPTION": "Description carries instruction-like text aimed at a classifier; text layer disabled, quarantined for review.",
    "FX_ASSUMED_RATES": "USD conversions rest on assumed rates; indicative only.",
    "FX_RATE_MISSING": "No reference rate for this currency; USD not computed.",
    "SOURCE_ID_COLLISION": "Transaction id reused for a different record; both kept - the id is not a business key.",
    "SRC_CONTROL_TOTAL": "Source printed a control total; captured and reconciled, never loaded as a fact.",
    "SRC_COUNT_MISMATCH": "Source metadata declares a record count that differs from the rows staged.",
    "CONTROL_TOTAL_MISMATCH": "Loaded total differs from the source's printed total.",
}

RULE_SEVERITY = {
    "AMOUNT_MISSING": "WARN", "AMOUNT_NEGATIVE": "INFO", "AMOUNT_SEPARATOR_AMBIGUOUS": "WARN",
    "AMOUNT_UNPARSEABLE": "ERROR", "DATE_UNPARSEABLE": "WARN", "DATE_OUT_OF_PERIOD": "ERROR",
    "DESCRIPTION_MISSING": "INFO", "DESCRIPTION_CASE_VARIANTS": "INFO", "SUBTXN_SUM_MISMATCH": "ERROR",
    "UNTRUSTED_TEXT_IN_DESCRIPTION": "ERROR", "FX_ASSUMED_RATES": "WARN", "FX_RATE_MISSING": "WARN",
    "SOURCE_ID_COLLISION": "ERROR", "SRC_CONTROL_TOTAL": "INFO", "SRC_COUNT_MISMATCH": "WARN",
    "CONTROL_TOTAL_MISMATCH": "WARN",
}
RULE_STAGE = {
    "SRC_CONTROL_TOTAL": "ingest", "SRC_COUNT_MISMATCH": "ingest", "UNTRUSTED_TEXT_IN_DESCRIPTION": "classify",
    "FX_ASSUMED_RATES": "load", "CONTROL_TOTAL_MISMATCH": "reconcile",
}

st.set_page_config(page_title="Health Expenditure Harmonisation",
                   page_icon=":material/monitoring:", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown(f"""
<style>
  .block-container {{ padding-top: 1.4rem; padding-bottom: 2rem; }}
  .hef-hero {{ border-left: 6px solid {WHO_BLUE}; padding: .2rem 0 .2rem 1rem; margin-bottom: .6rem; }}
  .hef-hero h1 {{ color: {NAVY}; font-size: 1.9rem; margin: 0 0 .15rem 0; padding: 0; line-height: 1.2; }}
  .hef-hero p {{ color: #4a5b6c; margin: 0; font-size: 1rem; }}
  .hef-kicker {{ color: {WHO_BLUE}; font-weight: 650; letter-spacing: .06em; font-size: .75rem; text-transform: uppercase; }}
  .hef-step {{ display:inline-block; background:{WHO_BLUE}; color:#fff; border-radius:999px; width:1.6rem; height:1.6rem;
               line-height:1.6rem; text-align:center; font-weight:700; margin-right:.5rem; font-size:.85rem; }}
  .hef-foot {{ color:#6b7a89; font-size:.8rem; border-top:1px solid #d9e2ea; padding-top:.6rem; margin-top:2rem; }}
  [data-testid="stSidebar"] h2 {{ color:#fff; }}
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------ helpers --

@st.cache_resource
def get_conn():
    """One connection for the process. Streamlit re-runs the script on every
    interaction, so it is cached; check_same_thread off because Streamlit's
    worker threads share it."""
    conn = sqlite3.connect(str(DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Query -> DataFrame. Every read in the app goes through here."""
    return pd.read_sql_query(sql, get_conn(), params=params)


# One slot per country, in the order countries first appear; fixed so a
# country keeps its colour from chart to chart.
COUNTRY_COLOURS = ["#0093d5", "#eb6834", "#4a3aa7", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#e34948"]


def hbar(df: pd.DataFrame, y: str, x: str, color: str | None = None, *, stack: bool = True,
         x_title: str = "USD (indicative)", y_title: str = "", height: int = 260,
         tooltip_fmt: str = ",.0f", domain: list | None = None, sort: str | list = "-x"):
    """Horizontal bar chart. Thin rounded marks, no grid drawn over the bars,
    a hover tooltip on every bar, and a fixed colour per category (a country
    keeps its slot whatever else is on screen)."""
    base = alt.Chart(df).mark_bar(cornerRadiusEnd=4, size=18 if not color or stack else 16)
    enc = {
        "y": alt.Y(f"{y}:N", sort=sort, title=y_title, axis=alt.Axis(labelLimit=220, grid=False)),
        "x": alt.X(f"{x}:Q", title=x_title, axis=alt.Axis(format="~s", grid=True, gridOpacity=.35,
                                                         gridDash=[2, 3], tickCount=5)),
        "tooltip": [alt.Tooltip(f"{y}:N", title=y_title or y),
                    alt.Tooltip(f"{x}:Q", title=x_title, format=tooltip_fmt)],
    }
    if color:
        dom = domain or sorted(df[color].dropna().unique().tolist())
        enc["color"] = alt.Color(f"{color}:N", title=color.replace("_", " ").title(),
                                 scale=alt.Scale(domain=dom, range=COUNTRY_COLOURS[:len(dom)]),
                                 legend=alt.Legend(orient="bottom", direction="horizontal"))
        enc["tooltip"].insert(0, alt.Tooltip(f"{color}:N", title="Country"))
        if not stack:
            enc["yOffset"] = alt.YOffset(f"{color}:N")
    else:
        base = base.encode(color=alt.value(WHO_BLUE))
    chart = base.encode(**enc).properties(height=height)
    if color and stack:
        chart = chart.encode(order=alt.Order(f"{color}:N"))
    return chart.configure_view(strokeWidth=0).configure_axis(labelColor="#4a5b6c", titleColor="#4a5b6c")


def nz(x, default=0.0):
    """None/NaN -> default, for aggregates over an empty table."""
    return default if x is None or (isinstance(x, float) and pd.isna(x)) else x


# Column definitions for st.dataframe: every column gets a label and a tooltip.
def num(label: str, help: str | None = None, **kw):
    """Numeric column, thousands-separated in the viewer's locale."""
    return st.column_config.NumberColumn(label, format="localized", help=help, **kw)


def T(label: str, help: str | None = None, **kw):
    """Text column."""
    return st.column_config.TextColumn(label, help=help, **kw)


CONF_HELP = (
    "An evidence grade, not a probability: how strong the kind of evidence behind the "
    "classification was. Account mapping entries carry their own grade (0.95 purpose unambiguous, "
    "0.80 boundary arguable, 0.40 input-type); keyword rule 0.70; fuzzy match 0.55; nothing matched "
    "0; an analyst's decision 1.0; capped at 0.40 where the description was untrusted. Anything "
    "below 0.70 (rules.yml: review_below) is routed to a human.")

CONFIDENCE_GUIDE = [
    ("Account mapping entry, purpose unambiguous (\"Medical drugs\" -> HC.5.1)", "0.95"),
    ("Account mapping entry, boundary arguable (vaccines: goods HC.5.1 or programme HC.6.2?)", "0.80"),
    ("Account mapping entry, input-type code (salaries, fuel -> ADMIN_INPUT)", "0.40"),
    ("Keyword rule on the description", "0.70"),
    ("Fuzzy match to a scheme label", "0.55"),
    ("Nothing matched (UNCLASSIFIED)", "0.00"),
    ("Analyst decision", "1.00"),
    ("Any of the above where the text was untrusted", "capped at 0.40"),
]


def render_confidence_guide(key: str) -> None:
    """The grade table, wherever a confidence is shown."""
    threshold = float((db.load_rules().get("settings") or {}).get("review_below", 0.70))
    with st.expander("What the confidence grade means"):
        st.markdown(
            "Confidence is an **evidence grade, not a probability**: it says how strong the kind of "
            "evidence behind a classification was. The country's own account code is the strongest "
            "signal, free text is weaker, and nothing at all is a guess. "
            f"**Anything below {threshold:.2f} is routed to a human** (`config/rules.yml: review_below`), "
            "on top of the records flagged explicitly for untrusted text, code/text conflicts and "
            "input-type codes.")
        st.dataframe(pd.DataFrame(CONFIDENCE_GUIDE, columns=["source", "confidence"]),
                     width="stretch", hide_index=True, key=f"guide_{key}", column_config={
                         "source": T("Source of the classification", "Which rule produced the result."),
                         "confidence": T("Confidence", "The grade that rule assigns.")})
METHOD_HELP = ("account_mapping = the country's account code looked up in the mapping table; keyword = description rules; "
               "fuzzy = similarity to a scheme label; manual = an analyst's decision; none = nothing matched.")
OUTCOME_COL_HELP = ("CLASSIFIED = mapped to a code; CAPITAL = capital formation (SHA HK, outside HC); "
                    "ADMIN_INPUT = input with no determinable function (provisional HC.7); "
                    "UNCLASSIFIED = a human decides.")
SEVERITY_HELP = ("ERROR = cannot be right as supplied; WARN = loaded under an assumption a reviewer "
                 "should know about; INFO = handled, recorded for completeness.")
RULE_COL_HELP = "Data-quality rule identifier. The Data quality tab lists the rows behind each."
COUNTRY_HELP = "Country code as used across the app and in config/sources.yml."
RECORDS_HELP = "Countable records - nested sub-transactions counted once."
USD_HELP = "USD at the assumed rates in config/fx_rates.yml - indicative."


def render_rules_guide() -> None:
    """Every classification rule in config/rules.yml, read-only."""
    rules = db.load_rules()
    settings = rules.get("settings") or {}
    with st.expander("Classification rules (config/rules.yml)"):
        st.markdown(
            "Layer 2 of the classifier: bilingual keyword rules applied to the description when "
            "the account code cannot answer, and used to corroborate it when it can. Patterns match "
            "against a casefolded, accent-stripped description, first match wins, and every rule "
            "carries an id so a classification can be traced to it. The classifier is deterministic: "
            "a lookup, then these patterns, then a string-similarity fallback - no model, no API. "
            "Patterns are wrapped in `\\b ... \\b` (a word boundary), so `arv` matches ARV and ARVs "
            "but not *harvest*. Read-only here: edit the file, then re-classify (Account mapping -> "
            "Save, or `run_pipeline.py`).")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Review threshold", f"{float(settings.get('review_below', 0.70)):.2f}", border=True,
                  help="Grades below this go to a human (review_below).")
        c2.metric("Keyword grade", f"{float(settings.get('default_confidence', 0.70)):.2f}", border=True,
                  help="Evidence grade a keyword hit records (default_confidence).")
        c3.metric("Fuzzy threshold", f"{float(settings.get('fuzzy_threshold', 88)):.0f}%", border=True,
                  help="Minimum similarity to a scheme label before the fuzzy layer answers at all.")
        c4.metric("Fuzzy grade", f"{float(settings.get('fuzzy_confidence', 0.55)):.2f}", border=True,
                  help="Evidence grade a fuzzy match records (fuzzy_confidence).")
        for scheme, key, code_key in (("SHA", "sha_rules", "sha"), ("SRHR", "srhr_rules", "srhr")):
            rows = [{"id": r.get("id"), "pattern": r.get("pattern"),
                     "result": r.get(code_key) or r.get("outcome"),
                     "grade": float(r.get("confidence", settings.get("default_confidence", 0.70))),
                     "note": r.get("note") or ""} for r in (rules.get(key) or [])]
            st.markdown(f"**{scheme} keyword rules** - {len(rows)}, in the order they are tried")
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, key=f"rules_{key}",
                         column_config={
                             "id": T("Rule", "Rule id, recorded on every record it classifies."),
                             "pattern": T("Pattern", "Regular expression, matched against the folded "
                                                     "description.", width="large"),
                             "result": T("Result", "The code assigned, or an outcome such as CAPITAL."),
                             "grade": st.column_config.NumberColumn("Grade", format="%.2f",
                                                                    help="Evidence grade recorded."),
                             "note": T("Note", "Why the rule exists.", width="medium")})
        pats = rules.get("injection_patterns") or []
        st.markdown(f"**Untrusted-text patterns** - {len(pats)}. A description matching any of these has "
                    "its free-text layer disabled, its grade capped at 0.40, and is quarantined for review.")
        st.dataframe(pd.DataFrame({"pattern": pats}), width="stretch", hide_index=True, key="rules_injection",
                     column_config={"pattern": T("Pattern", "Case-insensitive regular expression.", width="large")})


def conf_col(label: str = "Confidence (grade)", help: str = CONF_HELP):
    """Confidence as a 0-1 bar with the value beside it."""
    return st.column_config.ProgressColumn(label, min_value=0.0, max_value=1.0, format="%.2f", help=help)


def severity_style(df: pd.DataFrame):
    """Tint the severity cell red/amber/blue. Frames without that column pass through."""
    if "severity" not in df.columns:
        return df
    return df.style.map(lambda v: f"background-color: {SEV_TINT.get(v, '')}", subset=["severity"])


def section(title: str, what: str, how: str | None = None) -> None:
    """Section header. Hover the (?) for what the visual shows; the caption
    beneath says how to read it. Every table and chart gets both."""
    st.markdown(f'<span class="hef-kicker">{title}</span>', unsafe_allow_html=True, help=what)
    if how:
        st.caption(how)


def step(n: int, title: str) -> None:
    """Numbered step heading on the Ingest tab."""
    st.markdown(f'<span class="hef-step">{n}</span><b>{title}</b>', unsafe_allow_html=True)


def flash(key: str = "flash") -> None:
    """Show, once, a success message a button handler left in session state
    before it called st.rerun()."""
    msg = st.session_state.pop(key, None)
    if msg:
        st.success(msg, icon=":material/check_circle:")


def save_upload(folder: str, up) -> Path:
    """Write an uploaded file to uploads/<folder>/ and return its path.
    Written once per upload, not on every rerun: the (name, size) marker in
    session state says whether this file is already on disk."""
    dest = UPLOADS / folder
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / Path(up.name).name
    marker = (up.name, up.size)
    if st.session_state.get(f"saved:{folder}") != marker or not path.exists():
        path.write_bytes(up.getbuffer())
        st.session_state[f"saved:{folder}"] = marker
    return path


def country_options() -> dict[str, str]:
    """code -> 'CODE - Name' for every country in sources.yml."""
    return {code: f"{code} - {spec.get('name', code)}" for code, spec in db.load_sources().items()}


# ------------------------------------------------------------ pre-flight ----

if not DB.exists():
    st.markdown('<div class="hef-hero"><h1>Health Expenditure Harmonisation</h1>'
                '<p>No warehouse yet.</p></div>', unsafe_allow_html=True)
    st.error(f"No database at `{DB}`. Build it first:  `python run_pipeline.py`")
    st.stop()

# ---------------------------------------------------------------- sidebar ----

with st.sidebar:
    st.markdown("## Health expenditure harmonisation")
    st.caption("Prototype prepared for the WHO Regional Office for Africa - "
               "Health Systems and Services, Health Financing and Governance. "
               "Synthetic assessment data.")
    st.divider()
    st.markdown("**Countries loaded**")
    batches = q("""SELECT b.country_code, c.country_name, b.rows_loaded, b.finished_at,
                          (SELECT COUNT(*) FROM fact_expenditure f JOIN stg_raw_record s ON s.raw_id=f.raw_id
                            WHERE s.batch_id=b.batch_id AND f.is_countable=1) countable
                     FROM ingestion_batch b JOIN dim_country c USING(country_code)
                    ORDER BY b.country_code, b.batch_id""")
    if batches.empty:
        st.caption("None yet - use the Ingest tab.")
    for r in batches.itertuples():
        st.markdown(f"**{r.country_code}** {r.country_name}  \n"
                    f"<span style='font-size:.8rem;opacity:.8'>{r.countable:,} countable records "
                    f"- {str(r.finished_at or '')[:16].replace('T', ' ')}</span>", unsafe_allow_html=True)
    st.divider()
    st.caption(f"Warehouse: `{DB.name}` - rebuild with `python run_pipeline.py`")

# ----------------------------------------------------------------- header ----

st.markdown(
    '<div class="hef-hero"><span class="hef-kicker">Health financing - expenditure analysis</span>'
    '<h1>Health Expenditure Harmonisation</h1>'
    '<p>National extracts in any format, one harmonised model, SHA and SRHR classification '
    'with a reason on every record, and a path from every figure back to its source cell.</p></div>',
    unsafe_allow_html=True)

# Tabs in pipeline order: data in, references, mapping, DQ, review, then the
# result summarised, in full, and traced back to source; the docs last.
(tab_ingest, tab_refs, tab_map, tab_dq, tab_review, tab_over, tab_records, tab_trace,
 tab_docs) = st.tabs([
    ":material/upload_file: Ingest", ":material/library_books: References",
    ":material/account_tree: Account mapping", ":material/rule: Data quality",
    ":material/rate_review: Review queue", ":material/dashboard: Overview",
    ":material/table_view: Records", ":material/search: Traceability",
    ":material/description: Documentation",
])


def csv_download(df: pd.DataFrame, label: str, name: str, key: str) -> None:
    """Download button that hands the frame over as UTF-8 CSV."""
    st.download_button(label, df.to_csv(index=False).encode("utf-8"), file_name=name, mime="text/csv",
                       icon=":material/download:", key=key)

# ================================================================= OVERVIEW ==
with tab_over:
    tot = q("""SELECT COUNT(*) n, SUM(amount_usd) usd, COUNT(DISTINCT country_code) countries
                 FROM fact_expenditure WHERE is_countable=1""").iloc[0]
    cls = q("""SELECT COUNT(*) total,
                      SUM(CASE WHEN outcome='CLASSIFIED' THEN 1 ELSE 0 END) ok,
                      SUM(CASE WHEN needs_review=1 AND review_status='PENDING' THEN 1 ELSE 0 END) rev
                 FROM classification WHERE scheme='SHA' AND is_current=1""").iloc[0]
    dqn = q("""SELECT SUM(severity='ERROR') err, SUM(severity='WARN') warn FROM dq_issue""").iloc[0]

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Countries", f"{int(tot['countries'])}", border=True, icon=":material/public:",
              help="Countries with at least one countable record in the warehouse.")
    k2.metric("Countable records", f"{int(tot['n']):,}", border=True, icon=":material/receipt_long:",
              help="Nested splits counted once - see the load history on the Ingest tab.")
    k3.metric("Total, USD millions", f"{nz(tot['usd']) / 1e6:,.1f} m", border=True,
              icon=":material/attach_money:",
              help=f"{nz(tot['usd']):,.0f} USD across all countable records, at assumed reference "
                   f"rates - no rates were supplied, so treat it as indicative.")
    k4.metric("SHA classified", f"{int(cls['ok']):,}",
              delta=f"{(cls['ok'] / cls['total'] if cls['total'] else 0):.0%} of records",
              delta_color="off", delta_arrow="off", border=True, icon=":material/category:",
              help="Records with an SHA HC code (outcome CLASSIFIED). The rest are CAPITAL, "
                   "ADMIN_INPUT or UNCLASSIFIED - see the SHA function table.")
    k5.metric("Awaiting review", f"{int(cls['rev']):,}",
              delta=f"{int(nz(dqn['err']))} DQ errors, {int(nz(dqn['warn']))} warnings",
              delta_color="off", delta_arrow="off", border=True, icon=":material/pending_actions:",
              help="SHA classifications flagged for a human and not yet accepted or corrected. "
                   "Work them in the Review queue tab.")

    st.markdown("")
    left, right = st.columns([3, 2])
    with left:
        section("By country",
                "Countable records and their totals per country, in the country's own currency and "
                "in USD at the assumed rates declared in config/fx_rates.yml.",
                "Records count nested sub-transactions once. USD is indicative: the rates are an "
                "assumption, declared in config/fx_rates.yml.")
        by_c = q("""SELECT f.country_code, c.country_name, c.primary_currency currency,
                           COUNT(*) records, SUM(f.amount_original) total_local,
                           SUM(f.amount_usd) total_usd,
                           (SELECT COUNT(DISTINCT cl.expenditure_id) FROM classification cl
                              JOIN fact_expenditure x ON x.expenditure_id=cl.expenditure_id
                             WHERE x.country_code=f.country_code AND cl.is_current=1
                               AND cl.needs_review=1 AND cl.review_status='PENDING') awaiting_review,
                           (SELECT COUNT(*) FROM dq_issue d WHERE d.country_code=f.country_code
                               AND d.severity='ERROR') dq_errors
                      FROM fact_expenditure f JOIN dim_country c ON c.country_code=f.country_code
                     WHERE f.is_countable=1 GROUP BY 1,2,3 ORDER BY f.country_code""")
        st.dataframe(by_c, width="stretch", hide_index=True, column_config={
            "country_code": T("Code", COUNTRY_HELP),
            "country_name": T("Country", "From the supplied reference list, or from sources.yml for a country added in the app."),
            "currency": T("Currency", "The country's reporting currency as declared in sources.yml."),
            "records": num("Records", RECORDS_HELP),
            "total_local": num("Total (local)", "Sum of countable amounts in the country's own currency - "
                                                "the figure reconciled to any printed control total."),
            "total_usd": num("Total (USD)", "Total (local) divided by the country's rate in config/fx_rates.yml - indicative."),
            "awaiting_review": num("Awaiting review", "Records flagged for a human and not yet decided - the Review queue."),
            "dq_errors": num("DQ errors", "ERROR-severity findings on this country - the Data quality tab.")})
    with right:
        section("Total by country, USD",
                "The USD totals from the table as bars, one per country. A country keeps the same "
                "colour on every chart in the app.",
                "Read the bar for size; the table on the left has the exact figure.")
        st.altair_chart(hbar(by_c, "country_code", "total_usd", "country_code", height=200,
                             sort=by_c["country_code"].tolist()), width="stretch")

    st.markdown("")
    left, right = st.columns(2)
    with left:
        section("SHA function",
                "Countable spend by SHA 2011 health-care function (HC codes), plus the two outcomes "
                "that are deliberately not HC codes: CAPITAL and ADMIN_INPUT.",
                "Bars show size at a glance; the table beneath gives the exact USD and record count.")
        sha = q("""SELECT CASE WHEN cl.outcome='ADMIN_INPUT' THEN 'ADMIN_INPUT'
                               ELSE COALESCE(cl.code, cl.outcome) END AS sha,
                          CASE WHEN cl.outcome='ADMIN_INPUT'
                               THEN 'Inputs with no determinable function (provisional HC.7)'
                               ELSE COALESCE(d.sha_description, cl.outcome) END AS description,
                          COUNT(*) records, SUM(f.amount_usd) total_usd
                     FROM fact_expenditure f
                     JOIN classification cl ON cl.expenditure_id=f.expenditure_id
                                           AND cl.scheme='SHA' AND cl.is_current=1
                     LEFT JOIN dim_sha d ON d.sha_code=cl.code
                    WHERE f.is_countable=1
                    GROUP BY 1, 2 ORDER BY total_usd DESC""")
        st.altair_chart(hbar(sha, "sha", "total_usd", height=320), width="stretch")
        st.dataframe(sha, width="stretch", hide_index=True, height=250, column_config={
            "sha": T("SHA", "SHA 2011 health-care function code, or an outcome that is deliberately not an "
                            "HC code (CAPITAL, ADMIN_INPUT).", width="small"),
            "description": T("Function", "The function's name from the supplied SHA reference list.", width="medium"),
            "records": num("Records", "Countable records currently classified here.", width="small"),
            "total_usd": num("USD", USD_HELP, width="medium")})
        st.caption("CAPITAL = gross capital formation (SHA HK), outside the HC functions. "
                   "ADMIN_INPUT = input with no determinable function; provisional HC.7, flagged.")
    with right:
        section("SRHR theme, by country",
                "Spend tagged to a sexual and reproductive health theme, stacked by country so both "
                "the size of each theme and each country's share are visible.",
                "SRHR.NA (non-SRHR spend) is in the table but left off the chart so the themes are legible.")
        srhr_c = q("""SELECT f.country_code, cl.code srhr, SUM(f.amount_usd) usd, COUNT(*) records
                        FROM fact_expenditure f
                        JOIN classification cl ON cl.expenditure_id=f.expenditure_id
                                              AND cl.scheme='SRHR' AND cl.is_current=1
                       WHERE f.is_countable=1 AND cl.code IS NOT NULL AND cl.code<>'SRHR.NA'
                       GROUP BY 1,2 ORDER BY 1,2""")
        if not srhr_c.empty:
            st.altair_chart(hbar(srhr_c, "srhr", "usd", "country_code", height=320,
                                 domain=by_c["country_code"].tolist()), width="stretch")
        srhr = q("""SELECT COALESCE(cl.code, cl.outcome) AS srhr,
                           COALESCE(d.srhr_description, cl.outcome) AS description,
                           COUNT(*) records, SUM(f.amount_usd) total_usd
                      FROM fact_expenditure f
                      JOIN classification cl ON cl.expenditure_id=f.expenditure_id
                                            AND cl.scheme='SRHR' AND cl.is_current=1
                      LEFT JOIN dim_srhr d ON d.srhr_code=cl.code
                     WHERE f.is_countable=1
                     GROUP BY 1, 2 ORDER BY total_usd DESC""")
        st.dataframe(srhr, width="stretch", hide_index=True, height=250, column_config={
            "srhr": T("SRHR", "SRHR theme code; SRHR.NA marks spend that is not sexual and reproductive "
                              "health.", width="small"),
            "description": T("Theme", "The theme's name from the supplied SRHR reference list.", width="medium"),
            "records": num("Records", "Countable records currently classified here.", width="small"),
            "total_usd": num("USD", USD_HELP, width="medium")})
        st.caption("SRHR.NA (non-SRHR spend) is listed in the table and left off the chart.")

    st.markdown("")
    section("By ministry",
            "Countable spend by the ministry that booked it, per country. Health spending is not "
            "confined to the Ministry of Health, which is why classification keys off the purpose "
            "of the expenditure, never off the ministry.",
            "In SHA terms the ministry feeds the financing-agent axis, not the function axis; it is "
            "stored on every record and shown here, but it does not drive classification.")
    by_m = q("""SELECT f.country_code, COALESCE(m.ministry_name, '(not given)') ministry,
                      COUNT(*) records, SUM(f.amount_usd) total_usd
                 FROM fact_expenditure f LEFT JOIN dim_ministry m ON m.ministry_sk=f.ministry_sk
                WHERE f.is_countable=1 GROUP BY 1,2 ORDER BY 1, total_usd DESC""")
    ml, mr = st.columns([1, 1])
    with ml:
        st.dataframe(by_m, width="stretch", hide_index=True, height=480, column_config={
            "country_code": T("Country", COUNTRY_HELP),
            "ministry": T("Ministry", "As named in the extract (ministry_name)."),
            "records": num("Records", RECORDS_HELP), "total_usd": num("USD", USD_HELP)})
    with mr:
        if not by_m.empty:
            # one bar per ministry (each belongs to one country), coloured by country
            st.altair_chart(hbar(by_m, "ministry", "total_usd", "country_code", stack=True, height=480,
                                 domain=sorted(by_m["country_code"].unique().tolist()),
                                 sort=by_m.sort_values("total_usd", ascending=False)["ministry"].tolist()),
                            width="stretch")

    st.markdown("")
    section("How classifications were reached",
            "For each scheme, which classification method produced each outcome, at what average "
            "confidence, and how many of those records were flagged for review.",
            "account_mapping = the country's account code looked up in the mapping table (strongest signal) - "
            "keyword = description rules - fuzzy = label similarity - manual = an analyst's decision.")
    how = q("""SELECT scheme, method, outcome, COUNT(*) records, ROUND(AVG(confidence),2) avg_confidence,
                      SUM(needs_review) flagged
                 FROM classification WHERE is_current=1
                GROUP BY scheme, method, outcome ORDER BY scheme, records DESC""")
    st.dataframe(how, width="stretch", hide_index=True, column_config={
        "scheme": T("Scheme", "SHA or SRHR - every record is classified against both."),
        "method": T("Method", METHOD_HELP),
        "outcome": T("Outcome", OUTCOME_COL_HELP),
        "records": num("Records", "All current classifications in this group (countable or not)."),
        "avg_confidence": conf_col("Avg confidence (grade)",
                                   "Mean of the evidence grade recorded on each record in this group. " + CONF_HELP),
        "flagged": num("Flagged for review", "Records in this group marked for a human - below the review "
                                             "threshold or flagged explicitly. They sit in the Review queue "
                                             "until accepted or corrected.")})
    render_confidence_guide("overview")
    render_rules_guide()

# ================================================================== RECORDS ==
with tab_records:
    section("Records",
            "Every countable harmonised record with its current SHA and SRHR classification - the "
            "table behind every total in the app. Filter, sort by any column, and download the "
            "selection as CSV.",
            "Nested sub-transactions appear once, on the countable side of each split. The table "
            "shows up to 5,000 rows; the download has the whole selection.")
    r1, r2, r3, r4 = st.columns(4)
    countries = ["All"] + q("SELECT country_code FROM dim_country ORDER BY 1")["country_code"].tolist()
    rc = r1.selectbox("Country", countries, key="rec_country")
    sha_opts = ["All"] + q("""SELECT DISTINCT COALESCE(sha_code, sha_outcome) v FROM v_expenditure_classified
                              ORDER BY 1""")["v"].tolist()
    rs = r2.selectbox("SHA function / outcome", sha_opts, key="rec_sha")
    srhr_opts = ["All"] + q("SELECT DISTINCT COALESCE(srhr_code, srhr_outcome) v FROM v_expenditure_classified ORDER BY 1")["v"].tolist()
    rr = r3.selectbox("SRHR theme", srhr_opts, key="rec_srhr")
    rv = r4.selectbox("Review", ["All", "Awaiting review", "Not required", "Decided by an analyst"], key="rec_review")
    g1, g2, g3, g4 = st.columns([2, 1, 1, 1])
    text = g1.text_input("Description, supplier or account contains", "", key="rec_text")
    dfrom = g2.text_input("From date (YYYY-MM-DD)", "", key="rec_from")
    dto = g3.text_input("To date (YYYY-MM-DD)", "", key="rec_to")
    ministries = ["All"] + q("SELECT DISTINCT ministry_name FROM dim_ministry WHERE ministry_name IS NOT NULL ORDER BY 1")["ministry_name"].tolist()
    rm = g4.selectbox("Ministry", ministries, key="rec_ministry")

    where, params = ["1=1"], []
    if rc != "All":
        where.append("country_code=?"); params.append(rc)
    if rs != "All":
        where.append("COALESCE(sha_code, sha_outcome)=?"); params.append(rs)
    if rr != "All":
        where.append("COALESCE(srhr_code, srhr_outcome)=?"); params.append(rr)
    if rv == "Awaiting review":
        where.append("sha_review_status='PENDING'")
    elif rv == "Not required":
        where.append("sha_review_status='NOT_REQUIRED'")
    elif rv == "Decided by an analyst":
        where.append("sha_review_status IN ('ACCEPTED','CORRECTED')")
    if text.strip():
        like = f"%{text.strip()}%"
        where.append("(description LIKE ? OR supplier LIKE ? OR account_code LIKE ? OR account_label LIKE ?)")
        params += [like, like, like, like]
    if dfrom.strip():
        where.append("txn_date >= ?"); params.append(dfrom.strip())
    if dto.strip():
        where.append("txn_date <= ?"); params.append(dto.strip())
    if rm != "All":
        where.append("ministry_name=?"); params.append(rm)
    cols = ("country_code, source_record_id, txn_date, ministry_name, account_code, account_label, description, "
            "supplier, amount_original, currency_original, amount_usd, sha_code, sha_outcome, sha_method, "
            "sha_confidence, sha_review_status, srhr_code, srhr_outcome, srhr_confidence, dq_status, record_level")
    sql_where = " AND ".join(where)
    tot = q(f"SELECT COUNT(*) n, SUM(amount_usd) usd FROM v_expenditure_classified WHERE {sql_where}",
            tuple(params)).iloc[0]
    k1, k2, k3 = st.columns([1, 1, 3])
    k1.metric("Records selected", f"{int(tot['n']):,}", border=True, help="Countable records matching the filters.")
    k2.metric("USD millions (indicative)", f"{nz(tot['usd']) / 1e6:,.1f} m", border=True,
              help=f"{nz(tot['usd']):,.0f} USD. " + USD_HELP)
    recs = q(f"SELECT {cols} FROM v_expenditure_classified WHERE {sql_where} ORDER BY amount_usd DESC LIMIT 5000",
             tuple(params))
    st.dataframe(recs, width="stretch", hide_index=True, height=480, column_config={
        "country_code": T("Country", COUNTRY_HELP), "source_record_id": T("Source id", "The country's own transaction id."),
        "txn_date": T("Date", "Posting date, ISO."), "ministry_name": T("Ministry", "As named in the extract."),
        "account_code": T("Account", "The country's account code."), "account_label": T("Account label", "Label for that code."),
        "description": T("Description", "Harmonised description (majority spelling).", width="large"),
        "supplier": T("Supplier", "As supplied."), "amount_original": num("Amount", "In the source currency."),
        "currency_original": T("Ccy", "Source currency."), "amount_usd": num("USD", USD_HELP),
        "sha_code": T("SHA", "SHA function code, blank where the outcome is not a code."),
        "sha_outcome": T("SHA outcome", OUTCOME_COL_HELP), "sha_method": T("Method", METHOD_HELP),
        "sha_confidence": conf_col("SHA grade"),
        "sha_review_status": T("Review", "NOT_REQUIRED, PENDING, ACCEPTED or CORRECTED."),
        "srhr_code": T("SRHR", "SRHR theme code."), "srhr_outcome": T("SRHR outcome", OUTCOME_COL_HELP),
        "srhr_confidence": conf_col("SRHR grade"),
        "dq_status": T("DQ", "OK, or WARN where a data-quality rule touched the row."),
        "record_level": T("Level", "STANDALONE, PARENT or CHILD of a nested split.")})
    if int(tot["n"]) > 0:
        full = q(f"SELECT {cols} FROM v_expenditure_classified WHERE {sql_where} ORDER BY country_code, txn_date",
                 tuple(params))
        csv_download(full, f"Download {int(tot['n']):,} records (CSV)", "harmonised_records.csv", "dl_records")

# =================================================================== INGEST ==

def load_known_country(code: str, spec: dict) -> None:
    """Ingest steps 2-3 for a country already in sources.yml: show its config,
    take an upload (or use the configured file), load and classify. An upload
    is kept under uploads/<code>/ and the spec pointed at it for this load
    only - sources.yml is not changed."""
    c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
    c1.metric("Reader", spec["reader"].upper(), border=True)
    c2.metric("Currency", spec.get("currency", "-"), border=True)
    per = spec.get("period") or {}
    c3.metric("Declared period", f"{per.get('start', '?')} to {per.get('end', '?')}", border=True)
    c4.metric("Fields mapped", len(spec.get("field_map", {})), border=True)
    with st.expander("Configuration for this country (config/sources.yml)"):
        st.code(yaml.safe_dump({code: spec}, sort_keys=False, allow_unicode=True), language="yaml")

    step(2, "Upload the extract - or load the configured file")
    types = {"csv": ["csv", "txt"], "excel": ["xlsx", "xlsm"], "json": ["json"]}[spec["reader"]]
    up = st.file_uploader(f"Extract ({', '.join(types)})", type=types, key=f"up_{code}")
    configured = ingest.source_path(spec, DATA_DIR)
    if up is None:
        st.caption(f"No upload - **Load** will use the configured file `{spec['path']}`"
                   + ("" if configured.exists() else " (not found)"))
    replace = st.checkbox("Replace this country's existing data (its review decisions are discarded)",
                          value=True, key=f"rep_{code}")

    step(3, "Load")
    can = up is not None or configured.exists()
    if st.button("Load", type="primary", icon=":material/play_arrow:", disabled=not can, key=f"go_{code}"):
        run_spec = dict(spec)
        if up is not None:
            path = save_upload(code, up)
            run_spec.update({"path": path.name, "data_dir": f"uploads/{code}"})
        with st.spinner(f"Loading {code}..."):
            try:
                result = pipeline.load_country(get_conn(), code, run_spec, DATA_DIR,
                                               db.load_fx(), replace=replace)
                cls = pipeline.classify_country(get_conn(), code)
            except Exception as exc:  # surface the reason, keep the app alive
                st.error(f"Load failed: {exc}")
            else:
                st.session_state["load_report"] = (code, result, cls)
                st.session_state["flash_ingest"] = f"{code} loaded from `{run_spec['path']}`."
                st.rerun()


def new_country_wizard(existing: dict) -> None:
    """Ingest steps 2-5 for a country not yet in sources.yml.

    Upload -> reader options -> field mapping and conventions proposed from
    the file (ingest.guess_*) -> country details. Each proposal is shown as a
    widget the analyst can change; the resulting sources.yml block is shown
    before it is written. Save moves the file to uploads/<code>/, appends the
    block, writes the FX rate, loads and classifies. `existing` is the current
    country list, used to refuse a duplicate code.
    """
    step(2, "Upload the file - its columns drive everything that follows")
    up = st.file_uploader("Extract (CSV, Excel or JSON)", type=["csv", "txt", "xlsx", "xlsm", "json"],
                          key="wiz_file")
    if up is None:
        st.info("Upload the extract first. The app reads its columns, proposes a field mapping "
                "and the date and number conventions, and you confirm each one.",
                icon=":material/info:")
        return
    path = save_upload("_incoming", up)
    reader = ingest.READER_FOR_SUFFIX.get(path.suffix.lower(), "csv")
    block: dict = {"reader": reader, "path": path.name}
    options: dict = {}
    doc = None

    step(3, "Read the file")
    r1, r2, r3 = st.columns(3)
    if reader == "csv":
        delims = [",", ";", "\t", "|"]
        options["delimiter"] = r1.selectbox("Delimiter", delims, index=delims.index(ingest.guess_delimiter(path)),
                                            format_func=lambda d: {"\t": "tab"}.get(d, d), key="wiz_delim",
                                            help="Proposed from the header line; change it if the preview looks wrong.")
        options["encoding"] = r2.text_input("Encoding", "utf-8-sig", key="wiz_enc")
        block.update(delimiter=options["delimiter"], encoding=options["encoding"])
    elif reader == "excel":
        sheets = ingest.peek_source(path, "excel")["sheets"]
        options["sheet"] = r1.selectbox("Data sheet", sheets, key="wiz_sheet")
        options["header_row"] = r2.number_input("Header row", 1, 60, 1, key="wiz_hdr")
        marker = r3.text_input("Printed total-row marker (optional)", "", key="wiz_marker")
        chart_of_accounts_sheet = st.selectbox("Chart-of-accounts sheet (optional)", ["(none)"] + sheets, key="wiz_chart_sheet")
        block.update(sheet=options["sheet"], header_row=int(options["header_row"]))
        if marker.strip():
            block["total_row_marker"] = marker.strip()
        if chart_of_accounts_sheet != "(none)":
            cols = ingest.peek_source(path, "excel", {"sheet": chart_of_accounts_sheet})["columns"]
            cc1, cc2 = st.columns(2)
            block.update(chart_of_accounts_sheet=chart_of_accounts_sheet,
                         chart_of_accounts_code_column=cc1.selectbox("Code column", cols, key="wiz_chart_code"),
                         chart_of_accounts_label_column=cc2.selectbox("Label column", cols, index=min(1, len(cols) - 1),
                                                        key="wiz_chart_label"))
    else:
        doc = json.loads(path.read_text(encoding="utf-8"))
        top = ingest.peek_source(path, "json")
        lists = top["nested"] or [""]
        options["records_path"] = r1.selectbox("Records list", lists, key="wiz_records")
        block["records_path"] = options["records_path"]
        if isinstance(doc, dict):
            blocks = [k for k, v in doc.items() if isinstance(v, dict)]
            meta = r2.selectbox("Metadata block (optional)", ["(none)"] + blocks,
                                index=(blocks.index("metadata") + 1) if "metadata" in blocks else 0,
                                key="wiz_meta")
            if meta != "(none)":
                block["metadata_path"] = meta
                stamps = [k for k, v in (doc.get(meta) or {}).items() if ingest.guess_date_format([v])]
                if stamps:
                    block["metadata_extracted_at"] = r3.selectbox("Extraction timestamp key", stamps,
                                                                  key="wiz_stamp")

    try:
        peek = ingest.peek_source(path, reader, options)
    except Exception as exc:
        st.error(f"Could not read the file with these settings: {exc}")
        return
    if not peek["columns"]:
        st.warning("No columns found - check the sheet, header row or records list.")
        return
    st.caption(f"{len(peek['columns'])} columns - first rows as supplied:")
    preview = pd.DataFrame(peek["sample"], columns=peek["columns"]).fillna("").astype(str)
    st.dataframe(preview, width="stretch", hide_index=True)

    if reader == "json" and peek["nested"]:
        child = st.selectbox("Nested sub-transactions (optional)", ["(none)"] + peek["nested"], key="wiz_child")
        if child != "(none)":
            records = doc.get(options["records_path"], []) if isinstance(doc, dict) else doc
            first = next((r for r in records if r.get(child)), {})
            ckeys = list((first.get(child) or [{}])[0].keys())
            cg = ingest.guess_field_map(ckeys)
            block["child_path"] = child
            block["child_field_map"] = {k: cg[k] for k in ("source_record_id", "description", "amount") if k in cg}

    step(4, "Confirm the field mapping - proposed from the column names")
    guess = ingest.guess_field_map(peek["columns"])
    NONE = "(not present)"
    cols = [NONE] + peek["columns"]
    grid = st.columns(4)
    fmap: dict = {}
    for i, field in enumerate(ingest.CANONICAL_FIELDS):
        default = guess.get(field, NONE)
        val = grid[i % 4].selectbox(field, cols, index=cols.index(default), key=f"wiz_{field}")
        if val != NONE:
            fmap[field] = val
    missing = [f for f in ("account_code", "amount", "txn_date") if f not in fmap]
    if missing:
        st.warning(f"Required: {', '.join(missing)}.", icon=":material/warning:")
    block["field_map"] = fmap

    sample_dates = [r.get(fmap["txn_date"]) for r in peek["sample"]] if "txn_date" in fmap else []
    sample_amts = [r.get(fmap["amount"]) for r in peek["sample"]] if "amount" in fmap else []
    conv_guess = ingest.guess_conventions(sample_amts)
    v1, v2, v3, v4 = st.columns(4)
    date_fmt = v1.text_input("Date format (strptime)", ingest.guess_date_format(sample_dates) or "%Y-%m-%d",
                             help="Only this format is accepted - no dd/mm vs mm/dd guessing.", key="wiz_datefmt")
    dec = v2.selectbox("Decimal separator", [".", ","], index=[".", ","].index(conv_guess["decimal_separator"]),
                       key="wiz_dec")
    thou = v3.multiselect("Thousands separators", [",", ".", " "], default=conv_guess["thousands_separators"],
                          key="wiz_thou")
    suff = v4.text_input("Currency suffixes to strip", ", ".join(conv_guess["strip_suffixes"]), key="wiz_suff")
    block["conventions"] = {"date_formats": [date_fmt], "decimal_separator": dec,
                            "thousands_separators": thou,
                            "strip_suffixes": [s.strip() for s in suff.split(",") if s.strip()]}

    step(5, "Describe the country")
    d1, d2, d3, d4 = st.columns(4)
    code = d1.text_input("Country code", placeholder="e.g. CTX", max_chars=8, key="wiz_code").strip().upper()
    name = d2.text_input("Country name", placeholder="e.g. Country X", key="wiz_name")
    sample_ccy = next((str(r.get(fmap["currency"])) for r in peek["sample"]
                       if fmap.get("currency") and r.get(fmap["currency"])), "")
    currency = d3.text_input("Currency (ISO)", sample_ccy, max_chars=3, key="wiz_ccy").strip().upper()
    rate = d4.number_input("Local units per 1 USD", min_value=0.0, value=0.0, step=1.0, key="wiz_rate",
                           help="Indicative rate for USD totals; 0 = no conversion.")
    # Period proposed from the sample dates: the July-June year containing the
    # earliest one. A window read off the whole file would hide out-of-period
    # rows, which is the point of asserting it - so the analyst confirms.
    parsed = [ingest.harmonise.parse_date(v, {"date_formats": [date_fmt]})[0] for v in sample_dates]
    parsed = sorted(d for d in parsed if d)
    if parsed:
        y, m = int(parsed[0][:4]), int(parsed[0][5:7])
        fy_start = y if m >= 7 else y - 1
    else:
        fy_start = date.today().year - 1
    e1, e2, e3, e4, e5 = st.columns(5)
    language = e1.selectbox("Language", ["en", "fr", "pt", "other"], key="wiz_lang")
    system = e2.text_input("Source system", "National IFMIS extract", key="wiz_system")
    fy = e3.text_input("Fiscal year label", f"FY{fy_start}/{str(fy_start + 1)[-2:]}", key="wiz_fy")
    p_start = e4.date_input("Period start", date(fy_start, 7, 1), key="wiz_pstart",
                            help="Proposed from the sample dates as a July-June year. Confirm.")
    p_end = e5.date_input("Period end", date(fy_start + 1, 6, 30), key="wiz_pend")
    block.update(name=name, currency=currency or None, language=language, source_system=system,
                 fiscal_year=fy, period={"start": p_start.isoformat(), "end": p_end.isoformat()},
                 data_dir=f"uploads/{code or '_incoming'}")
    block = {k: v for k, v in block.items() if v not in (None, "", [], {})}
    with st.expander("Entry that will be written to config/sources.yml"):
        st.code(yaml.safe_dump({code or "CODE": block}, sort_keys=False, allow_unicode=True), language="yaml")

    if code in existing:
        st.warning(f"{code} already exists - choose it from the list to re-load it.")
    ready = bool(code and name and currency and not missing and code not in existing)
    if st.button("Save country and load", type="primary", icon=":material/save:", disabled=not ready,
                 key="wiz_save"):
        with st.spinner(f"Adding {code} and loading..."):
            try:
                dest = UPLOADS / code
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest / path.name))
                block["data_dir"] = f"uploads/{code}"
                db.append_source(code, block)
                if rate > 0:
                    db.upsert_fx_rate(currency, rate)
                conn = get_conn()
                pipeline.ensure_countries(conn, {code: block})
                result = pipeline.load_country(conn, code, block, DATA_DIR, db.load_fx(), replace=True)
                cls = pipeline.classify_country(conn, code)
            except Exception as exc:
                st.error(f"Could not add {code}: {exc}")
            else:
                st.session_state["load_report"] = (code, result, cls)
                st.session_state["flash_ingest"] = f"{code} - {name} added to config/sources.yml and loaded."
                # Widget state cannot be changed once the widget exists in this run,
                # so the reset and the jump to the new country happen on the next run.
                st.session_state["wiz_reset"] = True
                st.session_state["ingest_country_pending"] = f"{code} - {name}"
                st.rerun()


def render_load_history() -> None:
    """One row per batch with the counts at each stage and the control-total
    reconciliation, a funnel chart of the same, and where each country's
    account labels came from."""
    section("Load history and reconciliation",
            "One row per load: the file, how many rows it held, how many were staged, became facts "
            "and count towards totals, and - where the source printed its own total - whether the "
            "loaded sum matches it.",
            "Where rows in file exceed staged, the difference is a printed TOTAL line captured as the "
            "control total rather than loaded as a transaction. Difference 0.00 against that total is "
            "the strongest evidence that amounts were parsed correctly - Country B is the example.")
    lin = q("""SELECT b.batch_id, b.country_code, b.source_file, b.source_system,
                      b.rows_read, b.rows_loaded,
                      (SELECT COUNT(*) FROM fact_expenditure f JOIN stg_raw_record s ON s.raw_id=f.raw_id
                        WHERE s.batch_id=b.batch_id) facts,
                      (SELECT COUNT(*) FROM fact_expenditure f JOIN stg_raw_record s ON s.raw_id=f.raw_id
                        WHERE s.batch_id=b.batch_id AND f.record_level='CHILD') sub_transactions,
                      (SELECT COUNT(*) FROM fact_expenditure f JOIN stg_raw_record s ON s.raw_id=f.raw_id
                        WHERE s.batch_id=b.batch_id AND f.is_countable=1) countable,
                      b.control_total, b.loaded_total,
                      CASE WHEN b.control_total IS NULL THEN NULL
                           ELSE ROUND(COALESCE(b.loaded_total,0)-b.control_total,2) END difference,
                      b.started_at, b.finished_at
                 FROM ingestion_batch b ORDER BY b.batch_id""")
    for c in ("control_total", "loaded_total", "difference"):
        lin[c] = pd.to_numeric(lin[c])
    st.dataframe(lin, width="stretch", hide_index=True, column_config={
        "batch_id": T("Batch", "Load number - one per file per country."),
        "country_code": T("Country", COUNTRY_HELP),
        "source_file": T("File", "The extract that was loaded."),
        "source_system": T("System", "Source system as declared in sources.yml."),
        "rows_read": num("Rows in file", "Every data row the reader saw. Where a source prints a TOTAL line "
                                         "(Country B), that line is counted here but never staged: it is "
                                         "captured as the printed total and reconciled against the load."),
        "rows_loaded": num("Staged", "Rows landed verbatim in stg_raw_record - transactions only; a printed "
                                     "TOTAL line is excluded, which is why Country B shows 2,001 -> 2,000."),
        "facts": num("Facts", "Harmonised records, including sub-transactions."),
        "sub_transactions": num("Splits", "Sub-transactions loaded beneath a parent."),
        "countable": num("Countable", "Records that enter totals - each parent/child set counted once."),
        "control_total": num("Printed total", "Control total printed by the source, where it has one."),
        "loaded_total": num("Loaded total", "Sum of countable amounts in the source currency."),
        "difference": num("Difference", "Loaded minus printed. 0.00 is the proof the amounts were parsed correctly."),
        "started_at": T("Started", "UTC."), "finished_at": T("Finished", "UTC.")})

    st.markdown("**Record counts through the pipeline**",
                help="Rows at each stage per country. A drop from staged to countable is expected "
                     "where nested splits replace their parent; anything else is in the DQ register.")
    funnel = lin.melt(id_vars=["country_code"], value_vars=["rows_read", "rows_loaded", "facts", "countable"],
                      var_name="stage", value_name="records")
    funnel["stage"] = funnel["stage"].map({"rows_read": "1 rows in file", "rows_loaded": "2 staged",
                                           "facts": "3 facts", "countable": "4 countable"})
    st.altair_chart(hbar(funnel, "stage", "records", "country_code", stack=False, height=340,
                         x_title="records", sort=["1 rows in file", "2 staged", "3 facts", "4 countable"],
                         domain=sorted(funnel["country_code"].unique().tolist())), width="stretch")
    st.markdown("**Chart of accounts held per country**",
                help="Where each country's account labels came from: its own chart-of-accounts sheet "
                     "(authoritative) or observed in the transaction extract.")
    st.dataframe(q("""SELECT country_code, source, COUNT(*) accounts FROM dim_account
                      GROUP BY 1,2 ORDER BY 1"""), width="stretch", hide_index=True,
                 column_config={"country_code": T("Country", COUNTRY_HELP),
                                "source": T("Label source", "country_chart_of_accounts = the country's own chart-of-accounts "
                                                            "sheet (authoritative); observed_in_extract = label "
                                                            "taken from the transaction descriptions."),
                                "accounts": num("Accounts", "Distinct account codes held for the country.")})


def render_load_report() -> None:
    """Report on the load that just ran (left in session state by the Load
    button): stage counts, reconciliation, DQ rules fired, classification
    outcomes, and a nudge to the Account mapping tab if codes are unmapped."""
    code, result, cls = st.session_state["load_report"]
    st.divider()
    section(f"Load report - {code}",
            "What happened to the file just loaded: how many rows came in and survived each stage, "
            "whether the load reconciles to the source's own printed total, which data-quality rules "
            "fired, and how the records were classified.",
            "Rows in file -> staged -> facts is the completeness trail; the DQ table is the assessment.")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Rows in file", f"{result['rows_read']:,}", border=True,
              help="Every data row the reader saw, including a printed TOTAL line if there was one.")
    m2.metric("Staged", f"{result['rows_staged']:,}", border=True,
              help="Rows landed verbatim. A printed TOTAL line is captured as a control figure, not staged.")
    m3.metric("Facts", f"{result['facts']:,}", delta=f"+{result['children']:,} sub-transactions",
              delta_color="off", border=True,
              help="Harmonised records. Sub-transactions are loaded beneath their parent and "
                   "counted only where they reconcile to it.")
    m4.metric("Countable total (local)", f"{nz(result['loaded_total']):,.0f}", border=True,
              help="Sum of countable amounts in the source currency - the figure compared with the "
                   "printed control total where the source provides one.")
    if result["control_total"]:
        diff = nz(result["difference"])
        ok = abs(diff) <= 0.5
        m5.metric("vs printed control total", f"{diff:,.2f}",
                  delta="reconciles" if ok else "does not reconcile",
                  delta_color="normal" if ok else "inverse", border=True)
    else:
        m5.metric("Control total", "not supplied", border=True,
                  help="Ask for one in every extract specification - it is the cheapest strong check.")

    l, r = st.columns([3, 2])
    with l:
        st.markdown("**Data-quality report for this load**",
                    help="Every rule that fired on this file, with the number of rows affected and what "
                         "the rule means. Errors are findings to raise with the country; warnings are "
                         "assumptions the load made; information is context.")
        issues = q("""SELECT rule_code, severity, COUNT(*) issues FROM dq_issue
                       WHERE batch_id=? OR (country_code=? AND batch_id IS NULL)
                       GROUP BY 1,2 ORDER BY CASE severity WHEN 'ERROR' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
                                issues DESC""", (result["batch_id"], code))
        if issues.empty:
            st.success("No issues raised.", icon=":material/check_circle:")
        else:
            issues["what it means"] = issues["rule_code"].map(RULE_HELP).fillna("")
            st.dataframe(severity_style(issues), width="stretch", hide_index=True, column_config={
                "rule_code": T("Rule", RULE_COL_HELP), "severity": T("Severity", SEVERITY_HELP),
                "issues": num("Issues", "Rows in this load the rule fired on."),
                "what it means": T("What it means", "The rule in one line, and what the pipeline did.", width="large")})
    with r:
        st.markdown("**Classification**",
                    help="How the records were classified against SHA: mapped to an HC code, capital "
                         "formation, an input with no determinable function, or unclassified.")
        oc = pd.DataFrame({
            "outcome": ["CLASSIFIED", "CAPITAL", "ADMIN_INPUT", "UNCLASSIFIED"],
            "records": [cls["sha_classified"], cls["capital"], cls["admin_input"], cls["sha_unclassified"]]})
        st.dataframe(oc, width="stretch", hide_index=True,
                     column_config={"outcome": T("SHA outcome", OUTCOME_COL_HELP),
                                    "records": num("Records", "Records from this load with that outcome.")})
        st.metric("Records needing review", f"{cls['needs_review']:,}", border=True,
                  help="Records the classifier flagged for a human - low confidence, an unmapped code, "
                       "a code/text conflict or untrusted text. They are in the Review queue.")
        if cls["injection_flagged"]:
            st.error(f"{cls['injection_flagged']} descriptions carry instruction-like text - "
                     f"quarantined. See Data quality.", icon=":material/gpp_maybe:")
        in_use = q("""SELECT COUNT(*) n FROM dim_account a WHERE a.country_code=?
                       AND EXISTS (SELECT 1 FROM fact_expenditure f WHERE f.account_sk=a.account_sk)""",
                   (code,)).iloc[0]["n"]
        mapped = len(db.load_account_mapping().get(code) or {})
        if in_use > mapped:
            st.warning(f"{in_use} account codes in use, {mapped} in the account mapping. Unmapped codes were "
                       f"classified from text and routed to review - map them in the **Account mapping** tab.",
                       icon=":material/account_tree:")


with tab_ingest:
    if st.session_state.pop("wiz_reset", False):
        for k in [k for k in st.session_state if k.startswith("wiz_") or k == "saved:_incoming"]:
            del st.session_state[k]
    if "ingest_country_pending" in st.session_state:
        st.session_state["ingest_country"] = st.session_state.pop("ingest_country_pending")
    flash("flash_ingest")
    section("Load an extract",
            "Where data enters. Pick a country (or add one from its file), upload the extract, and it "
            "runs through the same pipeline as run_pipeline.py: staged verbatim, harmonised, "
            "reconciled against any printed total, classified. The load report below is the "
            "data-quality assessment for that file.",
            "A re-delivered file replaces the country's previous batch, so a period is never counted twice.")

    NEW = "+ Add a new country from its file"
    opts = country_options()
    step(1, "Choose the country")
    choice = st.selectbox("Country", list(opts.values()) + [NEW], key="ingest_country",
                          label_visibility="collapsed")
    if choice == NEW:
        new_country_wizard(opts)
    else:
        code = next(c for c, label in opts.items() if label == choice)
        load_known_country(code, db.load_sources()[code])
    if st.session_state.get("load_report"):
        render_load_report()
    st.divider()
    render_load_history()

# ================================================================ ACCOUNT MAPPING ==
with tab_map:
    flash("flash_map")
    section("Account mapping",
            "The primary classification layer. Each row is one of the country's own account codes as "
            "observed in its data, with the SHA function and SRHR theme it maps to and the confidence "
            "of that mapping. Unmapped codes fall back to the description and go to review.",
            "Edit the grid and save: the mapping is written to config/account_mapping.yml and the country is "
            "re-classified at once - no file is re-read.")
    countries = q("SELECT country_code, country_name FROM dim_country ORDER BY 1")
    labels = {r.country_code: f"{r.country_code} - {r.country_name}" for r in countries.itertuples()}
    if not labels:
        st.info("No countries yet.")
    else:
        pick = st.selectbox("Country", list(labels.values()), key="map_country", label_visibility="collapsed")
        cc = next(c for c, l in labels.items() if l == pick)
        mapping = db.load_account_mapping().get(cc) or {}
        acc = q("""SELECT a.account_code, a.account_label, a.source,
                          COUNT(f.expenditure_id) records, SUM(f.amount_original) total_local
                     FROM dim_account a
                     LEFT JOIN fact_expenditure f ON f.account_sk=a.account_sk AND f.is_countable=1
                    WHERE a.country_code=? GROUP BY 1,2,3 ORDER BY records DESC, 1""", (cc,))
        rows = []
        for r in acc.itertuples():
            e = mapping.get(str(r.account_code)) or {}
            rows.append({
                "account_code": str(r.account_code), "label": e.get("label") or r.account_label or "",
                "records": int(r.records), "total_local": float(r.total_local or 0),
                "mapped": bool(e),
                "outcome": e.get("outcome") if e else None,
                "sha": e.get("sha") or "" if e else "",
                "srhr": e.get("srhr") or "" if e else "",
                "confidence": float(e.get("confidence", 0.8)) if e else 0.8,
                "needs_review": bool(e.get("needs_review", False)),
                "note": e.get("note") or "",
            })
        grid = pd.DataFrame(rows)
        n_map = int(grid["mapped"].sum()) if not grid.empty else 0
        rec_unmapped = int(grid.loc[~grid["mapped"], "records"].sum()) if not grid.empty else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("Account codes in use", len(grid), border=True,
                  help="Distinct account codes that appear in this country's loaded records.")
        c2.metric("Mapped", n_map, delta=f"{len(grid) - n_map} unmapped", delta_color="off",
                  delta_arrow="off", border=True,
                  help="Codes with an entry in the account mapping. Unmapped codes are classified from text "
                       "at lower confidence and routed to review.")
        c3.metric("Records on unmapped codes", f"{rec_unmapped:,}", border=True,
                  help="How many records would gain an account mapping classification by mapping the rest.")

        sha_codes = [""] + q("SELECT sha_code FROM dim_sha ORDER BY 1")["sha_code"].tolist()
        srhr_codes = [""] + q("SELECT srhr_code FROM dim_srhr ORDER BY 1")["srhr_code"].tolist()
        edited = st.data_editor(
            grid, width="stretch", hide_index=True, key=f"editor_{cc}", num_rows="fixed",
            column_config={
                "account_code": T("Code", "The country's own account code, exactly as it appears in the extract.",
                                  disabled=True),
                "label": T("Label", "From the country's chart-of-accounts sheet where it supplies one; otherwise "
                                    "the description observed with the code.", disabled=True, width="large"),
                "records": num("Records", "Countable records on this code.", disabled=True),
                "total_local": num("Total (local)", "Their sum in the country's currency.", disabled=True),
                "mapped": st.column_config.CheckboxColumn("Mapped", help="Whether config/account_mapping.yml has an entry "
                                                          "for this code.", disabled=True),
                "outcome": st.column_config.SelectboxColumn(
                    "Outcome", options=["CLASSIFIED", "CAPITAL", "ADMIN_INPUT"], required=False,
                    help="CLASSIFIED needs an SHA code. CAPITAL = SHA HK, outside HC. ADMIN_INPUT = provisional "
                         "HC.7, always flagged for review."),
                "sha": st.column_config.SelectboxColumn("SHA", options=sha_codes, required=False,
                                                        help="SHA 2011 function the code maps to."),
                "srhr": st.column_config.SelectboxColumn("SRHR", options=srhr_codes, required=False,
                                                         help="SRHR theme, or SRHR.NA for non-SRHR spend."),
                "confidence": st.column_config.NumberColumn("Confidence", min_value=0.0, max_value=1.0,
                                                            step=0.05, format="%.2f",
                                                            help="Evidence grade recorded on every record classified "
                                                                 "from this entry: 0.95 purpose unambiguous, 0.80 "
                                                                 "boundary between two codes arguable, 0.40 input-type. "
                                                                 "Below 0.70 the records go to review automatically."),
                "needs_review": st.column_config.CheckboxColumn(
                    "Always review",
                    help="Ticked: every record booked to this code goes to the Review queue for a human, "
                         "whatever its grade. Ticked by design on codes that name an input (salaries, "
                         "fuel, travel, stationery) rather than a health function - the extract cannot "
                         "say what they were for. Unticked: records are counted as classified unless "
                         "something else flags them (grade below 0.70, untrusted text, a code/text "
                         "conflict). See 'Why some codes are always reviewed' below."),
                "note": T("Note", "Free text appended to the explanation on each record.", width="medium"),
            },
        )
        with st.expander("Why some codes are always reviewed"):
            st.markdown(
                "One test is applied to every account label: **does it say what the money was *for* "
                "(a health function), or only what it *bought* (an input)?** SHA's HC classification is "
                "functional - curative care, prevention, medical goods, administration. A label such as "
                "*Cervical cancer screening* or *Medical drugs* names its function and maps cleanly at a "
                "high grade. A label such as *Basic salaries*, *Domestic travel* or *Refined fuels* names "
                "an input with no purpose attached: a nurse's salary is curative care and a clerk's is "
                "administration; travel for an immunisation outreach is prevention and travel to a budget "
                "meeting is administration - and the extract does not say which.\n\n"
                "Real health accounts resolve this with **allocation keys** - a share of staff time, a "
                "vehicle log, a cost-centre split - which the extracts do not contain. So these codes are "
                "given the outcome `ADMIN_INPUT`, provisionally placed under governance and administration "
                "(HC.7) at grade 0.40, and **always reviewed**, so that a person sees them rather than "
                "salaries and fuel quietly landing in administration. This is a limitation of the data, "
                "not of the method; agreeing allocation keys with each country is item 8 of the production "
                "list, and this grid is where an agreed split would replace the flag.")
            flagged = grid[grid["needs_review"]].copy() if not grid.empty else grid
            if not flagged.empty:
                flagged["why"] = flagged["note"].where(flagged["note"].astype(bool),
                                                       "Input-type code - the function is not determinable from the extract.")
                st.markdown(f"**{cc}: {len(flagged)} of {len(grid)} codes are always reviewed**")
                st.dataframe(flagged[["account_code", "label", "outcome", "records", "why"]],
                             width="stretch", hide_index=True, key=f"why_review_{cc}", column_config={
                                 "account_code": T("Code", "The country's account code."),
                                 "label": T("Label", "As supplied or observed."),
                                 "outcome": T("Outcome", OUTCOME_COL_HELP),
                                 "records": num("Records", "Countable records on this code."),
                                 "why": T("Why", "The reason it cannot be placed in a function from the "
                                                 "extract alone.", width="large")})
        st.caption("A row is saved when it has an outcome, an SHA code or an SRHR code. CAPITAL and "
                   "ADMIN_INPUT need no SHA code; CLASSIFIED needs one.")
        if st.button("Save account mapping and re-classify", type="primary", icon=":material/save:", key=f"save_{cc}"):
            entries = dict(mapping)      # keep codes in the file that are not in the grid
            skipped = []
            for r in edited.itertuples():
                outcome = r.outcome or ("CLASSIFIED" if r.sha else None)
                if not outcome and not r.sha and not r.srhr:
                    entries.pop(r.account_code, None)
                    continue
                if outcome == "CLASSIFIED" and not r.sha:
                    skipped.append(r.account_code)
                    continue
                entry = {"label": r.label, "outcome": outcome,
                         "sha": r.sha or None if outcome == "CLASSIFIED" else (r.sha or ("HC.7" if outcome == "ADMIN_INPUT" else None)),
                         "srhr": r.srhr or "SRHR.NA", "confidence": round(float(r.confidence), 2)}
                if r.needs_review or outcome == "ADMIN_INPUT":
                    entry["needs_review"] = True
                if r.note:
                    entry["note"] = r.note
                entries[r.account_code] = entry
            db.upsert_account_mapping(cc, entries)
            stats = pipeline.classify_country(get_conn(), cc)
            msg = (f"Account mapping for {cc} saved ({len(entries)} codes) and {stats['records']:,} records "
                   f"re-classified - {stats['needs_review']:,} still need review.")
            if skipped:
                msg += f" Skipped (CLASSIFIED without an SHA code): {', '.join(skipped)}."
            st.session_state["flash_map"] = msg
            st.rerun()

# =============================================================== REFERENCES ==

def render_reference_editor(scheme: str, title: str, what: str) -> None:
    """One editable reference list. Supplied rows can be edited, not deleted;
    added rows can be deleted while no classification uses them."""
    table, code_col, desc_col = db.REFERENCE_SCHEMES[scheme]
    st.markdown(f"**{title}**", help=what)
    rows = q(f"""SELECT d.{code_col} code, d.{desc_col} description, d.notes, d.source,
                        (SELECT COUNT(*) FROM classification c
                          WHERE c.is_current=1 AND c.scheme=? AND c.code=d.{code_col}) records
                   FROM {table} d ORDER BY d.{code_col}""", (scheme.upper(),))
    rows["notes"] = rows["notes"].fillna("")
    # source and usage are kept for the save rules, not shown: a reference list
    # is the code, what it means, and any guidance - nothing about the data.
    before = {r.code: (r.description, r.notes, r.source) for r in rows.itertuples()}
    edited = st.data_editor(
        rows[["code", "description", "notes"]], width="stretch", hide_index=True, num_rows="dynamic",
        key=f"ref_{scheme}", height=min(38 * (len(rows) + 2) + 4, 720),
        column_config={
            "code": T("Code", "The code as it appears in classifications and the account mapping. "
                              "Supplied codes cannot be renamed or deleted.", width="small"),
            "description": T("Description", "What the code means - shown beside it throughout the app.",
                             width="large"),
            "notes": T("Notes", "Optional guidance for the analyst mapping account codes to it.",
                       width="medium"),
        })
    if st.button(f"Save {title}", type="primary", icon=":material/save:", key=f"save_ref_{scheme}"):
        conn = get_conn()
        overrides = db.load_reference_overrides()
        mine = dict(overrides.get(scheme) or {})
        problems, after = [], {}
        for r in edited.itertuples():
            code = str(r.code or "").strip()
            desc = str(r.description or "").strip()
            notes = str(r.notes or "").strip()
            if not code and not desc:
                continue
            if not code or not desc:
                problems.append(f"'{code or desc}' needs both a code and a description")
                continue
            if code in after:
                problems.append(f"{code} appears twice")
                continue
            after[code] = (desc, notes)
        for code in before:
            if code not in after:
                if before[code][2] in ("supplied", "edited"):
                    problems.append(f"{code} is a supplied code and cannot be deleted")
                elif db.reference_in_use(conn, scheme, code):
                    problems.append(f"{code} is used by {db.reference_in_use(conn, scheme, code):,} "
                                    f"classifications and cannot be deleted")
        if problems:
            st.error("Not saved: " + "; ".join(problems) + ".", icon=":material/error:")
        else:
            supplied = {r[code_col]: (r[desc_col], r.get("notes") or "")
                        for r in db._read_csv(DATA_DIR / f"ref_{scheme}_classification.csv")}
            for code, (desc, notes) in after.items():
                if code in supplied and supplied[code] == (desc, notes):
                    mine.pop(code, None)              # back to the supplied wording
                else:
                    mine[code] = {"description": desc, "notes": notes or None}
            for code in before:
                if code not in after:
                    mine.pop(code, None)
                    db.delete_reference(conn, scheme, code)
            # a code restored to its supplied wording goes back to source=supplied
            for code, (desc, notes) in supplied.items():
                if code not in mine and code in after:
                    conn.execute(f"UPDATE {table} SET {desc_col}=?, notes=?, source='supplied'"
                                 f" WHERE {code_col}=?", (desc, notes or None, code))
            overrides[scheme] = mine
            db.save_reference_overrides(overrides)
            db.apply_reference_overrides(conn, overrides)
            st.session_state["flash_refs"] = (
                f"{title} saved - {len(mine)} addition(s)/edit(s) kept in config/references.yml.")
            st.rerun()


def render_country_editor() -> None:
    """Countries: the supplied list, those configured on the Ingest tab, and any
    added here ahead of their first extract."""
    st.markdown("**Countries**",
                help="From the supplied ref_countries.csv, plus countries configured on the Ingest tab "
                     "(config/sources.yml), plus any added here before their first extract arrives. "
                     "Add a row and save; when the country's file comes, the Ingest wizard uses the code.")
    rows = q("SELECT country_code code, country_name name, primary_currency currency, language"
             " FROM dim_country ORDER BY 1")
    rows["language"] = rows["language"].fillna("")
    before = {r.code: (r.name, r.currency, r.language) for r in rows.itertuples()}
    supplied = {r["country_code"]: (r["country_name"], r["primary_currency"], r.get("language") or "")
                for r in db._read_csv(DATA_DIR / "ref_countries.csv")}
    configured = set(db.load_sources())
    edited = st.data_editor(
        rows, width="stretch", hide_index=True, num_rows="dynamic", key="ref_countries",
        column_config={
            "code": T("Code", "Short code used everywhere in the app - letters and digits, e.g. CTD. "
                              "Supplied and configured codes cannot be renamed or deleted here.", width="small"),
            "name": T("Country", "Display name.", width="medium"),
            "currency": T("Currency", "ISO 4217 reporting currency, three letters. Add its rate to "
                                      "config/fx_rates.yml for USD figures.", width="small"),
            "language": T("Language", "Language of the extract's descriptions (en, fr, pt ...).", width="small"),
        })
    if st.button("Save countries", type="primary", icon=":material/save:", key="save_ref_countries"):
        conn = get_conn()
        overrides = db.load_reference_overrides()
        mine = dict(overrides.get("countries") or {})
        problems, after = [], {}
        for r in edited.itertuples():
            code = str(r.code or "").strip().upper()
            name = str(r.name or "").strip()
            ccy = str(r.currency or "").strip().upper()
            lang = str(r.language or "").strip()
            if not code and not name:
                continue
            if not code or not name or not ccy:
                problems.append(f"'{code or name}' needs a code, a name and a currency")
            elif not code.isalnum() or len(code) > 8:
                problems.append(f"{code}: letters and digits only, up to 8")
            elif len(ccy) != 3 or not ccy.isalpha():
                problems.append(f"{code}: currency must be a three-letter ISO code")
            elif code in after:
                problems.append(f"{code} appears twice")
            else:
                after[code] = (name, ccy, lang)
        for code in before:
            if code not in after:
                if code in supplied:
                    problems.append(f"{code} is a supplied country and cannot be deleted")
                elif code in configured:
                    problems.append(f"{code} is configured on the Ingest tab (sources.yml) and cannot be deleted here")
                elif db.country_has_data(conn, code):
                    problems.append(f"{code} has data loaded and cannot be deleted")
        if problems:
            st.error("Not saved: " + "; ".join(problems) + ".", icon=":material/error:")
        else:
            for code, (name, ccy, lang) in after.items():
                if code in supplied and supplied[code] == (name, ccy, lang):
                    mine.pop(code, None)
                elif code in before and code not in supplied and before[code] == (name, ccy, lang) and code not in mine:
                    continue                       # configured on the Ingest tab, unchanged
                else:
                    mine[code] = {"name": name, "currency": ccy, "language": lang or None}
            for code in before:
                if code not in after:
                    mine.pop(code, None)
                    db.delete_country(conn, code)
            for code, (name, ccy, lang) in supplied.items():
                if code not in mine and code in after:
                    conn.execute("UPDATE dim_country SET country_name=?, primary_currency=?, language=?"
                                 " WHERE country_code=?", (name, ccy, lang or None, code))
            overrides["countries"] = mine
            db.save_reference_overrides(overrides)
            db.apply_reference_overrides(conn, overrides)
            st.session_state["flash_refs"] = (
                f"Countries saved - {len(mine)} addition(s)/edit(s) kept in config/references.yml. "
                f"A country added here appears on the Ingest tab once its extract is configured.")
            st.rerun()


with tab_refs:
    flash("flash_refs")
    section("Reference classifications",
            "The analytical frameworks every record is classified against: the SHA 2011 health-care "
            "functions and the SRHR themes, as supplied with the assessment, plus anything added or "
            "edited here. Codes added here become selectable in the Account mapping and Review "
            "queue at once.",
            "Edit a description, add a row for a new code or country, save. The supplied files are "
            "never modified - additions and edits are kept in config/references.yml and re-applied on "
            "every load. A supplied entry cannot be deleted; an added one can, until it is in use.")
    render_reference_editor("sha", "SHA 2011 health-care functions",
                            "Functional classification of current health expenditure (HC codes) from the "
                            "supplied ref_sha_classification.csv.")
    st.markdown("")
    render_reference_editor("srhr", "SRHR themes",
                            "Sexual and reproductive health and rights themes from the supplied "
                            "ref_srhr_classification.csv; SRHR.NA marks non-SRHR spend.")
    st.markdown("")
    render_country_editor()

# ============================================================= REVIEW QUEUE ==
with tab_review:
    flash("flash_review")
    section("Records the classifier could not settle",
            "Every current classification that is flagged for review and still pending, largest "
            "value first. Each row shows the automated answer, how it was reached and its confidence.",
            "Select a row to see the reasoning and accept it or record a correction. A decision "
            "supersedes the automated result; the financial fact is never touched.")
    r1, r2 = st.columns([1, 3])
    scheme = r1.segmented_control("Scheme", ["SHA", "SRHR"], default="SHA", key="rev_scheme") or "SHA"
    ccs = ["All"] + q("SELECT country_code FROM dim_country ORDER BY 1")["country_code"].tolist()
    rc = r2.segmented_control("Country", ccs, default="All", key="rev_country") or "All"
    rq = q(f"""SELECT cl.classification_id, f.expenditure_id, f.country_code,
                      f.source_record_id, a.account_code, a.account_label,
                      f.description, f.amount_original, f.currency_original,
                      f.amount_usd, cl.code, cl.outcome, cl.method,
                      cl.confidence, cl.explanation
                 FROM classification cl
                 JOIN fact_expenditure f ON f.expenditure_id=cl.expenditure_id
                 LEFT JOIN dim_account a ON a.account_sk=f.account_sk
                WHERE cl.is_current=1 AND cl.scheme=? AND cl.review_status='PENDING'
                  AND (cl.needs_review=1 OR cl.outcome='UNCLASSIFIED')
                  {"AND f.country_code=?" if rc != "All" else ""}
                ORDER BY f.amount_usd DESC LIMIT 500""",
           (scheme, rc) if rc != "All" else (scheme,))
    pending = q(f"""SELECT COUNT(*) n FROM classification cl JOIN fact_expenditure f USING(expenditure_id)
                     WHERE cl.is_current=1 AND cl.scheme=? AND cl.review_status='PENDING'
                       AND (cl.needs_review=1 OR cl.outcome='UNCLASSIFIED')
                       {"AND f.country_code=?" if rc != "All" else ""}""",
                (scheme, rc) if rc != "All" else (scheme,)).iloc[0]["n"]
    st.markdown(f"**{int(pending):,} pending**"
                + (f" - showing the {len(rq):,} largest" if pending > len(rq) else "")
                + " - select a row to resolve it")
    event = st.dataframe(
        rq.drop(columns=["classification_id", "expenditure_id", "explanation"]),
        width="stretch", hide_index=True, height=340,
        on_select="rerun", selection_mode="single-row", key=f"rq_{scheme}_{rc}",
        column_config={
            "country_code": T("Country", COUNTRY_HELP),
            "source_record_id": T("Source id", "The country's own transaction id (not unique in Country A)."),
            "account_code": T("Account", "The country's account code on the record."),
            "account_label": T("Account label", "Label for that code from the country's chart of accounts or "
                                                "as observed in the extract."),
            "description": T("Description", "Harmonised description (majority spelling). The raw text is on "
                                            "the Traceability tab.", width="large"),
            "amount_original": num("Amount", "In the source currency."),
            "currency_original": T("Ccy", "Source currency."),
            "amount_usd": num("USD", USD_HELP),
            "code": T("Code", "The automated classification's code, blank where the outcome is not a code."),
            "outcome": T("Outcome", OUTCOME_COL_HELP),
            "method": T("Method", METHOD_HELP),
            "confidence": conf_col()})
    render_confidence_guide("review")
    decided = q("""SELECT f.country_code, f.source_record_id, cl.scheme, cl.code, cl.outcome, cl.method,
                        cl.review_status, cl.reviewed_by, cl.reviewed_at, cl.review_note, f.amount_usd
                   FROM classification cl JOIN fact_expenditure f ON f.expenditure_id=cl.expenditure_id
                  WHERE cl.is_current=1 AND cl.review_status IN ('ACCEPTED','CORRECTED')
                  ORDER BY cl.reviewed_at DESC""")
    with st.expander(f"Decisions recorded ({len(decided):,})"):
        st.markdown("Every accept or correction an analyst has made, newest first. A correction is a new "
                    "current classification with method `manual`; the automated one it replaced is kept "
                    "in the record's history (Traceability).")
        if decided.empty:
            st.caption("No decisions yet.")
        else:
            st.dataframe(decided, width="stretch", hide_index=True, key="decisions", column_config={
                "country_code": T("Country", COUNTRY_HELP), "source_record_id": T("Source id", "The country's own id."),
                "scheme": T("Scheme", "SHA or SRHR."), "code": T("Code", "The code now current."),
                "outcome": T("Outcome", OUTCOME_COL_HELP), "method": T("Method", METHOD_HELP),
                "review_status": T("Decision", "ACCEPTED the automated result, or CORRECTED it."),
                "reviewed_by": T("Reviewer", "Name entered at the time."), "reviewed_at": T("When", "UTC."),
                "review_note": T("Note", "Free text left by the reviewer.", width="medium"),
                "amount_usd": num("USD", USD_HELP)})
            csv_download(decided, "Download decisions (CSV)", "review_decisions.csv", "dl_decisions")
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.info("Select a row above to accept or correct it.", icon=":material/touch_app:")
    else:
        row = rq.iloc[sel[0]]
        with st.container(border=True):
            h1, h2, h3, h4 = st.columns([2, 2, 3, 2])
            h1.markdown(f"**{row['source_record_id']}**  \n{row['country_code']} - account {row['account_code']}")
            h2.markdown(f"**{(row['amount_original'] or 0):,.2f} {row['currency_original']}**  \n"
                        f"{(row['amount_usd'] or 0):,.0f} USD")
            h3.markdown(f"{row['description'] or '*(no description)*'}")
            h4.markdown(f"**{row['code'] or row['outcome']}**  \n{row['method']} - grade {row['confidence']:.2f}")
            st.caption(row["explanation"] or "")
            codes = q("SELECT sha_code code FROM dim_sha" if scheme == "SHA"
                      else "SELECT srhr_code code FROM dim_srhr")["code"].tolist()
            f1, f2, f3 = st.columns([2, 1, 2])
            new_code = f1.selectbox("Correct code", [""] + codes,
                                    index=(codes.index(row["code"]) + 1) if row["code"] in codes else 0,
                                    key="rev_code")
            who = f2.text_input("Reviewer", "analyst", key="rev_who")
            note = f3.text_input("Note", "", key="rev_note")
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("Accept as-is", icon=":material/check:", key="rev_accept"):
                get_conn().execute(
                    "UPDATE classification SET review_status='ACCEPTED', reviewed_by=?,"
                    " reviewed_at=datetime('now'), review_note=? WHERE classification_id=?",
                    (who, note, int(row["classification_id"])))
                get_conn().commit()
                st.session_state["flash_review"] = (
                    f"Accepted {row['source_record_id']} as {row['code'] or row['outcome']}.")
                st.rerun()
            if b2.button("Save correction", type="primary", icon=":material/edit:", disabled=not new_code,
                         key="rev_correct"):
                conn = get_conn()
                conn.execute("UPDATE classification SET is_current=0 WHERE classification_id=?",
                             (int(row["classification_id"]),))
                conn.execute(
                    "INSERT INTO classification (expenditure_id, scheme, code, outcome, method,"
                    " rule_id, confidence, needs_review, review_status, reviewed_by, reviewed_at,"
                    " review_note, classified_at, is_current, explanation)"
                    " VALUES (?,?,?,'CLASSIFIED','manual',NULL,1.0,0,'CORRECTED',?,"
                    " datetime('now'),?,datetime('now'),1,?)",
                    (int(row["expenditure_id"]), scheme, new_code, who, note,
                     f"Manually corrected from {row['code']} ({row['method']})."))
                conn.commit()
                st.session_state["flash_review"] = f"Recorded {row['source_record_id']} as {new_code}."
                st.rerun()

# ============================================================== DATA QUALITY ==
with tab_dq:
    section("Data-quality register",
            "Every data-quality issue the pipeline recorded, by rule, severity and country. Issues are "
            "recorded rather than silently corrected, so the condition of each extract stays visible.",
            "Errors are findings to raise with the country; warnings are assumptions the load had to "
            "make; information is context. Pick a rule below to see the rows behind it.")
    sev = q("SELECT severity, COUNT(*) n FROM dq_issue GROUP BY 1")
    counts = dict(zip(sev["severity"], sev["n"]))
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Errors", int(counts.get("ERROR", 0)), border=True, icon=":material/error:",
              help="Values that cannot be right as supplied - to raise with the country.")
    s2.metric("Warnings", int(counts.get("WARN", 0)), border=True, icon=":material/warning:",
              help="Loaded under an assumption a reviewer should know about.")
    s3.metric("Information", int(counts.get("INFO", 0)), border=True, icon=":material/info:",
              help="Handled, recorded for completeness.")
    fired_rules = q("SELECT rule_code, COUNT(*) n FROM dq_issue GROUP BY 1")
    fired = dict(zip(fired_rules["rule_code"], fired_rules["n"]))
    s4.metric("Rules fired", f"{len(fired)} of {len(RULE_HELP)}", border=True, icon=":material/rule:",
              help="Distinct rules with at least one issue, out of every rule the pipeline defines. "
                   "The full catalogue is below.")

    summary = q("""SELECT rule_code, severity, country_code, COUNT(*) issues
                     FROM dq_issue GROUP BY rule_code, severity, country_code
                    ORDER BY CASE severity WHEN 'ERROR' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END, issues DESC""")
    summary["country_code"] = summary["country_code"].fillna("all")
    summary["what it means"] = summary["rule_code"].map(RULE_HELP).fillna("")
    st.dataframe(severity_style(summary), width="stretch", hide_index=True,
                 height=min(38 * (len(summary) + 1) + 4, 640), column_config={
        "rule_code": T("Rule", RULE_COL_HELP), "severity": T("Severity", SEVERITY_HELP),
        "country_code": T("Country", "Country the rule fired for; 'all' for a warehouse-wide finding."),
        "issues": num("Issues", "Number of rows the rule fired on."),
        "what it means": T("What it means", "The rule in one line, and what the pipeline did.", width="large")})

    with st.expander(f"All data-quality rules ({len(RULE_HELP)}) - fired or not"):
        st.markdown("Every check the pipeline runs. A rule that has not fired on this data is still a "
                    "guarantee about the next extract - the load will say so if it does.")
        cat = pd.DataFrame([{"rule_code": r, "severity": RULE_SEVERITY.get(r, ""),
                             "stage": RULE_STAGE.get(r, "harmonise"),
                             "fired": int(fired.get(r, 0)), "what it means": RULE_HELP[r]}
                            for r in sorted(RULE_HELP)])
        st.dataframe(severity_style(cat), width="stretch", hide_index=True, key="dq_catalogue",
                     height=min(38 * (len(cat) + 1) + 4, 700), column_config={
                         "rule_code": T("Rule", RULE_COL_HELP), "severity": T("Severity", SEVERITY_HELP),
                         "stage": T("Stage", "Where in the pipeline the check runs."),
                         "fired": num("Fired", "Issues raised on the data currently loaded."),
                         "what it means": T("What it means", "The rule in one line.", width="large")})
    rules = q("SELECT DISTINCT rule_code FROM dq_issue ORDER BY rule_code")["rule_code"].tolist()
    chosen = st.selectbox("Inspect a rule", rules,
                          help="Shows the individual rows behind the rule: the raw value, the message, "
                               "and the file and row it came from.")
    if chosen in RULE_HELP:
        st.caption(f"**{chosen}** - {RULE_HELP[chosen]}")
    if chosen == "UNTRUSTED_TEXT_IN_DESCRIPTION":
        st.error(
            "These records carry text written to manipulate an automated classifier - instructions "
            "to return a fixed code, a forged '<<SYSTEM>>' block, and a fake reviewer note claiming a "
            "prior audit. Classification here is deterministic, so the payloads have no effect - but "
            "the free-text layer is disabled for these records and each is routed to a human. This is "
            "the control that has to exist before any LLM is allowed near the description field.",
            icon=":material/gpp_maybe:")
    detail = q("""SELECT d.country_code, d.severity, d.field, d.raw_value, d.message,
                         COALESCE(s.source_record_id, f.source_record_id) source_id,
                         s.source_file, s.source_row_no
                    FROM dq_issue d
                    LEFT JOIN stg_raw_record s ON s.raw_id=d.raw_id
                    LEFT JOIN fact_expenditure f ON f.expenditure_id=d.expenditure_id
                   WHERE d.rule_code=? ORDER BY d.issue_id LIMIT 300""", (chosen,))
    st.dataframe(severity_style(detail), width="stretch", hide_index=True, column_config={
        "country_code": T("Country", COUNTRY_HELP), "severity": T("Severity", SEVERITY_HELP),
        "field": T("Field", "The source field the rule looked at."),
        "raw_value": T("Raw value", "The value exactly as supplied, or the figures the rule compared."),
        "message": T("Message", "What the rule found and what the pipeline did about it.", width="large"),
        "source_id": T("Source id", "The country's own transaction id."),
        "source_file": T("File", "Source file the row came from."),
        "source_row_no": num("Row", "1-based row number in the source file (Excel: sheet row).")})
    d1, d2, _ = st.columns([1, 1, 3])
    with d1:
        csv_download(detail, f"Download {chosen} rows (CSV)", f"dq_{chosen.lower()}.csv", "dl_dq_rule")
    with d2:
        full = q("""SELECT d.rule_code, d.severity, d.country_code, d.field, d.raw_value, d.message,
                           COALESCE(s.source_record_id, f.source_record_id) source_id, s.source_file,
                           s.source_row_no, d.detected_at
                      FROM dq_issue d LEFT JOIN stg_raw_record s ON s.raw_id=d.raw_id
                      LEFT JOIN fact_expenditure f ON f.expenditure_id=d.expenditure_id
                     ORDER BY d.issue_id""")
        csv_download(full, "Download the whole register (CSV)", "dq_register.csv", "dl_dq_all")

# ============================================================== TRACEABILITY ==
with tab_trace:
    section("From figure to source row",
            "One record followed end to end: the file and row it came from, the verbatim staging "
            "copy, the harmonised values, and every classification it has ever had.",
            "Enter a source transaction id, or leave blank for the country's largest record. The four "
            "cards are the four stages; the raw payload is exactly what the country sent.")
    t1, t2 = st.columns([1, 3])
    countries = q("SELECT country_code FROM dim_country ORDER BY 1")["country_code"].tolist()
    cc = t1.selectbox("Country", countries, key="trace_country")
    ident = t2.text_input("Source transaction id (blank = the largest record)", "", key="trace_id")
    if ident.strip():
        hit = q("SELECT * FROM v_expenditure_classified WHERE country_code=? AND source_record_id=?",
                (cc, ident.strip()))
    else:
        hit = q("SELECT * FROM v_expenditure_classified WHERE country_code=? ORDER BY amount_usd DESC LIMIT 1",
                (cc,))
    if hit.empty:
        st.warning("No record found.")
    else:
        rec = hit.iloc[0]
        raw = q("""SELECT s.source_file, s.source_sheet, s.source_row_no, s.ingested_at,
                          s.payload_json, b.source_system, b.batch_id
                     FROM fact_expenditure f
                     JOIN stg_raw_record s ON s.raw_id=f.raw_id
                     JOIN ingestion_batch b ON b.batch_id=s.batch_id
                    WHERE f.expenditure_id=?""", (int(rec["expenditure_id"]),)).iloc[0]
        a, b, c, d = st.columns(4)
        with a, st.container(border=True):
            st.markdown("**1 - Source**")
            st.markdown(f"`{raw['source_file']}`" + (f"  \nsheet `{raw['source_sheet']}`" if raw["source_sheet"] else ""))
            st.markdown(f"row **{raw['source_row_no']}**  \n{raw['source_system'] or ''}")
        with b, st.container(border=True):
            st.markdown("**2 - Staged verbatim**")
            st.markdown(f"batch {raw['batch_id']}  \n{str(raw['ingested_at'])[:16].replace('T', ' ')}")
            st.markdown(f"source id `{rec['source_record_id']}`")
        with c, st.container(border=True):
            st.markdown("**3 - Harmonised**")
            st.markdown(f"{rec['txn_date']} - account `{rec['account_code']}`  \n{rec['description'] or '*(no description)*'}")
            st.markdown(f"**{(rec['amount_original'] or 0):,.2f} {rec['currency_original']}** "
                        f"= {(rec['amount_usd'] or 0):,.0f} USD  \n"
                        f"<span style='font-size:.8rem;color:#6b7a89'>at {rec['fx_rate'] or '-'} per USD "
                        f"({rec['fx_rate_source'] or 'no rate'})</span>", unsafe_allow_html=True)
        with d, st.container(border=True):
            st.markdown("**4 - Classified**")
            st.markdown(f"SHA **{rec['sha_code'] or rec['sha_outcome']}** - {rec['sha_method']} "
                        f"({(rec['sha_confidence'] or 0):.2f})")
            review_words = {
                "NOT_REQUIRED": "not required - counted as classified",
                "PENDING": "awaiting a human (in the Review queue)",
                "ACCEPTED": "accepted by an analyst",
                "CORRECTED": "corrected by an analyst",
            }
            st.markdown(f"SRHR **{rec['srhr_code'] or rec['srhr_outcome']}**  \n"
                        f"review: {review_words.get(rec['sha_review_status'], rec['sha_review_status'])}")

        x, y = st.columns([1, 1])
        with x:
            st.markdown("**Harmonised record**")
            st.dataframe(pd.DataFrame({"field": hit.columns,
                                       "value": ["" if pd.isna(v) else str(v) for v in rec.values]}),
                         width="stretch", hide_index=True, height=420, column_config={
                             "field": T("Field", "Column of the harmonised view v_expenditure_classified."),
                             "value": T("Value", "The record's value after harmonisation; compare with the raw "
                                                 "payload on the right.")})
        with y:
            st.markdown("**Raw payload as supplied**")
            st.json(json.loads(raw["payload_json"]))
        st.markdown("**Classification history**",
                    help="Every classification this record has had, oldest first. Only the current one "
                         "counts; superseded rows are kept so a restated figure can be explained.")
        st.dataframe(
            q("""SELECT scheme, code, outcome, method, rule_id, confidence, review_status, reviewed_by,
                        is_current, explanation FROM classification WHERE expenditure_id=?
                 ORDER BY classification_id""", (int(rec["expenditure_id"]),)),
            width="stretch", hide_index=True, column_config={
                "scheme": T("Scheme", "SHA or SRHR."), "code": T("Code", "Code assigned, if any."),
                "outcome": T("Outcome", OUTCOME_COL_HELP), "method": T("Method", METHOD_HELP),
                "rule_id": T("Rule", "The account mapping entry or keyword rule that produced this result."),
                "confidence": conf_col(),
                "review_status": T("Review", "NOT_REQUIRED = counted as classified without a human; PENDING = "
                                             "flagged and awaiting one; ACCEPTED / CORRECTED = an analyst decided."),
                "reviewed_by": T("Reviewer", "Who accepted or corrected it."),
                "is_current": st.column_config.CheckboxColumn("Current", help="Only one row per scheme is "
                                                              "current; superseded rows are kept so a "
                                                              "restated figure can be explained."),
                "explanation": T("Explanation", "Why the record landed here, in plain words.", width="large")})
        kids = q("""SELECT source_record_id, description, amount_original, is_countable
                      FROM fact_expenditure WHERE parent_expenditure_id=?""", (int(rec["expenditure_id"]),))
        if not kids.empty:
            st.markdown("**Sub-transactions** - `countable` off means the parent carries the value")
            st.dataframe(kids, width="stretch", hide_index=True, column_config={
                "source_record_id": T("Sub id", "The sub-transaction's own id from the source."),
                "description": T("Description", "As supplied on the sub-transaction."),
                "amount_original": num("Amount", "In the source currency."),
                "is_countable": st.column_config.CheckboxColumn("Countable", help="Off when the parent carries "
                                                                "the value because the children did not sum to it.")})

# ============================================================ DOCUMENTATION ==
# The repository's own write-up, readable without leaving the app. Named files
# first, in reading order; any other .md beside app.py is appended after them.
DOCS = [("README", "README.md"), ("Architecture", "ARCHITECTURE.md"),
        ("Data-quality findings", "DATA_QUALITY_FINDINGS.md"), ("AI disclosure", "AI_DISCLOSURE.md")]


def render_doc(name: str) -> None:
    """Show one markdown file from the repository root.

    Two adjustments for Streamlit's renderer: links to sibling .md files cannot
    open inside the app, so they become the file name in code font (the reader
    switches sub-tab instead); and `$` is escaped so a pair of dollar amounts
    in one paragraph is not read as LaTeX."""
    text = (ROOT / name).read_text(encoding="utf-8")
    text = re.sub(r"\[([^\]]+)\]\(([A-Za-z0-9_]+\.md)\)", r"`\2`", text)
    text = text.replace("$", r"\$")
    st.caption(f"`{name}`")
    st.markdown(text)


with tab_docs:
    section("Documentation",
            "The project's markdown documents, read from the repository root each time, so the "
            "write-up shown here is the one that ships with the code.",
            "One sub-tab per file. A reference to another document names the file; open its tab.")
    known = {name for _, name in DOCS}
    extra = sorted(p.name for p in ROOT.glob("*.md") if p.name not in known)
    docs = ([(title, name) for title, name in DOCS if (ROOT / name).exists()]
            + [(name[:-3].replace("_", " ").capitalize(), name) for name in extra])
    if not docs:
        st.info("No markdown files found beside app.py.", icon=":material/info:")
    else:
        for sub, (title, name) in zip(st.tabs([title for title, _ in docs]), docs):
            with sub:
                render_doc(name)

st.markdown('<div class="hef-foot">Prototype for the WHO AFRO technical assessment, September 2026 - '
            'synthetic data - USD at assumed rates, indicative only.</div>', unsafe_allow_html=True)
