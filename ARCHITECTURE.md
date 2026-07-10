# Architecture

Procedure → disease/phenotype (`diagnoses` / `treats`) edges mined from the
LLM-verified SemMedDB subset, grounded to open ontologies and IC-scored.

## Two-stage shape (why preprocess is heavy, transform is thin)

Koza is row-by-row; the grounding/scoring here is graph + API + join work that
can't be a Koza transform. So:

```
scripts/preprocess.py  (graph/API/join)  ->  data/procedure_to_disease.tsv  ->  koza transform  ->  KGX
```

`preprocess.py` chains the stages in `scripts/preprocess/` (ported from the
exploratory work) and assembles one clean, grounded, IC-scored TSV. The Koza
transform (`src/procedure_to_disease.py`) is deliberately trivial and unit-tested.

Stage contract (tables in the shared duckdb): `nodes` (NodeNorm categories),
`umls2ncit` (+NCIT IC), `cui_xref` (MRCONSO CUI↔SNOMED/NCIT/…), `snomed_ic`,
`procedure_disease_clean` (predicted=True + typed + normalized + best_ic/object_ic).

## Source tiers (why not everything is in a GitHub release)

| tier | sources | where it lives | in `download.yaml`? |
|---|---|---|---|
| **open, large** | SemMedDB LLM-verified subset (~6 GB) | **data bucket (S3/GCS) or Zenodo** | yes (bucket URL) |
| **open, small** | NCIt FLAT/OWL, Mondo/HP SSSOM, MONDO/HP subclass graph | public | yes |
| **license-gated** | SNOMED CT RF2, UMLS MRCONSO | credentialed fetch (`fetch_licensed.py`, `UMLS_API_KEY`) | **no** — never committed, never re-emitted |

The source subset is too big for a GitHub release → host it in a bucket and
point `download.yaml` at it (same pattern loinc-ingest uses for Tuva's S3).

## SNOMED / UMLS licensing rule

SNOMED CT and UMLS content are licensed. The **codes** are referenceable
(Mondo distributes SCTIDs); the **description text is not** openly redistributable.
So:

- License-gated inputs are fetched with `UMLS_API_KEY`, cached under
  `data/licensed/` (gitignored), and used only to derive codes + IC.
- In output, a SNOMED-grounded procedure emits the **SCTID code with a BLANK
  label**; open-vocabulary labels (NCIT / MONDO / HP) pass through. UMLS-only
  fallbacks are also label-blanked.
- The assemble step's procedure-ID preference is **NCIT (open label) > SNOMED
  (code only) > UMLS (code only)**.

### Output tiers
- **Open KGX → GitHub release / kghub**: codes-not-labels; license-clean, publishable like any Monarch ingest.
- **Restricted (optional)**: any label-bearing / full SNOMED enrichment → a UMLS-license-gated store, never the public release.

## Reproducibility

`src/versions.py` pins every input: subset build, SNOMED RF2 date, UMLS release
(2026AA), NCIt version, **NodeNorm snapshot**, SSSOM commit. NodeNorm is a moving
target, so the `nodes` categorisation is snapshotted and versioned rather than
re-fetched per build — same pins → same edges.

## Edge model

`biolink:diagnoses` / `biolink:treats_or_applied_or_studied_to_treat`,
`primary_knowledge_source=infores:semmeddb`, `agent_type=text_mining_agent`,
`knowledge_level=knowledge_assertion`, `original_predicate` carrying the raw
`diagnoses`/`treats`.

`procedure_ic` (max NCIT/SNOMED structural IC) and `disease_ic` (MONDO/HP
structural IC) stay as columns in the preprocess sidecar
`data/procedure_to_disease.tsv` so consumers can threshold (e.g.
`best_ic≥8 AND object_ic≥4` → the ~35.5k defensible core). They are NOT emitted
as edge properties — biolink pydantic forbids extra slots; promoting them to a
qualifier/slot is a follow-up. We score, not hard-filter, in the ingest.
