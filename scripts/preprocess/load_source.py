#!/usr/bin/env python3
"""Stage 0: load the SemMedDB subset parquet into the `edges` table.

The parquet MUST carry the LLM-verification columns (predicted, support, ...) —
that's the whole point of the subset. Derives subject_prefix/object_prefix for
cheap summarization. Downstream stages assume this `edges` table exists.
"""
import duckdb
import _paths as P

con = duckdb.connect(str(P.DB))
con.execute(f"""
CREATE OR REPLACE TABLE edges AS
SELECT *,
       split_part(subject_curie, ':', 1) AS subject_prefix,
       split_part(object_curie,  ':', 1) AS object_prefix
FROM read_parquet('{P.SEMMEDDB_PARQUET}')
""")
n = con.execute("SELECT count(*) FROM edges").fetchone()[0]
cols = [r[0] for r in con.execute("DESCRIBE edges").fetchall()]
assert "predicted" in cols, "source parquet lacks 'predicted' — wrong subset (need the LLM-verified one)"
con.close()
print(f"[load_source] edges table: {n:,} rows")
