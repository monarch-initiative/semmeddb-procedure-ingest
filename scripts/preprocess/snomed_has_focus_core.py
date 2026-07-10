#!/usr/bin/env python3
"""
Extract `procedure treats|diagnoses disease/phenotype` candidate edges from raw
SNOMED CT RF2, mapped to Mondo / HPO via the Monarch SSSOM mapping commons.

Spine:     Has focus (363702006)  procedure -> clinical finding/disorder
Direction: Method (260686004) action bucket, with Has intent (363703001) and
           the Therapeutic/Diagnostic procedure hierarchy as tie-break/backstop.
Mapping:   mondo.sssom.tsv  (SNOMED disorder -> MONDO)
           hpo-snomed.sssom.tsv (SNOMED finding -> HP)

Output is a flat TSV carrying BOTH the original SNOMED ids/labels and the mapped
ids/labels, plus the direction evidence, so every edge is auditable.

Usage:
    python3 extract_procedure_disease.py [--out edges.tsv] [--all]

    --all   keep candidate edges even when the focus has no Mondo/HPO mapping
            (mapped_* columns left blank). Default keeps only mapped edges.
"""

import argparse
import csv
import os
import sys

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
import _paths as P
HERE = os.path.dirname(os.path.abspath(__file__))
REL = str(P.snomed_rel_file())
DESC = str(P.snomed_desc_file())
MONDO_SSSOM = str(P.MONDO_SSSOM)
HPO_SSSOM = str(P.HPO_SSSOM)

# ---------------------------------------------------------------------------
# SNOMED concept ids
# ---------------------------------------------------------------------------
FSN_TYPE = "900000000000003001"          # Fully specified name
IS_A = "116680003"
HAS_FOCUS = "363702006"
METHOD = "260686004"
HAS_INTENT = "363703001"

THERAPEUTIC_PROC = "277132007"           # Therapeutic procedure
DIAGNOSTIC_PROCS = {"103693007", "386053000"}  # Diagnostic procedure / Evaluation procedure

# Action (260686004 value) roots -> direction. Classification walks the Is-a
# closure of each Method value up through the Action subtree and stops at the
# first root it hits. Roots are the direct children of Action (129264002).
THERAPEUTIC_ACTIONS = {
    "129284003",  # Surgical action
    "129303008",  # Removal - action
    "129325002",  # Introduction - action
    "419988009",  # Action of drug administration
    "129408000",  # Manipulation - action
    "129411004",  # Traction - action
    "360166003",  # Filtration - action
    "360270004",  # Therapy - action
    "360323003",  # Restore - action
    "360051004",  # Surgical toilet - action
    "281613004",  # Decompression - action
    "129333001",  # Insufflation - action
    "439237009",  # Mechanical repair - action
    "440647007",  # Mechanical construction - action
    "830059000",  # Nonsurgical repair - action
    "286792001",  # Denervation - action
    "129443004",  # Electrolysis - action
    "428381003",  # Therapeutic diathermy - action
    "303893007",  # Functional modification
    "303894001",  # Structural modification
    "278313005",  # Regeneration - action
    "302196006",  # Freeing - action
    "360240009",  # Connection - action
    "360393008",  # Disinsertion - action
    "257893003",  # Pressure - action
    "129271007",  # Management - action
    "129424004",  # Fitting - action
    "129300006",  # Puncture - action
}
DIAGNOSTIC_ACTIONS = {
    "129265001",  # Evaluation - action  (incl. Measurement)
    "419656003",  # Examination AND/OR history taking - action (incl. Inspection)
    "360220002",  # Mapping - action
    "302193003",  # Actions by modality (imaging) -- mostly diagnostic
}
# Everything else (Education, Counseling, Discussion, Assisting, Administrative,
# Recommendation, Preparation, Obstetric, Attention, ...) -> undetermined.

INTENT_TREATS = {
    "262202000",  # Therapeutic intent
    "363676003",  # Palliative
    "373847000",  # Neoadjuvant
    "373846009",  # Adjuvant
}
INTENT_DIAGNOSES = {
    "261004008",  # Diagnostic intent
    "360156006",  # Screening - procedure intent
}


# ---------------------------------------------------------------------------
def log(msg):
    print(msg, file=sys.stderr)


def semantic_tag(fsn):
    """Return the parenthetical semantic tag of an FSN, e.g. 'disorder'."""
    if fsn and fsn.endswith(")"):
        i = fsn.rfind("(")
        if i != -1:
            return fsn[i + 1:-1]
    return ""


def load_descriptions():
    """conceptId -> FSN (active, English FSN only)."""
    fsn = {}
    with open(DESC, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            c = line.rstrip("\n").split("\t")
            # id eff active module conceptId lang typeId term caseSig
            if c[2] == "1" and c[6] == FSN_TYPE:
                fsn[c[4]] = c[7]
    log(f"  descriptions: {len(fsn):,} active FSNs")
    return fsn


def load_relationships():
    """Single streaming pass; collect the relationship sets we need."""
    parents = {}                 # child -> set(parents)   (Is-a graph, all concepts)
    has_focus = {}               # proc  -> set(focus)
    method = {}                  # proc  -> set(method value)
    intent = {}                  # proc  -> set(intent value)
    with open(REL, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            c = line.rstrip("\n").split("\t")
            # id eff active module source dest group type charType modifier
            if c[2] != "1":
                continue
            src, dest, typ = c[4], c[5], c[7]
            if typ == IS_A:
                parents.setdefault(src, set()).add(dest)
            elif typ == HAS_FOCUS:
                has_focus.setdefault(src, set()).add(dest)
            elif typ == METHOD:
                method.setdefault(src, set()).add(dest)
            elif typ == HAS_INTENT:
                intent.setdefault(src, set()).add(dest)
    log(f"  is-a edges: {sum(len(v) for v in parents.values()):,} | "
        f"has-focus procs: {len(has_focus):,}")
    return parents, has_focus, method, intent


def make_ancestor_checker(parents):
    """Memoized 'does concept c have any ancestor in target set (or is one)?'"""
    cache = {}

    def reaches(c, targets):
        key = (c, id(targets))
        if key in cache:
            return cache[key]
        # iterative DFS up the parent graph
        seen = set()
        stack = [c]
        found = False
        while stack:
            n = stack.pop()
            if n in targets:
                found = True
                break
            if n in seen:
                continue
            seen.add(n)
            stack.extend(parents.get(n, ()))
        cache[key] = found
        return found

    return reaches


def load_sssom_object_snomed(path, subject_prefix):
    """
    Map SNOMED object_id -> (subject_id, subject_label, predicate_id).
    Both mapping files carry SNOMED as object_id. Keep best predicate
    (exactMatch preferred). Returns dict keyed by bare SCTID string.
    """
    out = {}
    rank = {"skos:exactMatch": 0, "skos:narrowMatch": 1,
            "skos:broadMatch": 2, "skos:closeMatch": 3}
    with open(path, encoding="utf-8") as fh:
        reader = csv.reader((l for l in fh if not l.startswith("#")), delimiter="\t")
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        si, sl = col["subject_id"], col["subject_label"]
        pi, oi = col["predicate_id"], col["object_id"]
        for row in reader:
            if len(row) <= oi:
                continue
            subj, pred, obj = row[si], row[pi], row[oi]
            if not subj.startswith(subject_prefix) or not obj.startswith("SCTID:"):
                continue
            sctid = obj.split(":", 1)[1]
            label = row[sl] if len(row) > sl else ""
            prev = out.get(sctid)
            if prev is None or rank.get(pred, 9) < rank.get(prev[2], 9):
                out[sctid] = (subj, label, pred)
    log(f"  {os.path.basename(path)}: {len(out):,} SNOMED->{subject_prefix} mappings")
    return out


def classify_direction(proc, methods, intents, reaches, fsn):
    """
    Return (predicate, evidence, confidence).
    Precedence: explicit intent > method action bucket > procedure hierarchy
    > lexical hint. 'undetermined' when signals are absent or conflicting.
    """
    # 1. Has intent -- sparse but authoritative
    iv = intents.get(proc, set())
    if iv & INTENT_DIAGNOSES and not (iv & INTENT_TREATS):
        return "diagnoses", "intent", "high"
    if iv & INTENT_TREATS and not (iv & INTENT_DIAGNOSES):
        return "treats", "intent", "high"

    # 2. Method action bucket
    mv = methods.get(proc, set())
    is_tx = any(reaches(m, THERAPEUTIC_ACTIONS) for m in mv)
    is_dx = any(reaches(m, DIAGNOSTIC_ACTIONS) for m in mv)
    if is_tx and not is_dx:
        return "treats", "method", "medium"
    if is_dx and not is_tx:
        return "diagnoses", "method", "medium"

    # 3. Procedure hierarchy backstop
    h_tx = reaches(proc, {THERAPEUTIC_PROC})
    h_dx = reaches(proc, DIAGNOSTIC_PROCS)
    if h_tx and not h_dx:
        return "treats", "hierarchy", "low"
    if h_dx and not h_tx:
        return "diagnoses", "hierarchy", "low"

    # 4. Lexical last resort
    name = (fsn.get(proc, "") or "").lower()
    if any(w in name for w in ("screening", "biopsy", "diagnostic", "assessment",
                               "evaluation", "examination", "monitoring")):
        return "diagnoses", "lexical", "low"
    if any(w in name for w in ("excision", "repair of", "removal of", "treatment of",
                               "management of", "resection", "reconstruction")):
        return "treats", "lexical", "low"

    return "undetermined", "none", "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "procedure_disease_edges.tsv"))
    ap.add_argument("--all", action="store_true",
                    help="keep unmapped focus targets too (mapped_* blank)")
    args = ap.parse_args()

    log("Loading SSSOM mappings...")
    mondo = load_sssom_object_snomed(MONDO_SSSOM, "MONDO:")
    hpo = load_sssom_object_snomed(HPO_SSSOM, "HP:")

    log("Loading SNOMED descriptions...")
    fsn = load_descriptions()

    log("Loading SNOMED relationships...")
    parents, has_focus, method, intent = load_relationships()
    reaches = make_ancestor_checker(parents)

    log("Building edges...")
    rows = []
    counts = {"treats": 0, "diagnoses": 0, "undetermined": 0}
    for proc, focuses in has_focus.items():
        for focus in focuses:
            tag = semantic_tag(fsn.get(focus, ""))
            if tag not in ("disorder", "finding"):
                continue

            # map focus: disorder -> mondo first; finding -> hpo first
            mapped_id = mapped_label = mapped_pred = obj_cat = map_src = ""
            order = (mondo, hpo) if tag == "disorder" else (hpo, mondo)
            srcname = ("mondo.sssom", "hpo-snomed.sssom") if tag == "disorder" \
                else ("hpo-snomed.sssom", "mondo.sssom")
            for tbl, sname in zip(order, srcname):
                if focus in tbl:
                    mapped_id, mapped_label, mapped_pred = tbl[focus]
                    obj_cat = "Disease" if mapped_id.startswith("MONDO:") else "Phenotype"
                    map_src = sname
                    break

            if not mapped_id and not args.all:
                continue

            predicate, evidence, conf = classify_direction(
                proc, method, intent, reaches, fsn)
            counts[predicate] = counts.get(predicate, 0) + 1

            rows.append([
                f"SNOMED:{proc}", fsn.get(proc, ""),
                predicate, evidence, conf,
                f"SNOMED:{focus}", fsn.get(focus, ""), tag,
                mapped_id, mapped_label, mapped_pred, obj_cat, map_src,
            ])

    header = [
        "procedure_id", "procedure_label",
        "predicate", "direction_evidence", "direction_confidence",
        "focus_snomed_id", "focus_snomed_label", "focus_semantic_tag",
        "mapped_id", "mapped_label", "mapping_predicate",
        "object_category", "mapping_source",
    ]
    rows.sort(key=lambda r: (r[2], r[1].lower()))
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(header)
        w.writerows(rows)

    log("")
    log(f"Wrote {len(rows):,} edges -> {args.out}")
    log(f"  treats={counts.get('treats',0):,}  "
        f"diagnoses={counts.get('diagnoses',0):,}  "
        f"undetermined={counts.get('undetermined',0):,}")


if __name__ == "__main__":
    main()
