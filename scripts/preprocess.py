#!/usr/bin/env python3
"""
Preprocessing for semmeddb-procedure-ingest.

Koza is row-by-row; everything here is graph/API/join work that can't be
expressed as a Koza transform. This orchestrates the stages (ported verbatim
from the exploratory session into scripts/preprocess/) against a shared duckdb,
then ASSEMBLES the transform-ready flat file `data/procedure_to_disease.tsv`.

Run order — this is a DEPENDENCY order, not a narrative one. Each stage needs the
tables/columns the ones above it produce, so reordering breaks the build (the
exploratory scripts were run ad hoc, and the order they were first listed in here
did not actually work from a cold database):

  0. fetch_licensed.py         SNOMED RF2 + UMLS MRCONSO via UMLS_API_KEY (credentialed, gitignored)
  1. load_source.py            subset parquet -> `edges`
  2. build_subclass_edges.py   mondo.obo + hp.obo -> mondo_edges.tsv (subclass graph for IC)
  3. build_node_categories.py  NodeNorm -> `nodes` (category, normalized_id)   [SNAPSHOT for reproducibility]
                               needs: edges
  4. materialize_clean.py      `procedure_disease_clean` (predicted=True + typed + normalized + publications)
                               needs: edges, nodes
  5. rescue_umls_objects.py    UMLS disease objects -> MONDO via mondo.sssom; adds `final_object_id`
                               needs: procedure_disease_clean
  5b. ground_objects.py       exact-label rescue of non-MONDO/HP objects (needs final_object_id)
  6. ncit_map_and_ic.py        `umls2ncit` + NCIT IC, `obj_ic` from mondo_edges; stamps subject_ic/object_ic
                               needs: procedure_disease_clean.final_object_id, mondo_edges.tsv
  7. umls_crosswalk.py         MRCONSO -> `cui_xref` (CUI <-> SNOMED/NCIT/LNC/...)
                               needs: procedure_disease_clean.final_object_id
  8. snomed_ic.py              `snomed_ic` (structural IC over RF2 Is-a)
                               needs: cui_xref, subject_ic
  9. route_procedures.py       `procedure_routing` (OBI:assay vs SNOMED:therapeutic)
                               needs: umls2ncit
 10. stamp_best_ic.py          proc_snomed_ic + best_ic (= max NCIT/SNOMED procedure IC)
                               needs: cui_xref, snomed_ic, subject_ic
 11. (this file) ASSEMBLE      -> data/procedure_to_disease.tsv

All stages resolve their inputs through scripts/preprocess/_paths.py (the shared
duckdb is $SEMMEDDB_DB, default data/semmeddb.duckdb).
"""
import os
import subprocess
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent

# --- Shipping filter -------------------------------------------------------
# Applied in assemble(), so data/procedure_to_disease.tsv IS the shipped set.
# The full unfiltered edge set stays in the duckdb and in the
# data/procedure_disease_clean.tsv sidecar for analysis.
#
# 1. Object namespace: MONDO/HP only. Everything else (UMLS/NCIT/EFO/DOID/OMIM)
#    is a concept Mondo has no term for — NodeNorm returns MONDO whenever one
#    exists, so these are exactly the residue it could not collapse. monarch-kg
#    carries ZERO NCIT/EFO nodes, so they would merge with nothing, and <0.1% of
#    them are rescuable via mondo.sssom or mondo.obo xrefs. Restricting here also
#    removes every nameless node and the worst vacuous objects (Lesion=NCIT:C3824).
# 2. Structural IC: drop terms too generic to assert anything. NULL fails these
#    comparisons, which is intended — an unscored term is not a defensible one.
OBJECT_NAMESPACES = ("MONDO", "HP")
MIN_DISEASE_IC = 6.0     # object_ic (MONDO/HP structural IC)

# NO procedure-IC threshold. `best_ic` is structural IC over SNOMED/NCIT, and it does
# not measure genericness — it measures leaf-ness in a poly-hierarchy. A generic CUI
# xrefs to many SNOMED codes and at least one is always a leaf at the 18.559 ceiling
# (64.8% of SNOMED concepts sit there), so the vaguest concepts scored HIGHEST:
# "Diagnosis" 16.56 > "Mammography" 12.54; "Operative Surgical Procedures" 18.56.
# Thresholding it removed 622 edges from 4 subjects while its NULL arm silently
# deleted 8,606 edges of unmapped-but-specific procedures — including the ECG.
# Verified unsalvageable: min(), max(), and qualifier-excluded variants all rank
# generic above specific. Genericness is handled by the curated blocklist instead.
BLOCKLIST = HERE.parent / "src" / "procedure_blocklist.tsv"

# Output tier (see ARCHITECTURE.md). SNOMED/UMLS description text is licensed and
# must NOT appear in a public artifact. "restricted" emits it for a private,
# license-gated destination; "open" blanks it. The build prints which tier it made.
OUTPUT_TIER = os.environ.get("OUTPUT_TIER", "restricted")
DB = Path(os.environ.get("SEMMEDDB_DB", HERE.parent / "data" / "semmeddb.duckdb"))
# Dependency order — see the module docstring. Do not reorder casually.
STAGES = [
    "load_source.py",
    "build_subclass_edges.py",
    "build_node_categories.py",
    "materialize_clean.py",
    "rescue_umls_objects.py",
    "ground_objects.py",
    "ncit_map_and_ic.py",
    "umls_crosswalk.py",
    "snomed_ic.py",
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

    # Guard BEFORE writing: every edge must carry exactly its predicted=True PMIDs.
    # This is the failure mode the column shipped empty from, and a guard that runs
    # after the COPY still leaves a corrupt TSV on disk for the next step to consume.
    n, bad = con.execute("""
      SELECT count(*),
             count(*) FILTER (WHERE coalesce(publications, '') = ''
                              OR len(str_split(publications, '|')) <> n_pmids_true)
      FROM procedure_disease_clean WHERE predicate IN ('diagnoses','treats')
    """).fetchone()
    if bad:
        raise SystemExit(f"[preprocess] {bad:,}/{n:,} edges have missing/miscounted publications")

    # Several distinct UMLS CUIs can resolve to the SAME NCIT/SNOMED procedure id
    # (9 SNOMED codes are claimed by >1 CUI here). Emitting one row per CUI then
    # produced duplicate (procedure, predicate, disease) triples -> identical uuid5
    # edge ids in the KGX, each carrying only PART of the evidence. So aggregate at
    # the RESOLVED id level: union the PMIDs, recount support, keep the max IC.
    emit_licensed = "TRUE" if OUTPUT_TIER == "restricted" else "FALSE"
    print(f"[preprocess] OUTPUT TIER = {OUTPUT_TIER!r} — licensed (SNOMED/UMLS) labels "
          f"{'INCLUDED, do NOT publish openly' if OUTPUT_TIER == 'restricted' else 'blanked'}")
    ns_pred = " OR ".join(
        f"p.final_object_id LIKE '{ns}:%'" for ns in OBJECT_NAMESPACES
    )
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE blocked AS
      SELECT subject_cui AS id FROM read_csv('{BLOCKLIST}', delim='\t', header=true)
    """)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE onto_label AS
      SELECT id, label FROM read_csv('{HERE.parent / "data" / "ontology_labels.tsv"}',
                                     delim='\t', header=true, quote='', ignore_errors=true)
    """)
    con.execute(f"""
      COPY (
        WITH resolved AS (
          SELECT
            coalesce(u.ncit, p.proc_snomed_id_v, p.subject_id) AS procedure_id,
            CASE
              WHEN coalesce(u.ncit, p.proc_snomed_id_v, p.subject_id) LIKE 'NCIT:%'
                THEN coalesce(op.label, u.ncit_label, '')
              WHEN {emit_licensed} THEN coalesce(p.subject_label, '')
              ELSE ''
            END                                                AS procedure_label,
            p.predicate                                        AS predicate,
            p.final_object_id                                  AS disease_id,
            -- ol.label (the ontology's own) wins over final_object_label (NodeNorm's
            -- UMLS-preferred string); licensed vocabularies stay blank.
            CASE WHEN (p.final_object_id LIKE 'UMLS:%' OR p.final_object_id LIKE 'SNOMEDCT:%')
                        AND NOT {emit_licensed}
                 THEN ''
                 ELSE coalesce(ol.label, p.final_object_label) END AS disease_label,
            p.object_category                                  AS disease_category,
            p.subject_category                                 AS procedure_category,
            p.best_ic, p.object_ic, p.publications
          FROM procedure_disease_clean p
          LEFT JOIN umls2ncit u ON u.umls = p.subject_id
          LEFT JOIN onto_label ol ON ol.id = p.final_object_id
          LEFT JOIN onto_label op ON op.id = coalesce(u.ncit, p.proc_snomed_id_v, p.subject_id)
          WHERE p.predicate IN ('diagnoses','treats')
            AND ({ns_pred})
            AND p.object_ic >= {MIN_DISEASE_IC}
            -- keyed on the UMLS CUI, NOT the resolved id: which NCIT/SNOMED code a CUI
            -- resolves to can shift between builds, and a resolved-id blocklist then
            -- silently fails open (it leaked 194 edges that way).
            AND p.subject_id NOT IN (SELECT id FROM blocked)
        ), merged AS (
          SELECT
            procedure_id, predicate, disease_id,
            max(procedure_label)  AS procedure_label,   -- max(), not any_value(): deterministic,
            max(disease_label)    AS disease_label,     -- and prefers a real label over ''
            max(disease_category)   AS disease_category,
            max(procedure_category) AS procedure_category,
            max(best_ic)          AS procedure_ic,
            max(object_ic)        AS disease_ic,
            list_sort(list_distinct(flatten(list(str_split(publications, '|'))))) AS pmids
          FROM resolved GROUP BY procedure_id, predicate, disease_id
        )
        SELECT procedure_id, procedure_label, procedure_category,
               predicate, disease_id, disease_label, disease_category,
               len(pmids)                       AS support_pmids,
               round(procedure_ic, 3)           AS procedure_ic,
               round(disease_ic, 3)             AS disease_ic,
               array_to_string(pmids, '|')      AS publications
        FROM merged
        ORDER BY procedure_id, predicate, disease_id   -- stable file byte-for-byte
      ) TO '{HERE.parent / "data" / "procedure_to_disease.tsv"}' (HEADER, DELIMITER '\t')
    """)

    emitted, triples = con.execute(f"""
      SELECT count(*), count(DISTINCT (procedure_id, predicate, disease_id))
      FROM read_csv_auto('{HERE.parent / "data" / "procedure_to_disease.tsv"}',
                         delim='\t', header=true, all_varchar=true)
    """).fetchone()
    if emitted != triples:
        raise SystemExit(f"[preprocess] {emitted - triples} duplicate triples survived aggregation")

    n_blocked = con.execute("SELECT count(*) FROM blocked").fetchone()[0]
    print(f"[preprocess] filter: objects in {'/'.join(OBJECT_NAMESPACES)}, "
          f"object_ic>={MIN_DISEASE_IC}, blocklist={n_blocked} procedures")
    for label, cond in (
        ("all clean edges", "1=1"),
        (f"  objects in {'/'.join(OBJECT_NAMESPACES)}", ns_pred.replace("p.", "")),
        ("  + disease IC ok", f"({ns_pred.replace('p.', '')}) AND object_ic >= {MIN_DISEASE_IC}"),
    ):
        k = con.execute("SELECT count(*) FROM procedure_disease_clean "
                        f"WHERE predicate IN ('diagnoses','treats') AND {cond}").fetchone()[0]
        print(f"[preprocess]   {label:<34} {k:>7,}")
    con.close()
    print(f"[preprocess] wrote data/procedure_to_disease.tsv "
          f"({emitted:,} edges from {n:,} CUI-level rows)")


if __name__ == "__main__":
    if "--assemble-only" not in sys.argv:
        run_stages()
    assemble()
