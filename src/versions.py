"""Upstream source version fetcher for semmeddb-procedure-ingest.

Pin EVERY input so the same pins reproduce the same edges. NodeNorm is a moving
target (categories/normalized_ids change across releases) — treat the cached
`nodes` snapshot as a versioned source, don't re-hit the API each build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kozahub_metadata_schema import (
    now_iso,
    urls_from_download_yaml,
    version_from_file_header,
    version_from_github_branch,
)

INGEST_DIR = Path(__file__).resolve().parents[1]
DOWNLOAD_YAML = INGEST_DIR / "download.yaml"
DATA = INGEST_DIR / "data"

# OBO files carry `data-version:` as a plain (uncommented) line near the top, so
# scan with an empty comment prefix rather than the default `!`.
OBO_VERSION_PATTERN = r"^data-version:\s*(\S+)"

# Pin these to the exact releases used for a given build.
# SNOMED CT US *Managed Service* RF2 release. fetch_licensed.py builds the NLM
# download URL from this string, so it must match the package name exactly.
SNOMED_RF2_VERSION = "US1000124_20260301"
UMLS_VERSION = "2026AA"                            # UMLS Metathesaurus (MRCONSO)
NCIT_VERSION = "26.05e"                            # NCIt Thesaurus release
NODENORM_VERSION = "snapshot-2026-07-09"           # data/nodes_snapshot.parquet build date
SEMMEDDB_SUBSET_VERSION = "llm-verified-2026-06"   # the upstream subset build this slices


def _src(id_, name, version, method, urls=None, note=None):
    d = {
        "id": id_, "name": name, "urls": urls or [],
        "version": version, "version_method": method, "retrieved_at": now_iso(),
    }
    if note:
        d["note"] = note
    return d


def get_source_versions() -> list[dict[str, Any]]:
    # Resolved at build time from the files actually downloaded, so the receipt
    # records the release that was used rather than one hardcoded here.
    mondo_v = version_from_file_header(DATA / "mondo.obo", pattern=OBO_VERSION_PATTERN,
                                       comment_prefix="")
    hp_v = version_from_file_header(DATA / "hp.obo", pattern=OBO_VERSION_PATTERN,
                                    comment_prefix="")
    mc_v = version_from_github_branch("monarch-initiative/monarch-mapping-commons")

    # version_from_file_header swallows every exception and returns ("unknown",
    # "unavailable"), so a receipt can otherwise claim "unknown" with exit 0 when the
    # OBO files simply were not downloaded. These two are build inputs; if we cannot
    # name the release we used, the receipt is worthless — say so.
    for name, (version, _method) in (("mondo.obo", mondo_v), ("hp.obo", hp_v)):
        if version == "unknown":
            raise RuntimeError(
                f"could not read data-version from data/{name} — run `just download` "
                "before `just metadata`, or the release receipt will be unverifiable."
            )

    return [
        _src("infores:semmeddb", "SemMedDB (LLM-verified subset)", SEMMEDDB_SUBSET_VERSION,
             "pinned", urls_from_download_yaml(DOWNLOAD_YAML, contains=["semmeddb"])),
        _src("infores:snomedct", "SNOMED CT US edition (RF2)", SNOMED_RF2_VERSION,
             "pinned", note="license-gated; fetched via fetch_licensed.py, codes only in output"),
        _src("infores:umls", "UMLS Metathesaurus (MRCONSO)", UMLS_VERSION,
             "pinned", note="license-gated; crosswalk only, not redistributed"),
        _src("infores:ncit", "NCI Thesaurus", NCIT_VERSION,
             # match on "Thesaurus": the NCIt URLs say NCI_Thesaurus, not ncit
             "pinned", urls_from_download_yaml(DOWNLOAD_YAML, contains=["Thesaurus"])),
        _src("infores:sri-node-normalizer", "Translator NodeNormalizer (snapshot)", NODENORM_VERSION,
             "pinned", note="categories/normalized_ids snapshotted for reproducibility"),
        _src("infores:mondo", "Mondo Disease Ontology (+ its SSSOM mappings)", *mondo_v,
             urls=urls_from_download_yaml(DOWNLOAD_YAML, contains=["mondo.obo", "mondo.sssom"]),
             note="subclass graph for disease IC; mondo.sssom ships with the same release"),
        _src("infores:hpo", "Human Phenotype Ontology", *hp_v,
             urls=urls_from_download_yaml(DOWNLOAD_YAML, contains=["hp.obo"]),
             note="subclass graph for phenotype IC"),
        _src("infores:monarchinitiative", "Monarch mapping commons (hpo-snomed SSSOM)", *mc_v,
             urls=urls_from_download_yaml(DOWNLOAD_YAML, contains=["hpo-snomed"])),
    ]
