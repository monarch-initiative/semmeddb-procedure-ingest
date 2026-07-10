#!/usr/bin/env python3
"""
Build a `nodes` table for the SemMedDB edges by normalizing every distinct
subject/object CURIE through the Translator NodeNorm API, then stamp
`subject_category` / `object_category` onto the edges table.

Why: raw SemMedDB rows carry CURIEs (lots of bare UMLS:) with no category, so
you can't tell "UMLS diagnoses UBERON" (anatomy = garbage) from
"UMLS:procedure diagnoses MONDO:disease" (what we want). Categories make the
garbage filterable.

Safe to re-run: the `nodes` table is a cache; only un-normalized CURIEs are
fetched, so a network hiccup just means running again.

Requires exclusive access to the duckdb file -- close any open `duckdb` CLI
session on it first.

Usage:
    python3 build_node_categories.py                 # full run + summary
    python3 build_node_categories.py --summary-only   # just reprint the pivot
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from pathlib import Path

import duckdb
import _paths as P

# Translator NodeNorm endpoints, tried in order (ITRB prod first; RENCI as
# fallback -- RENCI intermittently 500s).
NODENORM_ENDPOINTS = [
    "https://nodenorm.transltr.io/get_normalized_nodes",
    "https://nodenormalization-sri.renci.org/get_normalized_nodes",
]
DEFAULT_DB = str(P.DB)
BATCH = 1000

# candidate id-column names in the edges table (first match wins)
SUBJ_CANDIDATES = ["subject_curie", "subject", "subject_id"]
OBJ_CANDIDATES = ["object_curie", "object", "object_id"]


def pick_col(cols, candidates, what):
    for c in candidates:
        if c in cols:
            return c
    sys.exit(f"ERROR: could not find a {what} id column among {candidates}. "
             f"edges columns are: {cols}")


def _post(curies, conflate, retries=4):
    """One POST to NodeNorm with retries and endpoint failover; raises on
    persistent failure across all endpoints."""
    body = json.dumps({
        "curies": curies,
        "conflate": conflate,
        "description": False,
    }).encode()
    last_err = None
    for attempt in range(retries):
        for url in NODENORM_ENDPOINTS:
            try:
                req = urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.load(resp)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err)


def fetch_batch(curies, conflate):
    """
    Resilient fetch: if a batch persistently fails (NodeNorm 500s on a single
    bad CURIE), bisect to isolate it and mark the offender(s) as unmapped
    rather than aborting the whole run.
    """
    try:
        return _post(curies, conflate)
    except RuntimeError as e:
        if len(curies) == 1:
            print(f"  ! skipping un-normalizable CURIE {curies[0]}: {e}",
                  file=sys.stderr)
            return {curies[0]: None}
        mid = len(curies) // 2
        out = {}
        out.update(fetch_batch(curies[:mid], conflate))
        out.update(fetch_batch(curies[mid:], conflate))
        return out


def parse_node(curie, node):
    """-> (id, normalized_id, category, label, all_categories_json)."""
    if not node:
        return (curie, None, None, None, None)
    nid = node.get("id", {}) or {}
    types = node.get("type", []) or []
    return (
        curie,
        nid.get("identifier"),
        types[0] if types else None,           # most specific biolink category
        nid.get("label"),
        json.dumps(types) if types else None,   # full category list, for filtering
    )


def ensure_columns(con, table, *cols):
    have = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
    for col in cols:
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} VARCHAR")


def print_pivot(con, table, subj_col, obj_col):
    print("\n=== diagnoses / treats: object_category breakdown (the signal) ===")
    for pred in ("biolink:diagnoses",
                 "biolink:treats_or_applied_or_studied_to_treat"):
        print(f"\n# {pred}")
        rows = con.execute(f"""
            SELECT object_category, count(*) c
            FROM {table} WHERE predicate = ?
            GROUP BY 1 ORDER BY c DESC LIMIT 12
        """, [pred]).fetchall()
        for cat, c in rows:
            print(f"  {c:>8}  {cat}")
    print("\n=== diagnoses: subject_category x object_category (top pairs) ===")
    rows = con.execute(f"""
        SELECT subject_category, object_category, count(*) c
        FROM {table} WHERE predicate = 'biolink:diagnoses'
        GROUP BY 1,2 ORDER BY c DESC LIMIT 20
    """).fetchall()
    for s, o, c in rows:
        print(f"  {c:>7}  {s}  ->  {o}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--table", default="edges")
    ap.add_argument("--no-conflate", action="store_true",
                    help="disable gene/protein & drug/chemical conflation")
    ap.add_argument("--summary-only", action="store_true",
                    help="skip fetching; just (re)stamp categories and print pivot")
    ap.add_argument("--snapshot", default=str(P.NODES_SNAPSHOT),
                    help="versioned NodeNorm snapshot (loaded instead of the API if present)")
    ap.add_argument("--refresh", action="store_true",
                    help="force re-fetch from the NodeNorm API and rewrite the snapshot")
    args = ap.parse_args()
    conflate = not args.no_conflate

    con = duckdb.connect(args.db)
    cols = [r[0] for r in con.execute(f"DESCRIBE {args.table}").fetchall()]
    subj_col = pick_col(cols, SUBJ_CANDIDATES, "subject")
    obj_col = pick_col(cols, OBJ_CANDIDATES, "object")
    print(f"edges id columns: subject={subj_col}  object={obj_col}")

    con.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            id             VARCHAR PRIMARY KEY,
            normalized_id  VARCHAR,
            category       VARCHAR,
            label          VARCHAR,
            all_categories VARCHAR
        )
    """)

    snapshot = Path(args.snapshot)
    loaded_snapshot = False
    if snapshot.exists() and not args.refresh:
        print(f"loading NodeNorm snapshot (skipping API): {snapshot}")
        con.execute(f"INSERT OR REPLACE INTO nodes SELECT * FROM read_parquet('{snapshot}')")
        loaded_snapshot = True

    if not args.summary_only and not loaded_snapshot:
        # distinct endpoints not yet cached
        todo = [r[0] for r in con.execute(f"""
            SELECT DISTINCT c FROM (
                SELECT {subj_col} c FROM {args.table}
                UNION
                SELECT {obj_col} c FROM {args.table}
            ) WHERE c IS NOT NULL
              AND c NOT IN (SELECT id FROM nodes)
        """).fetchall()]
        total = len(todo)
        print(f"CURIEs to normalize: {total:,}  (batch={BATCH})")

        done = 0
        for i in range(0, total, BATCH):
            batch = todo[i:i + BATCH]
            resp = fetch_batch(batch, conflate)
            rows = [parse_node(c, resp.get(c)) for c in batch]
            con.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?)", rows)
            done += len(batch)
            mapped = sum(1 for r in rows if r[2] is not None)
            print(f"  {done:>6}/{total}  (+{mapped}/{len(batch)} mapped)",
                  file=sys.stderr)
        print(f"nodes cached: "
              f"{con.execute('SELECT count(*) FROM nodes').fetchone()[0]:,}")
        # write the versioned snapshot so future builds skip the (moving-target) API
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT * FROM nodes) TO '{snapshot}' (FORMAT parquet)")
        print(f"wrote NodeNorm snapshot: {snapshot}")

    # stamp categories onto edges
    ensure_columns(con, args.table, "subject_category", "object_category")
    con.execute(f"""
        UPDATE {args.table} e SET subject_category = n.category
        FROM nodes n WHERE n.id = e.{subj_col}
    """)
    con.execute(f"""
        UPDATE {args.table} e SET object_category = n.category
        FROM nodes n WHERE n.id = e.{obj_col}
    """)
    unmapped = con.execute(f"""
        SELECT count(*) FROM {args.table}
        WHERE subject_category IS NULL OR object_category IS NULL
    """).fetchone()[0]
    print(f"edges with an unresolved endpoint category: {unmapped:,}")

    print_pivot(con, args.table, subj_col, obj_col)
    con.close()


if __name__ == "__main__":
    main()
