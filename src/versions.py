"""Upstream source version fetcher for semmeddb-procedure-ingest.

Pin EVERY input so the same pins reproduce the same edges. NodeNorm is a moving
target (categories/normalized_ids change across releases) — treat the cached
`nodes` snapshot as a versioned source, don't re-hit the API each build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kozahub_metadata_schema import now_iso, urls_from_download_yaml

INGEST_DIR = Path(__file__).resolve().parents[1]
DOWNLOAD_YAML = INGEST_DIR / "download.yaml"

# Pin these to the exact releases used for a given build.
SNOMED_RF2_VERSION = "US1000124_20260301"      # SNOMED CT US edition release
UMLS_VERSION = "2026AA"                          # UMLS Metathesaurus (MRCONSO)
NCIT_VERSION = "26.05e"                           # NCIt Thesaurus release
NODENORM_VERSION = "2026-XX"                       # Translator NodeNorm release (snapshot)
SEMMEDDB_SUBSET_VERSION = "llm-verified-2026-06"   # the source subset build


def _src(id_, name, version, method, urls=None, note=None):
    d = {
        "id": id_, "name": name, "urls": urls or [],
        "version": version, "version_method": method, "retrieved_at": now_iso(),
    }
    if note:
        d["note"] = note
    return d


def get_source_versions() -> list[dict[str, Any]]:
    return [
        _src("infores:semmeddb", "SemMedDB (LLM-verified subset)", SEMMEDDB_SUBSET_VERSION,
             "pinned", urls_from_download_yaml(DOWNLOAD_YAML, contains=["semmeddb"])),
        _src("infores:snomedct", "SNOMED CT US edition (RF2)", SNOMED_RF2_VERSION,
             "pinned", note="license-gated; fetched via fetch_licensed.py, codes only in output"),
        _src("infores:umls", "UMLS Metathesaurus (MRCONSO)", UMLS_VERSION,
             "pinned", note="license-gated; crosswalk only, not redistributed"),
        _src("infores:ncit", "NCI Thesaurus", NCIT_VERSION,
             "pinned", urls_from_download_yaml(DOWNLOAD_YAML, contains=["ncit"])),
        _src("infores:sri-node-normalizer", "Translator NodeNormalizer (snapshot)", NODENORM_VERSION,
             "pinned", note="categories/normalized_ids snapshotted for reproducibility"),
        _src("infores:monarchinitiative", "Mondo/HP SSSOM + subclass graph", "mapping-commons-main",
             "pinned", urls_from_download_yaml(DOWNLOAD_YAML, contains=["sssom", "mondo_edges"])),
    ]
