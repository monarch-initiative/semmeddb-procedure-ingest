#!/usr/bin/env python3
"""
Rescue the UMLS-residual disease objects in procedure_disease_clean by mapping
them to MONDO via mondo.sssom (UMLS->MONDO skos:exactMatch). Adds:
  final_object_id / final_object_label  (MONDO when rescued, else original)
  object_rescued                        (boolean)
"""
import duckdb
import _paths as P

MONDO_SSSOM = str(P.MONDO_SSSOM)
con = duckdb.connect(str(P.DB))

# UMLS -> MONDO from sssom (object_id is the UMLS side, subject_id is MONDO)
con.execute(f"""
CREATE OR REPLACE TEMP TABLE umls2mondo AS
SELECT object_id AS umls, subject_id AS mondo, subject_label AS label
FROM read_csv('{MONDO_SSSOM}', delim='\t', header=true, comment='#',
              ignore_errors=true)
WHERE object_id LIKE 'UMLS:%' AND predicate_id = 'skos:exactMatch'
""")

for col in ("final_object_id", "final_object_label", "object_rescued"):
    try:
        con.execute(f"ALTER TABLE procedure_disease_clean ADD COLUMN {col} VARCHAR")
    except duckdb.Error:
        pass

con.execute("""
UPDATE procedure_disease_clean p SET
  final_object_id    = COALESCE(m.mondo, p.object_id),
  final_object_label = COALESCE(m.label, p.object_label),
  object_rescued     = CASE WHEN m.mondo IS NOT NULL THEN 'true' ELSE 'false' END
FROM (SELECT * FROM umls2mondo) m
WHERE m.umls = p.object_id
""")
# rows with no rescue: fill final_* with originals
con.execute("""
UPDATE procedure_disease_clean SET
  final_object_id = COALESCE(final_object_id, object_id),
  final_object_label = COALESCE(final_object_label, object_label),
  object_rescued = COALESCE(object_rescued, 'false')
""")

tot = con.execute("SELECT count(*) FROM procedure_disease_clean").fetchone()[0]
umls_before = con.execute("""SELECT count(*) FROM procedure_disease_clean
  WHERE object_id NOT LIKE 'MONDO:%' AND object_id NOT LIKE 'HP:%'""").fetchone()[0]
rescued = con.execute("""SELECT count(*) FROM procedure_disease_clean
  WHERE object_rescued='true'""").fetchone()[0]
umls_after = con.execute("""SELECT count(*) FROM procedure_disease_clean
  WHERE final_object_id NOT LIKE 'MONDO:%' AND final_object_id NOT LIKE 'HP:%'""").fetchone()[0]

print(f"total clean edges:            {tot:,}")
print(f"UMLS-residual objects before: {umls_before:,}")
print(f"  rescued to MONDO:           {rescued:,}")
print(f"UMLS-residual objects after:  {umls_after:,}")
print(f"now MONDO/HP-grounded:        {tot - umls_after:,}  ({(tot-umls_after)/tot:.1%})")

con.execute("""COPY (SELECT * FROM procedure_disease_clean
  ORDER BY predicate, n_pmids_true DESC) TO 'procedure_disease_clean.tsv'
  (HEADER, DELIMITER '\t')""")
print("rewrote procedure_disease_clean.tsv")
con.close()
