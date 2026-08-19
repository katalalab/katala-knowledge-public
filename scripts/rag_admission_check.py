#!/usr/bin/env python3
"""Validate the RAG corpus manifest without creating an index or calling Ollama."""
from __future__ import annotations

import json
import sys
from collections import Counter

from rag_core import RagSafetyError, admitted_files


def main() -> int:
    try:
        files = admitted_files()
    except RagSafetyError as error:
        print(f"RAG ADMISSION: blocked — {error}", file=sys.stderr)
        return 1
    sources = Counter(file.source_id for file in files)
    print(json.dumps({"admitted_files": len(files), "sources": dict(sorted(sources.items()))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
