# semmeddb-procedure-ingest

Procedure → Disease/Phenotype (`diagnoses` / `treats`) edges mined from the
LLM-verified SemMedDB subset, grounded to NCIT / SNOMED CT on the subject side and
MONDO / HP on the object side.

Built to fill a zero-edge node-type pair in the Monarch KG: monarch-kg currently has
**no `biolink:Procedure` nodes at all**.

## Output

Two artifacts, split by subject namespace — which is also the licensing boundary:

| transform | subjects | edges | licensing |
|---|---|---|---|
| `procedure_to_disease_ncit` | NCIT | ~23k | **open** — no licensed text, publishable |
| `procedure_to_disease_snomedct` | SNOMED CT | ~5k | **restricted** under `OUTPUT_TIER=restricted` |

`OUTPUT_TIER=open` blanks SNOMED/UMLS description text for public release.
See ARCHITECTURE.md for the full design, the shipping filter, and why there is no
procedure-IC threshold.

## Usage

```bash
just download          # open inputs (needs SUBSET_RELEASE_TAG)
just fetch-licensed    # SNOMED RF2 + UMLS MRCONSO (needs UMLS_API_KEY)
just run               # test + preprocess + transform + metadata
just publish           # upload output/ to gs://monarch-bdc-kg/semmeddb/<date>/
just test
```

`just run` is the whole pipeline from cold. See **Testing and verification** in
ARCHITECTURE.md before changing anything in `scripts/preprocess/` — in particular,
determinism must be checked with two cold builds under *different* `PYTHONHASHSEED`.

## Layout

- `download.yaml` — open inputs (kghub-downloader; parses as a bare top-level list)
- `scripts/fetch_licensed.py` — credentialed SNOMED/UMLS fetch
- `scripts/preprocess.py` — stage orchestration (dependency order; do not reorder) + assemble
- `scripts/preprocess/` — the stages
- `scripts/make_source_subset.py` — maintainer-only: slices the 6.2 GB upstream subset to
  the ~15 MB artifact `download.yaml` fetches
- `src/procedure_to_disease.py` — the (deliberately thin) Koza transform, shared by both configs
- `src/procedure_blocklist.tsv` — curated procedure exclusions, keyed on UMLS CUI
- `src/versions.py` — input version pins for the release receipt

`scripts/preprocess/snomed_has_focus_core.py` is **not** part of `just run`. It builds the
separate SNOMED Has-focus precision-anchor edge set published as its own bucket source.

## License

MIT (code). Source data licensing differs per tier — see ARCHITECTURE.md.
