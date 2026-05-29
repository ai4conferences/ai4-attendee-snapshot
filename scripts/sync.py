"""
Ai4 attendee snapshot sync.

Pulls attendees from Swapcard's GraphQL Content API, dedupes by company,
attaches industry from a custom field, marks Fortune 500 matches, and writes
output/snapshot.json. Designed to be run on a weekly cron by GitHub Actions.

Required environment variables:
  SWAPCARD_API_KEY        Organizer API key
  SWAPCARD_EVENT_ID       Event ID (the base64-looking thing)

Optional environment variables:
  INDUSTRY_FIELD_NAME     Name of the custom field holding industry
                          (default: "Industry")
  TARGET_GROUPS           Comma-separated group names to include
                          (default: "Attendees,Speakers,Press,Speaker | Press")
  SWAPCARD_API_URL        Override the API endpoint
                          (default: https://api.swapcard.com/graphql)

Usage:
  python scripts/sync.py                # full sync, writes output/snapshot.json
  python scripts/sync.py --discover     # just list groups & custom fields and exit
                                        # (run this first to verify config)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_PATH = ROOT / "output" / "snapshot.json"

# NOTE: `or` (not the dict .get default) so that an env var that is SET BUT
# EMPTY — which is what an undefined GitHub `vars.X` expands to — still falls
# back to the default instead of becoming "".
API_URL = (
    os.environ.get("SWAPCARD_API_URL")
    or "https://developer.swapcard.com/event-admin/graphql"
)
API_KEY = os.environ.get("SWAPCARD_API_KEY")
EVENT_ID = os.environ.get("SWAPCARD_EVENT_ID")
INDUSTRY_FIELD_NAME = os.environ.get("INDUSTRY_FIELD_NAME") or "Industry"
TARGET_GROUPS = [
    g.strip()
    for g in (
        os.environ.get("TARGET_GROUPS")
        or "Attendees,Speakers,Press,Speaker | Press"
    ).split(",")
    if g.strip()
]


# ---------- GraphQL helpers ----------

def gql(query: str, variables: dict | None = None) -> dict[str, Any]:
    """Execute a GraphQL query against Swapcard. Retries on 429/5xx."""
    if not API_KEY or not EVENT_ID:
        sys.exit("ERROR: SWAPCARD_API_KEY and SWAPCARD_EVENT_ID must be set.")

    headers = {"Authorization": API_KEY, "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(5):
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code in (429, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  ! HTTP {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        # On any error, surface Swapcard's actual message — a 400 on GraphQL
        # almost always carries a precise "field X doesn't exist" explanation.
        if not resp.ok:
            print(f"\n!! HTTP {resp.status_code} from Swapcard. Response body:")
            print(resp.text[:4000])
            resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(
                "GraphQL errors:\n" + json.dumps(data["errors"], indent=2)
            )
        return data["data"]
    raise RuntimeError("Exhausted retries on Swapcard API")


# ---------- Discovery (optional: lists field definitions & confirms config) ----------

DISCOVER_QUERY = """
query Discover($eventId: ID!) {
  event(id: $eventId) {
    id
    title
    fieldDefinitions(target: PEOPLE) {
      __typename
      ... on SelectFieldDefinition { id name }
      ... on MultipleSelectFieldDefinition { id name }
      ... on TextFieldDefinition { id name }
      ... on LongTextFieldDefinition { id name }
      ... on NumberFieldDefinition { id name }
      ... on UrlFieldDefinition { id name }
      ... on MediaFieldDefinition { id name }
    }
  }
}
"""


def discover() -> None:
    """Print custom field definitions so the user can verify the industry field."""
    print(f"Connecting to {API_URL} for event {EVENT_ID}...")
    data = gql(DISCOVER_QUERY, {"eventId": EVENT_ID})
    event = data["event"]
    print(f"\nEvent: {event.get('title')} ({event.get('id')})\n")

    print("=== Custom fields on People ===")
    for f in event.get("fieldDefinitions") or []:
        name = f.get("name")
        mark = "  ← INDUSTRY" if name == INDUSTRY_FIELD_NAME else ""
        print(f"  [{f.get('__typename')}]  {name}{mark}")

    print(
        "\nGroups are read per-person from the people query, so they're not "
        "listed here. If the INDUSTRY field isn't marked above, set "
        "INDUSTRY_FIELD_NAME to match one of the names shown."
    )


# ---------- People extraction ----------

# Rooted at eventPerson with cursor pagination (Swapcard's documented shape).
# Group membership comes back on each person, so we filter by group NAME in
# Python rather than resolving group IDs. Custom field values live under
# withEvent(eventId).fields; we read SELECT / MULTI-SELECT industry values.
PEOPLE_QUERY = """
query People($eventId: ID!, $cursor: CursorPaginationInput) {
  eventPerson(eventId: $eventId, cursor: $cursor) {
    pageInfo { hasNextPage endCursor }
    totalCount
    nodes {
      id
      organization
      groups { id name }
      withEvent(eventId: $eventId) {
        fields {
          __typename
          ... on SelectField {
            translations { value language }
            definition { id translations { name language } }
          }
          ... on MultipleSelectField {
            translations { value language }
            definition { id translations { name language } }
          }
          ... on NumberField {
            numValue: value
            definition { id translations { name language } }
          }
        }
      }
    }
  }
}
"""


def _def_name(field: dict) -> str:
    """Get a custom field's name from its (possibly translated) definition."""
    definition = field.get("definition") or {}
    translations = definition.get("translations") or []
    # Prefer English, else the first available translation.
    en = next((t for t in translations if t.get("language") == "en"), None)
    chosen = en or (translations[0] if translations else None)
    return (chosen or {}).get("name", "") or ""


def _field_values(field: dict) -> list[str]:
    """Get the selected value(s) of a select/multi-select custom field."""
    translations = field.get("translations") or []
    en = [t["value"] for t in translations if t.get("language") == "en" and t.get("value")]
    if en:
        return en
    return [t["value"] for t in translations if t.get("value")]


def extract_industry(person_with_event: dict | None) -> list[str]:
    """Pull raw industry value(s) from a person's event-specific custom fields."""
    if not person_with_event:
        return []
    out: list[str] = []
    for f in person_with_event.get("fields") or []:
        if _def_name(f).strip().lower() == INDUSTRY_FIELD_NAME.strip().lower():
            out.extend(_field_values(f))
    # De-dupe, preserve order
    seen, result = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


# Parse Swapcard Company Size values, returning the UPPER bound of the range.
# Handles formats with comma separators and trailing locale text:
#   "11-50 employees"          -> 50
#   "1,001-5,000 employees"    -> 5000
#   "10,001+ employees"        -> very large (effectively unbounded)
#   "51-100 empleados"         -> 100  (Spanish)
#   "101-250"                  -> 250
#   "42"                       -> 42
# Used to decide if a company qualifies as a Startup.
SIZE_DIGITS_RE = re.compile(r"\d+")
SIZE_PLUS_RE = re.compile(r"(\d+)\s*\+")
SIZE_RANGE_RE = re.compile(r"(\d+)\s*[-\u2013\u2014]\s*(\d+)")


def parse_company_size_upper(raw: str | None) -> int | None:
    if not raw:
        return None
    s = str(raw).replace(",", "").strip()
    if not s:
        return None
    # "10,001+ employees" -> open-ended top bucket
    if SIZE_PLUS_RE.search(s):
        return 10**9
    # "1-10 employees" or "101-250" -> use the second number
    m = SIZE_RANGE_RE.search(s)
    if m:
        return int(m.group(2))
    # plain "42"
    m = SIZE_DIGITS_RE.search(s)
    if m:
        return int(m.group(0))
    return None


def extract_company_size(person_with_event: dict | None) -> str | None:
    """Return the raw Company Size value (e.g. '11-50') or None."""
    if not person_with_event:
        return None
    for f in person_with_event.get("fields") or []:
        if _def_name(f).strip().lower() != "company size":
            continue
        vals = _field_values(f)
        if vals:
            return vals[0]
        # NumberField fallback
        if f.get("numValue") is not None:
            return str(f["numValue"])
    return None


def person_groups(node: dict) -> list[str]:
    return [g.get("name", "") for g in (node.get("groups") or [])]


def fetch_all_people() -> list[dict]:
    """Page through every person; filter to target groups in Python."""
    target = {g.lower() for g in TARGET_GROUPS}
    kept: list[dict] = []
    seen_field_types: set[str] = set()
    seen_field_names: set[str] = set()
    cursor_after = None
    page = 0
    total_scanned = 0

    while True:
        page += 1
        cursor = {"first": 100}
        if cursor_after:
            cursor["after"] = cursor_after
        data = gql(PEOPLE_QUERY, {"eventId": EVENT_ID, "cursor": cursor})
        block = data["eventPerson"]
        nodes = block["nodes"]
        total_scanned += len(nodes)

        for node in nodes:
            # Collect diagnostics so the log tells us the real field names/types
            we = node.get("withEvent") or {}
            for f in we.get("fields") or []:
                seen_field_types.add(f.get("__typename", "?"))
                nm = _def_name(f)
                if nm:
                    seen_field_names.add(nm)
            # Keep people in any target group
            if target & {g.lower() for g in person_groups(node)}:
                kept.append(node)

        print(f"  page {page}: scanned {len(nodes)} (kept {len(kept)} so far)")
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor_after = block["pageInfo"]["endCursor"]

    print(f"Scanned {total_scanned} people total; kept {len(kept)} in target groups.")
    print(f"Custom field types seen on People: {sorted(seen_field_types) or 'none'}")
    print(f"Custom field names seen on People: {sorted(seen_field_names) or 'none'}")
    if INDUSTRY_FIELD_NAME not in seen_field_names:
        print(
            f"  ! WARNING: industry field {INDUSTRY_FIELD_NAME!r} not seen. "
            f"Industries may come back empty — check the names listed above."
        )
    return kept


# ---------- Company normalization & Fortune 500 matching ----------

SUFFIX_RE = re.compile(
    r"\b(?:inc|inc\.|incorporated|corp|corp\.|corporation|llc|l\.l\.c\.|"
    r"ltd|ltd\.|limited|co|co\.|company|companies|plc|gmbh|sa|s\.a\.|"
    r"holdings|holding|group|the)\b",
    re.IGNORECASE,
)

PUNCT_RE = re.compile(r"[^\w\s]")


def normalize(name: str) -> str:
    """Normalize a company name for matching: lowercase, strip suffixes & punct."""
    n = name.lower()
    n = PUNCT_RE.sub(" ", n)
    n = SUFFIX_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def compact_key(name: str) -> str:
    """Stricter key for auto-merge: normalized + all whitespace removed.
    Collapses 'JP Morgan', 'J.P. Morgan', and 'JPMorgan' onto 'jpmorgan'.
    Leaves longer names like 'JPMorgan Chase' separate (-> 'jpmorganchase')."""
    return re.sub(r"\s+", "", normalize(name))


def load_fortune500_index() -> dict:
    """Build a search index for fuzzy Fortune 500 matching.

    Matches if EITHER:
      1) Compact keys are identical ('JP Morgan' == 'J.P. Morgan' == 'JPMorgan'), OR
      2) One name's tokens are a subset of the other's, in either direction.
         So 'JPMorgan' matches F500 'JPMorgan Chase' ({jpmorgan} ⊆ {jpmorgan, chase})
         and 'JPMorgan Chase & Co.' also matches ({jpmorgan, chase} ⊆ {jpmorgan, chase}).

    Tokens shorter than 3 chars are dropped to avoid pathological matches.
    """
    path = DATA_DIR / "fortune500.json"
    if not path.exists():
        print("! data/fortune500.json missing — no Fortune 500 tagging will happen")
        return {"compact_keys": set(), "token_sets": []}
    names = json.loads(path.read_text())
    compact_keys: set[str] = set()
    token_sets: list[tuple[str, frozenset[str]]] = []
    for raw in names:
        if not isinstance(raw, str):
            continue
        ck = compact_key(raw)
        if ck:
            compact_keys.add(ck)
        toks = frozenset(t for t in normalize(raw).split() if len(t) > 2)
        if toks:
            token_sets.append((raw, toks))
    return {"compact_keys": compact_keys, "token_sets": token_sets}


def is_fortune500(name: str, index: dict) -> bool:
    """Check whether a company matches any F500 entry.

    Strict rules (no fuzzy single-token matching to avoid false positives):
      1. Exact compact-key match after normalize+suffix-strip (handles 'Apple',
         'Apple Inc.', 'JPMorgan Chase & Co.' → 'JPMorgan Chase', etc.)
      2. Multi-token subset: the shorter side must have AT LEAST 2 meaningful
         tokens (after removing stopwords) and be a subset of the longer side.
         Prevents '37 Partners' matching 'Enterprise Products Partners' via
         the single 'partners' token.

    Single-token canonicals (e.g. 'Cisco' for 'Cisco Systems') are handled by
    listing both forms in data/fortune500.json, or by aliasing the raw value
    to a name that exact-matches an F500 entry.
    """
    STOPWORDS = {"the", "and", "for", "inc", "llc", "ltd", "corp", "group",
                 "company", "companies", "holdings", "international", "global",
                 "partners", "technologies", "services", "solutions", "systems",
                 "communications", "industries", "products", "enterprise",
                 "networks", "healthcare", "health", "financial", "capital",
                 "energy", "media", "consulting"}

    def meaningful_tokens(n: str) -> frozenset[str]:
        return frozenset(t for t in normalize(n).split()
                         if len(t) > 2 and t not in STOPWORDS)

    ck = compact_key(name)
    if not ck:
        return False

    # Rule 1: exact compact key match
    if ck in index["compact_keys"]:
        return True

    # Rule 2: multi-token subset (require 2+ meaningful tokens on shorter side)
    company_tokens = meaningful_tokens(name)
    if len(company_tokens) < 2:
        # Single-token names ONLY match via Rule 1 (exact compact). If you want
        # "Cisco" to match the F500, add "Cisco" to data/fortune500.json.
        return False

    for _f500_name, f500_tokens_all in index["token_sets"]:
        f500_tokens = frozenset(t for t in f500_tokens_all if t not in STOPWORDS)
        if len(f500_tokens) < 2:
            continue
        shorter, longer = (
            (company_tokens, f500_tokens)
            if len(company_tokens) <= len(f500_tokens)
            else (f500_tokens, company_tokens)
        )
        if shorter <= longer:
            return True

    return False


def load_aliases() -> dict[str, str]:
    """Map of raw Swapcard org name -> canonical display name. Case-insensitive lookup."""
    path = DATA_DIR / "name_aliases.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.startswith("_"):  # comment keys
            continue
        out[k.strip().lower()] = v
    return out


def load_industry_buckets() -> tuple[dict[str, str | None], list[str], int]:
    """Load bucket mapping, ordered bucket list, and startup size threshold."""
    path = DATA_DIR / "industry_buckets.json"
    if not path.exists():
        print("! data/industry_buckets.json missing — using raw Swapcard industries")
        return ({}, [], 0)
    config = json.loads(path.read_text())
    mappings = config.get("mappings") or {}
    order = config.get("bucket_order") or []
    startup_max = int(config.get("startup_max_size") or 0)
    # Case-insensitive lookup table
    mappings_ci = {k.strip().lower(): v for k, v in mappings.items()}
    return (mappings_ci, order, startup_max)


def apply_buckets(
    raw_industries: list[str],
    bucket_map: dict[str, str | None],
    seen_unmapped: set[str],
) -> set[str]:
    """Map raw Swapcard industries to display buckets. Unknown values -> 'Other'."""
    out: set[str] = set()
    for raw in raw_industries:
        key = raw.strip().lower()
        if key in bucket_map:
            bucket = bucket_map[key]
            if bucket:  # None means "no specific bucket"
                out.add(bucket)
        else:
            seen_unmapped.add(raw)
            out.add("Other")
    return out


# ---------- Main ----------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--discover",
        action="store_true",
        help="list People custom field definitions, then exit",
    )
    args = parser.parse_args()

    if args.discover:
        discover()
        return

    print(f"Target groups: {TARGET_GROUPS}")
    print(f"Industry field: {INDUSTRY_FIELD_NAME!r}")

    print("\nFetching people...")
    people = fetch_all_people()
    print(f"People in target groups: {len(people)}")

    aliases = load_aliases()
    fortune500_index = load_fortune500_index()
    bucket_map, bucket_order, startup_max = load_industry_buckets()
    seen_unmapped: set[str] = set()
    size_value_counts: dict[str, int] = {}

    # Aggregate by compact_key: 'JP Morgan', 'J.P. Morgan', 'JPMorgan' all merge.
    companies: dict[str, dict] = {}
    for p in people:
        raw_org = (p.get("organization") or "").strip()
        if not raw_org:
            continue
        display = aliases.get(raw_org.strip().lower(), raw_org)
        key = compact_key(display)
        if not key:
            continue

        raw_industries = extract_industry(p.get("withEvent"))
        buckets = (
            apply_buckets(raw_industries, bucket_map, seen_unmapped)
            if bucket_map else set(raw_industries)
        )

        # Company Size → Startups bucket (in addition to industry buckets)
        size = extract_company_size(p.get("withEvent"))
        if size:
            size_value_counts[size] = size_value_counts.get(size, 0) + 1
            upper = parse_company_size_upper(size)
            if startup_max and upper is not None and upper <= startup_max:
                buckets.add("Startups")

        bucket = companies.setdefault(
            key,
            {
                "name": display,
                "industries": set(),
                "fortune500": is_fortune500(display, fortune500_index),
                "_count": 0,
                "_size_votes": {},  # raw size -> count, for tie-breaking
            },
        )
        bucket["industries"].update(buckets)
        bucket["_count"] += 1
        if size:
            bucket["_size_votes"][size] = bucket["_size_votes"].get(size, 0) + 1
        # Prefer the longest display variant (often the most complete one)
        if len(display) > len(bucket["name"]):
            bucket["name"] = display

    # Second-pass startup tagging: if MOST attendees from a company report a
    # small size, tag the company as Startup even if some individuals didn't
    # fill in the field. Uses the modal (most common) size.
    if startup_max:
        for c in companies.values():
            votes = c.get("_size_votes") or {}
            if not votes:
                continue
            modal_size = max(votes.items(), key=lambda kv: kv[1])[0]
            upper = parse_company_size_upper(modal_size)
            if upper is not None and upper <= startup_max:
                c["industries"].add("Startups")

    # Build company list (alphabetical by name)
    out_companies = []
    for c in companies.values():
        out_companies.append(
            {
                "name": c["name"],
                "industries": sorted(c["industries"]),
                "fortune500": c["fortune500"],
                "attendee_count": c["_count"],
            }
        )
    out_companies.sort(key=lambda c: c["name"].lower())

    # Industries list: use the configured bucket order (so the page always shows
    # the same 13 buttons in the same order). Filter to only buckets that have
    # at least one company — empty buttons just clutter the UI.
    present = {ind for c in out_companies for ind in c["industries"]}
    if bucket_order:
        industries_out = [b for b in bucket_order if b in present]
        # Any present industry not in bucket_order (shouldn't happen, but safe)
        extras = sorted(present - set(bucket_order))
        industries_out.extend(extras)
    else:
        industries_out = sorted(present)

    snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_companies": len(out_companies),
        "total_attendees": sum(c["attendee_count"] for c in out_companies),
        "industries": industries_out,
        "companies": out_companies,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))

    # ----- Dedup review report -----
    # Flag pairs of companies whose compact keys are similar enough to look
    # like duplicates but weren't auto-merged. The user reviews this file and
    # adds explicit aliases for any real dupes to data/name_aliases.json.
    review = build_dedup_review(out_companies)
    review_path = OUTPUT_PATH.parent / "dedup_review.json"
    review_path.write_text(json.dumps(review, indent=2, ensure_ascii=False))

    # ----- Summary -----
    print(
        f"\nWrote {OUTPUT_PATH.relative_to(ROOT)}: "
        f"{snapshot['total_companies']} companies across "
        f"{len(snapshot['industries'])} buckets"
    )
    print(f"Buckets used: {industries_out}")
    if seen_unmapped:
        print(
            f"\n! {len(seen_unmapped)} Swapcard industry value(s) had no bucket "
            f"mapping and were routed to 'Other':"
        )
        for v in sorted(seen_unmapped):
            print(f"    - {v}")
        print(
            "  Add these to data/industry_buckets.json mappings if you want "
            "them somewhere specific."
        )
    if size_value_counts:
        print(f"\nCompany Size value distribution (top 10):")
        for v, n in sorted(size_value_counts.items(), key=lambda kv: -kv[1])[:10]:
            upper = parse_company_size_upper(v)
            tag = (
                " (→ Startups eligible)"
                if startup_max and upper is not None and upper <= startup_max
                else ""
            )
            print(f"    {v!r}: {n}{tag}")
    print(
        f"\nDedup review written to {review_path.relative_to(ROOT)}: "
        f"{len(review['pairs'])} possible duplicate pair(s) flagged"
    )


def build_dedup_review(companies: list[dict]) -> dict:
    """Find pairs of companies whose names look like potential duplicates.

    Two heuristics, both run on compact (whitespace-stripped, suffix-stripped)
    keys:
      1) Substring: one company's key is contained in another (e.g.
         'jpmorgan' ⊂ 'jpmorganchase').
      2) Token-set Jaccard ≥ 0.5 over normalized (whitespace-preserving) tokens.

    Caps at 300 pairs to keep the file readable. The script never auto-merges
    based on these heuristics — they're suggestions for human review.
    """
    pairs: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    info = []
    for c in companies:
        name = c["name"]
        compact = compact_key(name)
        tokens = set(t for t in normalize(name).split() if len(t) > 1)
        if compact:
            info.append((name, compact, tokens))

    # Sort by compact length so substring checks are directional (short ⊂ long)
    info.sort(key=lambda x: len(x[1]))

    # Substring pass: for each short company, find longer ones it's contained in
    # Skip very short compacts (< 4 chars) — too many false positives like "ibm"
    for i, (name_a, ca, ta) in enumerate(info):
        if len(ca) < 4:
            continue
        for name_b, cb, tb in info[i + 1:]:
            if ca == cb:
                continue
            if ca in cb:
                k = tuple(sorted([name_a, name_b]))
                if k in seen_pairs:
                    continue
                seen_pairs.add(k)
                pairs.append({"a": name_a, "b": name_b, "reason": "substring"})
                if len(pairs) >= 300:
                    return {
                        "generated_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "note": (
                            "Possible duplicates for human review. Add any real "
                            "matches to data/name_aliases.json as "
                            '"raw_swapcard_name": "Canonical Name".'
                        ),
                        "pairs": pairs,
                    }

    # Token-Jaccard pass (skip if tokens already covered above)
    n = len(info)
    for i in range(n):
        name_a, _, ta = info[i]
        if not ta:
            continue
        for j in range(i + 1, n):
            name_b, _, tb = info[j]
            if not tb:
                continue
            inter = len(ta & tb)
            if inter == 0:
                continue
            union = len(ta | tb)
            jaccard = inter / union
            if jaccard >= 0.5 and ta != tb:
                k = tuple(sorted([name_a, name_b]))
                if k in seen_pairs:
                    continue
                seen_pairs.add(k)
                pairs.append(
                    {
                        "a": name_a,
                        "b": name_b,
                        "reason": f"token_overlap_{jaccard:.2f}",
                    }
                )
                if len(pairs) >= 300:
                    break
        if len(pairs) >= 300:
            break

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "Possible duplicates for human review. Add any real matches to "
            'data/name_aliases.json as "raw_swapcard_name": "Canonical Name".'
        ),
        "pairs": pairs,
    }


if __name__ == "__main__":
    main()
