"""Centralized paths for the preprocess stages.

Everything resolves under the ingest's data/ dir (gitignored). The shared duckdb
is $SEMMEDDB_DB (default data/semmeddb.duckdb). License-gated inputs (SNOMED RF2,
MRCONSO) are located from the marker files fetch_licensed.py writes into
data/licensed/, and the date-stamped RF2 / NCIt files are globbed (release-agnostic).
"""
import glob
import os
import zipfile
from pathlib import Path

INGEST = Path(__file__).resolve().parents[2]      # repo root
DATA = INGEST / "data"
LICENSED = DATA / "licensed"

DB = Path(os.environ.get("SEMMEDDB_DB", DATA / "semmeddb.duckdb"))

# --- open inputs (download.yaml) ---
SEMMEDDB_PARQUET = DATA / "semmeddb_subset.parquet"   # MUST include predicted/support cols
MONDO_SSSOM = DATA / "mondo.sssom.tsv"
HPO_SSSOM = DATA / "hpo-snomed.sssom.tsv"
MONDO_EDGES = DATA / "mondo_edges.tsv"

# NodeNorm categorisation snapshot — versioned artifact so builds don't re-hit
# the (moving-target) API. Committed/hosted alongside the source for reproducibility.
NODES_SNAPSHOT = DATA / "nodes_snapshot.parquet"


def _unzip_once(zip_path: Path, member_glob: str) -> Path:
    """Extract the first member matching member_glob into data/ (idempotent)."""
    hits = sorted(DATA.glob(member_glob))
    if hits:
        return hits[0]
    with zipfile.ZipFile(zip_path) as zf:
        name = next(m for m in zf.namelist() if Path(m).name.startswith(member_glob.rstrip("*")))
        zf.extract(name, DATA)
        return DATA / name


def ncit_flat() -> Path:
    return _unzip_once(DATA / "ncit_flat.zip", "Thesaurus.txt")


def ncit_owl() -> Path:
    return _unzip_once(DATA / "ncit_owl.zip", "Thesaurus.owl")


def _read_marker(name: str) -> Path:
    p = LICENSED / name
    if not p.exists():
        raise FileNotFoundError(f"{p} missing — run `just fetch-licensed` first (needs UMLS_API_KEY).")
    return Path(p.read_text().strip())


def mrconso_zip() -> Path:
    return _read_marker("MRCONSO_ZIP_PATH")


def _snomed_terminology_dir() -> Path:
    """Extract SNOMED RF2 zip (once) and return its Snapshot/Terminology dir."""
    root = DATA / "snomed_rf2"
    if not root.exists():
        with zipfile.ZipFile(_read_marker("SNOMED_RF2_PATH")) as zf:
            zf.extractall(root)
    hits = glob.glob(str(root / "**" / "Snapshot" / "Terminology"), recursive=True)
    if not hits:
        raise FileNotFoundError("SNOMED Snapshot/Terminology not found under extracted RF2")
    return Path(hits[0])


def snomed_rel_file() -> Path:
    return next(_snomed_terminology_dir().glob("sct2_Relationship_Snapshot_*.txt"))


def snomed_desc_file() -> Path:
    return next(_snomed_terminology_dir().glob("sct2_Description_Snapshot-en_*.txt"))
