#!/usr/bin/env python3
"""
(a) UMLS -> NCIT coverage for the procedure subjects, using NCIt's own data
    (open, public-domain): code->CUI from the OWL's <P207>, hierarchy+label+
    semantic-type from the FLAT file.
(b) Structural IC (Resnik, descendant-based) for NCIT, MONDO and HP, stamped
    onto procedure_disease_clean as subject_ic / object_ic.

Inputs (NCIt FLAT+OWL already downloaded to NCIT_DIR):
  Thesaurus.txt  cols: code, iri, parents(|), synonyms(|), def, _, status, semtype, subset
  Thesaurus.owl  <owl:Class rdf:about="...#Ccode"> ... <P207>Ccui</P207>
"""
import math, re, sys
from collections import defaultdict, deque
import duckdb
import _paths as P

NCIT_FLAT = str(P.ncit_flat())
NCIT_OWL = str(P.ncit_owl())
MONDO_EDGES = str(P.MONDO_EDGES)
DB = str(P.DB)


def struct_ic(child2parents, nodes):
    """Resnik structural IC: -log2((descendants+1)/N) via memoized ancestor DFS."""
    anc_cache = {}
    def anc(n):
        if n in anc_cache:
            return anc_cache[n]
        s = set()
        for p in child2parents.get(n, ()):
            s.add(p); s |= anc(p)
        anc_cache[n] = s
        return s
    desc = defaultdict(int)
    for n in nodes:
        for a in anc(n):
            desc[a] += 1
    N = len(nodes)
    return {n: round(-math.log2((desc.get(n, 0) + 1) / N), 3) for n in nodes}, N


# ---- parse NCIt FLAT: code -> parents, label, semtype ----
ncit_parents = defaultdict(set)
ncit_nodes = set()
ncit_label = {}
ncit_semtype = {}
with open(NCIT_FLAT, encoding="utf-8") as f:
    for line in f:
        c = line.rstrip("\n").split("\t")
        if len(c) < 8:
            continue
        code = c[0]
        ncit_nodes.add(code)
        ncit_label[code] = c[3].split("|")[0] if c[3] else ""
        ncit_semtype[code] = c[7]
        if c[2]:
            for p in c[2].split("|"):
                ncit_parents[code].add(p); ncit_nodes.add(p)
print(f"NCIt concepts: {len(ncit_nodes):,}", file=sys.stderr)

# ---- parse OWL: code -> set(CUIs) via streaming ----
about_re = re.compile(r'rdf:about="[^"]*#(C\d+)"')
p207_re = re.compile(r'<P207>(C\d+|CL\d+)</P207>')
code2cui = defaultdict(set)
cur = None
with open(NCIT_OWL, encoding="utf-8") as f:
    for line in f:
        if "owl:Class" in line:
            m = about_re.search(line)
            if m:
                cur = m.group(1)
        elif cur and "<P207>" in line:
            m = p207_re.search(line)
            if m:
                code2cui[cur].add(m.group(1))
print(f"NCIt codes with a CUI: {len(code2cui):,}", file=sys.stderr)

# invert: CUI -> [ncit codes]
cui2codes = defaultdict(list)
for code, cuis in code2cui.items():
    for cui in cuis:
        cui2codes[cui].append(code)

# ---- NCIT structural IC ----
ncit_ic, ncit_N = struct_ic(ncit_parents, ncit_nodes)
print(f"NCIt IC computed over N={ncit_N:,}", file=sys.stderr)

# ---- (a) coverage of procedure subject CUIs ----
con = duckdb.connect(DB)
proc_cuis = [r[0] for r in con.execute("""
   SELECT DISTINCT replace(subject_id,'UMLS:','')
   FROM procedure_disease_clean WHERE subject_category='biolink:Procedure'
""").fetchall()]
mapped = {c: cui2codes[c] for c in proc_cuis if c in cui2codes}
print(f"\n=== (a) UMLS->NCIT procedure coverage ===")
print(f"distinct procedure CUIs:        {len(proc_cuis):,}")
print(f"  with >=1 NCIT code:           {len(mapped):,}  ({len(mapped)/len(proc_cuis):.1%})")

# pick best NCIT code per CUI (prefer procedure/activity semtypes), build map table
PROC_SEMTYPES = {"Therapeutic or Preventive Procedure", "Diagnostic Procedure",
                 "Laboratory Procedure", "Health Care Activity", "Activity",
                 "Research Activity", "Intellectual Product",
                 "Molecular Biology Research Technique"}
def best(codes):
    pc = [c for c in codes if ncit_semtype.get(c) in PROC_SEMTYPES]
    return (pc or codes)[0]
rows = []
semtype_hist = defaultdict(int)
for cui, codes in mapped.items():
    code = best(codes)
    semtype_hist[ncit_semtype.get(code, "?")] += 1
    rows.append(("UMLS:"+cui, "NCIT:"+code, ncit_label.get(code, ""),
                 ncit_semtype.get(code, ""), ncit_ic.get(code)))
print("  semantic types of matched NCIT codes (top):")
for st, c in sorted(semtype_hist.items(), key=lambda x: -x[1])[:8]:
    print(f"    {c:>5}  {st}")

con.execute("CREATE OR REPLACE TABLE umls2ncit(umls VARCHAR, ncit VARCHAR, ncit_label VARCHAR, ncit_semtype VARCHAR, ncit_ic DOUBLE)")
con.executemany("INSERT INTO umls2ncit VALUES (?,?,?,?,?)", rows)

# ---- (b) MONDO + HP structural IC from mondo_edges ----
m_parents = defaultdict(set); m_nodes = set()
res = con.execute(f"""
  SELECT subject, object FROM read_csv_auto('{MONDO_EDGES}', delim='\t', header=true)
  WHERE predicate='biolink:subclass_of'
    AND (subject LIKE 'MONDO:%' OR subject LIKE 'HP:%')
    AND (object  LIKE 'MONDO:%' OR object  LIKE 'HP:%')
""").fetchall()
for s, o in res:
    m_parents[s].add(o); m_nodes.add(s); m_nodes.add(o)
mondo_ic, m_N = struct_ic(m_parents, m_nodes)
print(f"\nMONDO+HP IC computed over N={m_N:,}", file=sys.stderr)

con.execute("CREATE OR REPLACE TABLE obj_ic(id VARCHAR, ic DOUBLE)")
con.executemany("INSERT INTO obj_ic VALUES (?,?)", list(mondo_ic.items()))

# ---- stamp ic columns onto procedure_disease_clean ----
for col in ("subject_ic", "object_ic"):
    try: con.execute(f"ALTER TABLE procedure_disease_clean ADD COLUMN {col} DOUBLE")
    except duckdb.Error: pass
con.execute("UPDATE procedure_disease_clean p SET object_ic = o.ic FROM obj_ic o WHERE o.id = p.final_object_id")
con.execute("UPDATE procedure_disease_clean p SET subject_ic = u.ncit_ic FROM umls2ncit u WHERE u.umls = p.subject_id")

print("\n=== stamped subject_ic (via NCIT) / object_ic (via MONDO/HP) ===")
for r in con.execute("""
   SELECT
     count(*) total,
     count(object_ic) has_obj_ic,
     count(subject_ic) has_subj_ic
   FROM procedure_disease_clean""").fetchall():
    print(f"  total={r[0]:,}  object_ic set={r[1]:,}  subject_ic set={r[2]:,}")
con.execute(f"COPY (SELECT * FROM umls2ncit) TO '{P.DATA}/umls2ncit_procedures.tsv' (HEADER, DELIMITER '\t')")
con.close()
print("wrote umls2ncit_procedures.tsv")
