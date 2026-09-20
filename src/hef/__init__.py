"""Health Expenditure Harmonisation (HEF) prototype.

A small pipeline that ingests national expenditure extracts in different
formats, harmonises them into one model, classifies them against SHA and SRHR
reference schemes, and exposes the result for analyst review with full
traceability back to the source row.

Modules
-------
db          schema creation, connection handling, reference data loading
ingest      format-specific readers -> immutable staging rows
harmonise   staging -> canonical fact rows (cleaning, FX, sub-transactions,
            period checks); data-quality findings go to db.add_issue
classify    layered SHA/SRHR classification with confidence and explanations
pipeline    orchestration
"""

__version__ = "0.1.0"
