#!/usr/bin/env python3
"""
Fetch the LICENSE-GATED inputs (SNOMED CT RF2, UMLS MRCONSO) using UMLS_API_KEY.

These are NEVER committed and NEVER re-emitted in ingest output — only codes
derived from them flow downstream (SNOMED labels are stripped; see ARCHITECTURE.md).
Cached under data/licensed/ (gitignored). Kept separate from download.yaml so the
open `just download` path needs no credentials.

Requires: UMLS_API_KEY in the environment. MRCONSO is streamed from the cached
zip (~500 MB) via stdlib zipfile — do NOT use umls_downloader.open_mrconso_reader
(broken against current pystow: passes an `operation` kwarg).
"""
import os
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "licensed"


def main():
    key = os.environ.get("UMLS_API_KEY")
    if not key:
        sys.exit("UMLS_API_KEY not set — required for SNOMED RF2 + UMLS MRCONSO.")
    DATA.mkdir(parents=True, exist_ok=True)

    # Charlie Hoyt's umls-downloader handles the UMLS TGT auth for both inputs.
    # Use it as a DOWNLOADER ONLY — its open_mrconso_reader is broken against
    # current pystow (passes an `operation` kwarg); read the cached zip with
    # stdlib zipfile instead (see scripts/preprocess/umls_crosswalk.py).
    # `bioversions` is a required transitive dep (imported in the versioned path).
    # Pin versions explicitly for reproducibility — never resolve "latest".
    import umls_downloader as ud
    from src.versions import UMLS_VERSION  # e.g. "2026AA"

    # UMLS MRCONSO subset (~500 MB), for cui_xref.
    mrconso_zip = ud.download_umls(version=UMLS_VERSION, api_key=key)
    print(f"[fetch_licensed] MRCONSO zip: {mrconso_zip}")
    (DATA / "MRCONSO_ZIP_PATH").write_text(str(mrconso_zip))

    # SNOMED CT US RF2, for snomed_ic + Has-focus core.
    snomed_zip = ud.download_snomed_us(api_key=key)  # pin version= once confirmed
    print(f"[fetch_licensed] SNOMED US RF2: {snomed_zip}")
    (DATA / "SNOMED_RF2_PATH").write_text(str(snomed_zip))

    print("[fetch_licensed] done — paths recorded for the preprocess stages.")


if __name__ == "__main__":
    main()
