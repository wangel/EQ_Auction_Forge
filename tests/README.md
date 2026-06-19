# Parity tests

The desktop app (`EQ-Auction_Forge.py`, Python/tkinter) and the web app
(`docs/app/app.js`, JS) are two implementations of the same core selling-loop
logic. These tests keep them honest: both run against **one shared golden
corpus** (`tests/golden/`), so if the two ever drift on shared logic, one side
goes red.

## Run

```sh
node   tests/parity_web.cjs      # web side
python tests/parity_desktop.py   # desktop side
```

Both should print `all parity checks passed` and exit 0.

## What's covered

Both runners feed the same fixtures and compare to the same goldens:

| Check | Fixture | Golden | Web fn | Desktop fn |
|-------|---------|--------|--------|------------|
| Inventory parse — carry-bag drop + per-location (bag vs non-bag) counts | `fixtures/parity_inventory.txt` | `golden/inventory.json` | `parseInventory` | `load_inventory` |
| Item link — DC2 + hash + space + name format | (inline) | `golden/make_link.json` | `makeLink` | `make_link` |

The inventory fixture deliberately exercises: a carried bag (dropped), a nested
bag (kept), equipped gear with augment slots (kept, not a bag), stacks combining
by id, and an item split across a bag + a non-bag slot (so `bag_count < count`).

The corpus normalizes naming differences (the web app is camelCase
`bagCount`/`bagLocation`, the desktop is snake_case `bag_count`/`bag_location`)
to canonical snake_case keys.

## Updating the golden

When you **intentionally** change shared logic, regenerate the golden from the
reference (web) impl and re-run both — the desktop side must then match (port the
change if it doesn't):

```sh
node -e '
const fs=require("fs"), {parseInventory,makeLink}=require("./docs/app/app.js");
const norm=o=>({name:o.name,location:o.location,count:o.count,id:o.id,bag_count:o.bagCount,bag_location:o.bagLocation});
fs.writeFileSync("tests/golden/inventory.json",
  JSON.stringify(parseInventory(fs.readFileSync("tests/fixtures/parity_inventory.txt","utf8")).map(norm),null,2)+"\n");
'
```

## Scope

Only module-level pure functions are cross-checked today (`make_link`,
`load_inventory`). Other shared logic (255/5/12 packing, vendor math, the
link/text threshold split, price parse/classify, the `[Socials]` merge) lives in
`App` methods on the desktop, so it's currently unit-tested on the web side only
— port those by hand and keep them matching. The shared `py ↔ js` function map
lives in `CLAUDE.md`.
