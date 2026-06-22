**Update recommended for everyone — especially if you're on 1.4.4 or older.**

The price-check service (tlp-auctions) changed how it's reached. Older versions point at the old address and will **stop being able to price-check or pull Recent Postings** once it goes away. 1.4.5 moves to the new address so pricing keeps working. If you do a lot of EC trading, grab this one.

While I was in there I also fixed a **certificate error** that could pop up on some Windows machines during price checks — it now uses Windows' own certificate store, so it just works on more setups without any fiddling.

**Also new in 1.4.5:**
- **Remove from watchlist on the fly:** in the log monitor, right-click a BUY lead and pick "Remove '<item>' from watchlist." It updates the live matcher and the editor immediately — no reopening anything.
- **Smarter watchlist matching:** uses exact phrase matching, so one line listing several items can't trigger a phantom match by stitching two of your items together.
- **Better update notice:** the "update available" nudge no longer gets clipped on narrow windows, and there's now a one-time "download the update?" prompt at startup so you don't miss it.
- **Version display fixes:** the title bar, Help, and `--help` now show the real version instead of a stale number.

**Install:** unzip the whole folder and run `EQ_Auction_Forge.exe` from inside it. Keep `items.txt.gz` next to it, same as always. No installer, no dependencies — your saved auction lists, settings, and aliases all carry over.

Scans clean — **0 detections** on VirusTotal across all engines.

Heads up: Windows SmartScreen may still show an "unknown publisher" notice (this is an unsigned hobby build) — "More info → Run anyway." That's separate from antivirus; it just means the build isn't code-signed yet.
