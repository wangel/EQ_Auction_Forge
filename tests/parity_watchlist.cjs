"use strict";
// Parity test (web side) for the match engine: runs docs/app/watchlist.js against
// the shared golden corpora (generated from logmon.py = source of truth). The
// desktop runner checks the SAME goldens, so any drift between the JS and Python
// matchers fails one of them. Covers both directions + the difflib-ratio port.
//
//   node tests/parity_watchlist.cjs

const fs = require("fs");
const path = require("path");
const WL = require("../docs/app/watchlist.js");

const HERE = __dirname;
const golden = (f) => JSON.parse(fs.readFileSync(path.join(HERE, "golden", f), "utf8"));
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
let fails = 0;

// --- watchlist (BUY) exact-phrase matcher ---
{
  const g = golden("watchlist.json");
  let bad = 0;
  for (const c of g.cases) {
    const parsed = WL.parseAuctionLine(c.line);
    const got = parsed ? WL.watchlistHits(parsed.msg, g.watchlist).sort() : [];
    if (!eq(got, c.buy_hits)) { bad++; console.error(`FAIL watchlist ${JSON.stringify(c.line)}\n  got ${JSON.stringify(got)} want ${JSON.stringify(c.buy_hits)}`); }
  }
  if (bad) fails++; else console.log(`PASS watchlist matcher (${g.cases.length} cases)`);
}

// --- difflib SequenceMatcher.ratio() port ---
{
  const ref = golden("seqratio.json");
  let bad = 0;
  for (const c of ref) {
    const got = Math.round(WL.seqRatio(c.a, c.b) * 1e6) / 1e6;
    if (got !== c.r) { bad++; console.error(`FAIL seqRatio(${c.a},${c.b}) got ${got} want ${c.r}`); }
  }
  if (bad) fails++; else console.log(`PASS seqRatio difflib port (${ref.length} pairs)`);
}

// --- SELL (fuzzy IDF) + mixed-line matchLine ---
{
  const g = golden("sell.json");
  const { idf } = WL.buildIdf(g.db);
  const aliasPats = WL.compileAliases(WL.DEFAULT_ALIASES);
  let bad = 0;
  for (const c of g.cases) {
    const parsed = WL.parseAuctionLine(c.line);
    const leads = parsed ? WL.matchLine(parsed.msg, { candidates: g.inventory, idf, aliasPats, watchlist: g.watchlist }) : [];
    const sellLead = leads.find((l) => l.kind === "SELL");
    const sell = sellLead ? { tier: sellLead.tier, item: sellLead.item } : null;
    const buy = leads.filter((l) => l.kind === "BUY").map((l) => l.item).sort();
    if (!eq(sell, c.sell) || !eq(buy, c.buy)) {
      bad++;
      console.error(`FAIL sell ${JSON.stringify(c.line)}`);
      console.error(`  sell got ${JSON.stringify(sell)} want ${JSON.stringify(c.sell)}`);
      console.error(`  buy  got ${JSON.stringify(buy)} want ${JSON.stringify(c.buy)}`);
    }
  }
  if (bad) fails++; else console.log(`PASS SELL matcher + matchLine (${g.cases.length} cases)`);
}

console.log(fails ? `\n${fails} parity check(s) FAILED` : "\nall parity checks passed");
process.exit(fails ? 1 : 0);
