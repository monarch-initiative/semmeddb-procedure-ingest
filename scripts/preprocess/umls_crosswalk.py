#!/usr/bin/env python3
"""
Build the one crosswalk that retires the recurring UMLS gaps: for every CUI we
care about (SemMedDB procedure subjects + residual UMLS disease objects),
collect its codes in SNOMEDCT_US / NCI / LNC / MSH / ICD10CM / RXNORM from
MRCONSO (streamed from the UMLS zip via umls-downloader — first run downloads
the release, ~minutes).

Writes `cui_xref` table + cui_xref.tsv, and reports per-source coverage.
"""
import sys, csv, zipfile, io
from collections import defaultdict
import duckdb
import _paths as P

MRCONSO_ZIP = str(P.mrconso_zip())
WANT_SAB = {"SNOMEDCT_US", "NCI", "LNC", "MSH", "ICD10CM", "RXNORM"}
# MRCONSO column order (RRF)
COLS = ["CUI","LAT","TS","LUI","STT","SUI","ISPREF","AUI","SAUI","SCUI","SDUI",
        "SAB","TTY","CODE","STR","SRL","SUPPRESS","CVF"]
C = {n: i for i, n in enumerate(COLS)}

# read target CUIs (short-lived connection so we don't hold the lock during the scan)
con = duckdb.connect(str(P.DB), read_only=True)
proc = set(r[0] for r in con.execute("""
   SELECT DISTINCT replace(subject_id,'UMLS:','') FROM procedure_disease_clean
   WHERE subject_category='biolink:Procedure'""").fetchall())
diso = set(r[0] for r in con.execute("""
   SELECT DISTINCT replace(final_object_id,'UMLS:','') FROM procedure_disease_clean
   WHERE final_object_id LIKE 'UMLS:%'""").fetchall())
con.close()
want = proc | diso
print(f"target CUIs: {len(want)} ({len(proc)} procedure, {len(diso)} residual-disease)", flush=True)

xref = defaultdict(lambda: defaultdict(set))   # cui -> sab -> {(code,str)}
print("scanning MRCONSO from cached zip...", flush=True)
n = 0
with zipfile.ZipFile(MRCONSO_ZIP) as zf:
    inner = [m for m in zf.namelist() if m.upper().endswith("MRCONSO.RRF")][0]
    with zf.open(inner) as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            n += 1
            if n % 5_000_000 == 0:
                print(f"  scanned {n//1_000_000}M rows", flush=True)
            row = line.rstrip("\n").split("|")
            if len(row) <= C["STR"]:
                continue
            cui = row[C["CUI"]]
            if cui not in want:
                continue
            sab = row[C["SAB"]]
            if sab in WANT_SAB and row[C["SUPPRESS"]] != "O":
                xref[cui][sab].add((row[C["CODE"]], row[C["STR"]]))
print(f"scanned {n:,} MRCONSO rows; matched {len(xref)} CUIs", flush=True)

# Write long-form table: cui, sab, code, str (ONE representative string per code).
#
# This must be deterministic. `pairs` is a set of tuples, so "first one wins" depends
# on per-process hash randomisation and genuinely produced different builds: code
# 257940003 kept "Transposition - action" in one run and "Transposition" in the next.
# Downstream, stamp_best_ic keys its qualifier/action detection on this string, so the
# instability propagated all the way into which SNOMED id a procedure got and which
# edges shipped.
#
# So: iterate in sorted order, and prefer a string that carries SNOMED's semantic tag
# — the FSN always does ("... (qualifier value)", "... - action"), and that tag is
# exactly what the downstream qualifier check needs to see.
def _representative(strings: list[str]) -> str:
    strings = sorted(strings)
    tagged = [s for s in strings if s.endswith(")") or " - action" in s.lower()]
    return (tagged or strings)[0]


rows = []
for cui, sabs in sorted(xref.items()):
    for sab, pairs in sorted(sabs.items()):
        by_code: dict[str, list[str]] = {}
        for code, s in pairs:
            by_code.setdefault(code, []).append(s)
        for code in sorted(by_code):
            rows.append((cui, sab, code, _representative(by_code[code])))
con = duckdb.connect(str(P.DB))
con.execute("CREATE OR REPLACE TABLE cui_xref(cui VARCHAR, sab VARCHAR, code VARCHAR, str VARCHAR)")
con.executemany("INSERT INTO cui_xref VALUES (?,?,?,?)", rows)
con.execute(f"COPY (SELECT * FROM cui_xref) TO '{P.DATA}/cui_xref.tsv' (HEADER, DELIMITER '\t')")

def cov(cuis, sab):
    return sum(1 for c in cuis if c in xref and sab in xref[c])
print("\n=== coverage ===")
print(f"{'SAB':<12} {'procedures':>11} {'residual-dis':>13}")
for sab in ["SNOMEDCT_US","NCI","LNC","MSH","ICD10CM","RXNORM"]:
    print(f"{sab:<12} {cov(proc,sab):>11} {cov(diso,sab):>13}")
print(f"\nprocedure CUIs with SNOMEDCT_US: {cov(proc,'SNOMEDCT_US')}/{len(proc)} "
      f"({cov(proc,'SNOMEDCT_US')/len(proc):.1%})")
print(f"residual-disease CUIs with SNOMEDCT_US: {cov(diso,'SNOMEDCT_US')}/{len(diso)} "
      f"({cov(diso,'SNOMEDCT_US')/max(len(diso),1):.1%})")
con.close()
print("wrote cui_xref.tsv")
