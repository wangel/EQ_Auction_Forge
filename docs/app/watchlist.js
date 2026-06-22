"use strict";
// Watchlist matcher — web port of the desktop's logmon.py BUY/watchlist path.
// Alerts you when someone is SELLING an item on your watchlist in EC tunnel.
//
// PARITY: this mirrors logmon.py's exact-phrase watchlist logic
//   tokenize / phrase_token_groups / phrase_match / _segments / sell_segments
//   / parse_auction_line / _watchlist_hits
// It is INTENTIONALLY only the watchlist (BUY) direction. The fuzzy IDF scorer
// (the SELL/"someone WTBs what I own" direction) is desktop-only for now.
//
// Watchlist matching is EXACT phrase containment, not fuzzy: sellers link real
// item names, and one line lists many items, so a bag-of-words scorer would
// stitch a phantom match across two items. The watchlist words must appear as a
// contiguous run inside ONE listed item. No alias expansion here (parity: the
// desktop expands aliases only in the fuzzy buy path, not the watchlist).

const STOPWORDS = new Set(['of', 'the', 'a', 'an', 'and', 'for', 'to', 'with',
                           'spell', 'spells', 'song', 'songs']);

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

function tokenize(text) {
  const out = [];
  const toks = text.toLowerCase().match(TOKEN_RE) || [];
  for (const t of toks) {
    if (STOPWORDS.has(t)) continue;
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

const WL = {
  tokenize, phraseTokenGroups, phraseMatch, isKronoTrade,
  segments, sellSegments, buySegments, parseAuctionLine, watchlistHits,
};

if (typeof module !== "undefined" && module.exports) module.exports = WL;     // Node (tests)
if (typeof window !== "undefined") window.WL = WL;                             // browser
