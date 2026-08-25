#!/usr/bin/env python3
"""
Build the PUBLISHED source artifact: data/semmeddb_subset.parquet.

Maintainer-only, run once per upstream subset build — NOT part of `just run`.
Its output is what download.yaml fetches; downstream builds never see the
full file.

Why a slice: the LLM-verified SemMedDB subset is ~6.2 GB / 26.7M predications
covering every predicate. This ingest reads two predicates and eight columns
of it. Slicing first takes the primary source to ~15 MB — small enough for a
plain GitHub release asset, so the ingest needs no bucket or Zenodo deposit and
stays reproducible from public URLs alone.

Dropped on purpose: SemMedDB_sentences / supporting_sentences / reasoning /
subject_mentioned / object_mentioned (bulky provenance text no stage reads;
they belong with the upstream subset, not with the ingest input). `support` is
kept even though no stage reads it today — it is cheap and is the other
quality lever alongside `predicted`.

Usage:
    python scripts/make_source_subset.py /path/to/results_with_names.parquet
"""
import sys
from pathlib import Path

import duckdb

PREDICATES = ("biolink:diagnoses", "biolink:treats_or_applied_or_studied_to_treat")
COLUMNS = ["subject_curie", "subject_name", "predicate",
           "object_curie", "object_name", "PMID", "predicted", "support"]
OUT = Path(__file__).resolve().parents[1] / "data" / "semmeddb_subset.parquet"


def main(src: str, out: Path = OUT) -> None:
    con = duckdb.connect()
    cols = ", ".join(COLUMNS)
    # build the IN list explicitly — tuple repr emits a trailing comma at len 1
    preds = ", ".join(f"'{p}'" for p in PREDICATES)
    # ORDER BY so the artifact is byte-stable across rebuilds of the same input.
    con.execute(f"""
      COPY (
        SELECT {cols} FROM read_parquet('{src}')
        WHERE predicate IN ({preds})
        ORDER BY subject_curie, predicate, object_curie, PMID
      ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    con.close()
    print(f"[make_source_subset] {out} — {n:,} rows, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
