# Health Expenditure Harmonisation - prototype

Technical assessment, Consultant - IT Systems and Data Extraction Tool Developer
(REQ2602624). WHO AFRO, September 2026.

Ingests national expenditure extracts that differ in format, language, currency
and chart of accounts; harmonises them into one model; classifies each record
against SHA and SRHR with a confidence and a reason; flags what it cannot
settle; and lets an analyst trace any figure back to its source cell.

## Run it

```bash
git clone https://github.com/firdavskurbonov/hef.git
cd hef
python -m pip install -r requirements.txt
# copy the supplied candidate_data/ folder into hef/ (it is not in the repository)
python run_pipeline.py          # builds warehouse.db from candidate_data/ (~20s)
streamlit run app.py            # analyst interface
python tests/test_pipeline.py   # 14 test groups
```

Python 3.10+ (verified on 3.13 with current pandas 3 / NumPy 2.5 /
Streamlit 1.64). Storage is one SQLite file - no server. `run_pipeline.py`
applies `schema.sql` to a fresh database each run, so there is no separate
schema step, and it finds `candidate_data/` inside the clone or beside it
(`--data-dir <path>` for anywhere else).

## Results

| | Country A | Country B | Country C |
|---|---|---|---|
| Format / language | CSV / EN | Excel / FR | JSON / EN |
| Currency | KES | XOF | RWF **+ USD** |
| Rows in file -> loaded | 2,500 -> 2,500 | 2,001 -> 2,000 | 2,500 -> 2,500 +183 splits |
| Control total | none supplied | **reconciles exactly (0.00)** | none supplied |

**7,183 records classified:** 4,968 to an SHA function, 812 capital formation,
1,403 unattributable inputs, 0 unclassified, 1,406 routed to human review.
11 data-quality rules fired 152 times.

Counts are over all 7,183 records; USD totals are over the 7,108 countable ones
(nested splits counted once) at assumed rates.

## The interface

`streamlit run app.py` - nine tabs, in the pipeline's order: data in, set up, check, decide, analyse, drill, read.
Every section carries a hover tooltip saying what it shows and a one-line note
on how to read it; every table column has its own tooltip.

| Tab | What it does |
|---|---|
| **Ingest** | Pick a country, upload its extract, **Load**; the load report is the data-quality assessment for that file. Below, the load history: per file, rows in -> staged -> facts -> countable and the reconciliation against any printed control total |
| **References** | The SHA functions, SRHR themes and countries as supplied - viewable, editable, extendable. Edits go to `config/references.yml` and are laid over the untouched CSVs on every load |
| **Account mapping** | The country's account codes with their labels and record counts; assign SHA / SRHR in a grid, save to `config/account_mapping.yml`, re-classify at once |
| **Data quality** | The register by rule, severity and country, drillable to the source rows; the full rule catalogue; CSV download |
| **Review queue** | Records the classifier flagged, largest value first; select a row, accept or correct; the log of decisions made |
| **Overview** | Totals by country, ministry, SHA function and SRHR theme; how each classification was reached and at what confidence; the classification rules |
| **Records** | Every harmonised record with its classifications - filter by country, SHA, SRHR, ministry, review state, date and text; download the selection as CSV |
| **Traceability** | One record end to end: file and row, verbatim payload, harmonised values, classification history |
| **Documentation** | This README, ARCHITECTURE, DATA_QUALITY_FINDINGS and AI_DISCLOSURE, one sub-tab each, read from the repository so they cannot drift from the code |

The interface calls the same functions as `run_pipeline.py`; there is one
pipeline, not a UI copy of it. A browser upload stands in for the scheduled
feed a production system would use.

## Design decisions

| Decision | Why |
|---|---|
| **SQLite now, PostgreSQL later** | A reviewer must be able to run it: no server, one portable file, real SQL. Nothing in the schema is SQLite-specific. Postgres buys `NUMERIC` money, concurrent reviewers, row-level security per country, partitioning. |
| **Raw before canonical** | Every source row is stored verbatim with its file and row number *before* cleaning. A mapping error is replayed without re-requesting an extract, and any figure traces to its cell. |
| **Per-country chart of accounts + an account mapping, not a canonical account model** | The answer to `ref_chart_of_accounts_README.txt`. `dim_account` is keyed `(country_code, account_code)` and holds each country's own codes and labels (Country B's `Plan_comptable` loaded as authoritative). Harmonisation is a separate mapping table, `config/account_mapping.yml`, from those codes to SHA and SRHR - what SHA practice calls a crosswalk. A canonical hierarchy would have to be invented and no country agreed it; a mapping is what a country team can validate. |
| **Account mapping first, text second** | The account code is the strongest signal: structured, assigned by the country, present even on Country C's 27 records with no description. Text is consulted only when the code cannot answer. |
| **Rules, not a model** | 54 account codes and a supplied framework: a rule table beats a model trained on a few thousand synthetic rows, and a person can audit a rule. Every result carries its rule id and a sentence of English. |
| **Country logic is configuration** | `config/*.yml` holds sources, account mappings, rules and FX - editable from the interface or by hand. Only a new file *shape* needs code. |

### Classification layers

| Layer | Basis | Confidence |
|---|---|---|
| 1 | account code -> SHA/SRHR account mapping (`account_mapping.yml`) | 0.40 - 0.95 |
| 2 | bilingual keyword rules (`rules.yml`) | 0.70 |
| 3 | fuzzy match to scheme labels | 0.55 |
| 4 | nothing matched -> `UNCLASSIFIED` | 0.00 |

Every code in this sample is in the account mapping, so layer 1 answers 100% and
layers 2-4 are exercised only by the tests. That is the expected shape - the
fallbacks exist for the next country, not this one.

### What confidence means

Confidence is an **evidence grade, not a probability**: how strong the *kind*
of evidence behind a classification was - the country's own account code is
strongest, free text weaker, nothing at all is a guess. It drives routing:
**anything below 0.70 goes to a human** (`rules.yml: review_below`), on top of
the explicit flags for untrusted text, code/text conflicts and input-type
codes. A model plugged in later only has to produce the same number. The grade
table is in [ARCHITECTURE.md](ARCHITECTURE.md) and in the app.

### Three outcomes that are not SHA codes

Forcing everything into an `HC.*` bucket gives a tidy, wrong answer.

- **`CAPITAL`** (812 records, $12.1m) - construction and vehicles. SHA 2011 puts
  capital formation under `HK`, outside the `HC` functions.
- **`ADMIN_INPUT`** (1,403 records, $22.6m) - salaries, fuel, stationery. SHA
  distributes these using allocation keys the extracts do not contain.
  Provisionally `HC.7` at confidence 0.40, flagged.
- **`UNCLASSIFIED`** - insufficient information. Distinct from "definitely not a
  health function", which is why `outcome` is a column rather than a null code.

## Data-quality findings

Detail: [DATA_QUALITY_FINDINGS.md](DATA_QUALITY_FINDINGS.md).

1. **Country B mixes two numeric conventions in one column** - `11548910,00`
   and `781,311 FCFA`. Parsed per value; the load reconciles to the printed
   total at 0.00 where one declared convention left it 11.7m XOF short.
2. **Four descriptions carry instructions aimed at an automated classifier.**
   Text layer disabled, quarantined for review; tests prove no payload can set
   a code.
3. **Nested splits in Country C would double-count.** `is_countable` counts each
   total once; the 7 splits that do not sum are flagged.
4. **No exchange rates supplied**, yet Country C mixes RWF and USD. Rates are a
   declared assumption, recorded on every row; USD is indicative only.
5. **`TXN_ID` is not unique in Country A.** Both records kept; the source id is
   not a business key.
6. **Five Country C postings are dated 2027**, after the file's own extraction
   date. Each country's reporting period is asserted, not read off the data.

Also handled: empty and negative amounts, missing descriptions, Country A's
three casings per description (resolved to the majority spelling), and three
different fiscal calendars.

## Assumptions

- The extracts cover FY2023/24; Country A's is inferred from its date range.
- No unit scaling (thousands/millions) is assumed anywhere.
- Negative amounts are genuine reversals and are counted.
- Country B's final `TOTAL` row is a report artefact, not a transaction.
- FX rates are indicative period averages, **not authoritative** - declared in
  `config/fx_rates.yml`; every record carries the rate and source it was
  converted with (visible on the Traceability tab). To change a rate, edit the
  file and re-run.
- SHA mappings are my reading of the supplied reference list and need
  confirmation with health-accounts specialists.

## Before production

1. **Validate the account mapping with country teams and health-accounts
   specialists** - a subject-matter problem, and the biggest determinant of
   whether the output is usable.
2. Authoritative period-specific FX (UN Operational Rates) as a time-varying
   dimension.
3. `NUMERIC` money, PostgreSQL, migrations.
4. Incremental idempotent loads - today it rebuilds, which is reproducible but
   will not survive monthly extracts from twenty countries.
5. Authentication, per-country authorisation, audited review workflow.
6. Orchestration with DQ rules as gates that can fail a load, not only annotate.
7. Versioned account mappings with an approval step and restatement.
8. Allocation keys for `ADMIN_INPUT`, agreed per country.

**Then a model, not before.** Reviewer decisions accumulating in the
`classification` table are the labelled training data; collect labels first. An
LLM is plausible for proposing mappings for a *new* country's chart of accounts -
dozens of codes reviewed once - and finding #2 is why it must treat description
text strictly as untrusted data.

## Layout

```
hef/                         repository root (the clone)
  run_pipeline.py  app.py    entry points - what you run
  schema.sql                 data model (commented DDL)
  .streamlit/config.toml     interface theme (WHO blue; validated chart palette)
  candidate_data/            the supplied extracts go here (not in the repository)
  uploads/                   files loaded through the interface (not versioned)
  config/                    per-country behaviour - the interface writes here too
                             (sources, account_mapping, rules, fx_rates; references.yml
                             appears when a reference list is edited or extended)
  src/hef/                   the library: db, ingest, harmonise, classify, pipeline
  tests/                     parser, date, FX, classifier, injection, load/replace,
                             wizard-guess and config-writer tests
  tools/                     dev tools, not part of the product:
                             profile_data.py (evidence behind the DQ findings)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the data flow and model rationale,
[AI_DISCLOSURE.md](AI_DISCLOSURE.md) for tooling used.
