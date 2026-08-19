#!/usr/bin/env python3
"""Search the approved local RAG index and print cited evidence as JSON."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rag_core import DEFAULT_DATABASE, DEFAULT_ENDPOINT, OllamaClient, result_as_dict, search


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--mode", choices=("lexical", "dense", "hybrid"), default="lexical")
    parser.add_argument("--embedding-model", default="nomic-embed-text:latest")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()
    try:
        embedder = None if args.mode == "lexical" else OllamaClient(args.embedding_model, args.endpoint)
        results = search(args.database, args.query, args.limit, args.mode, embedder)
    except (RuntimeError, ValueError) as error:
        print(f"RAG SEARCH: blocked — {error}", file=sys.stderr)
        return 1
    print(json.dumps([result_as_dict(result) for result in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
