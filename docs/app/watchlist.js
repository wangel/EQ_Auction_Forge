"use strict";
// Match engine — web port of the desktop's logmon.py matcher. Two directions:
//   BUY  lead: their WTS contains an item on my WATCHLIST   (exact phrase)
//   SELL lead: their WTB fuzzy-matches an item in my INVENTORY (IDF scorer)
//
// PARITY: mirrors logmon.py
//   BUY : tokenize / phrase_token_groups / phrase_match / _segments /
//         sell_segments / parse_auction_line / _watchlist_hits
//   SELL: build_idf / token_sim / score / confidence + alias expansion / _best
//
// Watchlist (BUY) matching is EXACT phrase containment, not fuzzy: sellers link
// real item names, and one line lists many items, so a bag-of-words scorer would
// stitch a phantom match across two items. The words must appear as a contiguous
// run inside ONE listed item; no alias expansion (parity: the desktop expands
// aliases only in the fuzzy buy path). SELL matching IS fuzzy because buyers
// abbreviate/typo ("WTB fungi", "cof", "fbss") — rare words carry the signal
// (IDF), an exact "anchor" word gates the loud tier, and aliases expand slang.
//
// DOM-free on purpose so tests/parity_watchlist.cjs can run it in Node.

const STOPWORDS = new Set(['of', 'the', 'a', 'an', 'and', 'for', 'to', 'with',
                           'spell', 'spells', 'song', 'songs']);
// Auction boilerplate: stripped from the QUERY only (the drop-boilerplate path).
const BOILERPLATE = new Set(['wtb', 'wts', 'wtt', 'iso', 'lf', 'pst', 'pstme',
                             'plat', 'pp', 'kr', 'krono', 'kronos', 'ea', 'each',
                             'cash', 'price', 'offer']);

const TOKEN_RE = /[a-z0-9'`]+/g;
const PHRASE_TOK_RE = /[a-z0-9'`]+|,/g;
const PRICEY_RE = /^\d+(?:\.\d+)?[kpm]*$/;            // 5k, 500p, 1.5k, 2000

// [Tue Jun 09 03:14:02 2026] Soandso auctions, 'WTS fungi tunic 5k'
const LINE_RE = /^\[(.*?)\]\s+(\S+)\s+auctions,\s+'(.*)'\s*$/;

const DIR_MARK = /\b(wtb|iso|lf|buying|wts|wtt|selling)\b/gi;
const BUY_MARK = new Set(['wtb', 'iso', 'lf', 'buying']);   // poster wants to BUY
const SELL_MARK = new Set(['wts', 'wtt', 'selling']);       // poster is SELLING

// A buy segment leading with krono is a currency purchase, not an item.
const KRONO_LEAD_RE = /^\W*(?:\d+(?:\.\d+)?[kpm]*\W+)*(?:krono|kronos|kr)\b/i;

function tokenize(text, dropBoilerplate = false) {
  const out = [];
  const toks = text.toLowerCase().match(TOKEN_RE) || [];
  for (const t of toks) {
    if (STOPWORDS.has(t)) continue;
    if (dropBoilerplate && (BOILERPLATE.has(t) || PRICEY_RE.test(t))) continue;
    if (t.length < 2) continue;
    out.push(t);
  }
  return out;
}

// Split an auction segment into one token list per listed item, so a phrase
// can't span items. A comma or a price-like token ends the current item;
// stopwords and sub-2-char tokens are transparent.
function phraseTokenGroups(text) {
  const groups = [];
  let cur = [];
  const lower = text.toLowerCase();
  let m;
  PHRASE_TOK_RE.lastIndex = 0;
  while ((m = PHRASE_TOK_RE.exec(lower)) !== null) {
    const tok = m[0];
    if (tok === ',' || PRICEY_RE.test(tok)) {          // item boundary
      if (cur.length) { groups.push(cur); cur = []; }
      continue;
    }
    if (STOPWORDS.has(tok) || tok.length < 2) continue;
    cur.push(tok);
  }
  if (cur.length) groups.push(cur);
  return groups;
}

// True if queryName's words appear as a contiguous run inside any single group.
function phraseMatch(queryName, groups) {
  const q = tokenize(queryName);
  if (!q.length) return false;
  const span = q.length;
  for (const g of groups) {
    for (let i = 0; i + span <= g.length; i++) {
      let ok = true;
      for (let k = 0; k < span; k++) { if (g[i + k] !== q[k]) { ok = false; break; } }
      if (ok) return true;
    }
  }
  return false;
}

function isKronoTrade(seg) {
  return KRONO_LEAD_RE.test(seg.trim());
}

// Return only the portion(s) of a line governed by markers in `want`. Traders
// post both directions in one line ("WTS a // WTB b"); slicing on markers keeps
// each intent separate. Krono-purchase segments are dropped. '' if none match.
function segments(msg, want) {
  const marks = [...msg.matchAll(DIR_MARK)];
  if (!marks.length) return '';
  const segs = [];
  for (let i = 0; i < marks.length; i++) {
    if (want.has(marks[i][1].toLowerCase())) {
      const end = i + 1 < marks.length ? marks[i + 1].index : msg.length;
      const seg = msg.slice(marks[i].index + marks[i][0].length, end);
      if (isKronoTrade(seg)) continue;
      segs.push(seg);
    }
  }
  return segs.join(' ');
}

const sellSegments = (msg) => segments(msg, SELL_MARK);
const buySegments = (msg) => segments(msg, BUY_MARK);

// ('timestamp', 'Speaker', 'message') for an auctions line, else null.
function parseAuctionLine(raw) {
  const m = LINE_RE.exec(raw.replace(/[\r\n]+$/, ''));
  return m ? { ts: m[1], speaker: m[2], msg: m[3] } : null;
}

// Watchlist names a seller is offering in this line's WTS segment(s). Exact
// phrase containment; a single WTS can hit several watchlist items.
function watchlistHits(msg, watchlist) {
  if (!msg || !watchlist || !watchlist.length) return [];
  const groups = phraseTokenGroups(sellSegments(msg));
  if (!groups.length) return [];
  const hits = [];
  for (const name of watchlist) {
    if (phraseMatch(name, groups)) hits.push(name);
  }
  return hits;
}

// Like phraseTokenGroups, but also captures the price token that CLOSED each item
// group — in EC's "WTS <Item> <price>, <Item2> <price2>" format that trailing
// price is the item's ask. Display-only (not in the parity-locked matchLine).
// Full price incl. decimals + krono suffix, since the tokenizer splits "1.5k"
// into "1"/"5k" on the dot. Matches at a position in the lowered text.
const PRICE_FULL_RE = /^\d+(?:\.\d+)?\s*(?:kr|kronos?|k|pp?|plat|m)?/;

function phraseGroupsWithPrice(text) {
  const groups = [];
  let cur = [];
  const lower = text.toLowerCase();
  let m;
  PHRASE_TOK_RE.lastIndex = 0;
  while ((m = PHRASE_TOK_RE.exec(lower)) !== null) {
    const tok = m[0];
    if (tok === ",") { if (cur.length) { groups.push({ tokens: cur, price: null }); cur = []; } continue; }
    if (PRICEY_RE.test(tok)) {                          // price boundary (same as the matcher)
      const pm = lower.slice(m.index).match(PRICE_FULL_RE);   // but capture the WHOLE price
      const price = pm ? pm[0].replace(/\s+/g, "") : tok;
      if (cur.length) { groups.push({ tokens: cur, price }); cur = []; }
      continue;
    }
    if (STOPWORDS.has(tok) || tok.length < 2) continue;
    cur.push(tok);
  }
  if (cur.length) groups.push({ tokens: cur, price: null });
  return groups;
}

// The asking price (raw token, e.g. "8k"/"500p"/"1500") for `item` within a
// segment, or null if none was listed ("pst"/"offer"). Finds the item's phrase
// group and returns the price that closed it.
function priceFor(seg, item) {
  if (!seg) return null;
  const q = tokenize(item);
  if (!q.length) return null;
  const span = q.length;
  for (const g of phraseGroupsWithPrice(seg)) {
    for (let i = 0; i + span <= g.tokens.length; i++) {
      let ok = true;
      for (let k = 0; k < span; k++) { if (g.tokens[i + k] !== q[k]) { ok = false; break; } }
      if (ok) return g.price;
    }
  }
  return null;
}

// The actual item as listed (original casing) that contains a watchlist `term`,
// so a one-word watch ("Deepwater") shows "Deepwater Vambraces" in the feed, not
// just the word. Splits on EC's item separators, then strips a trailing quantity
// (xN) and/or price from the matching chunk. null if not found. Display-only.
// EC lists each item as "<name> <price/qty>", so we isolate item names by splitting
// the segment on explicit separators AND on price/quantity tokens. That stops a
// multi-item line (esp. space-separated, no //) from merging and showing the wrong
// item for the match. Returns null if it can't cleanly isolate one, so the caller
// falls back to the (always-accurate) watch word.
const ITEM_SPLIT_RE = /\s*(?:\/\/|--|;|\||,)\s*|\s+x\d+\b|\s+\d+(?:\.\d+)?\s*(?:kr|kronos?|k|pp?|plat|m)?\b/gi;
function listedItemFor(seg, term) {
  if (!seg) return null;
  if (!tokenize(term).length) return null;
  for (const part of seg.split(ITEM_SPLIT_RE)) {
    if (!part) continue;
    const clean = part.replace(/^[-\s]+|[-\s]+$/g, "");
    if (clean && phraseMatch(term, phraseTokenGroups(clean))) return clean;
  }
  return null;
}

// ===========================================================================
// SELL direction: fuzzy IDF match of a WTB segment against my inventory.
// ===========================================================================

// Calibrated against real data — do not tweak without re-running the desktop's
// prototypes/logmon_calibrate.py against a captured log (these mirror logmon.py).
const T_HIGH = 6.0;          // min total score for the loud tier
const T_MAYBE = 3.0;         // min total score to register at all
const ITEM_COV_HIGH = 0.85;  // query must hit ~all of my item's idf mass -> loud
const ITEM_COV_MAYBE = 0.55; // strong partial -> quiet feed
const EXACT_ANCHOR = 4.0;    // loud also needs ONE exact match on a distinctive word

// Inverse document frequency over every DB item name, so rare words (Nife, Karana)
// carry the signal and common ones (helm, scale) weigh ~0. Returns {idf:Map, n}.
function buildIdf(allNames) {
  const df = new Map();
  for (const name of allNames) {
    for (const t of new Set(tokenize(name))) df.set(t, (df.get(t) || 0) + 1);
  }
  const n = allNames.length;
  const idf = new Map();
  for (const [t, c] of df) idf.set(t, Math.log(n / c));
  return { idf, n };
}

function commonPrefixLen(a, b) {
  let n = 0; const m = Math.min(a.length, b.length);
  while (n < m && a[n] === b[n]) n++;
  return n;
}

// Faithful port of Python difflib.SequenceMatcher(None, a, b).ratio() for the
// short, junk-free strings token_sim feeds it (item-name words << 200 chars, so
// difflib's autojunk never triggers). ratio = 2*M/T over recursively-found
// longest matching blocks (Ratcliff-Obershelp).
function seqMatchedChars(a, b) {
  const b2j = new Map();
  for (let j = 0; j < b.length; j++) {
    if (!b2j.has(b[j])) b2j.set(b[j], []);
    b2j.get(b[j]).push(j);
  }
  function longestMatch(alo, ahi, blo, bhi) {
    let besti = alo, bestj = blo, bestsize = 0;
    let j2len = new Map();
    for (let i = alo; i < ahi; i++) {
      const newj2len = new Map();
      const idxs = b2j.get(a[i]) || [];
      for (const j of idxs) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) || 0) + 1;
        newj2len.set(j, k);
        if (k > bestsize) { besti = i - k + 1; bestj = j - k + 1; bestsize = k; }
      }
      j2len = newj2len;
    }
    return [besti, bestj, bestsize];
  }
  let matched = 0;
  const queue = [[0, a.length, 0, b.length]];
  while (queue.length) {
    const [alo, ahi, blo, bhi] = queue.pop();
    const [i, j, k] = longestMatch(alo, ahi, blo, bhi);
    if (k) {
      matched += k;
      if (alo < i && blo < j) queue.push([alo, i, blo, j]);
      if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
    }
  }
  return matched;
}
function seqRatio(a, b) {
  const T = a.length + b.length;
  return T ? (2.0 * seqMatchedChars(a, b)) / T : 1.0;
}

// Similarity of two tokens, tuned for EQ item names. Prefix/substring beats raw
// edit-distance, but a contained token must be >=4 chars so 'bo'/'ro' can't ride
// in on 'symbol'/'necro'.
function tokenSim(q, c) {
  if (q === c) return 1.0;
  if (Math.min(q.length, c.length) >= 4 && (c.includes(q) || q.includes(c))) return 0.92;
  const cpl = commonPrefixLen(q, c);
  if (cpl >= 4) return 0.8;
  if (cpl >= 2) { const r = seqRatio(q, c); if (r >= 0.85) return 0.5 * r; }
  return 0.0;
}

// Score a query string against one candidate item name. Returns
// {total, coverage, bestSingle, itemCov, exactAnchor, hits}. itemCov is the
// fraction of the candidate's own idf mass the query EARNED; exactAnchor is the
// strongest exact (sim==1.0) word match, which gates the loud tier.
function score(query, candName, idf) {
  const qToks = tokenize(query, true);
  const cToks = tokenize(candName);
  let total = 0.0, bestSingle = 0.0;
  const hits = [];
  const earned = new Map();
  const earnedExact = new Map();
  for (const q of qToks) {
    let best = 0.0, bestC = null, bestSim = 0.0;
    for (const c of cToks) {
      const sim = tokenSim(q, c);
      if (sim <= 0) continue;
      let w = idf.get(c) || 0.0;
      if (sim < 1.0) {
        const qw = idf.get(q);                 // cap inexact weight by the query
        if (qw !== undefined) w = Math.min(w, qw);  // token's own rarity
      }
      const contrib = sim * w;
      if (contrib > best) { best = contrib; bestC = c; bestSim = sim; }
    }
    if (best > 0) {
      total += best;
      bestSingle = Math.max(bestSingle, best);
      hits.push([q, bestC, Math.round(best * 10) / 10]);
      earned.set(bestC, Math.max(earned.get(bestC) || 0.0, best));
      if (bestSim >= 1.0) earnedExact.set(bestC, Math.max(earnedExact.get(bestC) || 0.0, best));
    }
  }
  const coverage = qToks.length ? hits.length / qToks.length : 0.0;
  let itemIdfTotal = 0.0;
  for (const c of new Set(cToks)) itemIdfTotal += (idf.get(c) || 0.0);
  let earnedSum = 0.0; for (const v of earned.values()) earnedSum += v;
  const itemCov = itemIdfTotal > 0 ? earnedSum / itemIdfTotal : 0.0;
  let exactAnchor = 0.0; for (const v of earnedExact.values()) exactAnchor = Math.max(exactAnchor, v);
  return { total, coverage, bestSingle, itemCov, exactAnchor, hits };
}

function confidence(total, coverage, bestSingle, itemCov, exactAnchor) {
  if (total < T_MAYBE) return null;
  if (itemCov >= ITEM_COV_HIGH && exactAnchor >= EXACT_ANCHOR) return "HIGH";
  if (itemCov >= ITEM_COV_MAYBE && total >= T_HIGH) return "MAYBE";
  return null;
}

// Slang/abbreviation -> canonical item name (mirrors logmon.DEFAULT_ALIASES).
// Expanded into the buy text before scoring, only useful when the canonical item
// is actually in inventory.
const DEFAULT_ALIASES = {
  "fbss": "Flowing Black Silk Sash", "cof": "Cloak of Flames",
  "acof": "Ancient Cloak of Flames", "fungi": "Fungus Covered Scale Tunic",
  "fungi tunic": "Fungus Covered Scale Tunic", "jboots": "Journeyman's Boots",
  "j boots": "Journeyman's Boots", "guise": "Guise of the Deceiver",
  "ssoy": "Short Sword of the Ykesha", "ykesha": "Short Sword of the Ykesha",
  "eot": "Spell: Eye of Tallon", "aon": "Amulet of Necropotence",
  "bcg": "Bone-Clasped Girdle", "boc": "Blade of Carnage",
  "css": "Crystalline Short Sword", "gebs": "Golden Efreeti Boots",
  "lami": "Lamentation", "lammy": "Lamentation",
  "oss": "Obtenebrate Short Sword", "pgt": "Polished Granite Tomahawk",
  "sbs": "Sarnak Battle Shield", "bfg": "Breezeboot's Frigid Gnasher",
  "alex": "Aged Left Eye of Xygoz",
};
function escapeReg(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function compileAliases(aliasesObj) {
  const pats = [];
  for (const k of Object.keys(aliasesObj)) {
    pats.push([new RegExp("\\b" + escapeReg(k) + "\\b", "i"), aliasesObj[k]]);
  }
  return pats;
}
function expandAliases(seg, pats) {
  if (!pats || !pats.length) return seg;
  const extra = [];
  for (const [re, canon] of pats) if (re.test(seg)) extra.push(canon);
  return extra.length ? seg + " " + extra.join(" ") : seg;
}

// Best (tier, score, item) of a WTB segment vs my inventory candidates, or null.
// Top-1: a post is usually one item, and best-only suppresses a weak MAYBE when a
// real HIGH is on the same line. Port of Matcher._best.
function bestSellLead(seg, candidates, idf, aliasPats) {
  if (!seg || !candidates || !candidates.length || !idf) return null;
  seg = expandAliases(seg, aliasPats);
  let best = null;   // { total, tier, item }
  for (const name of candidates) {
    const s = score(seg, name, idf);
    const conf = confidence(s.total, s.coverage, s.bestSingle, s.itemCov, s.exactAnchor);
    if (conf && (best === null || s.total > best.total)) best = { total: s.total, tier: conf, item: name };
  }
  return best ? { tier: best.tier, score: Math.round(best.total * 10) / 10, item: best.item } : null;
}

// All leads for one already-parsed line. ctx: {candidates (inventory names), idf,
// aliasPats, watchlist}. Returns [{kind:'SELL'|'BUY', tier, item, score?}].
//   SELL = poster WTBs an item I OWN (fuzzy)   -> I can sell to them
//   BUY  = poster WTSs an item on my WATCHLIST -> I can buy from them
function matchLine(msg, ctx) {
  const out = [];
  const sell = bestSellLead(buySegments(msg), ctx.candidates, ctx.idf, ctx.aliasPats);
  if (sell) out.push({ kind: "SELL", ...sell });
  for (const item of watchlistHits(msg, ctx.watchlist)) out.push({ kind: "BUY", tier: "HIGH", item });
  return out;
}

const WL = {
  tokenize, phraseTokenGroups, phraseMatch, isKronoTrade,
  segments, sellSegments, buySegments, parseAuctionLine, watchlistHits,
  buildIdf, seqRatio, tokenSim, score, confidence,
  DEFAULT_ALIASES, compileAliases, expandAliases, bestSellLead, matchLine,
  phraseGroupsWithPrice, priceFor, listedItemFor,
};

if (typeof module !== "undefined" && module.exports) module.exports = WL;     // Node (tests)
if (typeof window !== "undefined") window.WL = WL;                             // browser
