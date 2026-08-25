#!/usr/bin/env python3
"""
Stage: rescue disease/phenotype objects that never reached MONDO/HP, by EXACT
label match against the ontologies themselves.

Runs after rescue_umls_objects (which owns final_object_id) and before
ncit_map_and_ic (so rescued ids get structural IC stamped).

Why local and not Monarch's search API: this is grounding, not search. Grounding
wants "exact name or exact synonym, else abstain"; a ranked search returns a
best-effort hit, which on real input grounds `Septic shock` onto
`septic shock, non-human animal` and `Drug Resistant Epilepsy` onto
`pyridoxine-dependent epilepsy`. Building the index from mondo.obo + hp.obo — which
this ingest already downloads and pins by `data-version` — gives exact matching with
no network in the build path and no moving-target snapshot to maintain.
(See monarch-app#1391 / #1392 for the API-side asks.)

Precision rules, all of which matter:
  - MONDO:/HP: ids only. mondo.obo imports PATO/GO/UBERON/NCBITaxon, and indexing
    every [Term] grounds "Present" onto PATO:0000467 and "Inflammatory Response"
    onto GO:0006954.
  - EXACT synonyms only. A NARROW synonym match means the query is BROADER than the
    term — that is how "Drug Resistant Epilepsy" becomes pyridoxine-dependent epilepsy.
  - Obsolete terms and `venom_*` (VeNom veterinary) terms excluded.
  - A label matching more than one term is dropped, not arbitrated.

Expected yield is small and that is the finding, not a bug: only ~4% of the residual
objects have an exact MONDO/HP label. NodeNorm already returns MONDO whenever one
exists, so this residue is where Mondo genuinely has no term.
"""
import re
from collections import defaultdict

import duckdb

import _paths as P

# Generic findings that DO have a MONDO/HP term but say nothing as an edge object.
# They pass structural IC (MONDO:0021178 "injury" scores 11.08) because IC measures
# depth, not informativeness — the same trap that made best_ic useless on the
# procedure side. Rescuing them would re-introduce exactly what the shipping filter
# removes, so refuse them here.
GENERIC_OBJECT_LABELS = {
    "injury", "infection", "inflammation", "lesion", "present", "thickened",
    "atrophic", "obstruction", "hemorrhage", "symptom", "sign", "complication",
    "disease progression", "adverse effects", "recovery of function", "abnormality",
}

_PAREN = re.compile(
    r"\s*\((disorder|finding|morphologic abnormality|procedure|qualifier value)\)\s*$"
)
_NONALNUM = re.compile(r"[^a-z0-9]+")


def normalize(s: str | None) -> str:
    """Conservative label normalisation: casefold, drop a SNOMED-style semantic tag,
    collapse punctuation. Deliberately NOT stemming or word-dropping — this is an
    exact-match index and loosening it trades precision for recall."""
    if not s:
        return ""
    s = _PAREN.sub("", s.lower().strip())
    return _NONALNUM.sub(" ", s).strip()


def parse_terms(path):
    """yield (id, name, exact_synonyms, subsets, obsolete) for [Term] stanzas."""
    cur = name = None
    syn, subs, obs, in_term = [], set(), False, False
    for line in path.open(encoding="utf-8"):
        line = line.rstrip("\n")
        if line.startswith("["):
            if in_term and cur:
                yield cur, name, syn, subs, obs
            in_term = line == "[Term]"
            cur = name = None
            syn, subs, obs = [], set(), False
        elif not in_term:
            continue
        elif line.startswith("id: "):
            cur = line[4:].strip()
        elif line.startswith("name: "):
            name = line[6:].strip()
        elif line.startswith("subset: "):
            subs.add(line[8:].split()[0])
        elif line.startswith("is_obsolete: true"):
            obs = True
        elif line.startswith("synonym: "):
            m = re.match(r'synonym: "(.*?)" (EXACT|NARROW|BROAD|RELATED)', line)
            if m and m.group(2) == "EXACT":
                syn.append(m.group(1))
    if in_term and cur:
        yield cur, name, syn, subs, obs


def build_index() -> dict[str, str]:
    idx = defaultdict(set)
    kept = obsolete = vet = 0
    for path in (P.MONDO_OBO, P.HP_OBO):
        for cid, name, syn, subs, obs in parse_terms(path):
            if not (cid.startswith("MONDO:") or cid.startswith("HP:")):
                continue
            if obs:
                obsolete += 1
                continue
            if any(s.startswith("venom_") for s in subs):
                vet += 1
                continue
            kept += 1
            for label in ([name] if name else []) + syn:
                n = normalize(label)
                if n and n not in GENERIC_OBJECT_LABELS:
                    idx[n].add(cid)
    ambiguous = sum(1 for v in idx.values() if len(v) > 1)
    print(f"[ground_objects] indexed {kept:,} MONDO/HP terms "
          f"(excluded {obsolete:,} obsolete, {vet:,} veterinary)")
    print(f"[ground_objects] {len(idx):,} labels, {ambiguous:,} ambiguous (dropped)")
    return {k: next(iter(v)) for k, v in idx.items() if len(v) == 1}


def main():
    idx = build_index()
    con = duckdb.connect(str(P.DB))

    con.execute("CREATE OR REPLACE TEMP TABLE ground(label VARCHAR, id VARCHAR)")
    con.executemany("INSERT INTO ground VALUES (?,?)", list(idx.items()))

    rows = con.execute("""
      -- max(), not any_value(): the label chosen here decides whether a term grounds,
      -- and any_value() is not stable across builds. ORDER BY for a stable scan too.
      SELECT final_object_id, max(final_object_label), max(object_label), count(*)
      FROM procedure_disease_clean
      WHERE final_object_id NOT LIKE 'MONDO:%' AND final_object_id NOT LIKE 'HP:%'
      GROUP BY 1 ORDER BY 1
    """).fetchall()

    mapping = []
    for oid, final_label, nn_label, _edges in rows:
        for candidate in (final_label, nn_label):
            hit = idx.get(normalize(candidate))
            if hit:
                mapping.append((oid, hit))
                break

    con.execute("CREATE OR REPLACE TEMP TABLE grounded(old_id VARCHAR, new_id VARCHAR)")
    if mapping:
        con.executemany("INSERT INTO grounded VALUES (?,?)", mapping)

    try:
        con.execute("ALTER TABLE procedure_disease_clean ADD COLUMN object_grounded VARCHAR")
    except duckdb.Error:
        pass

    moved = con.execute("""
      SELECT count(*) FROM procedure_disease_clean p
      JOIN grounded g ON g.old_id = p.final_object_id
    """).fetchone()[0]
    con.execute("""
      UPDATE procedure_disease_clean p SET
        final_object_id = g.new_id,
        final_object_label = NULL,        -- assemble re-labels from ontology_labels.tsv
        object_grounded = 'true'
      FROM grounded g WHERE g.old_id = p.final_object_id
    """)
    con.execute("UPDATE procedure_disease_clean SET object_grounded = coalesce(object_grounded,'false')")

    print(f"[ground_objects] grounded {len(mapping):,} residual objects "
          f"-> {moved:,} edges moved into MONDO/HP")
    con.close()


if __name__ == "__main__":
    main()
