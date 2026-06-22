**A quality-of-life update — recommended, not urgent.** 1.4.5's pricing-service fix is the important one; if you're already on 1.4.5 this is just nicer to use.

**New in 1.4.6:**
- **Stops listing near-worthless items.** There's now a "Min profit" floor: if an item would only beat what a vendor pays you by less than this (default 50p), it's pulled out of the macro as vendor-trash instead of cluttering your auction with junk. Tune it with the new **Min profit** box.
- **Won't try to sell the bags you're carrying your wares in.** Inventory loading now skips your active carry-bags, so they don't show up as items to post.
- **CHA and Min profit live with the pricing controls now** — they sit next to Undercut on the right, since they're both about pricing. No more boxes getting clipped down to "00" on the left.
- **"Bags only" no longer gets cut off** on the inventory toolbar.
- **Log monitor keeps your selection.** It used to lose whatever row you had highlighted every time a new auction line came in — so a right-click (copy name / silence / remove from watchlist) could land on the wrong row mid-action. It now holds your selection while new tells stream in.

**Install:** unzip the whole folder and run `EQ_Auction_Forge.exe` from inside it. Keep `items.txt.gz` next to it, same as always. No installer, no dependencies — your saved auction lists, settings, and aliases all carry over.

Scans clean — **0 detections** on VirusTotal across all engines.

Heads up: Windows SmartScreen may still show an "unknown publisher" notice (this is an unsigned hobby build) — "More info → Run anyway." That's separate from antivirus; it just means the build isn't code-signed yet.
