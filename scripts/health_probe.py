#!/usr/bin/env python3
"""health_probe.py — knowledge-base health probe.

Read-only. Emits a JSON report (schema katala.product-knowledge-health.v1):
schema/encoding/links/secrets/debt + verdict. Windows: run with PYTHONUTF8=1.

verdict RED if any of: dead_links, missing_type, missing_provenance, secrets,
non_utf8, crlf_md, dup > 0. orphans/stale are WARN only (probe v0 — staged gate).

Usage: python3 scripts/health_probe.py <wiki-root> [--node NAME]
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_scan  # noqa: E402

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
SLA_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 90, "static": None}
JST = timezone(timedelta(hours=9))
# Required page frontmatter fields (12); see README. Nested list values count as present if the key exists.
REQUIRED_OKF_FIELDS = (
    "type",
    "title",
    "id",
    "description",
    "owner",
    "node",
    "visibility",
    "review_status",
    "freshness_sla",
    "tags",
    "timestamp",
    "source",
)
VALID_TYPES = {"concept", "entity", "summary", "runbook", "decision"}


def parse_fm(text):
    m = FM_RE.match(text)
    if not m:
        return None
    fm = {}
    for line in m.group(1).split("\n"):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        if line[0] in (" ", "\t"):  # nested list value, skip key tracking
            continue
        key = k.strip()
        val = v.strip()
        # Empty value for source:/tags: means a following YAML list body.
        if key in ("source", "tags") and not val:
            fm[key] = "(list)"
        else:
            fm[key] = val
    return fm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--node", default="local")
    a = ap.parse_args()
    root = Path(a.root)
    wiki = root / "wiki"
    files = list(wiki.rglob("*.md"))

    pages = set()
    for p in files:
        pages.add(p.stem)
        pages.add(str(p.relative_to(wiki).with_suffix("")).replace("\\", "/"))

    missing_type = missing_prov = missing_required = invalid_type = non_utf8 = crlf = stale = 0
    stems, ids = [], []
    now = datetime.now(JST)
    for p in files:
        raw = p.read_bytes()
        if b"\r\n" in raw:
            crlf += 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            non_utf8 += 1
            continue
        if p.name == "index.md":
            continue
        fm = parse_fm(text) or {}
        if "type" not in fm or not fm.get("type"):
            missing_type += 1
        elif fm.get("type") not in VALID_TYPES:
            invalid_type += 1
        if "source" not in fm or not str(fm.get("source", "")).strip():
            missing_prov += 1
        for field in REQUIRED_OKF_FIELDS:
            val = fm.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing_required += 1
                break
        stems.append(p.stem)
        if fm.get("id"):
            ids.append(fm["id"])
        sla = SLA_DAYS.get(fm.get("freshness_sla"))
        ts = fm.get("timestamp")
        if sla and ts:
            try:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if (now - d).days > sla:
                    stale += 1
            except ValueError:
                pass

    inbound, dead = set(), 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for link in WIKILINK_RE.findall(text):
            link = link.strip()
            if link in pages or Path(link).stem in pages:
                inbound.add(Path(link).stem)
            else:
                dead += 1
    orphans = [p for p in files
               if p.stem not in inbound and p.stem != "index" and p.parent != wiki]

    dup = (len(stems) - len(set(stems))) + (len(ids) - len(set(ids)))

    secrets = 0
    for sub in ("wiki", "raw", "audit"):
        d = root / sub
        if d.exists():
            secrets += len(secret_scan.scan(d))

    npages = len([p for p in files if p.name != "index.md"])
    stale_pct = round(100 * stale / npages) if npages else 0
    red = any(
        [
            dead,
            missing_type,
            missing_prov,
            missing_required,
            invalid_type,
            secrets,
            non_utf8,
            crlf,
            dup,
        ]
    )

    report = {
        "schema": "katala.product-knowledge-health.v1",
        "node": a.node,
        "checked_at": now.isoformat(timespec="seconds"),
        "pages": npages,
        "schema_ok": {
            "missing_type": missing_type,
            "missing_provenance": missing_prov,
            "missing_required_okf": missing_required,
            "invalid_type": invalid_type,
            "required_fields": list(REQUIRED_OKF_FIELDS),
        },
        "links": {"dead": dead, "orphans": len(orphans)},
        "secrets": {"hits": secrets},
        "encoding": {"non_utf8": non_utf8, "crlf_md": crlf},
        "debt": {"stale_pct": stale_pct, "dup": dup},
        "warnings": {"orphans": len(orphans), "stale_pct": stale_pct},
        "verdict": "RED" if red else "GREEN",
    }
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
