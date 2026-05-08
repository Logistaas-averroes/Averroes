"""
scripts/benchmark_ngrams.py

Logistaas Ads Intelligence — N-Gram Endpoint Benchmark Tool
PR-ADS-061 — N-Gram Benchmark / UI Performance Polish

Measures response time and summarises metadata from the existing
GET /api/search-terms/ngrams endpoint.  Read-only; no external writes.

Usage:
    python -m scripts.benchmark_ngrams \\
        --base-url "$BASE_URL" \\
        --token "$TOKEN" \\
        --days 14 \\
        --n 1,2,3 \\
        --limit 100

The token can also be supplied via the ADS_SESSION environment variable
to avoid exposing it in shell history or process listings:

    ADS_SESSION="<value>" python -m scripts.benchmark_ngrams \\
        --base-url "$BASE_URL" \\
        --days 14

Rules:
  - Standard-library only (no third-party dependencies).
  - No direct DB access.
  - No Google Ads / HubSpot calls.
  - No scheduler calls.
  - Only write: optional local JSON report via --output.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


# ── URL builder ───────────────────────────────────────────────────────────────

def build_url(base_url: str, args: argparse.Namespace) -> str:
    params: dict[str, str] = {
        "days": str(args.days),
        "n": args.n,
        "limit": str(args.limit),
    }
    if args.campaign:
        params["campaign"] = args.campaign
    if args.match_type:
        params["match_type"] = args.match_type
    if args.waste_state:
        params["waste_state"] = args.waste_state
    if args.min_spend is not None:
        params["min_spend"] = str(args.min_spend)
    if args.q:
        params["q"] = args.q

    return base_url.rstrip("/") + "/api/search-terms/ngrams?" + urllib.parse.urlencode(params)


# ── Single request ─────────────────────────────────────────────────────────────

def fetch_once(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Cookie": f"ads_session={token}"},
        method="GET",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        elapsed = time.perf_counter() - started
        data = json.loads(body.decode("utf-8"))
        return {
            "status": resp.status,
            "elapsed_seconds": round(elapsed, 4),
            "payload_bytes": len(body),
            "data": data,
        }


# ── Result printer ────────────────────────────────────────────────────────────

def print_run_summary(run_num: int, result: dict, url: str) -> None:
    data = result.get("data", {})
    summary = data.get("summary", {})
    dq = data.get("data_quality", {})

    print(f"\n── Run {run_num} ──────────────────────────────────────")
    print(f"  URL:                       {url}")
    print(f"  HTTP status:               {result['status']}")
    print(f"  Elapsed:                   {result['elapsed_seconds']} s")
    print(f"  Payload:                   {result['payload_bytes']} bytes")
    print(f"  ngrams_returned:           {summary.get('ngrams_returned', '—')}")
    print(f"  source_rows_analyzed:      {summary.get('source_rows_analyzed', '—')}")
    print(f"  unique_search_terms:       {summary.get('unique_search_terms_analyzed', '—')}")
    print(f"  row_cap_applied:           {dq.get('row_cap_applied', False)}")
    if dq.get("row_cap_applied"):
        print(f"  row_cap:                   {dq.get('row_cap', '—')}")
    print(f"  db_unavailable:            {data.get('db_unavailable', False)}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark the Logistaas N-Gram endpoint (read-only).",
    )
    parser.add_argument("--base-url",    required=True,  help="Base URL of the app (e.g. https://app.example.com)")
    parser.add_argument("--token",       default=None,   help="ads_session cookie value. Falls back to ADS_SESSION env var if not provided.")
    parser.add_argument("--days",        type=int,  default=14,      help="Look-back window in days (default: 14)")
    parser.add_argument("--n",           default="1,2,3",            help="Comma-separated n-gram lengths (default: 1,2,3)")
    parser.add_argument("--limit",       type=int,  default=100,     help="Max n-gram rows to return (default: 100)")
    parser.add_argument("--campaign",    default=None,               help="Filter by campaign name")
    parser.add_argument("--match-type",  dest="match_type", default=None, help="Filter by match type")
    parser.add_argument("--waste-state", dest="waste_state", default=None, help="Filter by waste state (all|flagged|clean|unanalyzed)")
    parser.add_argument("--min-spend",   dest="min_spend", type=float, default=None, help="Minimum spend filter (omitted when not provided; server applies no minimum by default)")
    parser.add_argument("--q",           default=None,               help="Search term substring filter")
    parser.add_argument("--runs",        type=int,  default=1,       help="Number of benchmark runs (default: 1)")
    parser.add_argument("--output",      default=None,               help="Optional path to write JSON report")

    args = parser.parse_args()

    token = args.token or os.environ.get("ADS_SESSION")
    if not token:
        print("ERROR: Provide --token or set the ADS_SESSION environment variable.")
        return 1

    if args.runs < 1:
        print("ERROR: --runs must be >= 1")
        return 1

    url = build_url(args.base_url, args)

    print(f"Benchmarking: {url}")
    print(f"Runs: {args.runs}")

    results: list[dict] = []
    elapsed_times: list[float] = []

    for i in range(1, args.runs + 1):
        try:
            result = fetch_once(url, token)
        except urllib.error.HTTPError as exc:
            print(f"\nRun {i}: HTTP {exc.code} — {exc.reason}")
            return 1
        except urllib.error.URLError as exc:
            print(f"\nRun {i}: Request failed — {exc.reason}")
            return 1

        print_run_summary(i, result, url)
        elapsed_times.append(result["elapsed_seconds"])
        results.append(result)

    if args.runs > 1:
        print("\n── Timing summary ───────────────────────────────────")
        print(f"  min:  {min(elapsed_times)} s")
        print(f"  avg:  {round(statistics.mean(elapsed_times), 4)} s")
        print(f"  max:  {max(elapsed_times)} s")

    if args.output:
        report = {
            "url": url,
            "runs": args.runs,
            "results": results,
            "timing": {
                "min_seconds": min(elapsed_times),
                "avg_seconds": round(statistics.mean(elapsed_times), 4),
                "max_seconds": max(elapsed_times),
            } if args.runs > 1 else {
                "elapsed_seconds": elapsed_times[0],
            },
        }
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nReport written to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
