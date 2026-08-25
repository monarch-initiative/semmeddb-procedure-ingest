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

## Source tiers

| tier | sources | where it lives | in `download.yaml`? |
|---|---|---|---|
| **open** | SemMedDB LLM-verified subset **slice** (~15 MB), NCIt FLAT/OWL, Mondo SSSOM, mondo.obo + hp.obo | public URLs / GitHub release | yes |
| **license-gated** | SNOMED CT RF2, UMLS MRCONSO | credentialed fetch (`fetch_licensed.py`, `UMLS_API_KEY`) | **no** — never committed, never re-emitted |

No bucket or Zenodo deposit is needed. The upstream LLM-verified subset is ~6.2 GB
/ 26.7M predications across all predicates, but this ingest reads **two predicates
and eight columns** of it. `scripts/make_source_subset.py` slices that once
(2.54M rows, ~15 MB zstd parquet) and the slice is what ships as a GitHub release
asset — comfortably under the 2 GB asset limit. Everything the build fetches is a
public URL.

## SNOMED / UMLS licensing rule

SNOMED CT and UMLS content are licensed. The **codes** are referenceable
(Mondo distributes SCTIDs); the **description text is not** openly redistributable.
So:

- License-gated inputs are fetched with `UMLS_API_KEY` into the shared pystow
  cache (`~/.data/bio/...`), with `data/licensed/` holding only pointer files
  (gitignored). They are used only to derive codes + IC.
- `umls-downloader` supplies the UTS ticket-granting auth, but its
  `download_snomed_us()` is pinned to the *USEditionRF2 20220301* package. We
  need *ManagedServiceUS `SNOMED_RF2_VERSION`*, so the URL is built from the pin
  in `versions.py` and passed to the generic `download_tgt`.
- In output, a SNOMED-grounded procedure emits the **SCTID code with a BLANK
  label**; open-vocabulary labels (NCIT / MONDO / HP) pass through. UMLS-only
  fallbacks are also label-blanked.
- The assemble step's procedure-ID preference is **NCIT (open label) > SNOMED
  (code only) > UMLS (code only)**.

### Output tiers
Set by `OUTPUT_TIER` (default `restricted`); the build prints which tier it produced.

- **`restricted`** → a private, UMLS-license-gated bucket. SNOMED/UMLS description text
  IS emitted, so every node carries a name. **Never publish this artifact openly.**
- **`open`** → GitHub release / kghub. SNOMED/UMLS labels blanked (codes only);
  NCIT/MONDO/HP labels pass through. License-clean and publishable like any Monarch ingest.

## Shipping filter

Applied in `assemble()`, so `data/procedure_to_disease.tsv` IS the shipped set; the full
unfiltered edge set stays in the duckdb and the `procedure_disease_clean.tsv` sidecar.

1. **Object namespace ∈ {MONDO, HP}.** Everything else is a concept Mondo has no term
   for — NodeNorm returns MONDO whenever one exists, so UMLS/NCIT/EFO objects are exactly
   the residue it could not collapse, and <0.1% are rescuable via mondo.sssom or mondo.obo
   xrefs. monarch-kg carries zero NCIT/EFO nodes, so they would merge with nothing.
2. **`object_ic >= 6`.** MONDO/HP structural IC over a clean mono-hierarchy; this one
   discriminates properly (`disease` 0.72, `cancer` 4.52, `breast cancer` 9.05).
3. **Curated procedure blocklist** (`src/procedure_blocklist.tsv`, 170 concepts): vacuous
   terms, drug-modality/regimen subjects that belong on the drug→disease axis, non-procedure
   NCIT semantic types, and SNOMED qualifier/action concepts.

### Why there is no procedure-IC threshold

There was one (`best_ic >= 8`) and it was actively harmful. `best_ic` is structural IC over
SNOMED/NCIT, which measures **leaf-ness in a poly-hierarchy, not genericness**. A generic
CUI xrefs to many SNOMED codes and at least one is always a leaf at the 18.559 ceiling
(64.8% of SNOMED concepts sit there), so the vaguest concepts scored highest:

| concept | best_ic |
|---|---|
| Operative Surgical Procedures | 18.56 (ceiling) |
| Diagnosis | 16.56 |
| **Mammography** | **12.54** |
| Colonoscopy / Cholecystectomy | no SNOMED xref → NULL |

Thresholding it removed 622 edges from 4 subjects, while its NULL arm silently deleted
8,606 edges of unmapped-but-specific procedures — **it dropped the electrocardiogram and
kept "Operative Surgical Procedures."** `min()`, `max()` and qualifier-excluded variants
all still rank generic above specific, so it is unsalvageable by tuning. Genericness is a
curation problem here, not a metric problem. `procedure_ic` remains in the sidecar as a
descriptive column — do not filter on it.

## Reproducibility

`src/versions.py` pins every input: subset build, SNOMED RF2 date, UMLS release
(2026AA), NCIt version, **NodeNorm snapshot**, SSSOM/OBO release. Disease and
phenotype IC come from the canonical `mondo.obo` + `hp.obo` releases (which
self-version via their `data-version:` header) rather than a Monarch KG dump, so
the hierarchy IC is computed over is the ontology's own. NodeNorm is a moving
target, so the `nodes` categorisation is snapshotted and versioned rather than
re-fetched per build — same pins → same edges.

## Edge model

`biolink:diagnoses` / `biolink:treats_or_applied_or_studied_to_treat`,
`primary_knowledge_source=infores:semmeddb`, `agent_type=text_mining_agent`,
`knowledge_level=knowledge_assertion`, `original_predicate` carrying the raw
`diagnoses`/`treats`.

`publications` carries the supporting PMIDs — **only those the LLM accepted**
(`predicted='True'`), sorted for build determinism. The rejected rows never reach
an edge (49% of the sliced source is `predicted='False'`), so the PMID list is
verified evidence, not raw SemMedDB support; `evidence_count` on the edge and
`support_pmids` in the sidecar are its count. The assemble step
fails the build if the two ever disagree.

`procedure_ic` (max NCIT/SNOMED structural IC) and `disease_ic` (MONDO/HP
structural IC) stay as columns in the preprocess sidecar
`data/procedure_to_disease.tsv` so we can threshold at build time. They are NOT
emitted.

Note the tempting wrong fix: `information_content` is a real biolink slot on
`named thing`, so it *looks* like the natural home. But **monarch-kg computes its
own IC over the merged graph** (421,065 terms) and it is a different number —
cancer (MONDO:0004992) is 7.53 there and 4.52 here, because ours is structural IC
over a single ontology's asserted hierarchy. Emitting ours into that slot would
collide with Monarch's, so it stays a build-time triage signal.

Caveat on using it as a knob: 27,721 edges (39.8%) have at least one NULL IC —
NULL fails a `>=` comparison silently, so a threshold quietly drops them rather
than flagging them.
