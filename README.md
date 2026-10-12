# Ai4 attendee snapshot

Auto-updating "snapshot of companies attending" for [ai4.io/who-attends/](https://ai4.io/who-attends/).
Pulls attendees from Swapcard once a week, dedupes by company, tags Fortune 500, and publishes a JSON file. The page on ai4.io fetches that JSON and renders the list with industry filters.

```
Swapcard ──(weekly GH Actions cron)──> output/snapshot.json ──(jsDelivr CDN)──> ai4.io WordPress block
                                                     └──> SSR Worker (ai4.com/who-attends*) ──> company list pre-rendered in the HTML
```

## First-time setup

### 1. Push this repo to GitHub
A public repo is simplest because jsDelivr serves the JSON for free with CDN caching. (If it has to be private, swap jsDelivr for GitHub Pages with the repo set to public Pages, or for an S3/Cloudflare R2 bucket.)

### 2. Configure GitHub secrets & variables
In **Settings → Secrets and variables → Actions**:

**Secrets:**
- `SWAPCARD_API_KEY` — your organizer API key
- `SWAPCARD_EVENT_ID` — the event ID (the base64-looking string)

**Variables** (optional, only if defaults don't match):
- `INDUSTRY_FIELD_NAME` — defaults to `Industry`
- `TARGET_GROUPS` — defaults to `Attendees,Speakers,Press,Speaker | Press`

### 3. Verify config locally
Before relying on the cron, run the discover step to confirm the group names and custom field name match exactly what's in your Swapcard event:

```bash
cd scripts
pip install -r requirements.txt
export SWAPCARD_API_KEY=...
export SWAPCARD_EVENT_ID=...
python sync.py --discover
```

It'll print all groups and EventPerson custom fields, marking which ones it would target. If anything is mismatched, update the env vars (or the GitHub variables) so the names line up exactly.

### 4. Run a real sync locally
```bash
python sync.py
```
This writes `output/snapshot.json`. Commit it so the WordPress block has something to fetch immediately.

### 5. Wire up the WordPress block
1. Open `wordpress/snapshot-block.html`
2. Replace `YOUR_GH_USER/YOUR_REPO` in `SNAPSHOT_URL` with the actual repo path
3. Adjust the CSS variables at the top of `<style>` to match ai4.io's color theme
4. Paste the whole thing into a **Custom HTML** block on `/who-attends/` where the snapshot should appear

### 6. Enable the cron
The workflow at `.github/workflows/sync.yml` runs Mondays 14:00 UTC. You can also trigger it manually from the Actions tab.

## Server-side rendering (SEO / LLM readability)

The block builds its list in the browser, so crawlers and LLMs that don't run
JavaScript saw an empty list. A small Cloudflare Worker (`worker/`) runs on
`ai4.com/who-attends*` and writes the full company list into the WordPress
page as real HTML while it streams through. The block's JS then reuses that
list instead of rebuilding it; filters work as before.

| URL | What it serves |
|---|---|
| `/who-attends/` | WordPress page with every company pre-rendered (header `X-Snapshot-SSR: <count>`) |
| `/who-attends.md` | Plain-markdown list for LLMs: company, industries, Fortune 500 |

- Same HTML for everyone (no bot detection).
- Reads the same `snapshot.json` from jsDelivr (edge-cached 15 min, in memory 10 min). No secrets or KV.
- If the snapshot can't be fetched, the page passes through untouched. If the JSON fails in the browser, the pre-rendered list stays.
- Works with both the old and new `snapshot-block.js`, so deploy order doesn't matter.

### Deploy

```bash
cd worker
npx wrangler deploy --env staging      # preview: https://ai4-attendee-snapshot-ssr-staging.<subdomain>.workers.dev/who-attends/
npx wrangler deploy --env production   # adds the ai4.com/who-attends* route
```

Staging proxies the live ai4.com page, is `noindex`, and loads `snapshot-block.js`
from the branch in `PREVIEW_JS_REF` (default `ssr-hydration`) so JS changes can
be tested before merging.

Check production with `curl -sI https://ai4.com/who-attends/` → `X-Snapshot-SSR: <count>`.

After merging a change to `output/snapshot-block.js`, jsDelivr can serve the
old copy for up to 12 hours. To refresh it immediately, open
https://purge.jsdelivr.net/gh/ai4conferences/ai4-attendee-snapshot@main/output/snapshot-block.js

## File layout

```
scripts/sync.py            # Swapcard → snapshot.json
scripts/requirements.txt   # just `requests`
data/fortune500.json       # Fortune 500 company names (starter list — replace with canonical)
data/name_aliases.json     # Maps Swapcard org variants → canonical names (e.g. "JPMC" → "JPMorgan Chase")
output/snapshot.json       # The published JSON (generated)
wordpress/snapshot-block.html  # Paste into a Custom HTML block on /who-attends/
output/snapshot-block.js   # The block's JS (served via jsDelivr)
worker/worker.js           # SSR Worker for ai4.com/who-attends*
worker/wrangler.toml       # Worker config (staging + production)
.github/workflows/sync.yml # Weekly cron
```

## Maintenance

- **Fortune 500 list**: `data/fortune500.json` ships with a starter list — replace with the canonical 2025/2026 list when you have it. Names are matched after normalizing (lowercasing, stripping `Inc/LLC/Corp/Co/Ltd/&/.`), so "JPMorgan Chase & Co." in Swapcard matches "JPMorgan Chase" in the list.
- **Aliases**: if attendees enter their company name oddly (e.g. "JPMC", "Eli Lilly Co"), add a mapping in `data/name_aliases.json` so they collapse onto one canonical row.
- **Adding/renaming industries**: nothing to change here — whatever industry values come back from Swapcard are what shows in the filter buttons.
- **Watch the workflow**: if Swapcard changes their schema, the run will fail loudly and you'll get a GitHub email. The `PEOPLE_QUERY` in `sync.py` is the most likely thing to need a small tweak.

## How the JSON is structured

```json
{
  "updated_at": "2026-05-27T14:00:12+00:00",
  "total_companies": 2543,
  "total_attendees": 9871,
  "industries": ["AI & Data Services", "Education", ...],
  "companies": [
    {"name": "JPMorgan Chase & Co.", "industries": ["Financial Services"], "fortune500": true, "attendee_count": 14},
    ...
  ]
}
```

Companies are sorted alphabetically in the JSON; the WordPress JS handles "Fortune 500 first" at render time.
