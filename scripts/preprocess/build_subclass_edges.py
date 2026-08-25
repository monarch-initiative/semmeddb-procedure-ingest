#!/usr/bin/env python3
"""
Stage: build `mondo_edges.tsv` (the MONDO+HP subclass graph) from the canonical
OBO releases.

`ncit_map_and_ic.py` needs a KGX-ish edge file to compute structural IC for
disease/phenotype objects. Previously that file came from a Monarch KG dump —
a 330 MB download whose subclass edges are a merged, filtered view. The IC we
want is over the ontology's own class hierarchy, so read mondo.obo + hp.obo
directly: canonical, public, self-versioning (`data-version:` header), and far
smaller.

Obsolete terms are excluded — they are not real classes and would inflate the
descendant counts that IC is computed from.

Emits: subject <TAB> predicate <TAB> object, predicate always
`biolink:subclass_of` (what the consuming query filters on).
"""
import re
import sys
from pathlib import Path

import _paths as P

_QUALIFIER = re.compile(r"\{[^}]*\}")


def _parse_ref(value: str) -> str | None:
    """Strip an OBO tag value down to the bare ID, or None if it isn't one.

    Mondo is heavy on trailing qualifier blocks, so a real line looks like:
        is_a: MONDO:0004992 {source="DOID:1725", source="MONDO:Redundant"} ! cancer
    Both the `{...}` qualifier and the `! comment` must come off — leaving the
    qualifier attached mints a distinct fake parent per source combination and
    silently shatters the hierarchy (35% of edges, when we got this wrong).

    Order matters: strip the qualifier FIRST. A `!` inside a qualifier value would
    otherwise truncate the line mid-brace, and the leftover fragment would sail
    through as a parent id. No such line exists in the current releases, but the
    failure is silent, so don't depend on that holding.

    Returns None for anything that still doesn't look like a bare ID, so a future
    OBO form we don't handle drops the edge loudly-ish rather than corrupting the
    hierarchy with a junk node.
    """
    value = _QUALIFIER.sub("", value)
    value = value.split("!", 1)[0].strip()
    if not value or any(c in value for c in "{}! \t"):
        return None
    return value


def parse_obo(path: Path):
    """Yield (child, parent) is_a pairs from an OBO file, skipping obsoletes."""
    term_id = None
    parents: list[str] = []
    obsolete = False
    in_term = False
    skipped: list[str] = []

    def flush():
        if in_term and term_id and not obsolete:
            for p in parents:
                yield term_id, p

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("["):
                yield from flush()
                in_term = line == "[Term]"
                term_id, parents, obsolete = None, [], False
            elif not in_term:
                continue
            elif line.startswith("id: "):
                term_id = line[4:].strip()
            elif line.startswith("is_a: "):
                ref = _parse_ref(line[6:])
                if ref is None:
                    skipped.append(line)
                else:
                    parents.append(ref)
            elif line.lower().startswith("is_obsolete: true"):
                obsolete = True
    yield from flush()

    if skipped:
        print(f"[build_subclass_edges] WARNING: {path.name}: {len(skipped)} is_a line(s) "
              f"were not parseable as a bare ID and were DROPPED — first: {skipped[0]!r}",
              file=sys.stderr)


def parse_obsolete(path: Path):
    """Yield (obsolete_id, replaced_by_id) for obsoleted terms that name a successor.

    build_subclass_edges drops obsoletes from the HIERARCHY, but the edge path knows
    nothing about them, so deprecated MONDO/HP ids were shipping as `disease_id` with
    no IC (the missing IC was the only tell). rescue_umls_objects applies this map.
    """
    term_id = None
    replaced = None
    obsolete = False
    in_term = False

    def flush():
        if in_term and term_id and obsolete and replaced:
            yield term_id, replaced

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("["):
                yield from flush()
                in_term = line == "[Term]"
                term_id, replaced, obsolete = None, None, False
            elif not in_term:
                continue
            elif line.startswith("id: "):
                term_id = line[4:].strip()
            elif line.startswith("replaced_by: "):
                replaced = _parse_ref(line[13:])
            elif line.lower().startswith("is_obsolete: true"):
                obsolete = True
    yield from flush()


def parse_names(path: Path) -> dict[str, str]:
    """id -> name for every non-obsolete [Term] (used to label obsolete successors)."""
    names: dict[str, str] = {}
    term_id = name = None
    in_term = False
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("["):
                if in_term and term_id and name:
                    names[term_id] = name
                in_term = line == "[Term]"
                term_id = name = None
            elif not in_term:
                continue
            elif line.startswith("id: "):
                term_id = line[4:].strip()
            elif line.startswith("name: "):
                name = line[6:].strip()
    if in_term and term_id and name:
        names[term_id] = name
    return names


def data_version(path: Path) -> str:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("data-version:"):
                return line.split(":", 1)[1].strip()
            if line.startswith("["):
                break
    return "unknown"


def main():
    out = P.MONDO_EDGES
    sources = (P.MONDO_OBO, P.HP_OBO)
    missing = [o for o in sources if not o.exists()]
    if missing:
        raise FileNotFoundError(
            f"{', '.join(str(m) for m in missing)} missing — run `just download` first."
        )

    n = 0
    with out.open("w", encoding="utf-8") as fh:
        fh.write("subject\tpredicate\tobject\n")
        for obo in sources:
            before = n
            for child, parent in parse_obo(obo):
                fh.write(f"{child}\tbiolink:subclass_of\t{parent}\n")
                n += 1
            print(f"[build_subclass_edges] {obo.name} (data-version: {data_version(obo)}): "
                  f"{n - before:,} subclass edges")
    print(f"[build_subclass_edges] wrote {out} ({n:,} edges)")

    names: dict[str, str] = {}
    for src in sources:
        names.update(parse_names(src))

    # Authoritative id -> label for the open vocabularies we emit. NodeNorm returns
    # UMLS-preferred strings, which are often a different term entirely, so node
    # names must come from the ontology itself.
    lab = P.ONTOLOGY_LABELS
    with lab.open("w", encoding="utf-8") as fh:
        fh.write("id\tlabel\n")
        for k, v in sorted(names.items()):
            fh.write(f"{k}\t{v}\n")
        n_ncit = 0
        with open(P.ncit_flat(), encoding="utf-8") as nf:
            for line in nf:
                c = line.rstrip("\n").split("\t")
                if len(c) > 3 and c[0] and c[3]:
                    fh.write(f"NCIT:{c[0]}\t{c[3].split('|')[0]}\n")
                    n_ncit += 1
    print(f"[build_subclass_edges] wrote {lab} ({len(names):,} OBO + {n_ncit:,} NCIT labels)")

    obs = P.OBSOLETE_MAP
    m = 0
    with obs.open("w", encoding="utf-8") as fh:
        fh.write("obsolete_id\treplaced_by\treplaced_by_label\n")
        for old_id, new_id in (pair for src in sources for pair in parse_obsolete(src)):
            fh.write(f"{old_id}\t{new_id}\t{names.get(new_id, '')}\n")
            m += 1
    print(f"[build_subclass_edges] wrote {obs} ({m:,} obsolete->replaced_by)")


if __name__ == "__main__":
    main()
