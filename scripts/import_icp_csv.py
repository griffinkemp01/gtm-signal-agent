"""Bulk-import ICP companies from a CSV.

Reads a CSV with at minimum a `Company` (or `name`) column and upserts each
row into the `companies` table. Domains are resolved via Claude when missing.

Usage:
    .venv/bin/python -m scripts.import_icp_csv <path-to-csv>
    .venv/bin/python -m scripts.import_icp_csv <path> --dry-run    # no DB writes
    .venv/bin/python -m scripts.import_icp_csv <path> --tier 2     # override default tier
    .venv/bin/python -m scripts.import_icp_csv <path> --segment B  # override default segment
    .venv/bin/python -m scripts.import_icp_csv <path> --skip-resolve  # leave domain blank

Design notes:
 - **Idempotent.** Re-running the same CSV upserts by name+domain, so adding
   companies later is safe. Existing target_tier/segment are NOT overwritten
   once set — we only fill them in on first insert.
 - **Domain resolution caches.** Claude lookups are cached to
   `~/.signal_agent/domain_cache.json` so re-runs don't repeat $ and API calls.
 - **Concurrent resolution.** Up to 8 Claude calls in parallel. 372 companies
   at ~1s each → ~45s to resolve the full sheet.
 - **Manual review bucket.** Any company whose domain Claude couldn't confirm
   (low confidence or blank) is inserted with `is_icp=false` + a `needs_review`
   marker in the name. Nothing polls them until RevOps confirms.
 - **CSV format flexibility.** Accepts `Company`, `Name`, `Company Name`,
   `company_name` as the name column. BOM-tolerant. CRLF-tolerant.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import structlog
from anthropic import Anthropic
from anthropic._exceptions import RateLimitError
from sqlalchemy import select

from signal_agent.config import settings
from signal_agent.db import session_scope
from signal_agent.models import Company

log = structlog.get_logger()

CACHE_PATH = Path.home() / ".signal_agent" / "domain_cache.json"
RESOLVE_CONCURRENCY = 3   # conservative — Anthropic rate-limits aggressively on cheap tiers
RESOLVE_MAX_RETRIES = 5
NAME_COLUMN_CANDIDATES = ("Company", "Name", "Company Name", "company_name", "company", "name")
DOMAIN_COLUMN_CANDIDATES = (
    "Domain", "domain", "Company Domain", "Website", "website", "URL", "url",
)
SIZE_COLUMN_CANDIDATES = (
    "Size", "size", "Employees", "Employee Count", "Company Size", "Headcount",
)

SYSTEM_PROMPT = """\
You resolve US/global company names to their primary corporate web domain.

Given a company name, return ONLY a JSON object:
{
  "domain": "example.com",                  // lowercase, no protocol, no www
  "confidence": 0.0-1.0,
  "ambiguous": boolean,                     // true if multiple well-known companies share this name
  "notes": "brief explanation if ambiguous or uncertain"
}

Rules:
- Return the canonical company homepage domain, not a subdomain.
- If the company name contains legal suffixes (Inc., Corp., LLC), strip them for matching.
- If the company is publicly traded, the ticker's parent company domain is preferred.
- If you're not confident (score < 0.7), return your best guess but set ambiguous=true.
- Never wrap the JSON in code fences.
"""


@dataclass
class ResolvedCompany:
    name: str
    domain: str | None
    confidence: float
    ambiguous: bool
    notes: str
    # Per-company segment/tier derived from the CSV Size column. None → fall
    # back to the CLI --segment / --tier defaults at upsert time.
    segment: str | None = None
    tier: int | None = None


def _load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _normalize_domain(raw: str) -> str | None:
    """Strip protocol / www / path so 'https://www.acme.com/jobs' → 'acme.com'."""
    d = (raw or "").strip().lower()
    if not d:
        return None
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    d = d.split("/")[0].strip()
    return d or None


def _segment_tier_from_size(raw: str) -> tuple[str | None, int | None]:
    """Map an employee-count Size string to ICP segment + tier (docs/icp.md).

    Uses the LOWER bound of the range so we don't over-promote: a "1,001-5,000"
    bucket lands in B, not A.
      lower >= 5000  → Segment A / tier 1   (large regulated enterprise)
      lower >= 1000  → Segment B / tier 2   (mid-market scaling AI)
      else           → Segment C / tier 3   (sub-1,000)
    Returns (None, None) when the size is blank/unparseable → caller defaults.
    """
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", raw or "")]
    if not nums:
        return (None, None)
    lower = min(nums)
    if lower >= 5000:
        return ("A", 1)
    if lower >= 1000:
        return ("B", 2)
    return ("C", 3)


def _read_rows(csv_path: Path) -> list[tuple[str, str | None, str | None, int | None]]:
    """Return (name, domain|None, segment|None, tier|None) from the CSV.

    Uses the CSV's domain column when present so we DON'T pay Claude to
    re-resolve domains we already have — only rows with a blank/missing domain
    fall back to LLM resolution. Segment/tier are derived from the Size column
    when available (None → caller's --segment/--tier defaults).
    """
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        name_col = next((c for c in NAME_COLUMN_CANDIDATES if c in fields), None)
        domain_col = next((c for c in DOMAIN_COLUMN_CANDIDATES if c in fields), None)
        size_col = next((c for c in SIZE_COLUMN_CANDIDATES if c in fields), None)
        if name_col is None:
            # Fallback: single-column CSV with no proper header.
            f.seek(0)
            rows = [row[0].strip() for row in csv.reader(f) if row and row[0].strip()]
            if rows and rows[0].lower() in {c.lower() for c in NAME_COLUMN_CANDIDATES}:
                rows = rows[1:]
            return [(r, None, None, None) for r in rows if r]
        out: list[tuple[str, str | None, str | None, int | None]] = []
        for row in reader:
            name = (row.get(name_col) or "").strip()
            if not name:
                continue
            domain = _normalize_domain(row.get(domain_col, "")) if domain_col else None
            segment, tier = (
                _segment_tier_from_size(row.get(size_col, "")) if size_col else (None, None)
            )
            out.append((name, domain, segment, tier))
        return out


def _resolve_one(client: Anthropic, name: str, cache: dict[str, dict]) -> ResolvedCompany:
    cached = cache.get(name.lower())
    # Only treat SUCCESSFUL resolutions as cached. Failures (no domain, or
    # explicit error notes) should be retried on re-runs, not persisted.
    if cached and cached.get("domain") and not cached.get("notes", "").startswith("error:"):
        return ResolvedCompany(
            name=name,
            domain=cached.get("domain"),
            confidence=cached.get("confidence", 0.0),
            ambiguous=cached.get("ambiguous", False),
            notes=cached.get("notes", "from cache"),
        )

    # Exponential backoff on rate limits. Anthropic's 429 response includes
    # a retry-after header that the SDK surfaces; we just sleep + retry.
    resp = None
    last_err: Exception | None = None
    for attempt in range(RESOLVE_MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Company name: {name}"}],
            )
            break
        except RateLimitError as e:
            last_err = e
            sleep_s = min(30, 2 ** attempt + 1)  # 2, 3, 5, 9, 17s
            time.sleep(sleep_s)
        except Exception as e:
            last_err = e
            break

    try:
        if resp is None:
            raise last_err or RuntimeError("no response")
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        # Strip code fences if the model ignored instructions.
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        resolved = ResolvedCompany(
            name=name,
            domain=(data.get("domain") or "").lower().strip() or None,
            confidence=float(data.get("confidence", 0.0)),
            ambiguous=bool(data.get("ambiguous", False)),
            notes=data.get("notes", ""),
        )
    except Exception as e:
        log.warning("import_icp.resolve_failed", name=name, err=str(e))
        resolved = ResolvedCompany(
            name=name, domain=None, confidence=0.0,
            ambiguous=False, notes=f"error: {e}",
        )

    cache[name.lower()] = {
        "domain": resolved.domain,
        "confidence": resolved.confidence,
        "ambiguous": resolved.ambiguous,
        "notes": resolved.notes,
    }
    return resolved


def resolve_all(names: list[str]) -> list[ResolvedCompany]:
    cache = _load_cache()
    client = Anthropic(api_key=settings.anthropic_api_key)
    results: list[ResolvedCompany] = []

    with ThreadPoolExecutor(max_workers=RESOLVE_CONCURRENCY) as pool:
        futures = {
            pool.submit(_resolve_one, client, name, cache): name for name in names
        }
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            tag = "✓" if r.domain and r.confidence >= 0.7 else ("?" if r.domain else "✗")
            print(f"  [{i:>3}/{len(names)}] {tag} {r.name} → {r.domain or '???'}"
                  + (f"  ({r.notes[:60]})" if r.ambiguous or not r.domain else ""))

    _save_cache(cache)
    # Re-sort to CSV order so DB inserts are stable + reviewable.
    by_name = {r.name: r for r in results}
    return [by_name[n] for n in names if n in by_name]


def upsert_companies(resolved: list[ResolvedCompany], default_tier: int,
                     default_segment: str, dry_run: bool) -> dict:
    inserted = updated = needs_review = skipped = 0
    with session_scope() as s:
        for r in resolved:
            if not r.domain:
                # No domain — still record but mark needs_review + is_icp=false.
                domain_value = f"unresolved.needs-review.{r.name.lower().replace(' ', '-').replace(',','').replace('.', '')[:80]}"
                is_icp = False
                needs_review += 1
            elif r.confidence < 0.7 or r.ambiguous:
                # Low confidence — insert but gate out of polling.
                domain_value = r.domain
                is_icp = False
                needs_review += 1
            else:
                domain_value = r.domain
                is_icp = True

            # Per-row segment/tier (derived from CSV Size) win; fall back to the
            # CLI defaults when the row didn't carry one.
            row_segment = r.segment or default_segment
            row_tier = r.tier or default_tier

            existing = s.execute(
                select(Company).where(Company.domain == domain_value)
            ).scalar_one_or_none()

            if existing is None:
                if not dry_run:
                    s.add(Company(
                        domain=domain_value,
                        name=r.name,
                        segment=row_segment,
                        target_tier=row_tier,
                        is_icp=is_icp,
                    ))
                inserted += 1
            else:
                # Fill in missing tier/segment but don't override explicit values.
                # Critically, do NOT flip `is_icp` back to True for existing rows:
                # operators intentionally set it to False (via SQL or the seed
                # YAML) to drop a company from polling, and that choice should
                # survive CSV re-imports. Re-enabling requires a manual SQL
                # update or editing the drop out of whatever list excluded it.
                if not dry_run:
                    if existing.target_tier is None:
                        existing.target_tier = row_tier
                    if not existing.segment:
                        existing.segment = row_segment
                updated += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "needs_review": needs_review,
        "skipped": skipped,
        "total": len(resolved),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-import ICP companies from a CSV")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve + print, don't write to DB")
    parser.add_argument("--tier", type=int, default=2,
                        help="Default target_tier for new rows (1-3, default 2)")
    parser.add_argument("--segment", default="B",
                        help="Default segment for new rows (A|B|C, default B)")
    parser.add_argument("--skip-resolve", action="store_true",
                        help="Skip Claude domain resolution (all rows go to needs_review)")
    args = parser.parse_args()

    if not args.csv_path.exists():
        print(f"✗ CSV not found: {args.csv_path}", file=sys.stderr)
        return 1

    rows = _read_rows(args.csv_path)
    print(f"=== Import {args.csv_path.name}: {len(rows)} companies ===\n")

    # Trust domains already in the CSV (confidence 1.0, no Claude call). Only
    # rows with a blank/missing domain need LLM resolution. Segment/tier are
    # carried from the CSV Size column (None → CLI --segment/--tier defaults).
    from_csv = [(n, d, seg, tier) for n, d, seg, tier in rows if d]
    need_resolve = [n for n, d, _seg, _tier in rows if not d]

    resolved: list[ResolvedCompany] = [
        ResolvedCompany(name=n, domain=d, confidence=1.0, ambiguous=False,
                        notes="from csv", segment=seg, tier=tier)
        for n, d, seg, tier in from_csv
    ]
    print(f"[csv] {len(from_csv)} domains taken from the CSV; "
          f"{len(need_resolve)} need resolution")

    # Segment/tier distribution derived from the Size column.
    seg_dist: dict[str, int] = {}
    for r in resolved:
        seg_dist[r.segment or f"default({args.segment})"] = (
            seg_dist.get(r.segment or f"default({args.segment})", 0) + 1
        )
    print(f"[segment] derived from Size: "
          + "  ".join(f"{k}={v}" for k, v in sorted(seg_dist.items())))

    if need_resolve:
        if args.skip_resolve:
            resolved += [ResolvedCompany(name=n, domain=None, confidence=0.0,
                                         ambiguous=False, notes="--skip-resolve")
                         for n in need_resolve]
        else:
            print(f"\n[resolve] {len(need_resolve)} missing domains → Claude "
                  f"(cached, concurrent × {RESOLVE_CONCURRENCY})...\n")
            resolved += resolve_all(need_resolve)

    counts_by_tier = {"high": 0, "low": 0, "missing": 0}
    for r in resolved:
        if not r.domain:
            counts_by_tier["missing"] += 1
        elif r.confidence >= 0.7 and not r.ambiguous:
            counts_by_tier["high"] += 1
        else:
            counts_by_tier["low"] += 1

    print(f"\n[resolve] summary: ✓ high={counts_by_tier['high']}  "
          f"? low/ambiguous={counts_by_tier['low']}  "
          f"✗ unresolved={counts_by_tier['missing']}")

    print(f"\n[db] upserting{' (DRY RUN)' if args.dry_run else ''}...")
    stats = upsert_companies(resolved, args.tier, args.segment, args.dry_run)
    print(f"\n=== Import complete ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["needs_review"] > 0:
        print(f"\n{stats['needs_review']} companies flagged for review. "
              f"They're in the DB with is_icp=false and won't be polled until "
              f"you confirm their domain. Query with:")
        print(f"  docker exec signalagent-postgres-1 psql -U signal -d signal_agent \\")
        print(f"    -c \"SELECT name, domain FROM companies WHERE is_icp=false ORDER BY name;\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
