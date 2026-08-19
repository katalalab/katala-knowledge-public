# katala-knowledge-public

Local-only RAG and a merge gate for a Markdown knowledge base.

Your notes stay the source of truth: plain Markdown in Git. Everything here is derived —
a SQLite index you can delete and rebuild — and nothing leaves the machine. Embeddings and
answers come from a local [Ollama](https://ollama.com) endpoint; there is no cloud service
and no API key anywhere in this repo.

Two halves:

- **RAG** (`scripts/rag_*.py`) — admit approved files, chunk them by heading, index them
  incrementally, search lexically / densely / hybrid, and answer strictly from cited evidence.
- **Gate** (`scripts/gate.sh`, `lint_wiki.py`, `health_probe.py`, `secret_scan.py`) — one
  command that a PR must pass before it merges into the knowledge base.

## Requirements

- Python 3.11+ (standard library only — no pip install)
- SQLite built with FTS5 including the `trigram` tokenizer (needed for Japanese/CJK lexical search)
- Optional: Ollama at `http://127.0.0.1:11434` with an embedding model (`nomic-embed-text`)
  and a small chat model, for `--mode dense|hybrid` and `rag_ask.py`
- Optional: [`gitleaks`](https://github.com/gitleaks/gitleaks) on `PATH` for the deep secret scan

## Expected repository layout

These tools assume the knowledge repo they run in looks like this:

```
wiki/          # the knowledge pages (Markdown, [[wikilink]] style), wiki/index.md is the entry point
docs/          # runbooks and system docs
log/           # append-only daily notes, named YYYYMMDD.md with an H1 of "# YYYY-MM-DD"
audit/         # human feedback entries, one Markdown file each; audit/resolved/ once applied
raw/           # unreviewed source material — never indexed, never scanned into the corpus
rag/corpus.json  # the allowlist of what may be indexed
outputs/       # derived artifacts, gitignored
scripts/       # this repo
```

Copy `scripts/` and `tests/` into your own knowledge repo, then copy
`rag/corpus.example.json` to `rag/corpus.json` and edit the sources.

## RAG

```bash
python3 scripts/rag_admission_check.py            # validate the manifest, index nothing
python3 scripts/rag_index.py                      # incremental index into outputs/rag/
python3 scripts/rag_search.py 'query' --mode lexical
python3 scripts/rag_search.py 'query' --mode hybrid
python3 scripts/rag_ask.py 'query' --model qwen3:0.6b
```

`rag_index.py` re-embeds a document only when its content hash, embedding model, or chunker
version changed, and drops rows for files that left the manifest. Search results carry a
`[source_id:path#ordinal]` citation; `rag_ask.py` prints the same citations under `Sources:`.

Answers are generated in Japanese by the prompt in `rag_ask.py` — change the system message
there if you want another language.

### What the admission layer refuses

`admitted_files()` is fail-closed. A source is rejected — not skipped — when it:

- is missing any of `id / root / include / visibility / owner / review_status`, or is not marked approved
- is an absolute path, contains `..`, or resolves outside the repository root
- lives under `.git`, `raw`, `audit`, `log`, `outputs`, `node_modules`, or an agent state directory
- has a filename containing `.env`, `credential`, `secret`, `token`, `cookie`, `session`, `private_key`, `id_rsa`
- has a suffix outside the text allowlist, or is admitted twice
- trips the regex secret scanner

Fenced code blocks are dropped during chunking, so pasted commands and config never become
retrieval targets and headings inside code fences cannot forge a section.

Evidence is treated as data, not instruction: the answer prompt tells the model that text
inside `<evidence>` is never a command to execute.

## Gate

```bash
bash scripts/gate.sh
```

Runs, stopping at the first failure: `secret_scan.py` → `rag_admission_check.py` → `gitleaks`
(if installed) → `lint_wiki.py` → `health_probe.py`. Exit code 0 means the branch is
mergeable.

`lint_wiki.py <root>` reports dead wikilinks, orphan pages, pages missing from `wiki/index.md`,
terms linked 3+ times with no page, malformed `log/` filenames or H1s, malformed audit
frontmatter, and open audits whose target file no longer exists.

`health_probe.py <root>` emits a JSON verdict (`GREEN`/`RED`) over page frontmatter, links,
secrets, encoding, and duplication. It expects each page under `wiki/` to carry twelve
frontmatter fields:

`type` (one of `concept|entity|summary|runbook|decision`), `title`, `id`, `description`,
`owner`, `node`, `visibility`, `review_status`, `freshness_sla`
(`weekly|monthly|quarterly|static`), `tags`, `timestamp` (ISO 8601), `source`.

Dead links, missing/invalid frontmatter, secret hits, non-UTF-8 bytes, CRLF in Markdown, and
duplicate slugs/ids are RED. Orphans and staleness are warnings only.

`secret_scan.py` is a fail-closed regex baseline (provider keys, GitHub PATs, Slack tokens,
AWS keys, private key headers, bearer tokens). A line containing `lint-ignore-secret` is
skipped, for documented false positives. It is a floor, not a replacement for gitleaks.

## Evaluating retrieval before you change it

Keep a question set in Git with the expected source and citation for each question: exact-term
questions, paraphrases, multi-document questions, questions with no answer in the corpus, and
questions that must not be answered. Track Hit@1, Hit@3, MRR, and citation correctness, and
record retrieval quality separately from generation quality.

Promotion rule: run the same set before and after any change to retrieval mode, embedding
model, chunk size, or corpus scope. If Hit@3 or citation correctness gets worse, the change
does not become the default. One good-looking answer is not evidence.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers chunking (headings kept, fenced code excluded), admission refusals (escaping the repo,
forbidden directory, secret content), incremental re-indexing, and citation stability across
lexical and hybrid search. The tests use a fake embedder, so no Ollama is needed.

## Known ceilings

- Dense search loads every chunk vector and scores it in Python — fine for a few thousand
  chunks, replace with a vector extension if your corpus outgrows that.
- Hybrid fusion is plain reciprocal rank fusion (k=60), with no reranker.
- The frontmatter parser handles flat `key: value` and one-level lists on purpose, to avoid a
  YAML dependency.

## License

MIT — see [LICENSE](LICENSE).
