#!/usr/bin/env python3
"""Stamp snomed_ic + best_ic (= max NCIT/SNOMED procedure IC) on
procedure_disease_clean, and report the edge-level rescue."""
import duckdb
import _paths as P
con = duckdb.connect(str(P.DB))

# most-specific SNOMED code+IC per procedure CUI
con.execute("""CREATE OR REPLACE TEMP TABLE proc_sn AS
  SELECT 'UMLS:'||x.cui AS subject_id, arg_max(x.code, s.ic) sctid, max(s.ic) snomed_ic
  FROM cui_xref x JOIN snomed_ic s ON s.sctid=x.code
  WHERE x.sab='SNOMEDCT_US' GROUP BY x.cui""")

for c in ("proc_snomed_id", "proc_snomed_ic", "best_ic"):
    try: con.execute(f"ALTER TABLE procedure_disease_clean ADD COLUMN {c} DOUBLE")
    except duckdb.Error: pass
try: con.execute("ALTER TABLE procedure_disease_clean ADD COLUMN proc_snomed_id_v VARCHAR")
except duckdb.Error: pass
con.execute("UPDATE procedure_disease_clean p SET proc_snomed_ic = ps.snomed_ic, proc_snomed_id_v='SNOMED:'||ps.sctid FROM proc_sn ps WHERE ps.subject_id=p.subject_id")
con.execute("UPDATE procedure_disease_clean SET best_ic = greatest(coalesce(subject_ic, proc_snomed_ic), coalesce(proc_snomed_ic, subject_ic))")

print("=== edge-level accounting (of 69,572 clean edges) ===")
for r in con.execute("""
  SELECT
    count(*) total,
    count(*) FILTER (WHERE subject_ic IS NOT NULL) had_ncit_ic,
    count(*) FILTER (WHERE subject_ic IS NULL AND proc_snomed_ic IS NOT NULL) rescued_by_snomed,
    count(*) FILTER (WHERE best_ic IS NOT NULL) now_have_ic,
    count(*) FILTER (WHERE best_ic IS NULL) still_no_ic
  FROM procedure_disease_clean""").fetchall():
    print(f"  total edges:                 {r[0]:,}")
    print(f"  had NCIT subject_ic:         {r[1]:,}")
    print(f"  RESCUED by SNOMED IC:        {r[2]:,}   <-- bucket-C edge-level win")
    print(f"  now have a procedure IC:     {r[3]:,}  ({r[3]/r[0]:.0%})")
    print(f"  still no procedure IC:       {r[4]:,}  ({r[4]/r[0]:.0%})")

print("\n=== specificity: edges that are 'specific enough' (best_ic>=8) ===")
for r in con.execute("""
  SELECT
    count(*) FILTER (WHERE subject_ic>=8) specific_ncit_only,
    count(*) FILTER (WHERE best_ic>=8)    specific_with_snomed
  FROM procedure_disease_clean""").fetchall():
    print(f"  specific with NCIT alone:        {r[0]:,}")
    print(f"  specific with NCIT∪SNOMED (best): {r[1]:,}   (+{r[1]-r[0]:,})")

print("\n=== the 'clean & specific' core: best_ic>=8 AND object_ic>=4 AND predicted support ===")
for r in con.execute("""
  SELECT count(*) FROM procedure_disease_clean
  WHERE best_ic>=8 AND object_ic>=4""").fetchall():
    print(f"  edges: {r[0]:,}")
con.close()
print("stamped proc_snomed_ic / best_ic on procedure_disease_clean")
