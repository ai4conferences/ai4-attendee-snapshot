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
    """Pull industry value(s) from a person's event-specific custom fields."""
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


def load_fortune500() -> set[str]:
    path = DATA_DIR / "fortune500.json"
    if not path.exists():
        print("! data/fortune500.json missing — no Fortune 500 tagging will happen")
        return set()
    names = json.loads(path.read_text())
    return {normalize(n) for n in names}


def load_aliases() -> dict[str, str]:
    """Map of raw Swapcard org name -> canonical display name (optional)."""
    path = DATA_DIR / "name_aliases.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


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
    fortune500 = load_fortune500()

    # Aggregate: company name -> set of industries
    companies: dict[str, dict] = {}
    for p in people:
        raw_org = (p.get("organization") or "").strip()
        if not raw_org:
            continue
        display = aliases.get(raw_org, raw_org)
        key = normalize(display)
        if not key:
            continue

        industries = extract_industry(p.get("withEvent"))

        bucket = companies.setdefault(
            key,
            {
                "name": display,
                "industries": set(),
                "fortune500": key in fortune500,
                "_count": 0,
            },
        )
        bucket["industries"].update(industries)
        bucket["_count"] += 1
        # Prefer the longest display variant (often the most complete one)
        if len(display) > len(bucket["name"]):
            bucket["name"] = display

    # Build sorted output
    industry_set: set[str] = set()
    out_companies = []
    for c in companies.values():
        industry_set.update(c["industries"])
        out_companies.append(
            {
                "name": c["name"],
                "industries": sorted(c["industries"]),
                "fortune500": c["fortune500"],
                "attendee_count": c["_count"],
            }
        )

    # Alphabetical (case-insensitive); frontend handles F500-first display.
    out_companies.sort(key=lambda c: c["name"].lower())

    snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_companies": len(out_companies),
        "total_attendees": sum(c["attendee_count"] for c in out_companies),
        "industries": sorted(industry_set),
        "companies": out_companies,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    print(
        f"\nWrote {OUTPUT_PATH.relative_to(ROOT)}: "
        f"{snapshot['total_companies']} companies across {len(snapshot['industries'])} industries"
    )


if __name__ == "__main__":
    main()
