Maintenance release. No feature or gameplay changes — this is purely a packaging fix for the **antivirus false positive** some of you ran into (Windows Defender quarantining `EQ_Auction_Forge.exe`, with a handful of other scanners flagging it too).

**What was wrong:** the app used to ship as a single self-extracting `.exe` (PyInstaller "onefile"). That unpack-to-temp-and-run pattern is exactly what a lot of real malware does, so antivirus *heuristics* flagged it — even though nothing in the app changed and it's the same open-source Python you can read on GitHub. It was a **false positive**, not an infection.

**The fix:** 1.4.4 is built a different way (Nuitka, as a normal program folder instead of a self-extracting exe). The dropper-shaped behavior antivirus was reacting to is simply gone — the new build scans **completely clean, 0 detections** across every major engine, including Microsoft Defender and BitDefender. We're also **reporting the older flagged build to the antivirus vendors as a false positive**, so those warnings clear on their end too.

**What changed for you:** the download is now a **folder** instead of a lone exe — it ships the program plus its support files together. Unzip the whole folder and run `EQ_Auction_Forge.exe` from inside it; keep `items.txt.gz` next to it, same as always. Everything else is identical — your saved auction lists, settings, and aliases carry over untouched.

**Install:** unzip the folder, run `EQ_Auction_Forge.exe`, keep `items.txt.gz` next to it. No installer, no dependencies.

Heads up: Windows SmartScreen may still show an "unknown publisher" notice since this is an unsigned hobby build — "More info → Run anyway." That's a *separate* prompt from the antivirus issue and just means the build isn't code-signed yet.
