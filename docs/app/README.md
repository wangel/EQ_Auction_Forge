# EQ Auction Forge — web

A browser version of EQ Auction Forge: **load your inventory → pull live prices →
build clickable item links → write your INI** — with **no upload, no server, no
install**. Everything runs locally in your browser; your inventory and INI never
leave your machine.

**Use it:** <https://wangel.github.io/EQ_Auction_Forge/app/>

(Prefer the desktop app? Grab it from
[Releases](https://github.com/wangel/EQ_Auction_Forge/releases/latest).)

## How to use

1. **Item DB** auto-loads the first time and is cached, so it's a one-time
   download (the **↻ Reload DB** button refreshes it).
2. **Load inventory** — pick an EQ `/outputfile inventory` dump (left pane).
   Stacks combine, newbie junk is filtered. **Bags only** / **Inv only** narrow
   the list.
3. **Build the list** — select items and **Add → ** to the auction list.
4. **Price** — **PC All** price-checks the whole list (or **Price Check** the
   selection); the **Undercut %** box trims the median. Type a price by hand any
   time (e.g. `2kr`). **Recent Postings** shows the live sale feed for an item
   with a suggested price. Live krono rate and a vendor-buyback floor are folded
   in automatically.
5. **Generate** — the macro `[Socials]` are built. The **Link if ≥** threshold
   decides which items become embedded clickable **links** vs compact **text**;
   vendor-trash is pulled out and reported.
6. **Write to INI file** (Chrome/Edge, edits in place) or **Copy macros** /
   **Download merged INI** (any browser). The merge is idempotent — it refreshes
   its own `WTS#`/`Rare#` buttons and leaves your hand-made socials untouched.

## Notes

- **In-place INI write** is Chromium-only (Chrome/Edge/Brave) and needs a served
  page (the live link above) — the **Copy** / **Download** paths work in any
  browser.
- The browser can't auto-find your EQ install, so you pick the files manually.
- Item links use the exact DC2 format the game expects and are written as
  ANSI/latin-1, so they paste in as proper clickable links.

## Privacy

The only network calls are the one-time item-DB download and live prices from
[TLP-Auctions](https://www.tlp-auctions.com). Nothing about your characters,
inventory, or INI is ever uploaded.
