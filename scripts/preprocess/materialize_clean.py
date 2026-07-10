#!/usr/bin/env python3
"""
Materialize the clean procedure->disease/phenotype edge set from the categorized
SemMedDB edges, using:
  - typed endpoints  (subject Procedure/Device/Activity, object Disease/Phenotype)
  - LLM verdict       (>=1 PMID with predicted='True')
  - normalized IDs    (UMLS collapsed to MONDO/HP via nodes.normalized_id)

Writes table `procedure_disease_clean` and `procedure_disease_clean.tsv` (KGX-ish).
"""
import duckdb
import _paths as P

SUBJ_CATS = ("biolink:Procedure", "biolink:Device", "biolink:Activity")
OBJ_CATS = ("biolink:Disease", "biolink:PhenotypicFeature",
            "biolink:DiseaseOrPhenotypicFeature")

con = duckdb.connect(str(P.DB))
con.execute(f"""
CREATE OR REPLACE TABLE procedure_disease_clean AS
WITH typed AS (
  SELECT
    COALESCE(ns.normalized_id, e.subject_curie) AS subject_id,
    ns.label    AS subject_label,
    ns.category AS subject_category,
    CASE e.predicate WHEN 'biolink:diagnoses' THEN 'diagnoses'
                     ELSE 'treats' END           AS predicate,
    COALESCE(no.normalized_id, e.object_curie)  AS object_id,
    no.label    AS object_label,
    no.category AS object_category,
    e.subject_curie AS orig_subject_curie,
    e.object_curie  AS orig_object_curie,
    e.PMID, e.predicted
  FROM edges e
  JOIN nodes ns ON ns.id = e.subject_curie
  JOIN nodes no ON no.id = e.object_curie
  WHERE e.predicate IN ('biolink:diagnoses',
                        'biolink:treats_or_applied_or_studied_to_treat')
    AND e.subject_category IN {SUBJ_CATS}
    AND e.object_category  IN {OBJ_CATS}
)
SELECT
  subject_id,
  any_value(subject_label)    AS subject_label,
  any_value(subject_category) AS subject_category,
  predicate,
  object_id,
  any_value(object_label)     AS object_label,
  any_value(object_category)  AS object_category,
  count(DISTINCT PMID)                                          AS n_pmids,
  count(DISTINCT CASE WHEN predicted='True' THEN PMID END)      AS n_pmids_true,
  any_value(orig_subject_curie) AS example_subject_curie,
  any_value(orig_object_curie)  AS example_object_curie
FROM typed
GROUP BY subject_id, predicate, object_id
HAVING count(DISTINCT CASE WHEN predicted='True' THEN PMID END) >= 1
""")

n = con.execute("SELECT count(*) FROM procedure_disease_clean").fetchone()[0]
print(f"procedure_disease_clean: {n:,} distinct edges")
for r in con.execute("""
   SELECT predicate, object_category, count(*) c
   FROM procedure_disease_clean GROUP BY 1,2 ORDER BY c DESC""").fetchall():
    print(f"  {r[2]:>7}  {r[0]:<10} -> {r[1]}")

con.execute("""
  COPY (SELECT * FROM procedure_disease_clean
        ORDER BY predicate, n_pmids_true DESC)
  TO 'procedure_disease_clean.tsv' (HEADER, DELIMITER '\t')""")
print("wrote procedure_disease_clean.tsv")
con.close()
