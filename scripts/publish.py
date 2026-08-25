#!/usr/bin/env python3
"""
Publish this build's artifacts to the BDC bucket.

Layout: ONE top-level prefix per ingest repo, dated immutable builds, no
latest/ mirror — consumers resolve the newest dated prefix (or pin a date):

    gs://monarch-bdc-kg/semmeddb/<YYYY-MM-DD>/
        procedure_to_disease_ncit_{nodes,edges}.jsonl        open tier
        procedure_to_disease_snomedct_{nodes,edges}.jsonl    RESTRICTED tier
        release-metadata.yaml                                kozahub receipt
        README.md                                            licensing + QA caveats

Both license tiers ship in one dated dir, distinguished by filename; the
README records the per-tier licensing so an artifact found in the bucket is
self-describing. The dated dir is written with `rsync -d`, so a same-date
republish is a clean overwrite (files the previous run produced and this one
did not are removed rather than left to rot).

Usage:
    just publish                 # date from release-metadata.yaml generated_at
    just publish 2026-08-25      # explicit date
    DRY_RUN=1 just publish       # print the gsutil calls, change nothing
"""
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OUTPUT = REPO / "output"
BUCKET = os.environ.get("BDC_BUCKET", "gs://monarch-bdc-kg")
INGEST = "semmeddb"

# transform stem -> licensing note for the README
TIERS = {
    "procedure_to_disease_ncit": (
        "ncit",
        "**OPEN.** NCIT is public domain and objects are MONDO/HP, so this artifact\n"
        "contains no licensed description text and is publishable as a normal Monarch ingest.",
    ),
    "procedure_to_disease_snomedct": (
        "snomedct",
        "**RESTRICTED — DO NOT REDISTRIBUTE.** Procedure node labels are SNOMED CT\n"
        "description text, which is licensed. Private bucket and internal use only.\n"
        "Rebuild with `OUTPUT_TIER=open` to blank the labels for any public release.",
    ),
}

CAVEATS = """## QA caveats for consumers

- Sampling QA: `diagnoses` ~8% broad error, `treats` ~30-37%. **The predicate is the
  fault line, not the support count.** Most failures are vacuous or over-generalized
  rather than false.
- `publication_count` OVERSTATES evidence. Roughly a third of individual PMIDs do not
  support their edge even when the edge is correct — independently measured here and by
  Translator's own LLM check over 26.7M pairs (35.4% unsupported). Do not surface these
  PMIDs as citations unchecked, and do not treat the count as a confidence score.
- No publication-count threshold is applied. Translator's comparable filter for SemMedDB
  is >3 PMIDs (>=4), with ARAX using >=10 for `treats` specifically.
"""


def run(cmd: list[str], dry: bool) -> None:
    print("  " + " ".join(cmd))
    if not dry:
        subprocess.run(cmd, check=True)


def main() -> None:
    dry = os.environ.get("DRY_RUN") == "1"
    meta_path = OUTPUT / "release-metadata.yaml"
    if not meta_path.exists():
        sys.exit("output/release-metadata.yaml missing — run `just metadata` first.")
    meta = yaml.safe_load(meta_path.read_text())

    date = sys.argv[1] if len(sys.argv) > 1 else meta["generated_at"][:10]
    build = meta.get("build_version", "unknown")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()

    print(f"publishing build={build} commit={commit} date={date}"
          f"{'  [DRY RUN]' if dry else ''}")

    stage = OUTPUT / ".publish" / INGEST
    stage.mkdir(parents=True, exist_ok=True)
    (stage / meta_path.name).write_bytes(meta_path.read_bytes())

    tier_sections = []
    for stem, (tier, licensing) in TIERS.items():
        edges = OUTPUT / f"{stem}_edges.jsonl"
        nodes = OUTPUT / f"{stem}_nodes.jsonl"
        missing = [p.name for p in (edges, nodes) if not p.exists()]
        if missing:
            sys.exit(f"{', '.join(missing)} missing — run `just transform-all` first.")

        n_edges = sum(1 for _ in edges.open())
        n_nodes = sum(1 for _ in nodes.open())
        for src in (edges, nodes):
            (stage / src.name).write_bytes(src.read_bytes())
        tier_sections.append(
            f"### {tier}\n\n"
            f"- files: `{edges.name}`, `{nodes.name}`\n"
            f"- edges: {n_edges:,}   nodes: {n_nodes:,}\n\n"
            f"{licensing}"
        )
        print(f"{tier}: {n_edges:,} edges / {n_nodes:,} nodes")

    (stage / "README.md").write_text(
        f"# {INGEST}\n\n"
        f"Procedure→Disease/Phenotype edges from the LLM-verified SemMedDB subset,\n"
        f"in two license tiers distinguished by filename.\n\n"
        f"- build_version: `{build}`\n"
        f"- source commit: `{commit}`\n"
        f"- published: {date}\n"
        f"- predicates: `biolink:diagnoses`, `biolink:treats_or_applied_or_studied_to_treat`\n\n"
        f"## Licensing, per tier\n\n"
        + "\n\n".join(tier_sections)
        + f"\n\n{CAVEATS}"
    )

    dated = f"{BUCKET}/{INGEST}/{date}/"
    run(["gsutil", "-m", "rsync", "-d", str(stage), dated], dry)

    print(f"\ndone: {dated}\nconsumers resolve the newest dated prefix (or pin); dated prefixes are the audit trail.")


if __name__ == "__main__":
    main()
