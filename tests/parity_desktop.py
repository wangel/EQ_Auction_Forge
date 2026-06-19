"""Parity test (desktop side): runs the desktop app's core logic against the
shared golden corpus in tests/golden/. The web runner (parity_web.cjs) checks
the SAME golden, so if the two implementations ever drift, one of them fails.

    python tests/parity_desktop.py

Keep this in lockstep with parity_web.cjs -- same fixtures, same goldens.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_app():
    """Import EQ-Auction_Forge.py by path (hyphen in the name blocks a normal
    import). The __main__ guard keeps the GUI from launching. Repo root goes on
    sys.path so the app's own sibling imports (e.g. logmon) resolve."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location(
        "eqaf", os.path.join(ROOT, "EQ-Auction_Forge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def golden(name):
    with open(os.path.join(HERE, "golden", name), encoding="utf-8") as fh:
        return json.load(fh)


def norm(o):
    # Desktop is already snake_case; pick the canonical keys in a stable order.
    return {"name": o["name"], "location": o["location"], "count": o["count"],
            "id": o["id"], "bag_count": o["bag_count"],
            "bag_location": o["bag_location"]}


def main():
    app = load_app()
    fails = 0

    # --- inventory parsing (carry-bag drop + per-location counts) ---
    got = [norm(x) for x in app.load_inventory(
        os.path.join(HERE, "fixtures", "parity_inventory.txt"))]
    want = golden("inventory.json")
    if got == want:
        print("PASS inventory parse (%d rows)" % len(got))
    else:
        print("FAIL inventory parse")
        print("  got :", json.dumps(got))
        print("  want:", json.dumps(want))
        fails += 1

    # --- make_link (DC2 link format) ---
    cases = golden("make_link.json")
    bad = 0
    for c in cases:
        g = app.make_link(c["itemlink"], c["name"])
        if g != c["expect"]:
            bad += 1
            print("FAIL make_link %r got %r want %r" % (c["name"], g, c["expect"]))
    if bad:
        fails += 1
    else:
        print("PASS make_link (%d cases)" % len(cases))

    print("\n%d parity check(s) FAILED" % fails if fails else "\nall parity checks passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
