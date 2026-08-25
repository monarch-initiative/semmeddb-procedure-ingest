#!/usr/bin/env python3
"""
Compute SNOMED structural IC (Resnik, descendant-based) over the full RF2 Is-a
graph, store as `snomed_ic(sctid, ic, desc)`, then ground bucket C (subject_ic
NULL = procedure not in NCIT) through cui_xref -> SNOMED -> SNOMED IC.
"""
import math
from collections import defaultdict, deque
import duckdb
import _paths as P

REL = str(P.snomed_rel_file())

# build parent map from active Is-a (116680003): src is-a dest
parents = defaultdict(set); nodes = set()
with open(REL) as f:
    next(f)
    for line in f:
        c = line.split("\t")
        if c[2] == "1" and c[7] == "116680003":
            parents[c[4]].add(c[5]); nodes.add(c[4]); nodes.add(c[5])
N = len(nodes)
print(f"SNOMED concepts: {N:,}")

# descendant counts via per-node ancestor walk (low memory, no global memo)
desc = defaultdict(int)
for n in nodes:
    seen = set(); stack = list(parents.get(n, ()))
    while stack:
        a = stack.pop()
        if a in seen:
            continue
        seen.add(a); stack.extend(parents.get(a, ()))
    for a in seen:
        desc[a] += 1
rows = [(sid, round(-math.log2((desc.get(sid, 0) + 1) / N), 3), desc.get(sid, 0)) for sid in nodes]
print(f"IC computed. e.g. Procedure(71388002) IC={dict((r[0],r[1]) for r in rows).get('71388002')}")

con = duckdb.connect(str(P.DB))
con.execute("CREATE OR REPLACE TABLE snomed_ic(sctid VARCHAR, ic DOUBLE, n_desc INTEGER)")
con.executemany("INSERT INTO snomed_ic VALUES (?,?,?)", rows)

# best SNOMED code per procedure CUI (prefer the most specific = highest IC)
con.execute("""CREATE OR REPLACE TEMP TABLE proc_sn AS
  SELECT 'UMLS:'||x.cui AS subject_id, arg_max(x.code, s.ic) AS sctid, max(s.ic) AS snomed_ic
  FROM cui_xref x JOIN snomed_ic s ON s.sctid = x.code
  WHERE x.sab='SNOMEDCT_US' GROUP BY x.cui""")

print("\n=== BUCKET C (subject_ic NULL): how many ground to SNOMED + IC distribution ===")
for r in con.execute("""
  SELECT
    count(DISTINCT p.subject_id) bucketC_procs,
    count(DISTINCT ps.subject_id) grounded_snomed,
    count(DISTINCT ps.subject_id) FILTER (WHERE ps.snomed_ic >= 8) specific_ge8,
    count(DISTINCT ps.subject_id) FILTER (WHERE ps.snomed_ic < 5) broad_lt5
  FROM (SELECT DISTINCT subject_id FROM procedure_disease_clean WHERE subject_ic IS NULL) p
  LEFT JOIN proc_sn ps USING(subject_id)""").fetchall():
    print(f"  bucket-C procedures: {r[0]}")
    print(f"  grounded to SNOMED:  {r[1]}  ({r[1]/r[0]:.0%})")
    print(f"    specific (IC>=8):  {r[2]}")
    print(f"    broad   (IC<5):    {r[3]}")

print("\n=== top bucket-C edges now with SNOMED IC ===")
for r in con.execute("""
  SELECT p.subject_label ||' -['||p.predicate||']-> '|| p.object_label AS edge,
         'SNOMEDCT:'||ps.sctid AS sctid, round(ps.snomed_ic,1) AS sn_ic, p.n_pmids_true AS n
  FROM procedure_disease_clean p JOIN proc_sn ps USING(subject_id)
  WHERE p.subject_ic IS NULL AND ps.snomed_ic >= 8
  ORDER BY p.n_pmids_true DESC LIMIT 15""").fetchall():
    print(f"  {r[0][:60]:<60} {r[1]:<18} IC={r[2]:<5} n={r[3]}")
con.close()
