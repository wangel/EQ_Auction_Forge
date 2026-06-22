Bug-fix release. Fixes items that share a name in the DB linking the **wrong** item in game.

**What was wrong:** ~5,400 item names in the database are shared by two (or more) genuinely different items — e.g. there's a newbie *Mistmoore Battle Drums* and a level-95 raid one, and *Apothic Robe* is really two items. The macro matched items by **name**, so it could embed the wrong item's link — you'd post the cheap drum and the link in the tunnel would pop the raid drum.

**The fix:** it now matches by the item **ID** that's already in your `/outputfile inventory` dump, so the link is always the exact item you're holding. Prices and vendor values use the ID too. Old saved auction lists still load (they fall back to name matching, same as before).

Nothing else changed — no new features, no settings to touch. If you were on 1.4.2, just drop in the new exe.

**Install:** unzip, run `EQ_Auction_Forge.exe`, keep `items.txt.gz` next to it. No installer, no dependencies.

Heads up: Windows SmartScreen may warn since the exe is an unsigned hobby build — "More info → Run anyway," or just run the source from GitHub if you'd rather.
