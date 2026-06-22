First non-beta 1.4 release. Here's the big stuff since 1.3.7:

- **Live Log Monitor** *(beta)* — watches your log and pings you the second someone WTB/WTS something on your watchlist. Aliases, keyword matching, and sound alerts. Click a match to copy a `/tell <name>` to your clipboard (alt-tab to EQ, Ctrl-V); right-click to silence a spammer. Spot deals while you're tabbed out.
- **Vendor Value** — punch in your Charisma and it estimates the NPC vendor price for every item, so you know what's worth tunneling vs. just dumping on a merchant.
- **Smart 3-band macro** — pricey items become clickable links, mid-tier becomes compact text (still price-tracked), and vendor trash gets pulled out and listed by bag slot.
- **Krono rate auto-syncs** from TLP Auctions at startup, with a Sync button and a "synced @" timestamp so you always know it's current.
- Pricing smarts: amber flags when recent asks are undercutting your price, undercut % that rounds to a clean number, and a "match recent median" button in Recent Postings.

**Install:** unzip, run `EQ_Auction_Forge.exe`, keep `items.txt.gz` next to it. No installer, no dependencies.

Heads up: Windows SmartScreen may warn since the exe is an unsigned hobby build — "More info → Run anyway," or just run the source from GitHub if you'd rather.
