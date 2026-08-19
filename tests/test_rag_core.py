from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.rag_core import RagSafetyError, admitted_files, build_index, chunk_markdown, search


class FakeEmbedder:
    model = "fake-embedding"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), float(text.count("クジラ")), float(text.count("展開"))] for text in texts]


class RagCoreTests(unittest.TestCase):
    def write_manifest(self, root: Path, sources: list[dict]) -> Path:
        manifest = root / "corpus.json"
        manifest.write_text(json.dumps({"schema": "katala.rag-corpus.v1", "sources": sources}), encoding="utf-8")
        return manifest

    def approved_source(self, root: str = "docs") -> dict:
        return {
            "id": "test-docs",
            "root": root,
            "include": ["**/*.md"],
            "exclude": [],
            "visibility": "private",
            "owner": "test",
            "review_status": "approved",
        }

    def test_chunks_keep_heading(self) -> None:
        chunks = chunk_markdown("# 設計\n\n青いクジラの展開手順です。")
        self.assertEqual(chunks[0][0], "設計")
        self.assertIn("青いクジラ", chunks[0][1])

    def test_chunking_excludes_fenced_code_and_headings_inside_it(self) -> None:
        chunks = chunk_markdown("# 手順\n\n本文です。\n\n```powershell\n# 偽の見出し\npython tool.py\n```")
        self.assertEqual(len(chunks), 1)
        self.assertNotIn("tool.py", chunks[0][1])

    def test_manifest_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.write_manifest(root, [self.approved_source("../outside")])
            with self.assertRaises(RagSafetyError):
                admitted_files(manifest, root)

    def test_manifest_blocks_forbidden_raw_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "raw").mkdir()
            (root / "raw" / "note.md").write_text("安全に見えるが投入禁止", encoding="utf-8")
            manifest = self.write_manifest(root, [self.approved_source("raw")])
            with self.assertRaises(RagSafetyError):
                admitted_files(manifest, root)

    def test_manifest_blocks_secret_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "note.md").write_text("key: " + "sk-" + "a" * 20, encoding="utf-8")
            manifest = self.write_manifest(root, [self.approved_source()])
            with self.assertRaises(RagSafetyError):
                admitted_files(manifest, root)

    def test_incremental_index_and_lexical_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "guide.md").write_text("# 展開\n\n青いクジラの展開手順を確認する。", encoding="utf-8")
            manifest = self.write_manifest(root, [self.approved_source()])
            database = root / "outputs" / "rag.sqlite"
            first = build_index(FakeEmbedder(), database, manifest, root)
            second = build_index(FakeEmbedder(), database, manifest, root)
            results = search(database, "青いクジラの展開手順を教えて", mode="lexical")
            self.assertEqual(first["documents_indexed"], 1)
            self.assertEqual(second["documents_skipped"], 1)
            self.assertEqual(results[0].path, "docs/guide.md")

    def test_hybrid_search_uses_same_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "guide.md").write_text("# 展開\n\n青いクジラの展開手順を確認する。", encoding="utf-8")
            manifest = self.write_manifest(root, [self.approved_source()])
            database = root / "outputs" / "rag.sqlite"
            embedder = FakeEmbedder()
            build_index(embedder, database, manifest, root)
            results = search(database, "青いクジラの展開手順", mode="hybrid", embedder=embedder)
            self.assertTrue(results[0].citation.startswith("[test-docs:docs/guide.md#"))


if __name__ == "__main__":
    unittest.main()
