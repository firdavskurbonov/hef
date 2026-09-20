#!/usr/bin/env python
"""Build the warehouse from the supplied country extracts.

    python run_pipeline.py                      # finds candidate_data/ automatically
    python run_pipeline.py --data-dir <path> --db warehouse.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from hef import ingest, pipeline  # noqa: E402


def main() -> int:
    """Parse arguments, locate the extracts, run pipeline.run. Exit 2 if the
    data directory is missing, 0 otherwise."""
    ap = argparse.ArgumentParser(description="Health expenditure harmonisation pipeline")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="folder holding the country extracts "
                         "(default: candidate_data/ inside or beside the repo)")
    ap.add_argument("--db", type=Path,
                    default=Path(__file__).resolve().parent / "warehouse.db",
                    help="SQLite file to build (default: ./warehouse.db)")
    ap.add_argument("--keep", action="store_true",
                    help="append to an existing database instead of rebuilding")
    args = ap.parse_args()
    if args.data_dir is None:
        args.data_dir = ingest.default_data_dir()

    if not args.data_dir.exists():
        print(f"ERROR: data directory not found: {args.data_dir}\n"
              f"Place the supplied extracts in 'candidate_data/' inside this repo "
              f"(or beside it), or pass --data-dir <path>.", file=sys.stderr)
        return 2

    pipeline.run(args.data_dir, args.db, rebuild=not args.keep)
    print(f"\nDone. Database: {args.db}")
    print("Review the results with:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
