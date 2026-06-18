"use strict";
/*
 * EQ Auction Forge — web proof of concept.
 *
 * Pure client-side: files are read in the browser, processed in JS, and the INI
 * is written back locally. Nothing is uploaded. The logic here is a faithful
 * port of the desktop app's core (EQ-Auction_Forge.py) — same DC2 link format,
 * same items.txt columns, same inventory-dump parsing, same 255-char / 5-line /
 * 12-button packing, same idempotent [Socials] merge.
 *
 * Out of scope for this PoC: the TLP-Auctions pricing API (CORS + cert work,
 * pending), vendor-trash filtering, the threshold link/text split, krono, etc.
 */

// ----- constants (mirror the Python module globals) -----
const DC2 = "\x12";              // EQ item-link delimiter (hex 0x12)
const BUTTONS_PER_PAGE = 12;
const MAX_PAGE = 10;
const BULK_PRICE_LIMIT = 10;     // max item ids per /prices/bulk request
const DEFAULT_KRONO_RATE = 4000; // fallback fold rate if the API reports none
const RECENT_CHECK_FLOOR = 1000; // only items with a bulk median >= this get a recent-asks lookup
const RECENT_SALES_LIMIT = 8;    // recent postings pulled per recent-asks lookup
// Correct apex host (valid cert). "/api" via dev-proxy.py dodges CORS in dev.
const API_HOST = "https://tlp-auctions.com/api";
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
  iniText: null,     // optional existing INI loaded for the Download path
};

// ----- tiny DOM helpers -----
const $ = (id) => (typeof document !== "undefined" ? document.getElementById(id) : null);
function log(msg) {
  const el = $("log");
  if (!el) { return; }   // no DOM (e.g. under Node logic tests) — stay silent
  el.textContent += msg + "\n";
  el.scrollTop = el.scrollHeight;
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
  let ni = 1, li = 0, ci = null, ii = null;
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
      continue;
    }
    if (parts.length < 3) continue;
    const name = (parts[ni] || "").trim().replace(/\*+$/, "");
    const loc = (parts[li] || "").trim();
    const lower = name.toLowerCase();
    if (lower === "" || lower === "empty" || lower === "name") continue;
    if (EXCLUDED_ITEMS.has(lower)) continue;
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
    const key = id ? `#${id}` : name;
    if (combined.has(key)) {
      combined.get(key).count += count;
    } else {
      combined.set(key, { name, location: loc, count, id, price: "" });
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
// startPage up; page 1 is never touched). Returns {entries, preview, overflow}.
function buttonsFromLines(lines, btnName, startPage, maxLinesBtn = 5) {
  const entries = [];     // [key, val] pairs in order
  const preview = [];
  let page = startPage, btn = 1, written = 0, overflow = 0;
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
    btn++; written++;
  }
  return { entries, preview, overflow };
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

// Auto-load the bundled item DB from ../items.txt.gz (one level up — single
// source of truth, not duplicated into web/). Cached in IndexedDB so it's
// downloaded only once, but REVALIDATED every load so a shipped DB update is
// picked up automatically: send a conditional request with the cached copy's
// validator (ETag on Pages, Last-Modified via dev-proxy) — unchanged → 304, use
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
    log(`Item DB: ${state.db.byName.size} names, ${state.db.byId.size} by id.`);
  } catch (err) {
    $("dbStatus").textContent = "auto-load failed — serve via localhost";
    log("DB auto-load failed (" + (err && err.message ? err.message : err) +
        "). Under file:// fetch is blocked — serve it (e.g. `python web/dev-proxy.py`).");
  }
}

// =====================================================================
// Pricing — TLP-Auctions bulk API (mirrors probe.html / the desktop app)
// =====================================================================

// Direct = the apex host (valid cert). Proxy = same-origin /api via dev-proxy.py,
// which dodges CORS while developing (tlp-auctions hasn't enabled CORS yet).
function apiBase() {
  const cb = $("useProxy");
  return cb && cb.checked ? "/api" : API_HOST;
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
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
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
      { headers: { "Accept": "application/json" } });
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
  const resp = await fetch(`${apiBase()}/sales?${qs}`, { headers: { "Accept": "application/json" } });
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
async function priceCheckAll() {
  if (!state.auction.length) return;
  const server = ($("server").value || "Frostreaver").trim();

  const rowsById = new Map();   // itemId -> [auction rows sharing that id]
  for (const item of state.auction) {
    if (!item.id) continue;
    if (!rowsById.has(item.id)) rowsById.set(item.id, []);
    rowsById.get(item.id).push(item);
  }
  const ids = [...rowsById.keys()];
  if (!ids.length) { log("Price check: no auction items have an id to look up (type prices by hand)."); return; }

  const pct = undercutPct();
  const pc = $("pcBtn"), st = $("pcStatus");
  pc.disabled = true; st.textContent = "checking…";
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
            if (it._priceInput) it._priceInput.value = res.priceStr;
          }
          priced++;
          if (res.krono) krono++;
          if (res.diverge) diverged.push({ name: rows[0].name, you: res.priceStr, ...res.diverge });
        }
      }
      st.textContent = `checking… ${Math.min(i + BULK_PRICE_LIMIT, ids.length)}/${ids.length}`;
      await sleep(120);   // gentle pacing between batches
    }
    st.textContent = `done — ${priced} priced, ${noData} no data` + (failed ? `, ${failed} failed` : "");
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
        log("  → Every batch failed. Direct calls need CORS (not enabled yet) — " +
            "on localhost run `python web/dev-proxy.py` and tick 'Use local proxy'.");
      } else {
        log("  → Some batches hit a transient API error. Run Price Check All again to fill the rest.");
      }
    }
  } catch (e) {
    st.textContent = "failed";
    log("Price check error: " + (e && e.message ? e.message : e));
  } finally {
    pc.disabled = false;
  }
}

// =====================================================================
// UI wiring
// =====================================================================

// ----- inventory pane (left): list + multi-select + add to auction -----
function buildInventoryTable() {
  const body = $("invBody");
  body.innerHTML = "";
  state.invSel.clear();
  if (!state.inventory.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty">Load an inventory dump above.</td></tr>`;
  } else {
    state.inventory.forEach((item, i) => {
      const tr = document.createElement("tr");
      tr.dataset.i = i;
      tr.innerHTML =
        `<td>${escapeHtml(item.name)}</td>` +
        `<td class="qty">${item.count > 1 ? "x" + item.count : ""}</td>` +
        `<td class="qty">${escapeHtml(item.location)}</td>`;
      tr.addEventListener("click", () => toggleInvSel(i, tr));
      tr.addEventListener("dblclick", () => {
        if (addToAuction(state.inventory[i])) { log(`Added ${item.name}.`); refreshAuction(); }
      });
      body.appendChild(tr);
    });
  }
  $("invCount").textContent = `${state.inventory.length} items`;
  $("selAllBtn").disabled = !state.inventory.length;
  $("addSelBtn").disabled = !state.inventory.length;
}

function toggleInvSel(i, tr) {
  if (state.invSel.has(i)) { state.invSel.delete(i); tr.classList.remove("sel"); }
  else { state.invSel.add(i); tr.classList.add("sel"); }
}

function selectAllInv() {
  const rows = $("invBody").querySelectorAll("tr[data-i]");
  const allSelected = state.invSel.size === state.inventory.length;
  state.invSel.clear();
  rows.forEach((tr) => {
    if (allSelected) { tr.classList.remove("sel"); }
    else { state.invSel.add(Number(tr.dataset.i)); tr.classList.add("sel"); }
  });
}

// Add one inventory item to the auction list as a fresh copy. Dedupe by id
// (unique) when present, else by name. Returns true if actually added.
function addToAuction(inv) {
  const key = inv.id ? `#${inv.id}` : inv.name.toLowerCase();
  if (state.auction.some((a) => (a.id ? `#${a.id}` : a.name.toLowerCase()) === key)) return false;
  state.auction.push({ name: inv.name, location: inv.location, count: inv.count, id: inv.id, price: "" });
  return true;
}

function addSelectedToAuction() {
  if (!state.invSel.size) { log("Select inventory rows first (click them), then Add Selected."); return; }
  const wanted = state.invSel.size;
  let added = 0;
  [...state.invSel].sort((a, b) => a - b).forEach((i) => { if (addToAuction(state.inventory[i])) added++; });
  log(`Added ${added} item(s) to the auction list` +
      (added < wanted ? ` (${wanted - added} already there)` : "") + ".");
  state.invSel.clear();
  $("invBody").querySelectorAll("tr.sel").forEach((tr) => tr.classList.remove("sel"));
  refreshAuction();
}

// ----- auction pane (right): the curated "to post" list -----
function refreshAuction() {
  const body = $("aucBody");
  body.innerHTML = "";
  state.aucSel.clear();
  if (!state.auction.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty">Add items from the left.</td></tr>`;
  } else {
    state.auction.forEach((item, i) => {
      const tr = document.createElement("tr");
      tr.dataset.i = i;
      tr.innerHTML =
        `<td>${escapeHtml(item.name)}</td>` +
        `<td class="qty">${item.count > 1 ? "x" + item.count : ""}</td>` +
        `<td></td>`;
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "e.g. 500p";
      input.value = item.price || "";
      input.addEventListener("input", () => { item.price = input.value.trim(); });
      // editing the price shouldn't toggle the row's selection
      input.addEventListener("click", (e) => e.stopPropagation());
      item._priceInput = input;
      tr.children[2].appendChild(input);
      tr.addEventListener("click", () => toggleAucSel(i, tr));
      body.appendChild(tr);
    });
  }
  $("aucCount").textContent = `${state.auction.length} items`;
  const has = state.auction.length > 0;
  $("pcBtn").disabled = !has;
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

function generate() {
  if (!state.db) { log("Item DB not loaded yet — wait for it or check the connection."); return; }
  const prefix = $("prefix").value;
  const suffix = $("suffix").value.trim();
  const page = parseInt($("page").value, 10) || 2;

  // Generate from the AUCTION list, not the whole inventory.
  const sellable = state.auction.filter((i) => linkFor(i));
  const skipped = state.auction.length - sellable.length;
  if (!sellable.length) { log("No auction items have a DB link to generate."); return; }

  const tokens = sellable.map(linkToken);
  const lines = packToLines(tokens, prefix, suffix, ", ");
  const { entries, preview, overflow } = buttonsFromLines(lines, "WTS", page);
  lastEntries = entries;

  // Show the INI entries (DC2 rendered as a visible marker in the textarea).
  const shown = entries.map(([k, v]) => `${k}=${v}`).join("\n").replace(new RegExp(DC2, "g"), "·");
  $("output").value = shown;
  $("writeBtn").disabled = false;
  $("downloadBtn").disabled = false;
  log(`Generated ${preview.length} button(s) from ${sellable.length} item(s)` +
      (skipped ? `, ${skipped} skipped (no DB link)` : "") +
      (overflow ? ` — WARNING: ${overflow} dropped past page ${MAX_PAGE}` : "") + ".");
}

// File System Access API: pick the character INI, read it, merge, write back.
async function writeInPlace() {
  if (!lastEntries) return;
  if (!window.showOpenFilePicker) {
    log("In-place write needs Chrome/Edge (File System Access API). Use Download instead.");
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

// Download path: merge into the optional uploaded INI (or a minimal stub) and
// trigger a latin-1 download — works in every browser.
function downloadIni() {
  if (!lastEntries) return;
  const base = state.iniText !== null ? state.iniText : "[Socials]\n";
  const merged = mergeIntoIni(base, lastEntries);
  const blob = new Blob([latin1Bytes(merged)], { type: "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "EQ_socials.ini";
  a.click();
  URL.revokeObjectURL(url);
  log(`Downloaded merged INI (latin-1, ${merged.length} chars).`);
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

$("iniFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) { state.iniText = null; return; }
  state.iniText = latin1Decode(new Uint8Array(await file.arrayBuffer()));
  $("iniStatus").textContent = `${file.name} loaded — download will merge into it`;
  log(`Loaded existing INI for merge: ${file.name}`);
});

$("reloadDb").addEventListener("click", async () => {
  await idbDel(DB_KEY);
  await idbDel(DB_META_KEY);
  autoLoadDb({ forceNetwork: true });
});
$("selAllBtn").addEventListener("click", selectAllInv);
$("addSelBtn").addEventListener("click", addSelectedToAuction);
$("removeBtn").addEventListener("click", removeSelectedFromAuction);
$("clearBtn").addEventListener("click", clearAuction);
$("pcBtn").addEventListener("click", priceCheckAll);
$("genBtn").addEventListener("click", generate);
$("writeBtn").addEventListener("click", writeInPlace);
$("downloadBtn").addEventListener("click", downloadIni);

log("Ready.");
autoLoadDb();   // pull the bundled DB automatically when served (localhost/Pages)
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
