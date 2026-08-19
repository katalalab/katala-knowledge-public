#!/usr/bin/env python3
"""Build the approved local RAG index incrementally."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rag_core import DEFAULT_DATABASE, DEFAULT_MANIFEST, DEFAULT_ENDPOINT, OllamaClient, RagSafetyError, build_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--embedding-model", default="nomic-embed-text:latest")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()
    try:
        stats = build_index(OllamaClient(args.embedding_model, args.endpoint), args.database, args.manifest)
    except (RagSafetyError, RuntimeError, ValueError) as error:
        print(f"RAG INDEX: blocked — {error}", file=sys.stderr)
        return 1
    print(json.dumps({"database": str(args.database), "embedding_model": args.embedding_model, **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
