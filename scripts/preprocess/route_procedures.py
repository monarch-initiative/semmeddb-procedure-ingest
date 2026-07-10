#!/usr/bin/env python3
"""
Apply the OBI-branch routing policy to procedure_disease_clean, using signals
already in the db: NCIT semantic type (umls2ncit) + SemMedDB predicate.

Roofs:  OBI:assay (diagnostic/measurement half)  |  SNOMED:therapeutic (treats half)
Leaf prefix policy: NCIT if mapped, else SNOMED (pending MRCONSO xwalk), else UMLS.
LOINC is the OUTPUT layer for measurement/assessment assays (not the procedure prefix).

Writes table `procedure_routing` + prints per-branch node/edge counts.
"""
import duckdb
import _paths as P
con = duckdb.connect(str(P.DB))

con.execute("""
CREATE OR REPLACE TABLE procedure_routing AS
WITH j AS (
  SELECT p.subject_id, any_value(p.subject_label) subject_label,
         max(u.ncit_semtype) ncit_semtype, max(u.ncit) ncit,
         list(DISTINCT p.predicate) preds, count(*) edges
  FROM procedure_disease_clean p
  LEFT JOIN umls2ncit u ON u.umls = p.subject_id
  GROUP BY p.subject_id
),
routed AS (
  SELECT *,
    CASE
      WHEN ncit_semtype = 'Laboratory Procedure'                 THEN 'measurement_assay'
      WHEN ncit_semtype = 'Intellectual Product'                 THEN 'assessment_assay'
      WHEN ncit_semtype = 'Diagnostic Procedure'                 THEN 'diagnostic_assay'
      WHEN ncit_semtype = 'Therapeutic or Preventive Procedure'  THEN 'therapeutic'
      WHEN ncit_semtype IS NOT NULL                              THEN  -- activity/etc: by predicate
           CASE WHEN list_contains(preds,'treats') AND NOT list_contains(preds,'diagnoses')
                THEN 'therapeutic' ELSE 'diagnostic_assay' END
      ELSE  -- no NCIT: route by predicate alone
           CASE WHEN list_contains(preds,'treats') AND NOT list_contains(preds,'diagnoses')
                THEN 'therapeutic' ELSE 'diagnostic_assay' END
    END AS branch
  FROM j
)
SELECT *,
  CASE WHEN branch='therapeutic' THEN 'SNOMED:therapeutic' ELSE 'OBI:assay' END AS roof,
  CASE WHEN ncit IS NOT NULL THEN 'NCIT'
       ELSE 'SNOMED (pending MRCONSO) / UMLS fallback' END                    AS leaf_prefix,
  CASE WHEN branch IN ('measurement_assay','assessment_assay') THEN 'LOINC' END AS output_layer
FROM routed
""")

print("=== per-branch: distinct procedures / edges / NCIT-grounded ===")
for r in con.execute("""
  SELECT roof, branch,
         count(*) procedures, sum(edges) edges,
         count(*) FILTER (WHERE ncit IS NOT NULL) ncit_grounded
  FROM procedure_routing GROUP BY 1,2 ORDER BY edges DESC""").fetchall():
    print(f"  {r[0]:<19} {r[1]:<18} procs={r[2]:>5}  edges={r[3]:>6}  ncit={r[4]:>5}")

print("\n=== roof totals ===")
for r in con.execute("""
  SELECT roof, count(*) procs, sum(edges) edges FROM procedure_routing
  GROUP BY 1 ORDER BY edges DESC""").fetchall():
    print(f"  {r[0]:<19} procs={r[1]:>5}  edges={r[2]:>6}")

print("\n=== leaf-prefix policy outcome ===")
for r in con.execute("""
  SELECT leaf_prefix, count(*) procs, sum(edges) edges FROM procedure_routing
  GROUP BY 1 ORDER BY edges DESC""").fetchall():
    print(f"  {r[0]:<42} procs={r[1]:>5}  edges={r[2]:>6}")

print("\n=== sample routing ===")
for r in con.execute("""
  SELECT subject_label, branch, roof, leaf_prefix, output_layer
  FROM procedure_routing WHERE ncit IS NOT NULL ORDER BY edges DESC LIMIT 12""").fetchall():
    out = f" +output:{r[4]}" if r[4] else ""
    print(f"  {r[0][:34]:<34} -> {r[1]:<17} [{r[2]}]{out}")
con.close()
