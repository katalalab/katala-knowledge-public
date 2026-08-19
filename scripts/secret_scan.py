#!/usr/bin/env python3
"""secret_scan.py — fail-closed secret scanner for a Markdown knowledge corpus.

Read-only. Returns/【prints】findings. A line containing 'lint-ignore-secret'
is skipped (documented false positives). Windows: run with PYTHONUTF8=1.

Usage:
    python3 scripts/secret_scan.py [<path> ...]   # default: wiki raw audit scripts
Exit: 0 clean, 1 findings.
"""
import re
import sys
from pathlib import Path

PATTERNS = [
    # Hyphen allowed so sk-ant-api03-… / sk-proj-… are detected (not only legacy sk-).
    ("openai_or_provider_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("github_pat_fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_akid", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
    ("aws_secret_kv", re.compile(r"\baws_secret_access_key\b\s*[:=]", re.I)),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
]
IGNORE = "lint-ignore-secret"
SCANNABLE = {".md", ".txt", ".yaml", ".yml", ".json", ".py", ".sh", ".ps1", ".toml", ""}


def scan(*paths):
    findings = []
    for base in paths:
        base = Path(base)
        targets = [base] if base.is_file() else base.rglob("*")
        for f in targets:
            if not f.is_file() or f.suffix.lower() not in SCANNABLE:
                continue
            if "__pycache__" in f.parts or f.name.endswith(".pyc"):
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError) as err:
                findings.append((str(f), 0, f"unreadable:{type(err).__name__}"))
                continue
            for n, line in enumerate(lines, 1):
                if IGNORE in line:
                    continue
                for name, rx in PATTERNS:
                    if rx.search(line):
                        findings.append((str(f), n, name))
    return findings


def main(argv):
    paths = argv or ["wiki", "raw", "audit", "scripts"]
    findings = scan(*paths)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if findings:
        print(f"SECRET SCAN: {len(findings)} finding(s) [FAIL-CLOSED]")
        for f, n, name in findings:
            print(f"  {f}:{n} — {name}")
        return 1
    print("SECRET SCAN: clean (0 findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
