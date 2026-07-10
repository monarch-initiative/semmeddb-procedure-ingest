# semmeddb-procedure-ingest justfile

# Package directory
PKG := "src"

# Explicitly enumerate transforms (add new ingests here)
TRANSFORMS := "procedure_to_disease"

# List all commands
_default:
    @just --list

# Initialize a new project
[group('project management')]
setup: _git-init install _git-add
    git commit -m "Initialize semmeddb-procedure-ingest"

# Install dependencies
[group('project management')]
install:
    uv sync --group dev

# Download source data
[group('ingest')]
download: install
    uv run downloader download.yaml

# Fetch license-gated inputs (SNOMED CT RF2, UMLS MRCONSO) — needs UMLS_API_KEY
[group('ingest')]
fetch-licensed: install
    uv run python scripts/fetch_licensed.py

# Preprocess: run the graph/API/join stages and assemble data/procedure_to_disease.tsv
[group('ingest')]
preprocess: download fetch-licensed
    uv run python scripts/preprocess.py

# Run all transforms
[group('ingest')]
transform-all: preprocess
    #!/usr/bin/env bash
    set -euo pipefail
    for t in {{TRANSFORMS}}; do
        if [ -n "$t" ]; then
            echo "Transforming $t..."
            uv run koza transform {{PKG}}/$t.yaml -f jsonl
        fi
    done

# Emit output/release-metadata.yaml describing this build's upstream sources and artifacts
[group('ingest')]
metadata:
    uv run python scripts/write_metadata.py

# Run full pipeline: install, download, transform, metadata, test
[group('ingest')]
run: test transform-all metadata

# Run specific transform
[group('ingest')]
transform NAME:
    uv run koza transform {{PKG}}/{{NAME}}.yaml -f jsonl

# Run tests
[group('development')]
test: install
    uv run pytest

# Run tests with coverage
[group('development')]
test-cov: install
    uv run pytest --cov=. --cov-report=term-missing

# Lint code
[group('development')]
lint:
    uv run ruff check .

# Format code
[group('development')]
format:
    uv run ruff format .

# Clean output directory
[group('ingest')]
clean:
    rm -rf output/

# Hidden recipes
_git-init:
    git init

_git-add:
    git add .
