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

API_URL = os.environ.get("SWAPCARD_API_URL", "https://api.swapcard.com/graphql")
API_KEY = os.environ.get("SWAPCARD_API_KEY")
EVENT_ID = os.environ.get("SWAPCARD_EVENT_ID")
INDUSTRY_FIELD_NAME = os.environ.get("INDUSTRY_FIELD_NAME", "Industry")
TARGET_GROUPS = [
    g.strip()
    for g in os.environ.get(
        "TARGET_GROUPS", "Attendees,Speakers,Press,Speaker | Press"
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
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
        return data["data"]
    raise RuntimeError("Exhausted retries on Swapcard API")


# ---------- Discovery (run once to verify config) ----------

DISCOVER_QUERY = """
query Discover($eventId: ID!) {
  event(id: $eventId) {
    id
    title
    groups {
      id
      name
    }
    peopleFields: customFields(type: EVENT_PERSON) {
      id
      name
      kind
    }
  }
}
"""


def discover() -> None:
    """Print groups and custom fields so the user can verify config."""
    print(f"Connecting to {API_URL} for event {EVENT_ID}...")
    data = gql(DISCOVER_QUERY, {"eventId": EVENT_ID})
    event = data["event"]
    print(f"\nEvent: {event['title']} ({event['id']})\n")

    print("=== Groups ===")
    for g in event["groups"]:
        in_target = "  ← TARGET" if g["name"] in TARGET_GROUPS else ""
        print(f"  {g['id']}  {g['name']}{in_target}")

    print("\n=== Custom fields on EventPerson ===")
    for f in event["peopleFields"]:
        in_target = "  ← INDUSTRY" if f["name"] == INDUSTRY_FIELD_NAME else ""
        print(f"  {f['id']}  [{f['kind']}]  {f['name']}{in_target}")

    print(
        "\nIf the TARGET groups or INDUSTRY field aren't marked above, "
        "set TARGET_GROUPS / INDUSTRY_FIELD_NAME env vars to match exactly."
    )


# ---------- People extraction ----------

# NOTE: Swapcard's exact field shape may need a small tweak depending on your
# event's custom field types. If this query errors, run --discover and tell me
# what `kind` your industry field is and we'll adjust the inline fragments.
PEOPLE_QUERY = """
query People($eventId: ID!, $groupIds: [ID!], $cursor: String) {
  event(id: $eventId) {
    people(first: 100, after: $cursor, groupIds: $groupIds) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        organization
        fields {
          definition { id name kind }
          ... on EventPersonCustomFieldText { textValue: value }
          ... on EventPersonCustomFieldSingleSelect { singleValue: value }
          ... on EventPersonCustomFieldMultiSelect { multiValues: values }
        }
      }
    }
  }
}
"""


def resolve_group_ids(target_names: list[str]) -> list[str]:
    data = gql(DISCOVER_QUERY, {"eventId": EVENT_ID})
    by_name = {g["name"]: g["id"] for g in data["event"]["groups"]}
    missing = [n for n in target_names if n not in by_name]
    if missing:
        sys.exit(
            f"ERROR: target group(s) not found in event: {missing}\n"
            f"Available: {list(by_name)}"
        )
    ids = [by_name[n] for n in target_names]
    print(f"Resolved {len(ids)} target groups: {target_names}")
    return ids


def extract_industry(fields: list[dict]) -> list[str]:
    """Pull industry value(s) from a person's custom fields."""
    for f in fields:
        if (f.get("definition") or {}).get("name") != INDUSTRY_FIELD_NAME:
            continue
        if "multiValues" in f and f["multiValues"]:
            return [v for v in f["multiValues"] if v]
        if "singleValue" in f and f["singleValue"]:
            return [f["singleValue"]]
        if "textValue" in f and f["textValue"]:
            return [f["textValue"]]
    return []


def fetch_all_people(group_ids: list[str]) -> list[dict]:
    people = []
    cursor = None
    page = 0
    while True:
        page += 1
        data = gql(
            PEOPLE_QUERY,
            {"eventId": EVENT_ID, "groupIds": group_ids, "cursor": cursor},
        )
        block = data["event"]["people"]
        people.extend(block["nodes"])
        print(f"  page {page}: +{len(block['nodes'])} (total {len(people)})")
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    return people


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
        help="list groups and custom fields, then exit",
    )
    args = parser.parse_args()

    if args.discover:
        discover()
        return

    print(f"Target groups: {TARGET_GROUPS}")
    print(f"Industry field: {INDUSTRY_FIELD_NAME!r}")

    group_ids = resolve_group_ids(TARGET_GROUPS)

    print("\nFetching people...")
    people = fetch_all_people(group_ids)
    print(f"Total people: {len(people)}")

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

        industries = extract_industry(p.get("fields") or [])

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
