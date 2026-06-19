"use strict";
/*
 * EQ Auction Forge — browser app (live at wangel.github.io/EQ_Auction_Forge/app/).
 *
 * Pure client-side: files are read in the browser, processed in JS, and the INI
 * is written back locally. Nothing is uploaded. A faithful port of the desktop
 * app's core (EQ-Auction_Forge.py) — same DC2 link format, items.txt columns,
 * inventory-dump parsing, 255-char / 5-line / 12-button packing, idempotent
 * [Socials] merge, bulk pricing + undercut + recent-asks divergence, live krono
 * folding, the CHA vendor-trash band, and the threshold link/text split.
 *
 * Pricing calls tlp-auctions directly (CORS is enabled for the Pages origin);
 * localhost dev uses a same-origin proxy. Desktop-only: log monitor, watchlist, the
 * Settings dialog, EQ auto-install detection, the update check.
 */

// ----- constants (mirror the Python module globals) -----
const DC2 = "\x12";              // EQ item-link delimiter (hex 0x12)
const BUTTONS_PER_PAGE = 12;
const MAX_PAGE = 10;
const BULK_PRICE_LIMIT = 10;     // max item ids per /prices/bulk request
const DEFAULT_KRONO_RATE = 4000; // fallback fold rate if the API reports none
const RECENT_CHECK_FLOOR = 1000; // only items with a bulk median >= this get a recent-asks lookup
const RECENT_SALES_LIMIT = 8;    // recent postings pulled per recent-asks lookup
// NPC vendor buyback estimate (CHA-based). Port of vendor_multiplier/value_pp.
const VENDOR_SLOPE = 0.004, VENDOR_INTERCEPT = 0.584, VENDOR_CAP = 1 / 1.05;
// Apex host (valid cert). "/api" routes to a same-origin proxy for local dev.
const API_HOST = "https://tlp-auctions.com/api";
const SERVER = "Frostreaver";    // only TLP with tlp-auctions data; no server picker needed
const APP_VERSION = "1.4.6";
// Identify our traffic to the API owner: every request carries this so they can
// see/measure our usage and reach out if needed.
const CLIENT_TAG = `EQ-Auction-Forge/${APP_VERSION}`;

// Anonymous visit beacon -> our own Cloudflare Worker (records standard web-visit
// metadata only: page/referrer/event + server-side IP/country/UA — never any
// inventory/INI data). Set the deployed Worker subdomain below; until then the
// placeholder check keeps it inert. Fires on the production origin only.
const ANALYTICS_URL = "https://eqforge-analytics.wangel.workers.dev/collect";
function track(event) {
  try {
    if (location.hostname !== "wangel.github.io") return;     // production only
    if (ANALYTICS_URL.includes("YOUR-SUBDOMAIN")) return;     // not configured yet
    if (!navigator.sendBeacon) return;
    const body = JSON.stringify({
      event: event || "view",
      path: location.pathname,
      ref: document.referrer || "",
    });
    navigator.sendBeacon(ANALYTICS_URL, body);   // text/plain -> no CORS preflight
  } catch { /* analytics must never break the app */ }
}

// Built-in newbie/starter junk dropped from inventory loads (exact, lowercase).
const EXCLUDED_ITEMS = new Set([
  "backpack", "small box", "dagger", "skin of milk", "bread cakes",
  "gloomingdeep lantern", "ethereal dreamweave satchel", "dreamweave satchel",
]);

// ----- app state -----
const state = {
  db: null,          // { byId: Map<int,{link,price,name}>, byName: Map<name,link> }
  inventory: [],     // left pane: [{name, location, count, id}]
  auction: [],       // right pane (curated "to post"): [{name, location, count, id, price, _priceInput}]
  invSel: new Set(), // selected inventory row indices
  aucSel: new Set(), // selected auction row indices
  invSort: { col: null, desc: false },   // inventory column sort
  aucSort: { col: null, desc: false },   // auction column sort
  kronoRate: 0,      // last krono->plat rate seen (for the Recent Postings hint)
};

// ----- tiny DOM helpers -----
const $ = (id) => (typeof document !== "undefined" ? document.getElementById(id) : null);
// The always-visible status bar shows the latest line; tint green on success,
// red on trouble so completion/errors are unmissable.
function setStatus(msg) {
  const el = $("statusMsg");
  if (!el) return;
  el.textContent = msg;
  const m = msg.toLowerCase();
  el.className = /error|failed|fail|couldn|blocked|no item|nothing|no auction|no recent/.test(m) ? "err"
    : /complete|generated|added|saved|wrote|downloaded|cleared|removed|in place|priced at/.test(m) ? "ok" : "";
}
function log(msg) {
  const el = $("log");
  if (el) { el.textContent += msg + "\n"; el.scrollTop = el.scrollHeight; }   // hidden store
  setStatus(msg);   // mirror the latest line to the status bar
}

// =====================================================================
// Faithful ports of the desktop logic
// =====================================================================

// make_link: DC2 + hash + SPACE + name + DC2. Split hash from name using the
// known name (NOT hex detection — names starting A-F would break that). The
// space between hash and name is CRITICAL or the link won't render in-game.
function makeLink(itemlink, itemName) {
  if (itemlink.endsWith(itemName)) {
    const hashPart = itemlink.slice(0, itemlink.length - itemName.length);
    return `${DC2}${hashPart} ${itemName}${DC2}`;
  }
  return `${DC2}${itemlink}${DC2}`;
}

// Parse items.txt (pipe-delimited, header row with name/itemlink/id/price).
// Builds the id-keyed map (unambiguous) plus a first-row-wins name fallback.
function parseItemDb(text) {
  const byId = new Map();
  const byName = new Map();
  let dupNames = 0;
  const lines = text.split(/\r?\n/);
  if (!lines.length) return { byId, byName };
  const header = lines[0].split("|").map((h) => h.trim().toLowerCase());
  const iName = header.indexOf("name");
  const iLink = header.indexOf("itemlink");
  const iId = header.indexOf("id");
  const iPrice = header.indexOf("price");
  if (iName < 0 || iLink < 0) throw new Error("items.txt missing name/itemlink columns");
  for (let r = 1; r < lines.length; r++) {
    if (!lines[r]) continue;
    const p = lines[r].split("|");
    const name = (p[iName] || "").trim();
    const link = (p[iLink] || "").trim();
    if (!name || !link) continue;
    const idStr = iId >= 0 ? (p[iId] || "").trim() : "";
    const id = /^\d+$/.test(idStr) ? parseInt(idStr, 10) : null;
    const prStr = iPrice >= 0 ? (p[iPrice] || "").trim() : "";
    const price = /^\d+$/.test(prStr) ? parseInt(prStr, 10) : null;
    if (id !== null) byId.set(id, { link, price, name });
    if (byName.has(name)) dupNames++;
    else byName.set(name, link);
  }
  if (dupNames) log(`  (${dupNames} duplicate item names in DB — id matching disambiguates)`);
  return { byId, byName };
}

// Parse an EQ /outputfile inventory dump (tab-separated). Combine stacks /
// duplicate slots by id (fall back to name when there's no id column), drop
// excluded junk and the phantom empty/KeyRing rows.
function parseInventory(text) {
  const combined = new Map();
  const order = [];
  const rows = text.split(/\r?\n/);
  let header = null;
  let ni = 1, li = 0, ci = null, ii = null, si = null;
  for (const raw of rows) {
    const line = raw.replace(/[\r\n]+$/, "");
    if (!line) continue;
    const parts = line.split("\t");
    if (header === null) {
      header = parts.map((p) => p.trim().toLowerCase());
      ni = header.indexOf("name"); if (ni < 0) ni = 1;
      li = header.indexOf("location"); if (li < 0) li = 0;
      ci = header.indexOf("count"); if (ci < 0) ci = null;
      ii = header.indexOf("id"); if (ii < 0) ii = null;
      si = header.indexOf("slots"); if (si < 0) si = null;
      continue;
    }
    if (parts.length < 3) continue;
    const name = (parts[ni] || "").trim().replace(/\*+$/, "");
    const loc = (parts[li] || "").trim();
    const lower = name.toLowerCase();
    if (lower === "" || lower === "empty" || lower === "name") continue;
    if (EXCLUDED_ITEMS.has(lower)) continue;
    // Drop bags you're CARRYING: a container (Slots>0, i.e. capacity for general
    // inventory) sitting directly in a top-level General slot ("General 3", not a
    // nested "General 3-SlotN") is storage holding your wares, not merchandise. A
    // bag you'd actually sell lives nested inside another bag, so it keeps a
    // "-Slot" location and survives this. Scoped to "General N" so equipped gear
    // (whose Slots column counts AUGMENT slots, e.g. raid gear = 6) is never hit.
    // No hardcoded bag list needed.
    const slots = si !== null && si < parts.length ? (parseInt((parts[si] || "").trim(), 10) || 0) : 0;
    if (slots > 0 && /^general \d+$/i.test(loc)) continue;
    let count = 1;
    if (ci !== null && ci < parts.length) {
      const n = parseInt((parts[ci] || "").trim(), 10);
      count = Number.isFinite(n) ? Math.max(n, 1) : 1;
    }
    let id = 0;
    if (ii !== null && ii < parts.length) {
      const n = parseInt((parts[ii] || "").trim(), 10);
      id = Number.isFinite(n) ? Math.max(n, 0) : 0;
    }
    // Track bag vs non-bag quantities separately so "Bags only" can show just
    // what's in your bags, not a total that folds in Bank/SharedBank copies.
    const inBag = isBagLocation(loc);
    const key = id ? `#${id}` : name;
    if (combined.has(key)) {
      const e = combined.get(key);
      e.count += count;
      if (inBag) { e.bagCount += count; if (!e.bagLocation) e.bagLocation = loc; }
    } else {
      combined.set(key, {
        name, location: loc, count, id, price: "",
        bagCount: inBag ? count : 0,
        bagLocation: inBag ? loc : "",
      });
      order.push(key);
    }
  }
  return order.map((k) => combined.get(k));
}

// The DB link for an inventory item: prefer the exact id, fall back to name.
function linkFor(item) {
  if (item.id && state.db.byId.has(item.id)) return state.db.byId.get(item.id).link;
  return state.db.byName.get(item.name) || null;
}

// One auction token: "<link> <price>" (no xN — tlp-auctions reads x2 as 2-for-price).
function linkToken(item) {
  const link = makeLink(linkFor(item), item.name);
  return item.price ? `${link} ${item.price}` : link;
}

// Pack tokens into <=255-char lines, each led by prefix (and optional suffix).
function packToLines(tokens, prefix, suffix, sep) {
  const lines = [];
  let cur = [];
  const base = prefix.length + 1;
  const suffixLen = suffix ? ` ${suffix}`.length : 0;
  let curLen = base;
  for (const tok of tokens) {
    const add = (cur.length ? sep.length : 0) + tok.length;
    if (cur.length && curLen + add + suffixLen > 255) {
      lines.push(`${prefix} ` + cur.join(sep) + (suffix ? ` ${suffix}` : ""));
      cur = [tok];
      curLen = base + tok.length;
    } else {
      cur.push(tok);
      curLen += add;
    }
  }
  if (cur.length) lines.push(`${prefix} ` + cur.join(sep) + (suffix ? ` ${suffix}` : ""));
  return lines;
}

// Lay packed lines into social buttons (5 lines/button, 12 buttons/page, from
// startPage up; page 1 is never touched). Returns {entries, preview, overflow,
// endPage} — endPage is the last page that got a button (startPage-1 if none),
// so a second group (links) can begin on a fresh page after this one.
function buttonsFromLines(lines, btnName, startPage, maxLinesBtn = 5) {
  const entries = [];     // [key, val] pairs in order
  const preview = [];
  let page = startPage, btn = 1, written = 0, overflow = 0, endPage = startPage - 1;
  const chunks = [];
  for (let i = 0; i < lines.length; i += maxLinesBtn) chunks.push(i);
  for (const bs of chunks) {
    if (btn > BUTTONS_PER_PAGE) { page++; btn = 1; }
    if (page > MAX_PAGE) { overflow = chunks.length - written; break; }
    const bl = lines.slice(bs, bs + maxLinesBtn);
    const label = `${btnName}${written + 1}`;
    entries.push([`Page${page}Button${btn}Name`, label]);
    entries.push([`Page${page}Button${btn}Color`, "0"]);
    bl.forEach((line, idx) => entries.push([`Page${page}Button${btn}Line${idx + 1}`, line]));
    preview.push([label, bl]);
    endPage = page;
    btn++; written++;
  }
  return { entries, preview, overflow, endPage };
}

// Idempotent merge into [Socials]: drop buttons we previously auto-wrote
// (Name matches ^(WTS|Rare)\d+$), then update/insert the new entries. Hand-made
// socials and every non-[Socials] section are left untouched. Faithful port of
// the desktop _write_ini merge.
function mergeIntoIni(existing, entries) {
  const newMap = new Map(entries);
  if (!existing.includes("[Socials]")) {
    existing = existing.replace(/\s+$/, "") + "\n\n[Socials]\n";
  }
  const autoNameRe = /^(?:WTS|Rare)\d+$/;
  const dropPrefixes = new Set();
  let inSocials = false;
  for (const raw of existing.split("\n")) {
    const st = raw.trim();
    if (st === "[Socials]") inSocials = true;
    else if (st.startsWith("[") && st.endsWith("]")) inSocials = false;
    else if (inSocials && st.includes("=")) {
      const eq = st.indexOf("=");
      const k = st.slice(0, eq).trim();
      const v = st.slice(eq + 1);
      if (k.endsWith("Name") && autoNameRe.test(v.trim())) dropPrefixes.add(k.slice(0, -4));
    }
  }
  const isAuto = (key) => {
    for (const p of dropPrefixes) {
      if (key === p + "Name" || key === p + "Color") return true;
      if (key.startsWith(p + "Line") && /^\d+$/.test(key.slice(p.length + 4))) return true;
    }
    return false;
  };
  const out = [];
  const written = new Set();
  inSocials = false;
  for (const line of existing.split("\n")) {
    const stripped = line.trim();
    if (stripped === "[Socials]") { inSocials = true; out.push(line); continue; }
    if (stripped.startsWith("[") && stripped.endsWith("]")) {
      if (inSocials) {
        for (const [k, v] of entries) if (!written.has(k)) { out.push(`${k}=${v}`); written.add(k); }
      }
      inSocials = false; out.push(line); continue;
    }
    if (inSocials && stripped.includes("=")) {
      const key = stripped.slice(0, stripped.indexOf("=")).trim();
      if (newMap.has(key)) { out.push(`${key}=${newMap.get(key)}`); written.add(key); continue; }
      if (isAuto(key)) continue;
    }
    out.push(line);
  }
  if (inSocials) {
    for (const [k, v] of entries) if (!written.has(k)) { out.push(`${k}=${v}`); written.add(k); }
  }
  return out.join("\n");
}

// ----- latin-1 byte helpers (the encoding gotcha, solved cleanly in JS) -----
// latin-1 is a 1:1 codepoint->byte map for 0-255, so DC2 (0x12) stays 0x12.
function latin1Bytes(str) {
  const bytes = new Uint8Array(str.length);
  for (let i = 0; i < str.length; i++) bytes[i] = str.charCodeAt(i) & 0xff;
  return bytes;
}
function latin1Decode(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return s;
}

// ----- gzip decompress in the browser (native, no library) -----
async function gunzipToText(arrayBuffer) {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("This browser lacks DecompressionStream — use a recent Chrome/Edge/Firefox.");
  }
  const ds = new DecompressionStream("gzip");
  const stream = new Blob([arrayBuffer]).stream().pipeThrough(ds);
  return await new Response(stream).text();
}

// ----- IndexedDB cache so the 11.6 MB gz is downloaded only once -----
const IDB_NAME = "eqaf", IDB_STORE = "kv", DB_KEY = "items-gz", DB_META_KEY = "items-meta";
function idbOpen() {
  return new Promise((res, rej) => {
    const r = indexedDB.open(IDB_NAME, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(IDB_STORE);
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function idbGet(key) {
  try {
    const db = await idbOpen();
    return await new Promise((res, rej) => {
      const q = db.transaction(IDB_STORE, "readonly").objectStore(IDB_STORE).get(key);
      q.onsuccess = () => res(q.result || null);
      q.onerror = () => rej(q.error);
    });
  } catch { return null; }
}
async function idbPut(key, val) {
  try {
    const db = await idbOpen();
    await new Promise((res, rej) => {
      const q = db.transaction(IDB_STORE, "readwrite").objectStore(IDB_STORE).put(val, key);
      q.onsuccess = () => res();
      q.onerror = () => rej(q.error);
    });
  } catch { /* best effort */ }
}
async function idbDel(key) {
  try {
    const db = await idbOpen();
    await new Promise((res) => {
      const q = db.transaction(IDB_STORE, "readwrite").objectStore(IDB_STORE).delete(key);
      q.onsuccess = () => res(); q.onerror = () => res();
    });
  } catch { /* best effort */ }
}

// Auto-load the bundled item DB from ../items.txt.gz (one level up — i.e.
// docs/items.txt.gz, served at the Pages root). Cached in IndexedDB so it's
// downloaded only once, but REVALIDATED every load so a shipped DB update is
// picked up automatically: send a conditional request with the cached copy's
// validator (ETag on Pages, Last-Modified when served locally) — unchanged → 304, use
// cache; changed → download the new one. Only works when SERVED (localhost /
// Pages); under file:// fetch is blocked.
async function autoLoadDb({ forceNetwork = false } = {}) {
  $("dbStatus").textContent = "loading…";
  try {
    let buf = forceNetwork ? null : await idbGet(DB_KEY);
    let meta = forceNetwork ? null : await idbGet(DB_META_KEY);   // {etag, lastModified}

    const store = async (resp) => {
      buf = await resp.arrayBuffer();
      meta = { etag: resp.headers.get("ETag"), lastModified: resp.headers.get("Last-Modified") };
      await idbPut(DB_KEY, buf);
      await idbPut(DB_META_KEY, meta);
    };

    if (buf) {
      // Revalidate with exactly ONE validator (ETag preferred). Sending both
      // trips SimpleHTTPRequestHandler, which ignores If-Modified-Since when
      // If-None-Match is present and would then re-send the whole file.
      const headers = {};
      if (meta && meta.etag) headers["If-None-Match"] = meta.etag;
      else if (meta && meta.lastModified) headers["If-Modified-Since"] = meta.lastModified;
      try {
        const resp = await fetch("../items.txt.gz", { headers, cache: "no-store" });
        if (resp.status === 304) {
          log("Item DB: cached copy is current (304, not re-downloaded).");
        } else if (resp.ok) {
          await store(resp);
          log("Item DB: server copy changed — downloaded the update.");
        } else {
          log(`Item DB: revalidation HTTP ${resp.status} — using cached copy.`);
        }
      } catch {
        log("Item DB: offline — using cached copy.");
      }
    } else {
      log("Item DB: first visit, downloading items.txt.gz…");
      const resp = await fetch("../items.txt.gz", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await store(resp);
    }

    state.db = parseItemDb(await gunzipToText(buf));
    $("dbStatus").textContent = `${state.db.byName.size} items loaded`;
    $("dbCount").textContent = `DB: ${state.db.byName.size.toLocaleString()} items`;
    log(`Item DB: ${state.db.byName.size} names, ${state.db.byId.size} by id.`);
  } catch (err) {
    $("dbStatus").textContent = "auto-load failed — serve via localhost";
    log("DB auto-load failed (" + (err && err.message ? err.message : err) +
        "). Under file:// fetch is blocked — open the served app instead.");
  }
}

// =====================================================================
// Pricing — TLP-Auctions bulk API (mirrors probe.html / the desktop app)
// =====================================================================

// Are we running from a local dev server? The proxy is a
// localhost-only crutch; on GitHub Pages we always go direct.
function isLocalhost() {
  return ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
}

// Direct = the apex host (valid cert). Proxy = same-origin /api for local dev.
// The proxy is only ever used on localhost, so a Pages visitor always goes direct.
function apiBase() {
  const cb = $("useProxy");
  return cb && cb.checked && isLocalhost() ? "/api" : API_HOST;
}

// Shared request headers. X-Client-App tags our traffic for the API owner; merge
// in any per-call extras (e.g. Content-Type for the POST).
function apiHeaders(extra) {
  return Object.assign(
    { "Accept": "application/json", "X-Client-App": CLIENT_TAG },
    extra || {});
}

// Undercut % from the box, clamped to [0,100); blank/invalid = 0 (desktop parity).
function undercutPct() {
  const n = parseFloat(($("undercut") || {}).value);
  return Number.isFinite(n) && n > 0 && n < 100 ? n : 0;
}
// Round to the nearest 5 plat (so 300p − 2% = 294 posts as 295, not 294).
function roundTo5(v) { return Math.round(v / 5) * 5; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// One /prices/bulk call with a single retry on transient failure (the upstream
// occasionally resets a connection mid-run). Returns parsed JSON or throws.
async function fetchBulk(server, itemIds) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const resp = await fetch(`${apiBase()}/prices/bulk`, {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ serverName: server, itemIds }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return await resp.json();
    } catch (e) {
      if (attempt === 2) throw e;
      await sleep(500);   // brief backoff, then one retry
    }
  }
}

// Median of a numeric list (avg of the two middles for even length), or null.
function median(nums) {
  if (!nums.length) return null;
  const s = [...nums].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// Live krono->plat rate: the 1-day window average (EC krono moves fast, longer
// windows lag). Returns an int, or null on failure. Port of fetch_krono_rate.
async function fetchKronoRate(server) {
  try {
    const resp = await fetch(`${apiBase()}/krono-prices/${encodeURIComponent(server)}/windows`,
      { headers: apiHeaders() });
    if (!resp.ok) return null;
    const data = await resp.json();
    const byDays = {};
    for (const w of data.windows || []) byDays[w.days] = w;
    for (const d of [1, 2, 3, 7]) {            // freshest window that has data
      const w = byDays[d];
      if (w && w.sampleSize > 0 && w.averagePrice > 0) return Math.round(w.averagePrice);
    }
  } catch { /* keep fallback */ }
  return null;
}

// Recent individual postings for ONE item (exact-name match), newest first.
// Port of fetch_recent_sales.
async function fetchRecentSales(name, server, limit = RECENT_SALES_LIMIT) {
  const qs = new URLSearchParams({ searchTerm: name, exactMatch: "true",
    serverName: server, pageSize: String(limit) });
  const resp = await fetch(`${apiBase()}/sales?${qs}`, { headers: apiHeaders() });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  const data = await resp.json();
  return (data.items || []).slice(0, limit);
}

// Summarize recent WTS asks into a denomination-aware read, or null if <2 priced
// asks. EC combines currencies, so each ask's effective plat is platPrice +
// kronoPrice*rate; krono-dominant when more asks name krono than not. Port of
// _recent_market.
function recentMarket(sales, rate) {
  const wts = sales.filter((s) => !s.transactionType);   // transactionType false = WTS
  const eff = []; let krCount = 0, platCount = 0;
  for (const s of wts) {
    const p = s.platPrice || 0, k = s.kronoPrice || 0;
    if (k > 0) { eff.push(p + k * rate); krCount++; }
    else if (p > 0) { eff.push(p); platCount++; }
  }
  const n = eff.length;
  const effMed = median(eff);
  if (effMed === null || n < 2) return null;
  const isKrono = krCount > platCount;
  const priceStr = isKrono
    ? `${Math.max(Math.round((effMed / rate) * 2) / 2, 0.5)}kr`   // nearest 0.5 krono
    : `${roundTo5(effMed)}p`;
  return { effMed, n, isKrono, priceStr };
}

// Resolve one bulk result into a postable price. Plain plat from the median
// (minus undercut); for items >= RECENT_CHECK_FLOOR, a recent-asks read that
// either swaps to a krono price (no undercut) or flags plat asks running >=15%
// under the median for review. Port of _resolve_price.
async function resolvePrice(name, r, rate, pct, server) {
  const med = Math.round(r.medianPlatPrice);
  const platStr = `${Math.max(roundTo5(pct ? med * (1 - pct / 100) : med), 5)}p`;
  if (med < RECENT_CHECK_FLOOR) return { priceStr: platStr, krono: false, diverge: null };
  let sales;
  try { sales = await fetchRecentSales(name, server); }
  catch { return { priceStr: platStr, krono: false, diverge: null }; }
  const mk = recentMarket(sales, rate);
  if (!mk) return { priceStr: platStr, krono: false, diverge: null };
  if (mk.isKrono) return { priceStr: mk.priceStr, krono: true, diverge: null };  // no undercut on krono
  const divPct = (mk.effMed - med) / med * 100;
  if (divPct <= -15 && mk.n >= 3) {
    return { priceStr: platStr, krono: false, diverge: { recent: mk.priceStr, pct: divPct, n: mk.n } };
  }
  return { priceStr: platStr, krono: false, diverge: null };
}

// Price-check every inventory item that has an id: batch ids <=10 per request,
// POST /prices/bulk, then resolve each (plat median minus undercut, or a krono
// swap / divergence flag for >=1000p items via recent asks). id-keyed, so items
// with no id are skipped. A failed batch is retried once then skipped (not
// fatal). Port of _price_check_all + _resolve_price. (Row coloring TODO.)
async function priceItems(items) {
  const server = SERVER;

  const rowsById = new Map();   // itemId -> [auction rows sharing that id]
  for (const item of items) {
    if (!item.id) continue;
    if (!rowsById.has(item.id)) rowsById.set(item.id, []);
    rowsById.get(item.id).push(item);
  }
  const ids = [...rowsById.keys()];
  if (!ids.length) { log("Price check: no auction items have an id to look up (type prices by hand)."); return; }

  const pct = undercutPct();
  const btns = [$("pcBtn"), $("pcSelBtn")];
  btns.forEach((b) => b && (b.disabled = true));
  setStatus("Price check: starting…");
  const batches = Math.ceil(ids.length / BULK_PRICE_LIMIT);
  log(`Price check: ${ids.length} item(s) on ${server} in ${batches} request(s)` +
      (pct ? `, undercut ${pct}%` : "") + "…");

  // Live 1-day krono rate up front (folds krono asks in the recent-asks read).
  let rate = await fetchKronoRate(server) || 0;

  let priced = 0, noData = 0, krono = 0, failed = 0, batchErr = 0;
  const diverged = [];
  try {
    for (let i = 0; i < ids.length; i += BULK_PRICE_LIMIT) {
      const batch = ids.slice(i, i + BULK_PRICE_LIMIT);
      let data = null;
      try {
        data = await fetchBulk(server, batch);
      } catch (e) {
        batchErr++; failed += batch.length;
        log(`  batch ${Math.floor(i / BULK_PRICE_LIMIT) + 1}/${batches} failed (${e.message}) — skipped`);
      }
      if (data) {
        if (!rate && data.kronoRate) rate = data.kronoRate;   // fall back to the bulk rate
        for (const r of data.items || []) {
          const rows = rowsById.get(r.itemId);
          if (!rows) continue;
          if (!(r.hasData && r.medianPlatPrice > 0)) { noData++; continue; }
          const res = await resolvePrice(rows[0].name, r, rate || DEFAULT_KRONO_RATE, pct, server);
          for (const it of rows) {
            it.price = res.priceStr;
            it.diverge = res.diverge;
            it._lastMedian = Math.round(r.medianPlatPrice);   // ref for the Recent Postings hint
            if (it._priceInput) it._priceInput.value = res.priceStr;
          }
          priced++;
          if (res.krono) krono++;
          if (res.diverge) diverged.push({ name: rows[0].name, you: res.priceStr, ...res.diverge });
        }
      }
      setStatus(`Price check: ${Math.min(i + BULK_PRICE_LIMIT, ids.length)}/${ids.length}…`);
      await sleep(120);   // gentle pacing between batches
    }
    log(`Price check complete: ${priced} priced` + (krono ? ` (${krono} krono)` : "") +
        `, ${noData} no data` +
        (failed ? `, ${failed} failed in ${batchErr} batch(es)` : "") +
        (pct ? `, undercut ${pct}%` : "") +
        (rate ? ` (krono rate ~${Math.round(rate)}p)` : "") + ".");
    if (diverged.length) {
      log(`${diverged.length} item(s) priced above recent asks — open Recent Postings to reprice:`);
      for (const d of diverged) log(`  ${d.name}: you ${d.you} / recent ~${d.recent} (${Math.round(d.pct)}%)`);
    }
    if (failed) {
      if (priced === 0 && noData === 0 && !(($("useProxy") || {}).checked)) {
        log("  → Every batch failed. Direct calls only work from the deployed " +
            "origin (CORS) — for local dev, serve the app and tick 'Use local proxy'.");
      } else {
        log("  → Some batches hit a transient API error. Run Price Check All again to fill the rest.");
      }
    }
  } catch (e) {
    log("Price check error: " + (e && e.message ? e.message : e));
  } finally {
    btns.forEach((b) => b && (b.disabled = false));
  }
  state.kronoRate = rate || state.kronoRate;   // remember for the Recent Postings hint
  refreshAuction();   // rebuild rows so prices/flags (and coloring) reflect the check
}

async function priceCheckAll() {
  if (!state.auction.length) return;
  track("pc_all");
  await priceItems(state.auction);
}

async function priceCheckSelected() {
  if (!state.aucSel.size) { log("Select auction row(s) to price-check, or use PC All."); return; }
  await priceItems([...state.aucSel].map((i) => state.auction[i]));
}

// ===================================================================
// Recent Postings (/api/sales viewer) + modal
// ===================================================================

// ISO UTC -> "MM/DD hh:mmAM (Nm/h/d ago)". Port of format_sale_age.
function formatSaleAge(iso) {
  const dt = new Date(iso);
  if (isNaN(dt.getTime())) return iso || "?";
  const secs = Math.floor((Date.now() - dt.getTime()) / 1000);
  const ago = secs < 3600 ? `${Math.max(Math.floor(secs / 60), 0)}m ago`
    : secs < 86400 ? `${Math.floor(secs / 3600)}h ago`
      : `${Math.floor(secs / 86400)}d ago`;
  const mo = String(dt.getMonth() + 1).padStart(2, "0"), da = String(dt.getDate()).padStart(2, "0");
  let h = dt.getHours(); const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12;
  const mi = String(dt.getMinutes()).padStart(2, "0");
  return `${mo}/${da} ${String(h).padStart(2, "0")}:${mi}${ap} (${ago})`;
}
// One posting is in plat OR krono. Port of format_posting_price.
function formatPostingPrice(plat, krono) {
  if (krono && krono > 0) return `${krono}kr`;
  if (plat && plat > 0) return `${Math.trunc(plat)}p`;
  return "—";
}

// ----- generic modal (web equivalent of the desktop's Toplevel windows) -----
function openModal(title, bodyNode) {
  $("modalTitle").textContent = title;
  const body = $("modalBody"); body.innerHTML = ""; body.appendChild(bodyNode);
  $("modal").hidden = false;
}
function closeModal() { $("modal").hidden = true; $("modalBody").innerHTML = ""; }

// Show the accumulated activity log in the modal (the log element is hidden;
// this is the on-demand viewer reached via the fixed Log button).
function showLog() {
  const pre = document.createElement("pre");
  pre.className = "postings";
  pre.style.maxHeight = "65vh";
  pre.style.whiteSpace = "pre-wrap";
  pre.textContent = ($("log").textContent || "").trim() || "(nothing logged yet)";
  openModal("Activity log", pre);
  requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });   // newest at bottom
}

// ----- header: live krono rate (+ Sync) and Help -----
async function syncKrono() {
  const server = SERVER;
  const btn = $("syncKronoBtn"); if (btn) btn.disabled = true;
  setStatus("Syncing krono rate…");
  try {
    const rate = await fetchKronoRate(server);
    if (rate) {
      state.kronoRate = rate;
      const t = new Date(); let h = t.getHours(); const ap = h < 12 ? "am" : "pm"; h = h % 12 || 12;
      $("kronoInfo").textContent = `krono ~${rate.toLocaleString()}p · synced @ ${h}:${String(t.getMinutes()).padStart(2, "0")}${ap}`;
      log(`Krono rate: ${rate.toLocaleString()}p/kr (1-day avg).`);
    } else {
      $("kronoInfo").textContent = `krono ~${(state.kronoRate || DEFAULT_KRONO_RATE).toLocaleString()}p (sync failed)`;
      log("Krono sync failed — using fallback rate.");
    }
  } finally { if (btn) btn.disabled = false; }
}

function showHelp() {
  const d = document.createElement("div");
  d.innerHTML =
    "<p><strong>Quick start</strong></p>" +
    "<ol style='margin:0 0 10px 18px;padding:0'>" +
    "<li>In EQ: <code>/outputfile inventory</code>, then load that file under <strong>1. Load files</strong>.</li>" +
    "<li><strong>Build your auction:</strong> select items on the left (Bags only hides worn gear), then <strong>Add Selected &rarr;</strong>.</li>" +
    "<li><strong>Price:</strong> <strong>PC All</strong> (or Price Check for the selected rows). Set <strong>Undercut %</strong> / <strong>CHA</strong> first if you like.</li>" +
    "<li><strong>Generate</strong> the macro, then <strong>Write</strong>/<strong>Download</strong> the INI (close EQ first).</li>" +
    "</ol>" +
    "<p class='hint'>Row colors: <span style='color:#cc99ff'>krono</span> &middot; " +
    "<span style='color:var(--orange)'>vendor-trash (left out of the macro)</span> &middot; " +
    "<span style='color:#ffd24d'>recent asks lower &mdash; check Recent Postings</span>.</p>" +
    "<p class='hint'>Use <strong>Look up any item</strong> to check the recent market for things you don't own. " +
    "In-place INI write needs Chrome/Edge served over https or localhost.</p>";
  openModal("Help — EQ Auction Forge", d);
}

// ----- preferences: persist the toolbar inputs (the lightweight "Settings") -----
// Saved values seed the boxes next session, exactly like the desktop's defaults.
const PREFS_KEY = "eqaf-prefs";
const PREF_IDS = ["undercut", "cha", "minProfit", "prefix", "page", "threshold", "suffix"];
function savePrefs() {
  const p = {};
  for (const id of PREF_IDS) { const el = $(id); if (el) p[id] = el.value; }
  const bo = $("invBagsOnly"); if (bo) p.bagsOnly = bo.checked;
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* private mode etc. */ }
}
function loadPrefs() {
  let p;
  try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { p = {}; }
  for (const id of PREF_IDS) { if (p[id] !== undefined && $(id)) $(id).value = p[id]; }
  if (typeof p.bagsOnly === "boolean" && $("invBagsOnly")) $("invBagsOnly").checked = p.bagsOnly;
}

// Set an auction item's price to the recent median (no undercut — match the live
// market, don't undercut it). Port of _use_recent_median (auction-list case).
function useRecentMedian(item, price) {
  item.price = price;
  item.diverge = null;
  if (item._priceInput) item._priceInput.value = price;
  refreshAuction();
  log(`  ${item.name}: priced at recent median ${price}`);
}

// Recent postings for ONE item by name. `item` is the auction row when we own it
// (enables the divergence hint vs the check median + a Set-price button); null
// for a DB lookup of something you don't have. Port of _show_recent_postings.
async function showRecentPostings(name, item = null) {
  const server = SERVER;
  log(`Fetching recent postings: ${name}…`);
  let sales;
  try { sales = await fetchRecentSales(name, server); }
  catch (e) { log(`  recent postings failed: ${e.message}`); alert("Couldn't fetch postings: " + e.message); return; }
  if (!sales.length) { log(`  ${name}: no recent postings on ${server}`); alert(`No recent postings found for:\n${name}`); return; }

  const rate = state.kronoRate || (await fetchKronoRate(server)) || DEFAULT_KRONO_RATE;
  const wrap = document.createElement("div");
  const sub = document.createElement("div");
  sub.className = "hint"; sub.style.marginBottom = "8px";
  sub.textContent = `Last ${sales.length} postings on ${server} (newest first)`;
  wrap.appendChild(sub);

  // Recent-asks divergence hint vs the last check median (item._lastMedian).
  const mk = recentMarket(sales, rate);
  const ref = item ? item._lastMedian : undefined;
  if (mk) {
    const shown = mk.isKrono
      ? `${mk.priceStr} (≈${Math.round(mk.effMed).toLocaleString()}p @ ${Math.round(rate).toLocaleString()}/kr)`
      : mk.priceStr;
    const hint = document.createElement("p");
    hint.className = "hint-line";
    hint.style.fontFamily = '"Segoe UI Emoji", Consolas, monospace';
    if (!ref) { hint.textContent = `Recent WTS median ${shown} (over ${mk.n} asks) — price-check this item to compare vs the live median.`; hint.style.color = "var(--fg)"; }
    else {
      const pct = (mk.effMed - ref) / ref * 100;
      if (pct <= -15) { hint.textContent = `📉 Recent WTS median ${shown} — ~${Math.abs(Math.round(pct))}% UNDER your ${ref.toLocaleString()}p check median. Median's lagging; consider repricing.`; hint.style.color = "#ff6666"; }
      else if (pct >= 15) { hint.textContent = `📈 Recent WTS median ${shown} — ~${Math.round(pct)}% ABOVE your ${ref.toLocaleString()}p check median. Asks are climbing.`; hint.style.color = "#00ff66"; }
      else { hint.textContent = `≈ Recent WTS median ${shown} — in line with your ${ref.toLocaleString()}p check median.`; hint.style.color = "var(--muted)"; }
    }
    wrap.appendChild(hint);
    if (item) {   // only owned items can be repriced from here
      const setBtn = document.createElement("button");
      setBtn.textContent = `Set price → ${mk.priceStr}  (match recent median)`;
      setBtn.style.marginBottom = "10px";
      setBtn.addEventListener("click", () => { useRecentMedian(item, mk.priceStr); closeModal(); });
      wrap.appendChild(setBtn);
    }
  }

  const pre = document.createElement("div");
  pre.className = "postings";
  pre.textContent = sales.map((s) => {
    const when = formatSaleAge(s.datetime).padEnd(22);
    const kind = s.transactionType ? "WTB" : "WTS";
    const price = formatPostingPrice(s.platPrice, s.kronoPrice).padStart(9);
    return `${when} ${kind}  ${price}  ${s.auctioneer || "?"}`;
  }).join("\n");
  wrap.appendChild(pre);

  openModal(`Recent Postings — ${name}`, wrap);
}

// Auction-pane button: recent postings for the single selected auction row.
function recentPostingsSelected() {
  if (state.aucSel.size !== 1) { log("Select exactly one auction item, then Recent Postings."); return; }
  const it = state.auction[[...state.aucSel][0]];
  showRecentPostings(it.name, it);
}

// DB lookup: recent postings for ANY item by name (owned or not). Exact match
// first, else a contains-search; multiple matches open a picker.
function recentPostingsLookup() {
  const q = $("lookupInput").value.trim();
  if (!q) return;
  if (!state.db) { log("Item DB not loaded yet."); return; }
  const lc = q.toLowerCase();
  let exact = null; const partial = [];
  for (const n of state.db.byName.keys()) {
    const nl = n.toLowerCase();
    if (nl === lc) { exact = n; break; }
    if (nl.includes(lc) && partial.length < 60) partial.push(n);
  }
  if (exact) { showRecentPostings(exact); return; }
  if (!partial.length) { log(`No DB item matches "${q}".`); alert(`No item in the database matches "${q}".`); return; }
  if (partial.length === 1) { showRecentPostings(partial[0]); return; }
  partial.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const list = document.createElement("div");
  list.className = "picker";
  for (const n of partial) {
    const row = document.createElement("div");
    row.className = "picker-row";
    row.textContent = n;
    row.addEventListener("click", () => showRecentPostings(n));   // replaces modal contents
    list.appendChild(row);
  }
  openModal(`${partial.length} matches for "${q}" — pick one`, list);
}

// =====================================================================
// UI wiring
// =====================================================================

// ----- inventory filter + column-sort helpers (port of desktop filter/sort) -----
// Bags only: general-inventory slots ('General 1', 'General 2-Slot4'); excludes
// worn gear, Bank, SharedBank, KeyRing, Power Source. Port of _is_bag_location.
function isBagLocation(loc) { return (loc || "").trim().toLowerCase().startsWith("general"); }
// Natural sort so 'General 2-Slot10' follows 'Slot9'. Port of _natkey.
function natkey(s) { return (s || "").split(/(\d+)/).filter((t) => t !== "").map((t) => /^\d+$/.test(t) ? parseInt(t, 10) : t.toLowerCase()); }
function natCmp(a, b) {
  const ax = natkey(a), bx = natkey(b);
  for (let i = 0; i < Math.max(ax.length, bx.length); i++) {
    const x = ax[i], y = bx[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (typeof x === "number" && typeof y === "number") { if (x !== y) return x - y; }
    else { const xs = String(x), ys = String(y); if (xs !== ys) return xs < ys ? -1 : 1; }
  }
  return 0;
}
// Group equipped/bank slots first, General bags second; natural-sort within. Port of _location_sort_key.
function locCmp(la, lb) {
  const ba = isBagLocation(la) ? 1 : 0, bb = isBagLocation(lb) ? 1 : 0;
  return ba !== bb ? ba - bb : natCmp(la, lb);
}
// Parse a displayed price ('500p','1.5kr','<1p','') to plat for ordering; '' sinks. Port of _price_sort_key.
function priceSortKey(v) {
  const s = (v || "").trim().toLowerCase();
  if (!s) return -1;
  const kr = s.match(/(\d+(?:\.\d+)?)\s*kr/), pp = s.match(/(\d+(?:\.\d+)?)\s*p/);
  if (kr || pp) return (kr ? parseFloat(kr[1]) * DEFAULT_KRONO_RATE : 0) + (pp ? parseFloat(pp[1]) : 0);
  const n = parseFloat(s.replace(/[^0-9.]/g, ""));
  return Number.isFinite(n) ? n : 0;
}
// Comparator for a column, operating on item objects.
function cmpFor(col) {
  if (col === "qty") return (a, b) => (a.count || 1) - (b.count || 1);
  if (col === "location") return (a, b) => locCmp(a.location, b.location);
  if (col === "price") return (a, b) => priceSortKey(a.price) - priceSortKey(b.price);
  if (col === "vendor") return (a, b) => priceSortKey(vendorStr(a)) - priceSortKey(vendorStr(b));
  return (a, b) => (a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase());
}
function toggleSortState(st, col) { st.desc = st.col === col ? !st.desc : false; st.col = col; }
// Show a ▲/▼ on the active column header (and clear the others).
function renderSortArrows(tableId, st) {
  document.querySelectorAll(`#${tableId} thead th`).forEach((th) => {
    const base = th.dataset.label || (th.dataset.label = th.textContent.replace(/[ ▲▼]+$/, ""));
    th.textContent = th.dataset.col === st.col ? `${base} ${st.desc ? "▼" : "▲"}` : base;
  });
}
function sortInventory(col) { toggleSortState(state.invSort, col); renderSortArrows("invTable", state.invSort); buildInventoryTable(); }
function sortAuction(col) {
  toggleSortState(state.aucSort, col);
  renderSortArrows("aucTable", state.aucSort);
  const cmp = cmpFor(col);
  state.auction.sort((a, b) => state.aucSort.desc ? -cmp(a, b) : cmp(a, b));
  refreshAuction();
}

// ----- inventory pane (left): filtered/sorted list + multi-select + add -----
// Visible inventory after Bags-only + search filter and the current sort. Each
// entry keeps its ORIGINAL index so selection maps back to state.inventory.
function inventoryView() {
  const bagsOnly = $("invBagsOnly").checked;
  const q = $("invSearch").value.trim().toLowerCase();
  let view = state.inventory
    .map((item, i) => ({ item, i }))
    .filter(({ item }) => (!bagsOnly || item.bagCount > 0) && (!q || item.name.toLowerCase().includes(q)));
  if (state.invSort.col) {
    const cmp = cmpFor(state.invSort.col);
    view.sort((A, B) => state.invSort.desc ? -cmp(A.item, B.item) : cmp(A.item, B.item));
  }
  return view;
}

function buildInventoryTable() {
  const body = $("invBody");
  body.innerHTML = "";
  state.invSel.clear();
  const bagsOnly = $("invBagsOnly").checked;
  const view = state.inventory.length ? inventoryView() : [];
  if (!state.inventory.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty">Load an inventory dump above.</td></tr>`;
  } else if (!view.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty">No items match the filter.</td></tr>`;
  } else {
    for (const { item, i } of view) {
      // In bags-only mode show the bag quantity/location, not the cross-location total.
      const cnt = bagsOnly ? item.bagCount : item.count;
      const loc = bagsOnly ? (item.bagLocation || item.location) : item.location;
      const tr = document.createElement("tr");
      tr.dataset.i = i;
      tr.innerHTML =
        `<td>${escapeHtml(item.name)}</td>` +
        `<td class="qty">${cnt > 1 ? "x" + cnt : ""}</td>` +
        `<td class="qty">${escapeHtml(loc)}</td>`;
      tr.addEventListener("click", () => toggleInvSel(i, tr));
      tr.addEventListener("dblclick", () => {
        if (addToAuction(state.inventory[i], cnt)) { log(`Added ${item.name}.`); refreshAuction(); }
      });
      body.appendChild(tr);
    }
  }
  const total = state.inventory.length;
  $("invCount").textContent = view.length === total ? `${total} items` : `${view.length} of ${total}`;
  $("selAllBtn").disabled = !view.length;
  $("addSelBtn").disabled = !view.length;
}

function toggleInvSel(i, tr) {
  if (state.invSel.has(i)) { state.invSel.delete(i); tr.classList.remove("sel"); }
  else { state.invSel.add(i); tr.classList.add("sel"); }
}

function selectAllInv() {
  const rows = $("invBody").querySelectorAll("tr[data-i]");
  const allSelected = rows.length > 0 && state.invSel.size >= rows.length;
  state.invSel.clear();
  rows.forEach((tr) => {
    if (allSelected) { tr.classList.remove("sel"); }
    else { state.invSel.add(Number(tr.dataset.i)); tr.classList.add("sel"); }
  });
}

// Add one inventory item to the auction list as a fresh copy. Dedupe by id
// (unique) when present, else by name. Returns true if actually added.
function addToAuction(inv, count) {
  const key = inv.id ? `#${inv.id}` : inv.name.toLowerCase();
  if (state.auction.some((a) => (a.id ? `#${a.id}` : a.name.toLowerCase()) === key)) return false;
  state.auction.push({ name: inv.name, location: inv.location, count: count != null ? count : inv.count, id: inv.id, price: "" });
  return true;
}

function addSelectedToAuction() {
  if (!state.invSel.size) { log("Select inventory rows first (click them), then Add Selected."); return; }
  const bagsOnly = $("invBagsOnly").checked;
  const wanted = state.invSel.size;
  let added = 0;
  [...state.invSel].sort((a, b) => a - b).forEach((i) => {
    const inv = state.inventory[i];
    if (addToAuction(inv, bagsOnly ? inv.bagCount : inv.count)) added++;
  });
  log(`Added ${added} item(s) to the auction list` +
      (added < wanted ? ` (${wanted - added} already there)` : "") + ".");
  state.invSel.clear();
  $("invBody").querySelectorAll("tr.sel").forEach((tr) => tr.classList.remove("sel"));
  refreshAuction();
}

// ----- auction pane (right): the curated "to post" list -----
// Single source of row color, priority krono > vendor-trash > diverge (port of _row_tag).
function rowTag(item) {
  if (classifyPrice(item.price)[0] === "krono") return "krono";
  if (isVendorTrash(item)) return "vendor";
  if (item.diverge) return "diverge";
  return null;
}

function refreshAuction() {
  const body = $("aucBody");
  body.innerHTML = "";
  state.aucSel.clear();
  if (!state.auction.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty">Add items from the left.</td></tr>`;
  } else {
    state.auction.forEach((item, i) => {
      const tr = document.createElement("tr");
      tr.dataset.i = i;
      tr.innerHTML =
        `<td>${escapeHtml(item.name)}</td>` +
        `<td class="qty">${item.count > 1 ? "x" + item.count : ""}</td>` +
        `<td></td>` +
        `<td class="qty">${escapeHtml(vendorStr(item))}</td>`;
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "e.g. 500p";
      input.value = item.price || "";
      input.addEventListener("input", () => {
        item.price = input.value.trim();
        item.diverge = null;                 // a manual price overrides the recent-asks flag
        tr.classList.remove("krono", "vendor", "diverge");
        const t = rowTag(item);
        if (t) tr.classList.add(t);          // live recolor (krono/vendor) as you type
      });
      // editing the price shouldn't toggle the row's selection
      input.addEventListener("click", (e) => e.stopPropagation());
      item._priceInput = input;
      tr.children[2].appendChild(input);
      const tag = rowTag(item);
      if (tag) tr.classList.add(tag);
      tr.addEventListener("click", () => toggleAucSel(i, tr));
      body.appendChild(tr);
    });
  }
  $("aucCount").textContent = `${state.auction.length} items`;
  const has = state.auction.length > 0;
  $("pcBtn").disabled = !has;
  $("pcSelBtn").disabled = !has;
  $("rpBtn").disabled = !has;
  $("removeBtn").disabled = !has;
  $("clearBtn").disabled = !has;
  $("genBtn").disabled = !has;
}

function toggleAucSel(i, tr) {
  if (state.aucSel.has(i)) { state.aucSel.delete(i); tr.classList.remove("sel"); }
  else { state.aucSel.add(i); tr.classList.add("sel"); }
}

function removeSelectedFromAuction() {
  if (!state.aucSel.size) { log("Select auction rows to remove (click them), or use Clear."); return; }
  const n = state.aucSel.size;
  [...state.aucSel].sort((a, b) => b - a).forEach((i) => state.auction.splice(i, 1));   // high->low
  refreshAuction();
  log(`Removed ${n} item(s) from the auction list.`);
}

function clearAuction() {
  state.auction = [];
  refreshAuction();
  log("Auction list cleared.");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let lastEntries = null;   // [key,val] pairs from the most recent Generate

// Classify a price string for the link/text split: ['krono'|'plat'|'none', plat].
// 'kr' anywhere -> krono (always links); digits -> plat; empty -> none (unpriced).
// Port of _classify_price.
function classifyPrice(priceStr) {
  const s = (priceStr || "").trim().toLowerCase();
  if (!s) return ["none", 0];
  if (s.includes("kr")) return ["krono", 0];
  const digits = s.replace(/[^0-9.]/g, "");
  if (digits) { const n = parseFloat(digits); return Number.isFinite(n) ? ["plat", Math.trunc(n)] : ["none", 0]; }
  return ["none", 0];
}

// Parse a price box ('600', '600p', '1kr') to a plat int. 0 = OFF. Port of
// _parse_plat_value.
function parsePlatValue(raw) {
  raw = (raw || "").trim().toLowerCase().replace(/,/g, "").replace(/\s/g, "");
  if (!raw) return 0;
  let mult = 1;
  if (raw.includes("kr")) { mult = DEFAULT_KRONO_RATE; raw = raw.replace("kr", ""); }
  raw = raw.replace(/p+$/, "");
  if (!raw) return mult > 1 ? mult : 0;   // bare "kr" -> one krono
  const n = parseFloat(raw);
  return Number.isFinite(n) ? Math.max(Math.trunc(n * mult), 0) : 0;
}
function thresholdPlat() { return parsePlatValue($("threshold").value); }
// Min profit over NPC vendor value to bother listing (plat). 0 = off.
function minProfitPlat() { return parsePlatValue(($("minProfit") || {}).value); }

// ----- NPC vendor value (CHA-based) — port of vendor_multiplier/_vendor_pp/_is_vendor_trash -----
function vendorMultiplier(cha) { return Math.max(0, Math.min(VENDOR_SLOPE * cha + VENDOR_INTERCEPT, VENDOR_CAP)); }
function vendorValuePp(priceCp, cha) { return (priceCp / 1000) * vendorMultiplier(cha); }
function chaVal() { const n = parseInt(($("cha") || {}).value, 10); return Number.isFinite(n) && n >= 0 ? n : null; }
// Base merchant value (copper) for an item, by id (the DB price column). null if unknown.
function baseCopper(item) { const rec = item.id && state.db ? state.db.byId.get(item.id) : null; return rec ? rec.price : null; }
function vendorPp(item) { const c = chaVal(), base = baseCopper(item); return (c === null || !base) ? null : vendorValuePp(base, c); }
function vendorStr(item) { const v = vendorPp(item); return v === null ? "" : (v >= 1 ? `${Math.round(v)}p` : "<1p"); }
// True if a PLAT-priced item isn't worth listing: either worth >= as much to a
// vendor as your post price, OR its profit over vendor value is under the "Min
// profit" floor (not worth the hassle). Krono/unpriced are never trash (can't
// compare). Port of _is_vendor_trash, extended with the min-profit floor.
function isVendorTrash(item) {
  const [kind, plat] = classifyPrice(item.price);
  if (kind !== "plat") return false;
  const v = vendorPp(item);
  if (v === null) return false;
  return v >= plat || (plat - v) < minProfitPlat();
}

// Log the vendor-trash items (with bag location + margin) left out of the macro.
function reportTrash(trash) {
  if (!trash.length) return;
  const floor = minProfitPlat();
  log(`${trash.length} item(s) not worth listing` +
      (floor ? ` (profit over vendor < ${floor}p)` : ` (worth more to a vendor)`) +
      ` — left OUT of the macro:`);
  for (const it of trash) {
    const v = vendorPp(it), [, plat] = classifyPrice(it.price);
    const margin = v !== null ? Math.round(plat - v) : null;
    log(`  VENDOR (${vendorStr(it)} vs ${it.price}${margin !== null ? `, +${margin}p` : ""}): ` +
        `${it.name} @ ${it.location || "?"}`);
  }
}

function generate() {
  if (!state.db) { log("Item DB not loaded yet — wait for it or check the connection."); return; }
  track("generate");
  const prefix = $("prefix").value;
  const suffix = $("suffix").value.trim();
  const page = parseInt($("page").value, 10) || 2;
  const threshold = thresholdPlat();

  // Band 1 (trash): worth >= as much to an NPC vendor as your post price. Dropped
  // from the macro and reported with bag locations so you know what to go sell.
  const trash = [], nontrash = [];
  for (const it of state.auction) (isVendorTrash(it) ? trash : nontrash).push(it);
  reportTrash(trash);

  // Generate from the AUCTION list (minus trash), not the whole inventory.
  const sellable = nontrash.filter((i) => linkFor(i));
  const skipped = nontrash.length - sellable.length;
  if (!sellable.length) {
    log(trash.length ? "All priced items are vendor-trash — nothing to auction. Go vendor them!"
                     : "No auction items have a DB link to generate.");
    return;
  }

  const textToken = (item) => item.price ? `${item.name} ${item.price}` : `${item.name} pst`;

  let entries, preview, overflow, unpriced = [];
  if (threshold <= 0) {
    // Split off -> classic: everything links (WTS#), from `page`.
    const r = buttonsFromLines(packToLines(sellable.map(linkToken), prefix, suffix, ", "), "WTS", page);
    entries = r.entries; preview = r.preview; overflow = r.overflow;
    log(`Generated ${preview.length} button(s) (link everything)` + (skipped ? `, ${skipped} no-link skipped` : "") + ".");
  } else {
    // Split on -> cheap plat + unpriced to compact text (WTS#); krono/movers to links (Rare#).
    const textItems = [], linkItems = [];
    for (const item of sellable) {
      const [kind, plat] = classifyPrice(item.price);
      if (kind === "krono" || (kind === "plat" && plat >= threshold)) linkItems.push(item);
      else { if (kind === "none") unpriced.push(item.name); textItems.push(item); }
    }
    const t = buttonsFromLines(packToLines(textItems.map(textToken), prefix, suffix, " | "), "WTS", page);
    // Both groups start at `page` and go up (page 1 untouched); links get a fresh page after the text.
    const linkStart = textItems.length ? Math.max(page, t.endPage + 1) : page;
    const l = buttonsFromLines(packToLines(linkItems.map(linkToken), prefix, suffix, ", "), "Rare", linkStart);
    entries = [...t.entries, ...l.entries];
    preview = [...t.preview, ...l.preview];
    overflow = t.overflow + l.overflow;
    log(`Split @ ${threshold}p: ${textItems.length} text (WTS, pg ${page}), ${linkItems.length} link (Rare, pg ${linkStart})` +
        (skipped ? `, ${skipped} no-link skipped` : "") + ".");
    if (unpriced.length) log(`  no price → 'pst': ${unpriced.join(", ")}`);
  }

  lastEntries = entries;
  // Show the INI entries (DC2 rendered as a visible marker in the textarea).
  const shown = entries.map(([k, v]) => `${k}=${v}`).join("\n").replace(new RegExp(DC2, "g"), "·");
  $("output").value = shown;
  $("writeBtn").disabled = false;
  $("copyBtn").disabled = false;
  if (overflow) log(`  WARNING: ${overflow} button(s) didn't fit past page ${MAX_PAGE} — lower the start page.`);
}

// File System Access API: pick the character INI, read it, merge, write back.
async function writeInPlace() {
  if (!lastEntries) return;
  if (!window.showOpenFilePicker) {
    log("In-place write needs Chrome/Edge (File System Access API). Use 'Copy macros' instead.");
    return;
  }
  try {
    const [handle] = await window.showOpenFilePicker({
      types: [{ description: "EQ character INI", accept: { "text/plain": [".ini"] } }],
    });
    const file = await handle.getFile();
    const existing = latin1Decode(new Uint8Array(await file.arrayBuffer()));
    const merged = mergeIntoIni(existing, lastEntries);
    const writable = await handle.createWritable();
    await writable.write(latin1Bytes(merged));
    await writable.close();
    log(`Wrote ${file.name} in place (latin-1, ${merged.length} chars). Make sure EQ was CLOSED.`);
  } catch (e) {
    if (e && e.name === "AbortError") return;   // user cancelled the picker
    log("Write failed: " + (e && e.message ? e.message : e));
  }
}

// Copy the generated [Socials] entries to the clipboard (with the real DC2 link
// char) and show paste instructions. Fallback for browsers without in-place
// write — Edge blocks .ini downloads, so this replaces the old download path.
async function copyMacros() {
  if (!lastEntries) return;
  const text = lastEntries.map(([k, v]) => `${k}=${v}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);   // needs a secure context (https/localhost) + this click
    log(`Copied ${lastEntries.length} [Socials] entries to the clipboard.`);
  } catch {
    log("Clipboard blocked (needs https/localhost) — use 'Write to INI file' instead.");
  }
  const d = document.createElement("div");
  d.innerHTML =
    "<p>The macro's <code>[Socials]</code> entries are on your clipboard.</p>" +
    "<p><strong>Easiest:</strong> use <strong>Write to INI file</strong> above (Chrome/Edge) — it edits your character INI directly, no copy-paste.</p>" +
    "<p><strong>Manual paste:</strong></p>" +
    "<ol style='margin:0 0 10px 18px;padding:0'>" +
    "<li><strong>Close EverQuest first</strong> — it rewrites the INI on exit.</li>" +
    "<li>Open your character file in the EQ folder, e.g. <code>&lt;Char&gt;_&lt;server&gt;_&lt;class&gt;.ini</code> (like <code>Alan_frostreaver_ROG.ini</code>), in a text editor (Notepad is fine).</li>" +
    "<li>Find the <code>[Socials]</code> section (add it at the end if it's missing).</li>" +
    "<li>Paste, replacing any old <code>WTS#</code>/<code>Rare#</code> buttons from a previous run.</li>" +
    "<li>Save, then launch EQ.</li>" +
    "</ol>" +
    "<p class='hint'>The clickable-link lines hold a special character; a plain editor preserves it fine.</p>";
  openModal("Copy macros → paste into your INI", d);
}

// ----- input handlers (browser only) -----
if (typeof document !== "undefined") {
$("invFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    state.inventory = parseInventory(text);
    $("invStatus").textContent = `${state.inventory.length} items`;
    log(`Inventory: ${state.inventory.length} items. Select items and Add them to the auction list →`);
    buildInventoryTable();
  } catch (err) {
    $("invStatus").textContent = "failed";
    log("Inventory load failed: " + (err && err.message ? err.message : err));
  }
});

$("reloadDb").addEventListener("click", async () => {
  await idbDel(DB_KEY);
  await idbDel(DB_META_KEY);
  autoLoadDb({ forceNetwork: true });
});
$("cha").addEventListener("change", refreshAuction);   // recompute Vendor column + trash coloring
$("invSearch").addEventListener("input", buildInventoryTable);
$("invBagsOnly").addEventListener("change", buildInventoryTable);
document.querySelectorAll("#invTable thead th").forEach((th) => th.addEventListener("click", () => sortInventory(th.dataset.col)));
document.querySelectorAll("#aucTable thead th").forEach((th) => th.addEventListener("click", () => sortAuction(th.dataset.col)));
$("selAllBtn").addEventListener("click", selectAllInv);
$("addSelBtn").addEventListener("click", addSelectedToAuction);
$("removeBtn").addEventListener("click", removeSelectedFromAuction);
$("clearBtn").addEventListener("click", clearAuction);
$("pcSelBtn").addEventListener("click", priceCheckSelected);
$("rpBtn").addEventListener("click", recentPostingsSelected);
$("pcBtn").addEventListener("click", priceCheckAll);
$("lookupBtn").addEventListener("click", recentPostingsLookup);
$("lookupInput").addEventListener("keydown", (e) => { if (e.key === "Enter") recentPostingsLookup(); });
$("logBtn").addEventListener("click", showLog);
$("syncKronoBtn").addEventListener("click", syncKrono);
$("helpBtn").addEventListener("click", showHelp);
$("modalClose").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });   // backdrop click
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modal").hidden) { closeModal(); return; }
  // Delete removes selected auction rows — but not while editing a price box.
  if (e.key === "Delete" && state.aucSel.size) {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    e.preventDefault();
    removeSelectedFromAuction();
  }
});
$("genBtn").addEventListener("click", generate);
$("writeBtn").addEventListener("click", writeInPlace);
$("copyBtn").addEventListener("click", copyMacros);

PREF_IDS.forEach((id) => { const el = $(id); if (el) el.addEventListener("change", savePrefs); });
$("invBagsOnly").addEventListener("change", savePrefs);

// The dev proxy only exists on localhost — hide its toggle entirely on Pages so
// a visitor never sees (or ticks) a dead control.
if (!isLocalhost()) {
  const cb = $("useProxy");
  if (cb) { cb.checked = false; const f = cb.closest(".field"); if (f) f.style.display = "none"; }
}

{ const av = $("appVersion"); if (av) av.textContent = "v" + APP_VERSION; }  // single source of truth

loadPrefs();    // restore saved toolbar values (lightweight Settings)
log("Ready.");
track("view");  // anonymous visit ping (production origin only)
autoLoadDb();   // pull the bundled DB automatically when served (localhost/Pages)
syncKrono();    // pull the live krono rate for the header (best-effort)
if (!window.showOpenFilePicker) {
  log("Note: in-place INI write needs Chrome/Edge; the Download button works everywhere.");
}
}  // end browser-only block

// Exported for Node-based logic tests; harmless/ignored in the browser.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    makeLink, parseItemDb, parseInventory, packToLines,
    buttonsFromLines, mergeIntoIni, latin1Bytes, latin1Decode,
  };
}
