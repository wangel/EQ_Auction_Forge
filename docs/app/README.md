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
- **Full experience:** serve it over `http://localhost` with the bundled proxy:
  `python docs/app/dev-proxy.py` (from the repo root), then open
  `http://localhost:8000/app/`. It serves `docs/` — the GitHub Pages root — so
  paths resolve exactly like production. In Chrome/Edge the **Write to INI file**
  button can then edit your character INI in place.

## How to test

1. The item DB **auto-loads** when served: it fetches `../items.txt.gz` (i.e.
   `docs/items.txt.gz`, served at the Pages root), decompresses it with the native
   `DecompressionStream`, and caches it in IndexedDB so it's a one-time download
   (the **↻ Reload DB** button busts the cache). Under `file://` fetch is blocked,
   so pick `items.txt.gz` manually instead.

   > **Keep in sync:** `docs/items.txt.gz` is a copy of the repo-root
   > `items.txt.gz` (the desktop build bundles the root one; Pages can only serve
   > files under `docs/`). They must be byte-identical — git stores one shared
   > blob when they are. After a DB refresh, copy root → `docs/` before committing.
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

- **Pricing** (TLP-Auctions API) — the base is `https://tlp-auctions.com/api` and
  its cert is valid, so the **only** remaining blocker is **CORS**: the server
  must send `Access-Control-Allow-Origin` for the page's origin and answer the
  preflight `OPTIONS` (the pricing POST is `Content-Type: application/json`).
  Type prices by hand until that's enabled.
- Vendor-trash filtering, the link/text threshold split, krono handling, the
  live krono rate, Recent Postings, the log monitor.

## Diagnostics

`probe.html` hits the price API from the browser (POST bulk, GET sales, bare GET)
so you can see exactly whether it's reachable — green `HTTP 200` means CORS is
open and pricing can be wired in; a `CORS policy` error in DevTools means the
server still needs the headers above.

**Test pricing locally** — CORS is now enabled for the production origin
(`https://wangel.github.io`), so the deployed app calls the API directly. But
`localhost` isn't a whitelisted origin, so local dev still needs `dev-proxy.py`
(stdlib Python, no deps): it serves `docs/` *and* proxies `/api/*` to
`tlp-auctions.com` server-side (server-to-server calls don't hit CORS):

```
python docs/app/dev-proxy.py     # or: python docs/app/dev-proxy.py 8899  (pick a port)
```

Then open `http://localhost:8000/app/probe.html` and tick **"Use local proxy"** —
calls go same-origin through the proxy. This is a **dev crutch only**; production
stays fully static (direct calls). The proxy checkbox is hidden when not on
localhost.

## Notes / known limits

- In-place INI write is **Chromium-only** (Chrome/Edge/Brave) and needs
  `http(s)`/localhost; the Download path works everywhere.
- The browser can't auto-find your EQ install (no registry access) — you pick the
  files manually.
