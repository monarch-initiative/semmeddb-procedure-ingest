#!/usr/bin/env python3
"""
Fetch the LICENSE-GATED inputs (SNOMED CT RF2, UMLS MRCONSO) using UMLS_API_KEY.

These are NEVER committed and NEVER re-emitted in ingest output — only codes
derived from them flow downstream (SNOMED labels are stripped; see ARCHITECTURE.md).
Downloads land in the shared pystow cache (~/.data/bio/...); data/licensed/ holds
only marker files pointing at them (gitignored). Kept separate from download.yaml
so the open `just download` path needs no credentials.

Requires: UMLS_API_KEY in the environment.

Uses Charlie Hoyt's umls-downloader for the UTS ticket-granting auth, but as a
DOWNLOADER ONLY:
  - its `open_mrconso_reader` is broken against current pystow (passes an
    `operation` kwarg) — read the cached zip with stdlib zipfile instead
    (see scripts/preprocess/umls_crosswalk.py).
  - its `download_snomed_us()` is hardcoded to the *USEditionRF2 20220301*
    package. We need *ManagedServiceUS US1000124_20260301* (the release the
    grounding was built on, and the one Mondo's SCTIDs track). So we build the
    URL from the pin in versions.py and hand it to the generic `download_tgt`.
"""
import os
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DATA = REPO / "data" / "licensed"

# NLM serves the US Managed Service RF2 packages from this path. Derived from the
# pin rather than hardcoded so bumping SNOMED_RF2_VERSION is the only edit needed.
SNOMED_URL_FMT = (
    "https://download.nlm.nih.gov/mlb/utsauth/USExt/"
    "SnomedCT_ManagedServiceUS_PRODUCTION_{version}T120000Z.zip"
)


def main():
    key = os.environ.get("UMLS_API_KEY")
    if not key:
        sys.exit("UMLS_API_KEY not set — required for SNOMED RF2 + UMLS MRCONSO.")
    DATA.mkdir(parents=True, exist_ok=True)

    import pystow
    import umls_downloader as ud

    from src.versions import SNOMED_RF2_VERSION, UMLS_VERSION

    # --- UMLS MRCONSO (~500 MB), for cui_xref ---
    mrconso_zip = ud.download_umls(version=UMLS_VERSION, api_key=key)
    print(f"[fetch_licensed] MRCONSO zip: {mrconso_zip}")
    (DATA / "MRCONSO_ZIP_PATH").write_text(str(mrconso_zip))

    # --- SNOMED CT US RF2 (~650 MB), for snomed_ic + Has-focus core ---
    url = SNOMED_URL_FMT.format(version=SNOMED_RF2_VERSION)
    snomed_zip = pystow.join("bio", "snomed", SNOMED_RF2_VERSION, name=url.rsplit("/", 1)[1])
    if not snomed_zip.is_file():
        print(f"[fetch_licensed] downloading {url}")
        ud.download_tgt(url, snomed_zip, api_key=key)
    # A bad/expired ticket makes NLM return an HTML login page with a 200, which
    # gets written to the cache path. Both this guard and download_tgt's own
    # is_file() short-circuit would then treat that poison as a valid cache
    # forever, so validate before recording the marker.
    if not zipfile.is_zipfile(snomed_zip):
        snomed_zip.unlink(missing_ok=True)
        sys.exit(f"{snomed_zip} is not a zip (likely an auth failure page) — removed; retry.")
    print(f"[fetch_licensed] SNOMED US RF2: {snomed_zip}")
    (DATA / "SNOMED_RF2_PATH").write_text(str(snomed_zip))

    print("[fetch_licensed] done — paths recorded for the preprocess stages.")


if __name__ == "__main__":
    main()
