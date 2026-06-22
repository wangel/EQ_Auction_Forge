"use strict";
// Parity test (web side) for the watchlist matcher: runs docs/app/watchlist.js
// against the shared golden corpus, which is generated from logmon.py (the
// source of truth). The desktop runner checks the SAME golden, so any drift
// between the JS and Python watchlist logic fails one of them.
//
//   node tests/parity_watchlist.cjs

const fs = require("fs");
const path = require("path");
const WL = require("../docs/app/watchlist.js");

const HERE = __dirname;
const golden = JSON.parse(fs.readFileSync(path.join(HERE, "golden", "watchlist.json"), "utf8"));
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

let fails = 0;
for (const c of golden.cases) {
  const parsed = WL.parseAuctionLine(c.line);
  const got = parsed ? WL.watchlistHits(parsed.msg, golden.watchlist).sort() : [];
  if (!eq(got, c.buy_hits)) {
    fails++;
    console.error(`FAIL ${JSON.stringify(c.line)}`);
    console.error(`  got : ${JSON.stringify(got)}`);
    console.error(`  want: ${JSON.stringify(c.buy_hits)}`);
  }
}

if (fails) console.error(`\n${fails} watchlist parity check(s) FAILED`);
else console.log(`PASS watchlist matcher (${golden.cases.length} cases)\n\nall parity checks passed`);
process.exit(fails ? 1 : 0);
