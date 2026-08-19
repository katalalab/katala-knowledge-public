#!/usr/bin/env python3
"""Local-first RAG primitives for a Markdown knowledge repository.

The canonical content remains in Markdown. This module writes only a derived SQLite
index and refuses sources outside the approved manifest and repository boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

try:
    from scripts.secret_scan import scan as scan_secrets
except ModuleNotFoundError:  # Direct execution from scripts/.
    from secret_scan import scan as scan_secrets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "rag" / "corpus.json"
DEFAULT_DATABASE = PROJECT_ROOT / "outputs" / "rag" / "katala.sqlite"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
SAFE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".ps1", ".sh", ".toml"}
FORBIDDEN_PATH_PARTS = {".git", ".codex", ".claude", ".gemini", "__pycache__", "raw", "audit", "log", "outputs", "node_modules"}
FORBIDDEN_FILE_TOKENS = {".env", "credential", "secret", "token", "cookie", "session", "private_key", "id_rsa"}
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
QUERY_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*|[\u3040-\u30ff\u3400-\u9fff]+")
CHUNKER_VERSION = "2026-08-05.2"


class RagSafetyError(ValueError):
    """Raised when a source cannot be admitted safely into the local index."""


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class AdmittedFile:
    source_id: str
    path: str
    content: str
    content_hash: str


@dataclass(frozen=True)
class SearchResult:
    source_id: str
    path: str
    heading: str
    ordinal: int
    content: str
    score: float
    mode: str

    @property
    def citation(self) -> str:
        return f"[{self.source_id}:{self.path}#{self.ordinal}]"


class OllamaClient:
    """Minimal local-only client for Ollama's documented embed and chat endpoints."""

    def __init__(self, model: str, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 120) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def _post(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.endpoint}{route}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(
                f"Ollama local endpoint is unavailable ({self.endpoint}{route}): {error}. "
                "Start the existing local Ollama service and confirm the requested model is installed."
            ) from error

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._post("/api/embed", {"model": self.model, "input": texts})
        embeddings = response.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise RuntimeError("Ollama /api/embed returned an invalid embedding count.")
        return [[float(value) for value in vector] for vector in embeddings]

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 384, context_tokens: int = 8192) -> str:
        if max_tokens < 1 or context_tokens < 1024:
            raise ValueError("Chat token limits must be positive and use at least 1024 context tokens.")
        response = self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {"num_predict": max_tokens, "num_ctx": context_tokens, "temperature": 0},
            },
        )
        content = response.get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("Ollama /api/chat returned no message content.")
        return content.strip()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_forbidden_path_part(path: Path) -> bool:
    return any(part.lower() in FORBIDDEN_PATH_PARTS for part in path.parts)


def _has_forbidden_filename(path: Path) -> bool:
    name = path.name.lower()
    return any(token in name for token in FORBIDDEN_FILE_TOKENS)


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RagSafetyError(f"Cannot read corpus manifest {manifest_path}: {error}") from error
    if manifest.get("schema") != "katala.rag-corpus.v1":
        raise RagSafetyError("Unsupported corpus manifest schema.")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RagSafetyError("Corpus manifest needs at least one approved source.")
    return manifest


def _validate_source(source: dict[str, Any], project_root: Path) -> Path:
    required = {"id", "root", "include", "visibility", "owner", "review_status"}
    missing = required - set(source)
    if missing:
        raise RagSafetyError(f"Source is missing required fields: {', '.join(sorted(missing))}")
    if source["visibility"] != "private" or source["review_status"] != "approved":
        raise RagSafetyError(f"Source {source['id']} is not approved private content.")
    source_root_value = Path(source["root"])
    if source_root_value.is_absolute() or ".." in source_root_value.parts:
        raise RagSafetyError(f"Source {source['id']} escapes the repository boundary.")
    source_root = (project_root / source_root_value).resolve()
    if not _is_under(source_root, project_root) or not source_root.is_dir():
        raise RagSafetyError(f"Source {source['id']} root is not a repository directory: {source['root']}")
    if _has_forbidden_path_part(source_root.relative_to(project_root)):
        raise RagSafetyError(f"Source {source['id']} uses a forbidden root: {source['root']}")
    includes = source["include"]
    if not isinstance(includes, list) or not includes or not all(isinstance(value, str) for value in includes):
        raise RagSafetyError(f"Source {source['id']} needs a non-empty string include list.")
    return source_root


def admitted_files(manifest_path: Path = DEFAULT_MANIFEST, project_root: Path = PROJECT_ROOT) -> list[AdmittedFile]:
    """Return approved text files, failing before any unsafe content is embedded."""
    project_root = project_root.resolve()
    manifest = _read_manifest(manifest_path)
    admitted: list[AdmittedFile] = []
    seen_paths: set[Path] = set()

    for source in manifest["sources"]:
        if not isinstance(source, dict):
            raise RagSafetyError("Corpus manifest sources must be objects.")
        source_root = _validate_source(source, project_root)
        excludes = source.get("exclude", [])
        if not isinstance(excludes, list) or not all(isinstance(value, str) for value in excludes):
            raise RagSafetyError(f"Source {source['id']} has an invalid exclude list.")
        candidates: set[Path] = set()
        for pattern in source["include"]:
            candidates.update(candidate for candidate in source_root.glob(pattern) if candidate.is_file())

        for candidate in sorted(candidates):
            relative_to_source = candidate.relative_to(source_root)
            if any(relative_to_source.match(pattern) for pattern in excludes):
                continue
            resolved = candidate.resolve()
            if not _is_under(resolved, project_root):
                raise RagSafetyError(f"Admitted path escapes the repository boundary: {candidate}")
            relative_to_project = resolved.relative_to(project_root)
            if _has_forbidden_path_part(relative_to_project) or _has_forbidden_filename(resolved):
                raise RagSafetyError(f"Forbidden RAG source path: {relative_to_project}")
            if resolved.suffix.lower() not in SAFE_SUFFIXES:
                raise RagSafetyError(f"Unsupported RAG source type: {relative_to_project}")
            if resolved in seen_paths:
                raise RagSafetyError(f"RAG source admitted more than once: {relative_to_project}")
            findings = scan_secrets(resolved)
            if findings:
                names = ", ".join(sorted({finding[2] for finding in findings}))
                raise RagSafetyError(f"Secret scanner blocked {relative_to_project}: {names}")
            try:
                content = resolved.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as error:
                raise RagSafetyError(f"Unreadable RAG source {relative_to_project}: {error}") from error
            seen_paths.add(resolved)
            admitted.append(
                AdmittedFile(
                    source_id=str(source["id"]),
                    path=relative_to_project.as_posix(),
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
    if not admitted:
        raise RagSafetyError("Corpus manifest admitted no files.")
    return admitted


def _split_text(text: str, limit: int, overlap: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + limit, len(normalized))
        if end < len(normalized):
            preferred = max(normalized.rfind("\n\n", start, end), normalized.rfind("\n", start, end))
            if preferred > start + (limit // 2):
                end = preferred
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_markdown(text: str, limit: int = 2200, overlap: int = 220) -> list[tuple[str, str]]:
    """Chunk Markdown prose by heading, excluding fenced code from retrieval."""
    if limit <= overlap:
        raise ValueError("Chunk limit must be greater than overlap.")
    sections: list[tuple[str, list[str]]] = []
    heading = "Overview"
    buffer: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if match:
            if buffer:
                sections.append((heading, buffer))
            heading = match.group(2).strip()
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        sections.append((heading, buffer))

    chunks: list[tuple[str, str]] = []
    for section_heading, lines in sections:
        body = "\n".join(lines).strip()
        for part in _split_text(body, limit - len(section_heading) - 3, overlap):
            chunks.append((section_heading, f"{section_heading}\n\n{part}"))
    return chunks


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            path TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            document_path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
            source_id TEXT NOT NULL,
            heading TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            UNIQUE(document_path, ordinal, content_hash)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, tokenize='trigram');
        """
    )
    document_columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
    if "chunker_version" not in document_columns:
        connection.execute(
            "ALTER TABLE documents ADD COLUMN chunker_version TEXT NOT NULL DEFAULT 'legacy'"
        )
    return connection


def _delete_document(connection: sqlite3.Connection, path: str) -> None:
    chunk_ids = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE document_path = ?", (path,))]
    for chunk_id in chunk_ids:
        connection.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    connection.execute("DELETE FROM documents WHERE path = ?", (path,))


def _batched(values: list[str], size: int = 16) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def build_index(
    embedder: Embedder,
    database_path: Path = DEFAULT_DATABASE,
    manifest_path: Path = DEFAULT_MANIFEST,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, int]:
    """Incrementally index approved files and remove obsolete derived records."""
    files = admitted_files(manifest_path, project_root)
    connection = _connect(database_path)
    stats = {"documents_indexed": 0, "documents_skipped": 0, "documents_removed": 0, "chunks_indexed": 0}
    active_paths = {file.path for file in files}
    active_sources = {file.source_id for file in files}
    try:
        with connection:
            existing_rows = connection.execute("SELECT path, source_id FROM documents").fetchall()
            for row in existing_rows:
                if row["source_id"] not in active_sources or row["path"] not in active_paths:
                    _delete_document(connection, row["path"])
                    stats["documents_removed"] += 1

            for file in files:
                existing = connection.execute(
                    "SELECT content_hash, embedding_model, chunker_version FROM documents WHERE path = ?", (file.path,)
                ).fetchone()
                if (
                    existing
                    and existing["content_hash"] == file.content_hash
                    and existing["embedding_model"] == embedder.model
                    and existing["chunker_version"] == CHUNKER_VERSION
                ):
                    stats["documents_skipped"] += 1
                    continue
                if existing:
                    _delete_document(connection, file.path)

                chunks = chunk_markdown(file.content)
                if not chunks:
                    continue
                vectors: list[list[float]] = []
                for batch in _batched([content for _, content in chunks]):
                    vectors.extend(embedder.embed(batch))
                if len(vectors) != len(chunks):
                    raise RuntimeError(f"Embedding count mismatch for {file.path}.")

                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """INSERT INTO documents(path, source_id, content_hash, embedding_model, chunker_version, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (file.path, file.source_id, file.content_hash, embedder.model, CHUNKER_VERSION, now),
                )
                for ordinal, ((heading, content), vector) in enumerate(zip(chunks, vectors), start=1):
                    chunk_hash = hashlib.sha256(
                        f"{file.source_id}:{file.path}:{file.content_hash}:{ordinal}".encode("utf-8")
                    ).hexdigest()
                    cursor = connection.execute(
                        """INSERT INTO chunks(document_path, source_id, heading, ordinal, content, content_hash, embedding_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (file.path, file.source_id, heading, ordinal, content, chunk_hash, json.dumps(vector)),
                    )
                    connection.execute("INSERT INTO chunks_fts(rowid, content) VALUES (?, ?)", (cursor.lastrowid, content))
                    stats["chunks_indexed"] += 1
                stats["documents_indexed"] += 1
    finally:
        connection.close()
    return stats


def _fts_query(query: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f]", " ", query).strip()
    if len(cleaned) < 3:
        raise ValueError("Search query must contain at least three characters.")
    terms: list[str] = []
    for token in QUERY_TOKEN.findall(cleaned):
        if re.fullmatch(r"[\u3040-\u30ff\u3400-\u9fff]+", token):
            terms.extend(token[index : index + 3] for index in range(max(len(token) - 2, 0)))
        elif len(token) >= 3:
            terms.append(token)
    unique_terms = list(dict.fromkeys(term for term in terms if len(term) >= 3))[:48]
    if not unique_terms:
        raise ValueError("Search query must contain a searchable term of at least three characters.")
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique_terms)


def lexical_search(database_path: Path, query: str, limit: int) -> list[SearchResult]:
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            """SELECT chunks.source_id, chunks.document_path, chunks.heading, chunks.ordinal, chunks.content,
                      bm25(chunks_fts) AS rank
               FROM chunks_fts JOIN chunks ON chunks_fts.rowid = chunks.id
               WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (_fts_query(query), limit),
        ).fetchall()
    finally:
        connection.close()
    return [
        SearchResult(
            source_id=row["source_id"], path=row["document_path"], heading=row["heading"], ordinal=row["ordinal"],
            content=row["content"], score=float(-row["rank"]), mode="lexical",
        )
        for row in rows
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def dense_search(database_path: Path, query: str, limit: int, embedder: Embedder) -> list[SearchResult]:
    query_vector = embedder.embed([query])[0]
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT source_id, document_path, heading, ordinal, content, embedding_json FROM chunks"
        ).fetchall()
    finally:
        connection.close()
    scored = [
        SearchResult(
            source_id=row["source_id"], path=row["document_path"], heading=row["heading"], ordinal=row["ordinal"],
            content=row["content"], score=_cosine(query_vector, json.loads(row["embedding_json"])), mode="dense",
        )
        for row in rows
    ]
    return sorted(scored, key=lambda result: result.score, reverse=True)[:limit]


def search(
    database_path: Path,
    query: str,
    limit: int = 5,
    mode: str = "lexical",
    embedder: Embedder | None = None,
) -> list[SearchResult]:
    """Search using lexical FTS5, dense cosine, or Cerebras-style rank fusion."""
    if limit < 1:
        raise ValueError("Search limit must be at least one.")
    if mode == "lexical":
        return lexical_search(database_path, query, limit)
    if embedder is None:
        raise ValueError(f"Search mode {mode} needs an embedding model.")
    dense = dense_search(database_path, query, limit * 4, embedder)
    if mode == "dense":
        return dense[:limit]
    if mode != "hybrid":
        raise ValueError(f"Unsupported search mode: {mode}")
    lexical = lexical_search(database_path, query, limit * 4)
    fused: dict[tuple[str, int], tuple[SearchResult, float]] = {}
    for ranking in (lexical, dense):
        for rank, result in enumerate(ranking, start=1):
            key = (result.path, result.ordinal)
            current = fused.get(key)
            score = (current[1] if current else 0.0) + 1.0 / (60 + rank)
            fused[key] = (result, score)
    return [
        SearchResult(
            source_id=result.source_id, path=result.path, heading=result.heading, ordinal=result.ordinal,
            content=result.content, score=score, mode="hybrid",
        )
        for result, score in sorted(fused.values(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def result_as_dict(result: SearchResult) -> dict[str, Any]:
    return {
        "citation": result.citation,
        "source_id": result.source_id,
        "path": result.path,
        "heading": result.heading,
        "ordinal": result.ordinal,
        "score": round(result.score, 6),
        "mode": result.mode,
        "content": result.content,
    }
