"use strict";
// Parity test (web side): runs the webapp's core logic against the shared
// golden corpus in tests/golden/. The desktop runner (parity_desktop.py) checks
// the SAME golden, so if the two implementations ever drift, one of them fails.
//
//   node tests/parity_web.cjs
//
// Keep this in lockstep with parity_desktop.py — same fixtures, same goldens.

const fs = require("fs");
const path = require("path");
const { parseInventory, makeLink } = require("../docs/app/app.js");

const HERE = __dirname;
const golden = (f) => JSON.parse(fs.readFileSync(path.join(HERE, "golden", f), "utf8"));
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The webapp uses camelCase internally; normalize to the canonical snake_case
// keys the golden corpus is written in (the desktop is already snake_case).
const norm = (o) => ({
  name: o.name, location: o.location, count: o.count, id: o.id,
  bag_count: o.bagCount, bag_location: o.bagLocation,
});

let fails = 0;

// --- inventory parsing (carry-bag drop + per-location counts) ---
{
  const txt = fs.readFileSync(path.join(HERE, "fixtures", "parity_inventory.txt"), "utf8");
  const got = parseInventory(txt).map(norm);
  const want = golden("inventory.json");
  if (eq(got, want)) {
    console.log(`PASS inventory parse (${got.length} rows)`);
  } else {
    console.error("FAIL inventory parse");
    console.error("  got :", JSON.stringify(got));
    console.error("  want:", JSON.stringify(want));
    fails++;
  }
}

// --- make_link (DC2 link format) ---
{
  const cases = golden("make_link.json");
  let bad = 0;
  for (const c of cases) {
    const got = makeLink(c.itemlink, c.name);
    if (got !== c.expect) {
      bad++;
      console.error(`FAIL make_link ${JSON.stringify(c.name)} got ${JSON.stringify(got)} want ${JSON.stringify(c.expect)}`);
    }
  }
  if (bad) fails++;
  else console.log(`PASS make_link (${cases.length} cases)`);
}

console.log(fails ? `\n${fails} parity check(s) FAILED` : "\nall parity checks passed");
process.exit(fails ? 1 : 0);
