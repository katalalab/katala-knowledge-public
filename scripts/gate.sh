#!/usr/bin/env bash
# gate.sh — knowledge-base merge gate (local-first, no cloud dependency).
# PR ブランチに対して実行: secret_scan -> RAG admission -> gitleaks -> lint -> health_probe。
# いずれか失敗で非ゼロ終了。GREEN のときだけ merge する。
#
# Usage:  scripts/gate.sh [label]
set -euo pipefail
cd "$(dirname "$0")/.."

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) PY="py -3" ;;   # Windows git-bash: WindowsApps python3 スタブ回避
  *) PY="python3" ;;
esac
export PYTHONUTF8=1                       # Windows の cp932 既定を回避

echo "== secret_scan (regex baseline, fail-closed) =="
$PY scripts/secret_scan.py
echo "== RAG admission (manifest boundary + selected-source secret scan) =="
$PY scripts/rag_admission_check.py
echo "== gitleaks (deep secret scan, fail-closed) =="
if command -v gitleaks >/dev/null 2>&1; then
  # --redact: 検知値をログに出さない(秘匿境界)
  if gitleaks dir . --no-banner --redact; then echo "gitleaks: clean"; else echo "gitleaks: LEAKS FOUND"; exit 1; fi
else
  echo "gitleaks: 未導入 — baseline(secret_scan.py)のみで続行"
fi
echo "== lint =="
$PY scripts/lint_wiki.py .
echo "== health_probe (GREEN 必須) =="
$PY scripts/health_probe.py . --node "${1:-gate}"

echo "GATE: GREEN — merge 可"
