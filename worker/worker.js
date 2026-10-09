// Ai4 attendee snapshot — server-side rendering Worker
// =====================================================
// The /who-attends/ block renders its company list client-side from
// output/snapshot.json, so crawlers and LLMs that don't run JS see an empty
// list. This Worker runs on ai4.com/who-attends* and, as the WordPress page
// streams through, writes the company list into the page as real HTML
// (Cloudflare HTMLRewriter). The block's JS then reuses it.
//
// Everyone gets the same HTML (no bot detection). If anything fails, the
// page is passed through untouched, i.e. exactly today's behaviour.
//
// Routes (relative to PAGE_PATH, default "/who-attends/"):
//   /who-attends/     → WordPress page + pre-rendered company list
//   /who-attends.md   → plain-markdown list (company, industries, Fortune 500)
// Everything else passes through to WordPress.

const DEFAULT_SNAPSHOT_URL =
  "https://cdn.jsdelivr.net/gh/ai4conferences/ai4-attendee-snapshot@main/output/snapshot.json";
const SHOW_ALL_LABEL = "All Industries";
const MEMO_MS = 10 * 60 * 1000;   // keep parsed data + rendered HTML in memory this long
const FETCH_TIMEOUT_MS = 4000;

let memo = null; // { at, url, data, listHtml, md }

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") return originFetch(request, env);
    const url = new URL(request.url);
    const page = pagePath(env);
    let res = null;
    try {
      if (url.pathname === page) res = await renderPage(request, env, ctx);
      else if (url.pathname === page.replace(/\/$/, "") + ".md") res = await renderMarkdown(request, env, ctx);
    } catch (err) {
      console.error("[snapshot-ssr] render failed, passing through:", (err && err.stack) || err);
    }
    if (!res) res = await originFetch(request, env);
    if (env.NOINDEX === "true") {
      res = new Response(res.body, res);
      res.headers.set("X-Robots-Tag", "noindex, nofollow");
    }
    return res;
  },
};

// ---------- data ----------

async function getSnapshot(env, ctx) {
  const src = env.SNAPSHOT_URL || DEFAULT_SNAPSHOT_URL;
  if (memo && memo.url === src && Date.now() - memo.at < MEMO_MS) return memo;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    // Cached at the edge so most requests never leave Cloudflare.
    const r = await fetch(src, { signal: ctrl.signal, cf: { cacheTtl: 900, cacheEverything: true } });
    if (!r.ok) throw new Error(`snapshot HTTP ${r.status}`);
    const data = await r.json();
    if (!data || !Array.isArray(data.companies)) throw new Error("snapshot has no companies");
    const ordered = orderCompanies(data.companies);
    memo = {
      at: Date.now(),
      url: src,
      data,
      count: ordered.length,
      listHtml: ordered.map((c) => `<li>${esc(c.name)}</li>`).join(""),
      md: null,
      ordered,
    };
    return memo;
  } catch (err) {
    console.error("[snapshot-ssr] snapshot fetch failed:", err && err.message);
    // Serve the last good copy if we have one, however old.
    return memo && memo.url === src ? memo : null;
  } finally {
    clearTimeout(timer);
  }
}

// Must match snapshot-block.js exactly ("All Industries" view):
// Fortune 500 first, then everyone else, each alphabetical (case-insensitive).
function orderCompanies(companies) {
  const cmp = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  return companies.filter((c) => c.fortune500).sort(cmp)
    .concat(companies.filter((c) => !c.fortune500).sort(cmp));
}

// ---------- page ----------

async function renderPage(request, env, ctx) {
  const snap = await getSnapshot(env, ctx);
  if (!snap || !snap.count) return null;

  const origin = await originFetch(request, env);
  if (!origin.ok || !(origin.headers.get("content-type") || "").includes("text/html")) return origin;

  const mdPath = pagePath(env).replace(/\/$/, "") + ".md";
  let rw = new HTMLRewriter()
    .on("#ai4-snapshot", {
      element(el) {
        el.setAttribute("data-ssr", String(snap.count));
        el.setAttribute("data-state", "ready"); // hides "Loading…" — the list is already here
      },
    })
    .on("#ai4-snapshot .ai4-companies", {
      element(el) { el.setInnerContent(snap.listHtml, { html: true }); },
    })
    .on("head", {
      element(el) {
        el.append(`<link rel="alternate" type="text/markdown" href="${esc(mdPath)}" title="Companies attending (markdown)">`, { html: true });
      },
    });

  // Staging only: load the block's JS from a branch/tag instead of @main so
  // new JS can be tested before it's merged.
  if (env.PREVIEW_JS_REF) {
    rw = rw.on('script[src*="ai4-attendee-snapshot@"]', {
      element(el) {
        const src = el.getAttribute("src") || "";
        el.setAttribute("src", src.replace(/ai4-attendee-snapshot@[^/]+/, "ai4-attendee-snapshot@" + env.PREVIEW_JS_REF));
      },
    });
  }

  const out = new Response(rw.transform(origin).body, origin);
  out.headers.delete("etag");
  out.headers.delete("last-modified");
  out.headers.delete("content-length");
  out.headers.set("X-Snapshot-SSR", String(snap.count));
  return out;
}

// ---------- markdown for LLMs ----------

async function renderMarkdown(request, env, ctx) {
  const snap = await getSnapshot(env, ctx);
  if (!snap) return null;
  if (!snap.md) {
    const d = snap.data;
    const pageUrl = siteOrigin(request, env) + pagePath(env);
    const updated = d.updated_at ? new Date(d.updated_at).toISOString().slice(0, 10) : "";
    const lines = [
      "# Companies attending Ai4",
      "",
      `${fmt(d.total_companies ?? snap.count)} companies and ${fmt(d.total_attendees)} attendees (attendees, speakers and press), updated ${updated}.`,
      `Canonical page: ${pageUrl}`,
      "",
      `## Fortune 500 companies (${fmt(snap.ordered.filter((c) => c.fortune500).length)})`,
      "",
    ];
    for (const c of snap.ordered) if (c.fortune500) lines.push(`- ${c.name}`);
    lines.push("", "## All companies", "", "Format: company — industries (Fortune 500 marked).", "");
    for (const c of snap.ordered) {
      const inds = (c.industries || []).filter((i) => i !== SHOW_ALL_LABEL);
      lines.push(`- ${c.name}${inds.length ? " — " + inds.join(", ") : ""}${c.fortune500 ? " · Fortune 500" : ""}`);
    }
    snap.md = lines.join("\n") + "\n";
  }
  return new Response(snap.md, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=1800",
      "Link": `<${siteOrigin(request, env) + pagePath(env)}>; rel="canonical"`,
      "X-Snapshot-SSR": "markdown",
    },
  });
}

// ---------- helpers ----------

function pagePath(env) {
  let p = env.PAGE_PATH || "/who-attends/";
  if (!p.startsWith("/")) p = "/" + p;
  if (!p.endsWith("/")) p += "/";
  return p;
}
function siteOrigin(request, env) {
  return (env.SITE_ORIGIN || new URL(request.url).origin).replace(/\/$/, "");
}
// In production this is fetch(request): a Worker's request to its own zone
// goes to the origin (WordPress), not back into the Worker. ORIGIN_OVERRIDE
// is for staging on workers.dev, which proxies the live site.
function originFetch(request, env) {
  if (env.ORIGIN_OVERRIDE) {
    const u = new URL(request.url);
    const o = new URL(env.ORIGIN_OVERRIDE);
    u.protocol = o.protocol;
    u.host = o.host;
    return fetch(new Request(u.toString(), request));
  }
  return fetch(request);
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}
function fmt(n) { return typeof n === "number" ? n.toLocaleString("en-US") : String(n ?? ""); }
