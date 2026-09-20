# Architecture

## Data flow

```
  Country A CSV        Country B XLSX        Country C JSON
  KES, English         XOF, French           RWF + USD, English
  7-digit codes        6-digit codes, banner 7-digit codes, nested
        |              + printed TOTAL              |
        +--------------------+---------------------+
                             |
                    config/sources.yml   (where data starts, column -> field,
                             |            date/decimal conventions)
                             v
   1. INGEST     csv / excel / json readers -> file -> (row_no, record)
                 Excel reader locates the header, isolates the TOTAL row.
                 NOTHING is cleaned here.
                             |
                             v
                 stg_raw_record     payload verbatim + file/sheet/row_no
                 ingestion_batch    counts, control total, source metadata
                             |
                             v
   2. HARMONISE  amounts   separator role inferred per value
                 dates     only source-declared formats, no guessing;
                           declared period + extraction date asserted
                 text      case variants resolved to the majority spelling;
                           codes never re-cased
                 currency  -> USD, rate + source recorded on every row
                 splits    parent/child reconciliation -> is_countable
                 Problems are RECORDED, never silently repaired.
                             |
                    +--------+--------+
                    v                 v
           fact_expenditure      dq_issue (11 rules)
           dim_country/ministry/account/sha/srhr
                    |
                    v
   3. CLASSIFY   L1 account mapping  account code -> SHA + SRHR    0.40-0.95
                 L2 keyword    bilingual regex                0.70
                 L3 fuzzy      rapidfuzz vs scheme labels     0.55
                 L4 none       UNCLASSIFIED                   0.00
                 GUARD: injection-pattern text -> free-text layer disabled,
                        confidence capped, quarantined for review.
                 ROUTE: confidence is an evidence grade; anything below
                        review_below (0.70) goes to a human.
                             |
                             v
                 classification   scheme, code, outcome, method, rule_id,
                                  confidence, explanation, is_current
                             |
                             v
   4. REVIEW     app.py - Ingest (with load history) | References |
                          Account mapping | Data quality | Review queue |
                          Overview | Records | Traceability | Documentation
                 Ingest and Account mapping call the same functions as steps 1-3;
                 References edits the SHA / SRHR / country lists over the
                 supplied CSVs; review decisions write back as a new current
                 classification.
```

### Evidence grades

Confidence is an evidence grade, not a probability. Each rule records how
strong its kind of evidence is; anything below `rules.yml: review_below` (0.70)
is routed to a human, on top of the explicit flags.

| Source of the classification | Grade |
|---|---|
| Account mapping entry, purpose unambiguous ("Medical drugs" -> HC.5.1) | 0.95 |
| Account mapping entry, boundary arguable (vaccines: goods HC.5.1 or programme HC.6.2?) | 0.80 |
| Account mapping entry, input-type code (salaries, fuel -> ADMIN_INPUT) | 0.40 |
| Keyword rule on the description | 0.70 |
| Fuzzy match to a scheme label | 0.55 |
| Nothing matched (UNCLASSIFIED) | 0.00 |
| Analyst decision | 1.00 |
| Any of the above where the text was untrusted | capped at 0.40 |

## Components

| Component | Responsibility | Why separate |
|---|---|---|
| `ingest.py` | file -> verbatim staging | New *formats* touch only this; re-mapping never needs a new extract |
| `harmonise.py` | cleaning, typing, FX, splits | The only place country conventions are interpreted |
| `classify.py` | SHA/SRHR with evidence | Re-classification must not require re-ingestion |
| `db.add_issue` | quality register | Quality is reported, not buried in logs |
| `app.py` | analyst interface | Writes only through the pipeline and the config writers: a load, an account mapping save, a reference edit, a review decision |
| `config/*.yml` | per-country behaviour | Edited from the interface or by hand; no deploy |

## Data model

```
dim_country ---+
dim_ministry --+--> fact_expenditure <--- stg_raw_record ---> ingestion_batch
dim_account ---+         |   ^                                      |
                         |   +-- parent_expenditure_id (self)       v
                         v                                      dq_issue
                  classification
                   |         |
               dim_sha   dim_srhr
```

Four decisions worth defending:

1. **`dim_account` is keyed `(country_code, account_code)`.** The pack states
   there is no canonical chart of accounts and the three countries use different
   structures. Inventing a canonical account model would fabricate a hierarchy
   nobody agreed; the SHA/SRHR account mapping carries the harmonisation instead.
   Country B supplies its own chart of accounts sheet, so its labels are authoritative and
   transaction text cannot overwrite them.
2. **`classification` is a table, not columns.** Several schemes, several
   methods, re-classification with history - without touching the financial
   fact. `is_current` keeps superseded rows so a restated figure is explainable.
3. **`outcome` is separate from `code`.** A null code cannot distinguish
   "definitely not an HC function" from "could not tell".
4. **`is_countable` on the fact.** One central answer to "does this row enter a
   total?", so nested splits cannot be double-counted by a careless query.

## Where the logic lives

| Concern | Location | Change means |
|---|---|---|
| Which column is the amount | `config/sources.yml` | config |
| How that amount is parsed | `harmonise.parse_amount` | code, shared by all |
| Account code -> SHA/SRHR | `config/account_mapping.yml` | config, no deploy |
| Keyword rules, injection patterns | `config/rules.yml` | config |
| FX assumption | `config/fx_rates.yml` | config |
| Additions / edits to the SHA, SRHR and country lists | `config/references.yml` (References tab) | config; supplied CSVs untouched |
| A new file format | `ingest.py` + `READERS` entry | one function |
