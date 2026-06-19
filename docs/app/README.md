# EQ Auction Forge — web

A browser port of EQ Auction Forge: **read your inventory locally → pull live
prices → build clickable item links → write the INI** — with **no upload, no
server, no install**. Just static files.

**Live:** <https://wangel.github.io/EQ_Auction_Forge/app/>

Everything runs in your browser. The only network calls are to the
[TLP-Auctions](https://www.tlp-auctions.com) pricing API (which the app links
back to) and the one-time item-DB download. Your inventory and INI never leave
your machine.

## Run it

No build step, no Node, no npm. Two ways:

- **Quickest:** double-click `index.html` (opens via `file://`). Most things work
  *except* the in-place INI write (the File System Access API needs a secure
  context — use **Download merged INI** instead) and the DB auto-load (`file://`
  blocks `fetch` — pick `items.txt.gz` manually).
- **Full experience:** serve it over `http://localhost` with the bundled proxy:
  `python docs/app/dev-proxy.py` (from the repo root), then open
  `http://localhost:8000/app/`. It serves `docs/` — the GitHub Pages root — so
  paths resolve exactly like production, the DB auto-loads, and in Chrome/Edge
  the **Write to INI file** button can edit your character INI in place. The proxy
  is only needed locally (see *Pricing & CORS* below).

## Using it

1. The item DB **auto-loads** when served: it fetches `../items.txt.gz` (i.e.
   `docs/items.txt.gz`, served at the Pages root), decompresses it with the native
   `DecompressionStream`, and caches it in IndexedDB so it's a one-time download
   (the **↻ Reload DB** button busts the cache). Revalidated on each load, so a
   shipped DB update is picked up automatically.

   > **Keep in sync:** `docs/items.txt.gz` is a copy of the repo-root
   > `items.txt.gz` (the desktop build bundles the root one; Pages can only serve
   > files under `docs/`). They must be byte-identical — git stores one shared
   > blob when they are. After a DB refresh, copy root → `docs/` before committing.
2. Pick an EQ `/outputfile inventory` dump (left pane). Stacks combine by item id;
   newbie junk is filtered. **Bags only** / **Inv only** narrow the list.
3. Select items and **Add → ** to the auction list.
4. **Price** them: **PC All** price-checks the whole list, **Price Check** the
   selection, and the **Undercut %** box trims the median (rounded to 5p). Type a
   price by hand any time (e.g. `2kr`). **Recent Postings** shows the live sale
   feed for one item with a divergence hint + *Set price → match recent median*.
   Live krono rate and the CHA-based vendor-buyback floor are pulled in too.
5. **Generate** — the `[Socials]` entries appear. The **Link if ≥** threshold
   splits the list into embedded clickable **links** (valuable/krono) vs compact
   **text** (cheap); vendor-trash is pulled out and reported. (The DC2 delimiter
   is shown as `·` in the preview; the real bytes are written correctly.)
6. **Write to INI file** (Chrome/Edge, in place) or **Copy macros** / **Download
   merged INI** (any browser) — both emit proper **ANSI / latin-1** bytes so DC2
   (`0x12`) survives. The `[Socials]` merge is idempotent (drops old `WTS#`/`Rare#`
   buttons, leaves hand-made socials and other sections untouched).

## Faithful to the desktop app

Ported 1:1 from `EQ-Auction_Forge.py`: `make_link` (DC2 + hash + space + name,
split by known name), id-keyed item lookups (names aren't unique), the
pipe-delimited `items.txt` / tab-separated inventory parsing (stacks combine by
id), the 255-char / 5-line / 12-button / page-from-2 packing, the idempotent
`[Socials]` merge, the bulk pricing + undercut + recent-asks divergence logic,
live krono folding, the CHA vendor-trash band, and the threshold link/text split.

## Not in the web version (desktop-only)

The log monitor, watchlist, the Settings dialog, EQ auto-install detection (the
browser has no registry access — you pick files manually), and the update check.

## Pricing & CORS

The deployed app calls `https://tlp-auctions.com/api` **directly** — tlp-auctions
sends CORS headers for the production origin (`https://wangel.github.io`) on the
bulk-price, sales, and krono endpoints, and allows our `X-Client-App` header
(which tags every request so the API owner can see/measure our traffic).

`localhost` is **not** a whitelisted origin, so for local dev tick **Use local
proxy** and run `dev-proxy.py` (stdlib Python, no deps): it serves `docs/` *and*
proxies `/api/*` to `tlp-auctions.com` server-side (server-to-server calls don't
hit CORS). It's a **dev crutch only**; production stays fully static. The proxy
checkbox is hidden when not on localhost.

```
python docs/app/dev-proxy.py     # or: python docs/app/dev-proxy.py 8899  (pick a port)
```

`probe.html` (`/app/probe.html`) hits the API straight from the browser so you can
see CORS state at a glance — green `HTTP 200` = reachable; a `CORS policy` error
in DevTools = the origin isn't whitelisted.

## Notes / known limits

- In-place INI write is **Chromium-only** (Chrome/Edge/Brave) and needs
  `http(s)`/localhost; the Download/Copy paths work everywhere.
- The browser can't auto-find your EQ install (no registry access) — you pick the
  files manually.
