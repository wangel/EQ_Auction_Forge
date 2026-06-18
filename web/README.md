# EQ Auction Forge — web (proof of concept)

A browser port of the core EQ Auction Forge loop, proving the architecture:
**read your inventory locally → build clickable item links → write the INI** —
with **no upload, no server, no install**. Just static files.

This is a scaffold to evaluate the webapp idea, not a finished product.

## Run it

No build step, no Node, no npm. Two ways:

- **Quickest:** double-click `index.html` (opens via `file://`). Everything works
  *except* the in-place INI write (the File System Access API needs a secure
  context) — use the **Download merged INI** button instead.
- **Full experience:** serve the folder over `http://localhost` (e.g.
  `python -m http.server` from this directory, then open
  `http://localhost:8000/`). In Chrome/Edge the **Write to INI file** button can
  then edit your character INI in place.

## How to test

1. Pick `items.txt.gz` (from the repo root) — it's decompressed and parsed in the
   browser via the native `DecompressionStream`.
2. Pick an EQ `/outputfile inventory` dump.
3. Type prices for a few items.
4. **Generate** — the `[Socials]` entries appear (the DC2 link delimiter is shown
   as `·` in the preview so it's visible; the real bytes are written correctly).
5. **Download merged INI** (any browser) or **Write to INI file** (Chrome/Edge):
   both emit proper **ANSI / latin-1** bytes so DC2 (`0x12`) survives.

## What's faithful to the desktop app

Ported 1:1 from `EQ-Auction_Forge.py`: `make_link` (DC2 + hash + space + name,
split by known name), the pipe-delimited `items.txt` columns (id-keyed +
name-fallback), the tab-separated inventory dump parsing (combine stacks by id),
the 255-char / 5-line / 12-button / page-from-2 packing, and the idempotent
`[Socials]` merge (drops old `WTS#`/`Rare#` buttons, leaves hand-made socials and
other sections untouched).

## Deliberately NOT in the PoC

- **Pricing** (TLP-Auctions API) — needs the CORS whitelist + a browser-valid
  cert from the API owner; type prices by hand for now.
- Vendor-trash filtering, the link/text threshold split, krono handling, the
  live krono rate, Recent Postings, the log monitor.

## Notes / known limits

- In-place INI write is **Chromium-only** (Chrome/Edge/Brave) and needs
  `http(s)`/localhost; the Download path works everywhere.
- The browser can't auto-find your EQ install (no registry access) — you pick the
  files manually.
