#!/usr/bin/env python3
"""
Preprocessing for semmeddb-procedure-ingest.

Koza is row-by-row; everything here is graph/API/join work that can't be
expressed as a Koza transform. This orchestrates the stages (ported verbatim
from the exploratory session into scripts/preprocess/) against a shared duckdb,
then ASSEMBLES the transform-ready flat file `data/procedure_to_disease.tsv`.

Run order (each stage writes tables into $SEMMEDDB_DB):
  0. fetch_licensed.py         SNOMED RF2 + UMLS MRCONSO via UMLS_API_KEY (credentialed, gitignored)
  1. build_node_categories.py  NodeNorm -> `nodes` (category, normalized_id)   [SNAPSHOT for reproducibility]
  2. ncit_map_and_ic.py        `umls2ncit` + NCIT structural IC
  3. umls_crosswalk.py         MRCONSO -> `cui_xref` (CUI <-> SNOMED/NCIT/LNC/...)
  4. snomed_ic.py              `snomed_ic` (structural IC over RF2 Is-a)
  5. materialize_clean.py      `procedure_disease_clean` (predicted=True + typed + normalized)
  6. rescue_umls_objects.py    UMLS disease objects -> MONDO via mondo.sssom
  7. route_procedures.py       `procedure_routing` (OBI:assay vs SNOMED:therapeutic)
  8. stamp_best_ic.py          proc_snomed_ic + best_ic (= max NCIT/SNOMED procedure IC)
  9. (this file) ASSEMBLE      -> data/procedure_to_disease.tsv

NOTE: the ported stage scripts currently hardcode `semmeddb.duckdb` and the
mapping-commons paths (relative to the session dir). Wiring them to read
$SEMMEDDB_DB / data/ is the remaining porting task; the contract below (the
`nodes`, `umls2ncit`, `cui_xref`, `snomed_ic`, `procedure_disease_clean` tables
and the assemble query) is stable.
"""
import os
import subprocess
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("SEMMEDDB_DB", HERE.parent / "data" / "semmeddb.duckdb"))
STAGES = [
    "load_source.py",
    "build_node_categories.py",
    "ncit_map_and_ic.py",
    "umls_crosswalk.py",
    "snomed_ic.py",
    "materialize_clean.py",
    "rescue_umls_objects.py",
    "route_procedures.py",
    "stamp_best_ic.py",
]


def run_stages():
    env = {**os.environ, "SEMMEDDB_DB": str(DB)}
    for stage in STAGES:
        print(f"[preprocess] {stage}", flush=True)
        subprocess.run([sys.executable, str(HERE / "preprocess" / stage)], check=True, env=env)


def assemble():
    """Resolve to the transform-ready flat file.

    Procedure ID preference: NCIT (open label kept) > SNOMED (code only, label
    BLANK per SNOMED licensing) > UMLS (label blank). Disease from MONDO/HP.
    IC scores carried through so downstream can threshold (e.g. best_ic>=8 AND
    object_ic>=4); we do NOT hard-filter on IC here.
    """
    con = duckdb.connect(str(DB), read_only=True)
    con.execute(f"""
      COPY (
        SELECT
          coalesce(u.ncit, p.proc_snomed_id_v, p.subject_id)            AS procedure_id,
          coalesce(u.ncit_label, '')                                    AS procedure_label,  -- open (NCIT) only; SNOMED/UMLS blank
          p.predicate                                                   AS predicate,        -- 'diagnoses' | 'treats'
          p.final_object_id                                             AS disease_id,
          CASE WHEN p.final_object_id LIKE 'UMLS:%' OR p.final_object_id LIKE 'SNOMED:%'
               THEN '' ELSE p.object_label END                         AS disease_label,  -- blank only licensed vocabs
          p.object_category                                             AS disease_category,
          p.n_pmids_true                                                AS support_pmids,
          round(p.best_ic, 3)                                           AS procedure_ic,
          round(p.object_ic, 3)                                         AS disease_ic,
          ''                                                            AS publications   -- TODO: aggregate distinct predicted=True PMIDs
        FROM procedure_disease_clean p
        LEFT JOIN umls2ncit u ON u.umls = p.subject_id
        WHERE p.predicate IN ('diagnoses','treats')
      ) TO '{HERE.parent / "data" / "procedure_to_disease.tsv"}' (HEADER, DELIMITER '\t')
    """)
    n = con.execute("SELECT count(*) FROM procedure_disease_clean WHERE predicate IN ('diagnoses','treats')").fetchone()[0]
    con.close()
    print(f"[preprocess] wrote data/procedure_to_disease.tsv ({n:,} edges)")


if __name__ == "__main__":
    if "--assemble-only" not in sys.argv:
        run_stages()
    assemble()
