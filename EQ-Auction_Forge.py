# EQ-Auction_Forge.py
# EQ Auction Macro Builder with price checking
#
# Features:
#   - Uses items.txt.gz database for pre-computed link hashes
#   - Per-item pricing (edit price per item in auction list)
#   - Price check via TLP Auctions API (Frostreaver)
#   - Auto-packs items across lines (max 255 chars/line, 5 lines/button)
#   - Load inventory to filter to items you own

# Requirements: None (stdlib only)

import os
import sys
import gzip
import csv
import json
import argparse
import tempfile
import threading
import re
import webbrowser
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk, filedialog, messagebox, scrolledtext
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote

import ssl

import logmon  # log-monitor matcher + tailer (beta, feature/log-monitor)

try:
    import winsound  # Windows-only; alert beep degrades to silent elsewhere
except ImportError:
    winsound = None

# A windowed PyInstaller build has no console, so sys.stdout/stderr are None.
# Guard against stray print()/traceback writes crashing the app.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

DC2 = '\x12'


def _app_dir():
    """Directory the app lives in — works whether run as a .py, a
    PyInstaller-frozen .exe, or a Nuitka standalone build. Used to locate
    items.txt.gz regardless of the working directory the app was launched
    from."""
    # PyInstaller sets sys.frozen; Nuitka injects a module-level __compiled__.
    # In both packaged cases sys.executable is the real on-disk exe (for a
    # Nuitka standalone build that's the .dist exe, with items.txt.gz beside it).
    if getattr(sys, 'frozen', False) or '__compiled__' in globals():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _cache_dir():
    """User-writable folder for the extracted items.txt, so the app works
    even when installed somewhere read-only (e.g. Program Files)."""
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    d = os.path.join(base, 'EQAuctionForge')
    os.makedirs(d, exist_ok=True)
    return d


def _is_eq_dir(d):
    """A real EverQuest folder has the game/launcher exe."""
    return bool(d) and (os.path.isfile(os.path.join(d, 'eqgame.exe'))
                        or os.path.isfile(os.path.join(d, 'launchpad.exe')))


def _eq_install_dir():
    """EverQuest install dir, or None.
     registry first (Daybreak/SOE 'EverQuest' key,
    'InstallDir' value, HKLM+HKCU, both WOW6432 views), then common fallback
    paths — each validated by the presence of eqgame.exe / launchpad.exe."""
    try:
        import winreg
        subkeys = (
            r"SOFTWARE\WOW6432Node\Daybreak Game Company\EverQuest",
            r"SOFTWARE\Daybreak Game Company\EverQuest",
            r"SOFTWARE\WOW6432Node\Sony Online Entertainment\EverQuest",
            r"SOFTWARE\Sony Online Entertainment\EverQuest",
        )
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in subkeys:
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "InstallDir")
                    if _is_eq_dir(val):
                        return val
                except OSError:
                    continue
    except ImportError:
        pass
    pub = os.environ.get('PUBLIC', r'C:\Users\Public')
    for p in (
        os.path.join(pub, 'Daybreak Game Company', 'Installed Games', 'EverQuest'),
        os.path.join(pub, 'Sony Online Entertainment', 'Installed Games', 'EverQuest'),
        r'C:\EverQuest',
        r'D:\Games\EverQuest',
    ):
        if _is_eq_dir(p):
            return p
    return None


def _eq_logs_dir():
    """<EQ>\\Logs from the registry, or None."""
    d = _eq_install_dir()
    if not d:
        return None
    logs = os.path.join(d, 'Logs')
    return logs if os.path.isdir(logs) else d


def _newest_eqlog(logs_dir, server):
    """Most recently written eqlog_<Char>_<server>.txt in logs_dir — i.e. the
    character you're actively playing on that server. None if none found."""
    import glob
    files = glob.glob(os.path.join(logs_dir, f"eqlog_*_{(server or '').lower()}.txt"))
    return max(files, key=os.path.getmtime) if files else None


ITEMS_DB = os.path.join(_app_dir(), "items.txt.gz")
SERVER = "Frostreaver"
# Servers selectable in the UI dropdown. The box is editable, so a server
# not listed here can still be typed in for price checks. Only Frostreaver is
# listed for now — once a TLP gets Bazaar, tlp-auctions data dries up. Add new
# TLPs (or EQ Legends) here when they launch.
SERVERS = ["Frostreaver"]
_config = {"server": "Frostreaver"}
# Endpoints hang off /api (e.g. {API_BASE}/api/prices/bulk). Use the apex host,
# NOT the api. subdomain — the TLS cert covers tlp-auctions.com but its SAN does
# NOT include api.tlp-auctions.com, so the subdomain fails hostname verification.
API_BASE = "https://tlp-auctions.com"

# EQ socials layout: 10 pages, 12 buttons per page.
BUTTONS_PER_PAGE = 12
MAX_PAGE = 10

# --- Character INI validation -------------------------------------------------
# The character settings file is named like  YourChar_server.ini  and lives in
# the EverQuest folder. Users routinely point the file picker at the wrong .ini,
# which either does nothing useful or clobbers an unrelated config. These help us
# catch the common mistakes before we write.

# Known EQ files that are NOT the character settings INI (basename, lowercased).
INI_BLOCKLIST = {
    'eqclient.ini': "the global EverQuest client config",
    'eqlsplayerdata.ini': "the login-server data file",
    'eqhost.txt': "the server host list",
}

# Section headers that only appear in a real character settings INI. A valid
# character file has at least one of these (we write into [Socials]).
EQ_CHAR_SECTIONS = ('[socials]', '[hot buttons]', '[key mapping]',
                    '[keymapping]', '[uisettings]', '[chatchannels]')

# SSL context for API calls. Verification is ON: the apex host tlp-auctions.com
# has a valid cert (Amazon-issued), so we no longer disable it. The old
# CERT_NONE workaround only existed to tolerate the api.-subdomain hostname
# mismatch, which the corrected API_BASE above removes.
_ssl_ctx = ssl.create_default_context()


def load_item_database(gz_path):
    """Load the item DB. Returns (links, ids, prices, by_id):
      links:  name -> itemlink (hash+name) for building clickable links
      ids:    name -> item id (int) for the bulk price API
      prices: name -> base merchant value in COPPER (the 'price' column),
              used to estimate NPC vendor value (1pp = 1000cp)
      by_id:  item id (int) -> {'link', 'price', 'name'} — the unambiguous
              lookups. The DB has DISTINCT items sharing a display name (e.g.
              two 'Mistmoore Battle Drums', ids 10177 vs 81785), so the name
              maps above silently collide; matching on the id from an inventory
              dump disambiguates exactly. Name maps are first-row-wins fallbacks
              for items with no known id (DB search, log monitor, legacy saves).
    """
    items = {}
    ids = {}
    prices = {}
    by_id = {}
    dup_names = 0
    txt_path = os.path.join(_cache_dir(), 'items.txt')
    gz_ok = os.path.isfile(gz_path)
    # Re-extract when the cache is missing OR the bundled .gz is newer than the
    # cached .txt (i.e. a new release shipped fresh data) — otherwise reuse the
    # cache so startup stays fast.
    stale = True
    if os.path.isfile(txt_path):
        try:
            stale = gz_ok and os.path.getmtime(gz_path) > os.path.getmtime(txt_path)
        except OSError:
            stale = False
    if stale:
        if not gz_ok:
            if not os.path.isfile(txt_path):
                return items, ids, prices      # no source and no cache
        else:
            print(f"  Extracting {gz_path} (new/updated database)...")
            with gzip.open(gz_path, 'rt', encoding='utf-8', errors='ignore') as f:
                with open(txt_path, 'w', encoding='utf-8') as out:
                    out.write(f.read())
    print(f"  Loading items...")
    with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f, delimiter='|')
        for row in reader:
            try:
                name = row.get('name', '').strip()
                link = row.get('itemlink', '').strip()
                if name and link:
                    pr = row.get('price', '').strip()
                    price = int(pr) if pr.isdigit() else None
                    item_id = row.get('id', '').strip()
                    iid = int(item_id) if item_id.isdigit() else None
                    # id-keyed maps are the unambiguous source (ids are unique).
                    if iid is not None:
                        by_id[iid] = {'link': link, 'price': price, 'name': name}
                    # Name-keyed fallbacks are FIRST-row-wins across the board so
                    # link/id/price agree on a name collision (they used to not).
                    if name in items:
                        dup_names += 1
                    else:
                        items[name] = link
                        if iid is not None:
                            ids[name] = iid
                        if price is not None:
                            prices[name] = price
            except Exception:
                continue
    print(f"  {len(items)} items loaded")
    if dup_names:
        print(f"  ({dup_names} duplicate item name(s) in DB — id matching "
              f"disambiguates inventory items)")
    return items, ids, prices, by_id


# Worthless newbie/starter items that clutter the inventory list but never get
# sold in EC tunnel. Matched by exact (case-insensitive) name so we don't nuke
# named gear that happens to share a word (e.g. "Dagger" the newbie item vs.
# "Ceremonial Dagger"). Add more here as you spot them.
# Built-in worthless newbie/starter junk, always dropped from inventory loads.
# The user can layer their own names on top via Settings (see EXCLUDED_ITEMS).
_DEFAULT_EXCLUDED = frozenset(n.lower() for n in (
    "Backpack",
    "Small Box",
    "Dagger",
    "Skin of Milk",
    "Bread Cakes",
    "Gloomingdeep Lantern",
    "Ethereal Dreamweave Satchel",
    "Dreamweave Satchel",
))

# Effective filter set = built-ins + the user's Settings additions. Rebuilt from
# settings at startup and whenever the user edits the list (apply_runtime_settings).
EXCLUDED_ITEMS = set(_DEFAULT_EXCLUDED)


def load_inventory(filepath):
    """Read an EQ /outputfile inventory dump (tab-separated).

    The dump has a Count column, and stackable items (spells, potions, etc.)
    or duplicate gear show up on separate lines per slot. We combine entries
    with the same name into one, summing their counts, so a stack of 2 scrolls
    in two slots becomes a single 'x2' entry. Worthless newbie/starter items
    (see EXCLUDED_ITEMS) are dropped. Returns a list of
    {'name', 'location', 'count'} in first-seen order.
    """
    combined = {}  # key (id or name) -> {'name', 'location', 'count', 'id'}
    order = []
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        header = None
        for line in f:
            line = line.rstrip('\n\r')
            if not line:
                continue
            parts = line.split('\t')
            if header is None:
                header = [p.strip().lower() for p in parts]
                continue
            if len(parts) < 3:
                continue
            ni = header.index('name') if 'name' in header else 1
            li = header.index('location') if 'location' in header else 0
            ci = header.index('count') if 'count' in header else None
            ii = header.index('id') if 'id' in header else None
            name = parts[ni].strip().rstrip('*')
            loc = parts[li].strip()
            # '' / 'empty' = empty slots; 'name' = the KeyRing sub-header row
            # at the bottom of the dump leaking in as a phantom item.
            if name.lower() in ('', 'empty', 'name'):
                continue
            if name.lower() in EXCLUDED_ITEMS:
                continue
            count = 1
            if ci is not None and ci < len(parts):
                try:
                    count = max(int(parts[ci].strip()), 1)
                except ValueError:
                    count = 1
            item_id = 0
            if ii is not None and ii < len(parts):
                try:
                    item_id = max(int(parts[ii].strip()), 0)
                except ValueError:
                    item_id = 0
            # Combine stacks/duplicate slots by id (stackables share an id) so
            # two DISTINCT items that share a display name stay separate rows;
            # fall back to name when the dump has no id column (item_id == 0).
            key = item_id if item_id else name
            if key in combined:
                combined[key]['count'] += count
            else:
                combined[key] = {'name': name, 'location': loc,
                                 'count': count, 'id': item_id}
                order.append(key)
    return [combined[k] for k in order]


def make_link(itemlink, item_name):
    """
    Build an EQ item link: DC2 + hash + SPACE + name + DC2

    Uses the known item name to find where the hash ends,
    since item names starting with A-F would break hex detection.
    """
    if itemlink.endswith(item_name):
        hash_part = itemlink[:-len(item_name)]
        return f"{DC2}{hash_part} {item_name}{DC2}"
    # Fallback: return as-is with no extra space
    return f"{DC2}{itemlink}{DC2}"


BULK_PRICE_LIMIT = 10  # API accepts up to 10 item ids per bulk request
DEFAULT_KRONO_RATE = 4000  # fallback for kr display when the API reports 0


# ---- User settings (settings.json in the cache dir) -----------------------
# A small global config the user can tune from the Settings dialog: the krono
# fallback rate, the default values the macro-builder boxes start with, and
# extra inventory-filter names layered onto the built-in _DEFAULT_EXCLUDED set.
# Lives next to aliases.json/watchlist.json so all user data is in one place.
DEFAULT_SETTINGS = {
    'krono_rate': 4000,
    'krono_synced_at': None,  # epoch secs of the last successful live krono sync
    'defaults': {
        'prefix': '/auc WTS',
        'page': '2',
        'suffix': '',
        'threshold': '600p',
        'undercut': '0',
        'cha': '75',
    },
    'excluded': [],   # user-added filter names (built-ins always apply too)
}


def _settings_path():
    return os.path.join(_cache_dir(), 'settings.json')


def load_settings():
    """Read settings.json, overlaying it on DEFAULT_SETTINGS so a partial or
    missing/corrupt file still yields a complete, well-typed settings dict."""
    s = {
        'krono_rate': DEFAULT_SETTINGS['krono_rate'],
        'krono_synced_at': None,
        'defaults': dict(DEFAULT_SETTINGS['defaults']),
        'excluded': [],
    }
    try:
        with open(_settings_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return s
    if not isinstance(data, dict):
        return s
    try:
        rate = int(data.get('krono_rate'))
        if rate > 0:
            s['krono_rate'] = rate
    except (TypeError, ValueError):
        pass
    try:
        ts = float(data.get('krono_synced_at'))
        if ts > 0:
            s['krono_synced_at'] = ts
    except (TypeError, ValueError):
        pass
    if isinstance(data.get('defaults'), dict):
        for k in s['defaults']:
            if data['defaults'].get(k) is not None:
                s['defaults'][k] = str(data['defaults'][k])
    if isinstance(data.get('excluded'), list):
        s['excluded'] = sorted({str(x).strip() for x in data['excluded'] if str(x).strip()},
                               key=str.lower)
    return s


def save_settings(settings):
    """Persist the settings dict to settings.json."""
    with open(_settings_path(), 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, sort_keys=True)


def apply_runtime_settings(settings):
    """Push settings into the module globals the rest of the code reads: the
    krono fallback rate and the effective EXCLUDED_ITEMS set (built-ins + the
    user's additions). Called at startup and after the Settings dialog saves."""
    global DEFAULT_KRONO_RATE, EXCLUDED_ITEMS
    DEFAULT_KRONO_RATE = settings.get('krono_rate') or 4000
    user_ex = {str(n).strip().lower()
               for n in settings.get('excluded', ()) if str(n).strip()}
    EXCLUDED_ITEMS = set(_DEFAULT_EXCLUDED) | user_ex


APP_VERSION = "1.4.5"

# Identify ourselves to the TLP Auctions API. Their operator asked tool authors
# to send a custom User-Agent so legit tool traffic isn't mistaken for spam and
# blocked, so we name the app + version and link the repo rather than spoofing
# a browser.
_API_HEADERS = {
    'User-Agent': f'EQ-Auction_Forge/{APP_VERSION} '
                  '(+https://github.com/wangel/EQ_Auction_Forge)',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}

# Startup update check against GitHub releases.
RELEASES_API = "https://api.github.com/repos/wangel/EQ_Auction_Forge/releases/latest"
RELEASES_URL = "https://github.com/wangel/EQ_Auction_Forge/releases"
# Feedback / bug reports — opens a pre-filled new GitHub issue.
ISSUES_URL = ("https://github.com/wangel/EQ_Auction_Forge/issues/new"
              f"?labels=feedback&title=%5Bv{APP_VERSION}%5D+")


def _version_tuple(v):
    """'v1.3.6' / '1.3.6' -> (1, 3, 6). Non-numeric parts are ignored so the
    comparison never raises on a weird tag."""
    return tuple(int(n) for n in re.findall(r'\d+', v or ''))


def check_latest_release():
    """Return the latest GitHub release version (e.g. '1.3.6'), or None on any
    failure. Best-effort and never raises — a version check must not break
    startup. Uses default (verified) SSL since GitHub's cert is valid."""
    try:
        req = Request(RELEASES_API, headers={
            'User-Agent': _API_HEADERS['User-Agent'],
            'Accept': 'application/vnd.github+json',
        })
        with urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        tag = (data.get('tag_name') or '').lstrip('v').strip()
        return tag or None
    except Exception:
        return None


def fetch_prices_bulk(item_ids, server=SERVER):
    """POST a batch of item ids (max 10) to the bulk price endpoint.

    Returns (results, krono_rate, error):
      results:    dict of itemId -> {medianPlatPrice, sampleSize, hasData, item}
      krono_rate: plat per krono on this server (0 if unknown)
      error:      error string, or None on success
    """
    ids = list(item_ids)[:BULK_PRICE_LIMIT]
    if not ids:
        return {}, 0, None
    try:
        url = f"{API_BASE}/api/prices/bulk"
        body = json.dumps({"serverName": server, "itemIds": ids}).encode()
        req = Request(url, data=body, headers=_API_HEADERS, method='POST')
        with urlopen(req, timeout=10, context=_ssl_ctx) as r:
            data = json.loads(r.read().decode())
        krono_rate = float(data.get('kronoRate') or 0)
        if krono_rate <= 0:
            krono_rate = DEFAULT_KRONO_RATE  # API didn't report a rate; use fallback
        results = {it.get('itemId'): it for it in data.get('items', [])}
        return results, krono_rate, None
    except Exception as e:
        return {}, 0, f"Error: {e}"


RECENT_SALES_LIMIT = 8  # postings shown by the "Recent Postings" reference lookup
# Only items whose bulk median is at/above this get a recent-asks lookup during a
# price check (krono auto-resolve + plat divergence flag). Bounds the extra per-item
# calls to the high-value items where mispricing actually costs plat.
RECENT_CHECK_FLOOR = 1000


def fetch_recent_sales(item_name, server=SERVER, limit=RECENT_SALES_LIMIT):
    """Fetch the most recent individual postings for ONE item, newest first.

    Uses the /api/sales feed with an exact-name match (so only this item's
    postings come back). Returns (sales, error):
      sales: list of {datetime, transactionType, platPrice, kronoPrice, auctioneer}
      error: error string, or None on success
    """
    try:
        qs = urlencode({'searchTerm': item_name, 'exactMatch': 'true',
                        'serverName': server, 'pageSize': limit})
        url = f"{API_BASE}/api/sales?{qs}"
        req = Request(url, headers=_API_HEADERS, method='GET')
        with urlopen(req, timeout=10, context=_ssl_ctx) as r:
            data = json.loads(r.read().decode())
        return data.get('items', [])[:limit], None
    except Exception as e:
        return [], f"Error: {e}"


def fetch_krono_rate(server=SERVER):
    """Current krono->plat rate from tlp-auctions' windowed krono feed, using the
    **1-day** average. EC krono moves fast — the flat `/krono-prices/{server}`
    endpoint and the 7-day window both lag (observed ~2k under the 1-day), so we
    take the tightest window with samples. Returns an int plat-per-krono, or None
    on failure (caller keeps its existing fallback rate)."""
    try:
        url = f"{API_BASE}/api/krono-prices/{quote(server)}/windows"
        req = Request(url, headers=_API_HEADERS, method='GET')
        with urlopen(req, timeout=10, context=_ssl_ctx) as r:
            data = json.loads(r.read().decode())
        by_days = {w.get('days'): w for w in data.get('windows', [])}
        for d in (1, 2, 3, 7):  # prefer the freshest window that actually has data
            w = by_days.get(d)
            if w and w.get('sampleSize', 0) > 0 and w.get('averagePrice', 0) > 0:
                return int(round(w['averagePrice']))
        return None
    except Exception:
        return None


def format_sale_age(iso_str):
    """Turn an ISO UTC timestamp (e.g. 2026-06-05T17:37:57Z) into a short
    local-time + relative-age string for the postings list."""
    try:
        dt = datetime.strptime(iso_str, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc)
    except Exception:
        return iso_str or "?"
    local = dt.astimezone()
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 3600:
        ago = f"{max(secs // 60, 0)}m ago"
    elif secs < 86400:
        ago = f"{secs // 3600}h ago"
    else:
        ago = f"{secs // 86400}d ago"
    return f"{local.strftime('%m/%d %I:%M%p')} ({ago})"


def format_posting_price(plat, krono):
    """Format one posting's price. A posting is in either plat or krono."""
    if krono and krono > 0:
        return f"{krono:g}kr"
    if plat and plat > 0:
        return f"{int(plat)}p"
    return "—"


# --- NPC vendor value -------------------------------------------------------
# What an NPC merchant pays you for an item = base_value x M(CHA), where M is
# I"m pretty sure this is pretty accurate --- I ended up looking at the code in
# eqemu to get a general idea.
#
# CHARACTER-WIDE (item-independent; 'sellrate' does NOT affect buyback on Live).
# Calibrated from measured Frostreaver (EQ Live TLP) home-vendor buyback — NOT
# the EQEmu rule constants (those don't govern Daybreak). Measured: M rises a
# flat ~0.4%/CHA and HARD-CAPS at 1/1.05 (~95.24%, the merchant markup
# reciprocal), reached around CHA ~92 — beyond that more CHA does nothing
# (verified: CHA 95 and 130 give the identical payout). Two non-capped points
# (65->0.844, 80->0.904) set the line. Re-measure if it drifts / for other factions.
_VENDOR_SLOPE = 0.004           # M gained per CHA point
_VENDOR_INTERCEPT = 0.584       # M = SLOPE*CHA + INTERCEPT, below the cap
_VENDOR_CAP = 1.0 / 1.05        # ~0.95238 ceiling, reached ~CHA 92


def vendor_multiplier(cha):
    """Fraction of an item's base value an NPC pays you at the given CHA — a flat
    line that hard-caps at _VENDOR_CAP (so high CHA past ~92 gains nothing)."""
    return max(0.0, min(_VENDOR_SLOPE * cha + _VENDOR_INTERCEPT, _VENDOR_CAP))


def vendor_value_pp(price_cp, cha):
    """Estimated NPC buyback in plat (float) for a base price in copper."""
    return (price_cp / 1000.0) * vendor_multiplier(cha)


class AuctionBuilder:
    def __init__(self, db_path=ITEMS_DB):
        self.db_path = db_path
        self.item_db = {}
        self.item_ids = {}
        self.item_prices = {}  # name -> base value in copper (for vendor estimate)
        self.item_by_id = {}   # id -> {'link','price','name'}; unambiguous lookup
        self.inv_row_id = {}   # inventory tree iid -> item id, for exact actions
        self._last_median = {}  # name -> last bulk-API median (plat), for the recent-asks hint
        self.inventory = []
        self.inv_by_name = {}  # name -> inventory entry, for count/location lookups
        self.auction_items = []  # list of {'name', 'price', 'count'}
        self.inv_loaded = False
        # Per-(tree, column) ascending/descending toggle for header sorting.
        self._sort_state = {}
        self._last_inv_dir = ''    # remembered inventory folder (defaults to EQ root)
        # Log monitor (beta): built lazily on first Start.
        self._last_ini_path = ''   # remembered char INI, for guessing the log path
        self._matcher = None       # logmon.Matcher (idf is ~1s to build, so cache)
        self._log_monitor = None   # logmon.LogMonitor while running
        self._lm_win = None        # the Toplevel window
        self._user_aliases = None  # lazily loaded from aliases.json
        self._alias_win = None     # the alias editor Toplevel
        self._watchlist = None     # lazily loaded from watchlist.json (items I want)
        self._watch_win = None     # the watchlist editor Toplevel
        self._silenced = None      # lazily loaded set of items muted from dinging
        self._settings_win = None  # the Settings dialog Toplevel

        # User settings (krono rate, box defaults, extra filter names). Loaded
        # before the UI so the toolbar boxes seed from the saved defaults, and
        # applied so the module globals (DEFAULT_KRONO_RATE / EXCLUDED_ITEMS) match.
        self.settings = load_settings()
        apply_runtime_settings(self.settings)
        # Effective krono rate: starts at the settings fallback, replaced by the
        # live tlp-auctions 1-day avg once _refresh_krono_rate lands (see __init__).
        self.krono_rate = self.settings['krono_rate']
        # Epoch secs of the last successful live krono sync (persisted across runs);
        # drives the "krono synced @ …" notes in the top bar and Settings.
        self.krono_synced_at = self.settings.get('krono_synced_at')

        self.root = tk.Tk()
        self.root.title(f"EQ Auction Forge v{APP_VERSION} — by wangel")
        self.root.configure(bg='#1a1a1a')
        # Open wide enough for the right-side price controls + columns, centered,
        # with a sane minimum so it can't be squished into uselessness. Resizes
        # freely from there.
        self._center_window(1250, 840, min_w=1040, min_h=620)
        self._build_ui()
        self.root.after(100, self._load_db)
        self.root.after(800, self._check_update)
        self.root.after(900, self._refresh_krono_rate)

    def _center_window(self, w, h, min_w=None, min_h=None):
        """Open at w×h centered on screen — clamped to fit smaller screens — and
        set a minimum size so it can't be squished. Free to resize after."""
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        x, y = max((sw - w) // 2, 0), max((sh - h) // 3, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        if min_w and min_h:
            self.root.minsize(min(min_w, w), min(min_h, h))

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#1a1a1a')
        style.configure('TLabel', background='#1a1a1a', foreground='#cccccc',
                        font=('Consolas', 10))
        style.configure('TButton', font=('Consolas', 9))
        style.configure('Treeview', background='#2a2a2a', foreground='#cccccc',
                        fieldbackground='#2a2a2a', font=('Consolas', 9))
        style.configure('Treeview.Heading', font=('Consolas', 9, 'bold'))
        style.configure('TCombobox', fieldbackground='#2a2a2a',
                        background='#2a2a2a', foreground='#cccccc',
                        font=('Consolas', 9))
        # Dark theme for the combobox dropdown list
        self.root.option_add('*TCombobox*Listbox.background', '#2a2a2a')
        self.root.option_add('*TCombobox*Listbox.foreground', '#cccccc')
        self.root.option_add('*TCombobox*Listbox.selectBackground', '#444444')

        # === Top ===
        top = ttk.Frame(self.root)
        top.pack(fill='x', padx=10, pady=5)
        ttk.Button(top, text="Load Inventory", command=self._load_inventory).pack(side='left')
        ttk.Button(top, text="Log Monitor", command=self._open_log_monitor).pack(side='left', padx=4)
        self.status_var = tk.StringVar(value="Loading...")
        ttk.Label(top, textvariable=self.status_var, foreground='#888888').pack(side='left', padx=10)

        # Server selector (drives price checks). Editable so any server can be typed.
        ttk.Label(top, text="Server:").pack(side='left', padx=(10, 2))
        self.server_var = tk.StringVar(value=_config["server"])
        self.server_var.trace_add(
            'write', lambda *a: _config.__setitem__('server', self.server_var.get().strip()))
        ttk.Combobox(top, textvariable=self.server_var, values=SERVERS,
                     width=13).pack(side='left')

        # Krono-rate note: shows the live rate + when it last synced from TLP
        # Auctions. Seeded from the persisted timestamp; updated by every sync.
        self.krono_status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.krono_status_var,
                  foreground='#888888').pack(side='left', padx=12)
        self._update_krono_label()  # seed from persisted rate/timestamp

        # Update nudge — packed early on the LEFT so it has pack priority and is
        # never clipped at narrow window widths (it was previously the last
        # right-packed widget, so it was the first to vanish when the toolbar ran
        # out of room — the one notice we most want seen). Stays blank/zero-width
        # unless a newer release exists. Click it to open the releases page.
        self.update_var = tk.StringVar(value="")
        self._update_lbl = ttk.Label(top, textvariable=self.update_var,
                                     foreground='#FFA500',
                                     font=('Consolas', 9, 'bold'), cursor='hand2')
        self._update_lbl.pack(side='left', padx=8)
        self._update_lbl.bind('<Button-1>', lambda e: webbrowser.open(RELEASES_URL))

        self.db_count_var = tk.StringVar()
        ttk.Label(top, textvariable=self.db_count_var, foreground='#00ff00').pack(side='right')
        ttk.Button(top, text="Help", command=self._show_help).pack(side='right', padx=5)
        ttk.Button(top, text="Settings", command=self._open_settings).pack(side='right', padx=5)

        # Feedback nudge — opens a pre-filled GitHub issue (version baked into title).
        feedback_lbl = ttk.Label(top, text="Feedback / report a bug",
                                 foreground='#5fafff',
                                 font=('Consolas', 9, 'underline'), cursor='hand2')
        feedback_lbl.pack(side='right', padx=5)
        feedback_lbl.bind('<Button-1>', lambda e: webbrowser.open(ISSUES_URL))

        # === Paned ===
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=10, pady=5)

        # --- Left: Item browser ---
        left = ttk.Frame(paned)
        paned.add(left, weight=2)

        lf = ttk.Frame(left)
        lf.pack(fill='x')
        ttk.Label(lf, text="Items (dbl-click / Enter to add · Shift+click range · "
                           "Ctrl+A all)").pack(side='left')
        self.item_count_var = tk.StringVar()
        ttk.Label(lf, textvariable=self.item_count_var, foreground='#666666').pack(side='right')

        ff = ttk.Frame(left)
        ff.pack(fill='x', pady=2)
        ttk.Label(ff, text="Search:").pack(side='left')
        self.filter_var = tk.StringVar()
        fe = ttk.Entry(ff, textvariable=self.filter_var, width=30)
        fe.pack(side='left', padx=5)
        fe.bind('<Return>', lambda e: self._apply_filter())
        ttk.Button(ff, text="Search", command=self._apply_filter).pack(side='left')
        self.inv_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ff, text="Inv only", variable=self.inv_only_var,
                        command=self._apply_filter).pack(side='left', padx=10)
        # Bags only: hide equipped/bank/keyring, show just items sitting in your
        # general-inventory bags (location starts with 'General').
        self.bags_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ff, text="Bags only", variable=self.bags_only_var,
                        command=self._apply_filter).pack(side='left')
        # Your character's CHA -> drives the "Vendor pp" estimate column. Default
        # 75 (Human baseline; base-race CHA averages ~60 but real leveled/buffed
        # toons run higher) — editable per character.
        ttk.Label(ff, text="CHA:").pack(side='left', padx=(10, 2))
        self.cha_var = tk.StringVar(value=self.settings['defaults']['cha'])
        ttk.Entry(ff, textvariable=self.cha_var, width=5).pack(side='left')
        self.cha_var.trace_add('write', self._on_cha_change)

        # Tree + scrollbar live in their own frame so a button row can sit
        # below them (mixing pack sides in one parent gets messy otherwise).
        tree_frame = ttk.Frame(left)
        tree_frame.pack(side='top', fill='both', expand=True)
        cols = ('name', 'qty', 'location', 'vendor')
        # selectmode='extended' lets the user shift/ctrl-click a range of items
        # and add them all at once instead of double-clicking each.
        self.item_tree = ttk.Treeview(tree_frame, columns=cols, show='headings',
                                      height=20, selectmode='extended')
        # Click a header to sort by that column (toggles asc/desc). Location
        # uses a natural sort so 'General 2-Slot10' lands after 'Slot9'.
        self.item_tree.heading(
            'name', text='Item Name',
            command=lambda: self._sort_column(self.item_tree, 'name'))
        self.item_tree.heading(
            'qty', text='Qty',
            command=lambda: self._sort_column(self.item_tree, 'qty'))
        self.item_tree.heading(
            'location', text='Location',
            command=lambda: self._sort_column(self.item_tree, 'location'))
        self.item_tree.heading(
            'vendor', text='Vendor pp',
            command=lambda: self._sort_column(self.item_tree, 'vendor'))
        self.item_tree.column('name', width=250)
        self.item_tree.column('qty', width=40, anchor='center')
        self.item_tree.column('location', width=110)
        self.item_tree.column('vendor', width=64, anchor='e')
        sb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.item_tree.yview)
        self.item_tree.configure(yscrollcommand=sb.set)
        self.item_tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.item_tree.bind('<Double-1>', self._add_to_auction)
        # Explorer-style keyboard: Ctrl+A selects everything, Enter adds the
        # current selection to the auction list.
        self._bind_select_all(self.item_tree)
        self.item_tree.bind('<Return>', lambda e: self._add_selected_to_auction())

        # Actions on the item list (work on the current selection — one or many)
        ibf = ttk.Frame(left)
        ibf.pack(side='top', fill='x', pady=3)
        ttk.Button(ibf, text="Add Selected →",
                   command=self._add_selected_to_auction).pack(side='left')
        ttk.Button(ibf, text="Price Check",
                   command=self._price_check_left).pack(side='left', padx=3)
        ttk.Button(ibf, text="Recent Postings",
                   command=self._recent_postings).pack(side='left', padx=3)

        # --- Right: Auction builder ---
        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        ttk.Label(right, text="Auction (dbl-click / Del to remove · "
                              "Ctrl+A all)").pack(anchor='w')

        auc_frame = ttk.Frame(right)
        auc_frame.pack(fill='both', expand=True)
        # extended selectmode so several rows can be removed/repriced at once.
        self.auc_tree = ttk.Treeview(auc_frame, columns=('name', 'price', 'qty', 'vendor'),
                                     show='headings', height=8,
                                     selectmode='extended')
        # Sortable headers — e.g. after PC All, sort by Price to find/multi-select
        # the cheap items. Sorting reorders the backing auction_items list too.
        self.auc_tree.heading(
            'name', text='Item',
            command=lambda: self._sort_column(self.auc_tree, 'name'))
        self.auc_tree.heading(
            'price', text='Price',
            command=lambda: self._sort_column(self.auc_tree, 'price'))
        self.auc_tree.heading(
            'qty', text='Qty',
            command=lambda: self._sort_column(self.auc_tree, 'qty'))
        self.auc_tree.heading(
            'vendor', text='Vendor',
            command=lambda: self._sort_column(self.auc_tree, 'vendor'))
        self.auc_tree.column('name', width=170)
        self.auc_tree.column('price', width=70)
        self.auc_tree.column('qty', width=36, anchor='center')
        self.auc_tree.column('vendor', width=60, anchor='e')
        # Orange = worth more to a vendor than to players (auto-excluded from macros).
        self.auc_tree.tag_configure('VENDOR', foreground='#ff9900')
        self.auc_tree.tag_configure('KRONO', foreground='#cc99ff')  # krono-priced row
        self.auc_tree.tag_configure('DIVERGE', foreground='#ffd24d')  # recent asks << your price
        auc_sb = ttk.Scrollbar(auc_frame, orient='vertical', command=self.auc_tree.yview)
        self.auc_tree.configure(yscrollcommand=auc_sb.set)
        self.auc_tree.pack(side='left', fill='both', expand=True)
        auc_sb.pack(side='right', fill='y')
        self.auc_tree.bind('<Double-1>', self._remove_from_auction)
        self.auc_tree.bind('<Delete>', self._remove_from_auction)
        self.auc_tree.bind('<<TreeviewSelect>>', self._on_auction_select)
        self._bind_select_all(self.auc_tree)

        # Color key — each word painted in its row-tag color so the meaning is
        # self-evident. The amber entry steers to Recent Postings (the action).
        legend = ttk.Frame(right)
        legend.pack(fill='x', pady=(2, 0))
        ttk.Label(legend, text="Colors:", foreground='#888888',
                  font=('Consolas', 8)).pack(side='left')
        for txt, color in (("krono", '#cc99ff'), ("vendor it", '#ff9900'),
                           ("recent asks lower → check Recent Postings", '#ffd24d')):
            ttk.Label(legend, text=txt, foreground=color,
                      font=('Consolas', 8)).pack(side='left', padx=(6, 0))

        # Price controls (always visible — core flow for price-only users)
        pf = ttk.Frame(right)
        pf.pack(fill='x', pady=3)
        ttk.Label(pf, text="Price:").pack(side='left')
        self.price_var = tk.StringVar(value="")
        ttk.Entry(pf, textvariable=self.price_var, width=10).pack(side='left', padx=5)
        ttk.Button(pf, text="Set Price", command=self._set_price).pack(side='left', padx=3)
        ttk.Button(pf, text="Price Check", command=self._price_check).pack(side='left', padx=3)
        ttk.Button(pf, text="PC All", command=self._price_check_all).pack(side='left', padx=3)
        # Undercut: shave this % off every price-checked median before it's set,
        # so your prices land just under the going rate. 0 = use the median as-is.
        ttk.Label(pf, text="Undercut:").pack(side='left', padx=(12, 2))
        self.undercut_var = tk.StringVar(value=self.settings['defaults']['undercut'])
        ttk.Entry(pf, textvariable=self.undercut_var, width=4).pack(side='left')
        ttk.Label(pf, text="%").pack(side='left')

        # Auction-list management (always visible)
        lbf = ttk.Frame(right)
        lbf.pack(fill='x', pady=3)
        ttk.Button(lbf, text="Clear", command=self._clear).pack(side='left')
        ttk.Button(lbf, text="Save List", command=self._save_auction).pack(side='left', padx=(15, 3))
        ttk.Button(lbf, text="Load List", command=self._load_auction).pack(side='left')

        # --- Collapsible "Macro Builder" section ---
        self.macro_expanded = False
        self.macro_toggle_btn = ttk.Button(
            right, text="▸  Macro Builder  (click to expand)",
            command=self._toggle_macro_panel)
        self.macro_toggle_btn.pack(fill='x', pady=(6, 0))

        # Panel is built but NOT packed yet (collapsed by default)
        self.macro_panel = ttk.Frame(right)

        # Settings
        sf = ttk.Frame(self.macro_panel)
        sf.pack(fill='x', pady=3)
        ttk.Label(sf, text="Prefix:").pack(side='left')
        self.prefix_var = tk.StringVar(value=self.settings['defaults']['prefix'])
        ttk.Entry(sf, textvariable=self.prefix_var, width=12).pack(side='left', padx=5)
        ttk.Label(sf, text="Page:").pack(side='left', padx=(10, 0))
        self.page_var = tk.StringVar(value=self.settings['defaults']['page'])
        ttk.Entry(sf, textvariable=self.page_var, width=3).pack(side='left', padx=5)
        ttk.Label(sf, text="Suffix:").pack(side='left', padx=(10, 0))
        self.suffix_var = tk.StringVar(value=self.settings['defaults']['suffix'])
        ttk.Entry(sf, textvariable=self.suffix_var, width=8).pack(side='left', padx=(5, 2))
        ttk.Button(sf, text="?", width=2, command=self._suffix_help).pack(side='left')

        # Macro pricing threshold: items priced at/above this go out as
        # clickable links (the high-ticket "movers"); cheaper items go out as
        # compact plain text. Krono-priced items always link. Blank/0 links
        # everything (the classic behavior).
        tf = ttk.Frame(self.macro_panel)
        tf.pack(fill='x', pady=3)
        ttk.Label(tf, text="Link if ≥:").pack(side='left')
        self.threshold_var = tk.StringVar(value=self.settings['defaults']['threshold'])
        ttk.Entry(tf, textvariable=self.threshold_var, width=6).pack(side='left', padx=(5, 8))
        ttk.Label(tf, text="≥ link · below = text · items worth more to a vendor "
                           "are auto-excluded (price-check first)",
                  foreground='#888888', font=('Consolas', 8)).pack(side='left')

        # Generate / Copy
        bf = ttk.Frame(self.macro_panel)
        bf.pack(fill='x', pady=3)
        ttk.Button(bf, text="Generate", command=self._generate).pack(side='left')
        ttk.Button(bf, text="Copy", command=self._copy).pack(side='left', padx=5)

        # INI writing
        bf2 = ttk.Frame(self.macro_panel)
        bf2.pack(fill='x', pady=2)
        ttk.Button(bf2, text="Write to INI", command=self._write_ini).pack(side='left')
        ttk.Label(bf2, text="INI:", foreground='#888888').pack(side='left', padx=(10, 2))
        self.ini_var = tk.StringVar(value="(select file)")
        ttk.Label(bf2, textvariable=self.ini_var, foreground='#666666',
                  font=('Consolas', 8)).pack(side='left')
        self.ini_path = None
        self._last_ini_dir = ''  # remembered between picks, so re-prompts land in the same folder

        # Output
        ttk.Label(self.macro_panel, text="INI Output:").pack(anchor='w', pady=(3, 0))
        self.output_text = scrolledtext.ScrolledText(
            self.macro_panel, height=8, bg='#2a2a2a', fg='#00ff00',
            font=('Consolas', 9), insertbackground='#00ff00')
        self.output_text.pack(fill='both', expand=True)

        ttk.Label(self.macro_panel,
                  text="Save INI as ANSI encoding! (Notepad++ > Encoding > ANSI)",
                  foreground='#FF4500', font=('Consolas', 8, 'bold')).pack(pady=(2, 0))

        # Log/Console (always visible — bigger now, grows to fill free space)
        self.log_label = ttk.Label(right, text="Log:")
        self.log_label.pack(anchor='w', pady=(3, 0))
        self.console = scrolledtext.ScrolledText(
            right, height=12, bg='#1e1e1e', fg='#cccccc',
            font=('Consolas', 8), insertbackground='#cccccc')
        self.console.pack(fill='both', expand=True)

    def _toggle_macro_panel(self):
        """Show/hide the macro-building controls. Collapsed by default so
        users who only want price checks aren't cluttered."""
        if self.macro_expanded:
            self.macro_panel.pack_forget()
            self.macro_expanded = False
            self.macro_toggle_btn.config(
                text="▸  Macro Builder  (click to expand)")
        else:
            self.macro_panel.pack(fill='both', expand=True, pady=(2, 0),
                                  before=self.log_label)
            self.macro_expanded = True
            self.macro_toggle_btn.config(text="▾  Macro Builder")

    def _suffix_help(self):
        """Explain what the optional Suffix field does."""
        messagebox.showinfo(
            "Suffix",
            "Optional text added to the END of every generated auction "
            "line, after your items.\n\n"
            "Leave it blank for none.\n\n"
            "Example — a suffix of \"PST\" turns:\n"
            "    /auc WTS <item>, <item>\n"
            "into:\n"
            "    /auc WTS <item>, <item> PST\n\n"
            "Note: it repeats on each line, so if your items span several "
            "lines the suffix appears on each one.")

    def _log(self, msg):
        self.console.insert('end', f"{msg}\n")
        self.console.see('end')

    def _show_help(self):
        """Show help/about dialog."""
        help_win = tk.Toplevel(self.root)
        help_win.title("Help — EQ Auction Forge")
        help_win.configure(bg='#1a1a1a')
        help_win.geometry("500x520")
        help_win.attributes('-topmost', True)

        txt = scrolledtext.ScrolledText(
            help_win, bg='#2a2a2a', fg='#cccccc',
            font=('Consolas', 9), wrap='word', padx=10, pady=10)
        txt.pack(fill='both', expand=True, padx=10, pady=10)

        help_text = f"""EQ Auction Forge v{APP_VERSION}
by wangel

HOW TO USE (auction macros):

1. In-game: /outputfile inventory
2. Click "Load Inventory" and select the file
3. Add items to your auction list (explorer-style
   multi-select):
   - Double-click an item, OR
   - Shift-click a range / Ctrl-click to toggle /
     Ctrl+A to select all, then "Add Selected"
     (or just press Enter)
   - In the auction list, Delete removes the
     selected rows (Ctrl+A there too)
4. Set prices:
   - Type a price, select item(s), click "Set Price"
     (applies to every selected row)
   - Or click "PC All" to auto-fetch prices from
     TLP Auctions (median, not average). Items that
     actually trade in krono are auto-detected and
     priced in krono (e.g. "1.5kr"), shown purple.
     Pricier items whose recent asks are running well
     under their median get flagged amber — open Recent
     Postings to reprice the ones that really moved
   - "Undercut %" shaves that % off the median and
     rounds to the nearest 5p (e.g. 5 = 5% under)
   - "Price Check" under the items list checks the
     selected inventory item(s) without adding them
   - "Recent Postings" lists the last few sales of
     one item, with time. It also medians the recent
     WTS asks and warns (📉) if they're running well
     under your price-check median — a sign the median
     is lagging and you'd be overpriced. A button there
     prices the item at that recent median (no undercut
     — match the market, don't undercut it). Krono-priced
     items read in krono (e.g. "1kr"), folded at the live
     rate
   - Krono rate is pulled live from TLP Auctions (1-day
     avg) at startup; set a fallback in Settings
5. Click "Generate" to build the macros
6. Click "Write to INI" to save directly to your
   character INI (auto-backup created)
7. Log into EQ — your macro buttons have clickable
   purple item links!

LOG MONITOR (beta) — live trade radar:
- Click "Log Monitor" to watch EC-tunnel auctions in
  real time. It auto-finds your /log on file (or
  Browse to it), then click Start.
- SELL lead (green): someone WTBs an item you OWN.
- BUY lead (cyan): someone WTSs an item on your
  Watchlist. Bright = loud (beep + popup), muted =
  quiet (shows in the feed only).
- Click a row to copy "/tell <name>"; click the "+"
  for the raw log line. Right-click a row to silence
  a spammer — or, on a BUY lead, to drop that item
  from your Watchlist once you've bought it.
- "Aliases" maps slang to items (CoF, fbss, fungi…).
- "Watchlist" = items you want to BUY; a keyword like
  "Nathsar" catches the whole set.
- Requires /log on in EQ. It only READS your log —
  no automation, ToS-safe.

VENDOR VALUE:
- Type your character's CHA in the "CHA" box.
- The "Vendor" columns estimate what an NPC merchant
  pays for each item. Blank = it can't be vendored.
- After PC All, items worth more to a vendor than to
  players turn ORANGE and are left OUT of your macro
  (go vendor those instead).

TIPS:
- Uncheck "Inv only" to search ALL 133k+ items
  (useful for WTB macros)
- Check "Bags only" to hide equipped/bank gear
- Click any column header to sort (Location to grab a
  whole bag; Price or Vendor after PC All). Click
  again to reverse.
- Items auto-pack 2+ per line under the 255-char limit
- Each macro button supports up to 5 lines
- Save/Load preserves your auction list as JSON

IMPORTANT:
- EQ must be CLOSED when writing to the INI file
- If editing INI manually, save as ANSI encoding
  (Notepad++ > Encoding > ANSI)

ISSUES / BUGS:
Report issues on Discord to: Wangel
Or open an issue on GitHub

Item data: items.sodeq.org
Pricing: tlp-auctions.com"""

        txt.insert('1.0', help_text)
        txt.config(state='disabled')

        ttk.Button(help_win, text="Close",
                   command=help_win.destroy).pack(pady=(0, 10))

    # ----- Log Monitor (beta) -------------------------------------------------
    def _open_log_monitor(self):
        """Open the Log Monitor window — watches your /log on file and alerts on
        WTB posts for items you own."""
        if self._lm_win is not None and self._lm_win.winfo_exists():
            self._lm_win.lift()
            return
        win = tk.Toplevel(self.root)
        self._lm_win = win
        win.title("Log Monitor (beta) — WTB alerts for your inventory")
        win.configure(bg='#1a1a1a')
        win.geometry("820x460")
        win.protocol("WM_DELETE_WINDOW", self._lm_close)
        self._lm_rows = []  # backing list (newest first) so 'Loud only' can re-filter

        # Log file row: auto-guess from the char INI, else the newest eqlog for
        # this server in the registry-discovered <EQ>\Logs.
        top = ttk.Frame(win)
        top.pack(fill='x', padx=8, pady=6)
        ttk.Label(top, text="Log file:").pack(side='left')
        self.lm_path_var = tk.StringVar(value=self._lm_guess_log_path())
        ttk.Entry(top, textvariable=self.lm_path_var).pack(
            side='left', fill='x', expand=True, padx=5)
        ttk.Button(top, text="Browse", command=self._lm_browse).pack(side='left')

        ctl = ttk.Frame(win)
        ctl.pack(fill='x', padx=8, pady=(0, 6))
        self.lm_toggle_btn = ttk.Button(ctl, text="Start", command=self._lm_toggle)
        self.lm_toggle_btn.pack(side='left')
        self.lm_sound_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Sound on loud match",
                        variable=self.lm_sound_var).pack(side='left', padx=10)
        # Loud only: hide the grey MAYBE (soft/near-miss) rows from the feed.
        self.lm_loud_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Loud only", variable=self.lm_loud_only_var,
                        command=self._lm_render).pack(side='left')
        ttk.Button(ctl, text="Clear feed", command=self._lm_clear).pack(side='left', padx=(10, 0))
        ttk.Button(ctl, text="Aliases…", command=self._open_alias_editor).pack(side='left', padx=(6, 0))
        ttk.Button(ctl, text="Watchlist…", command=self._open_watchlist_editor).pack(side='left', padx=(6, 0))
        self.lm_status_var = tk.StringVar(value="Idle — load your inventory, then Start.")
        ttk.Label(ctl, textvariable=self.lm_status_var,
                  foreground='#888888').pack(side='left', padx=10)

        # Clicking a row copies '/tell <who> '; this orange banner confirms it
        # (so you can alt-tab to EQ and just Ctrl-V the start of the tell).
        self.lm_copy_var = tk.StringVar(value="Click a match to copy  /tell <who>  to the clipboard")
        ttk.Label(win, textvariable=self.lm_copy_var,
                  foreground='#ff9900').pack(anchor='w', padx=8, pady=(0, 4))

        # Feed: newest on top. '+' opens the raw log line; Type = SELL (they WTB
        # your item) / BUY (they WTS a watchlist item); tier drives row color.
        self._lm_raw_by_iid = {}  # tree row id -> raw log line, for the '+' popup
        fr = ttk.Frame(win)
        fr.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        cols = ('exp', 'time', 'type', 'who', 'item', 'line')
        self.lm_feed = ttk.Treeview(fr, columns=cols, show='headings',
                                    selectmode='browse')
        for c, t, w in (('exp', '', 26), ('time', 'Time', 64), ('type', 'Type', 48),
                        ('who', 'Who', 104), ('item', 'Item', 188),
                        ('line', 'Their auction', 392)):
            self.lm_feed.heading(c, text=t)
            self.lm_feed.column(c, width=w)
        self.lm_feed.column('exp', anchor='center', stretch=False)
        self.lm_feed.column('type', anchor='center')
        # Color encodes both dimensions: hue = kind (SELL green / BUY cyan),
        # brightness = tier (loud bright / quiet muted).
        self.lm_feed.tag_configure('SELL_HIGH', foreground='#00ff66')
        self.lm_feed.tag_configure('SELL_MAYBE', foreground='#5f9e74')
        self.lm_feed.tag_configure('BUY_HIGH', foreground='#33ccff')
        self.lm_feed.tag_configure('BUY_MAYBE', foreground='#5f8fa6')
        self.lm_feed.tag_configure('MUTED', foreground='#666666')  # silenced item
        sb = ttk.Scrollbar(fr, orient='vertical', command=self.lm_feed.yview)
        self.lm_feed.configure(yscrollcommand=sb.set)
        self.lm_feed.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        # Click the '+' column -> raw log popup; click elsewhere -> select row
        # (which copies '/tell <who> '). Arrow keys still copy via TreeviewSelect.
        self.lm_feed.bind('<Button-1>', self._lm_feed_click)
        self.lm_feed.bind('<Button-3>', self._lm_feed_rightclick)  # silence menu
        self.lm_feed.bind('<<TreeviewSelect>>', self._lm_copy_tell)

    def _lm_guess_log_path(self):
        """Best default log path: the char-INI guess if that file exists, else
        the newest eqlog for this server in the registry-discovered Logs dir,
        else the (possibly missing) INI guess, else blank."""
        server = self.server_var.get().strip()
        ini_guess = logmon.LogMonitor.guess_log_path(self._last_ini_path, server)
        if ini_guess and os.path.isfile(ini_guess):
            return ini_guess
        logs = _eq_logs_dir()
        if logs:
            newest = _newest_eqlog(logs, server)
            if newest:
                return newest
        return ini_guess or ''

    def _lm_browse(self):
        cur = os.path.dirname(self.lm_path_var.get())
        init = cur if cur and os.path.isdir(cur) else (_eq_logs_dir()
                                                       or self._last_ini_dir or None)
        path = filedialog.askopenfilename(
            parent=self._lm_win,  # keep focus on the Log Monitor window after
            title="Select your EQ log file (eqlog_<Char>_<server>.txt)",
            initialdir=init,
            filetypes=[("EQ logs", "eqlog_*.txt"), ("Text", "*.txt"),
                       ("All files", "*.*")])
        if path:
            self.lm_path_var.set(path)
        # Return focus to the Log Monitor window, not the main app. Defer it:
        # doing it inline runs before Windows finishes reactivating the dialog's
        # owner (the main window), which would override us.
        def _refocus():
            if self._lm_win is not None and self._lm_win.winfo_exists():
                self._lm_win.lift()
                self._lm_win.focus_force()
        if self._lm_win is not None and self._lm_win.winfo_exists():
            self._lm_win.after(50, _refocus)

    def _lm_toggle(self):
        if self._log_monitor is not None:
            self._lm_stop()
        else:
            self._lm_start()

    def _lm_start(self):
        path = self.lm_path_var.get().strip()
        if not path:
            messagebox.showinfo("Log Monitor", "Pick your EQ log file first.")
            return
        if not self.inv_loaded or not self.inventory:
            messagebox.showinfo(
                "Log Monitor",
                "Load your inventory first — those are the items I watch for.")
            return
        names = [it['name'] for it in self.inventory]
        # Skip our own auctions: char name comes from eqlog_<Char>_<server>.txt
        m = re.search(r'eqlog_([^_]+)_', os.path.basename(path), re.I)
        self_char = m.group(1) if m else None

        def go():
            mon = logmon.LogMonitor(path, self_char, self._matcher,
                                    self._lm_on_alert)
            self._log_monitor = mon
            mon.start()
            self.lm_toggle_btn.config(text="Stop")
            watch = os.path.basename(path)
            who = f" (skipping {self_char})" if self_char else ""
            self.lm_status_var.set(f"Watching {watch}{who}")
            self._log(f"Log monitor started on {watch}")

        if self._matcher is None:
            # Build the IDF once (~1s over 133k names) off the UI thread.
            self.lm_status_var.set("Building match index…")
            self.lm_toggle_btn.config(state='disabled')

            def build():
                matcher = logmon.Matcher(self.item_db.keys(), names,
                                         aliases=self._effective_aliases(),
                                         watchlist=self._load_watchlist())

                def done():
                    self._matcher = matcher
                    self.lm_toggle_btn.config(state='normal')
                    go()
                self.root.after(0, done)
            threading.Thread(target=build, daemon=True).start()
        else:
            self._matcher.set_inventory(names)  # refresh candidates, idf cached
            self._matcher.set_aliases(self._effective_aliases())
            self._matcher.set_watchlist(self._load_watchlist())
            go()

    def _alias_path(self):
        return os.path.join(_cache_dir(), 'aliases.json')

    def _effective_aliases(self):
        """Built-in defaults overlaid with the user's aliases.json (user wins)."""
        if self._user_aliases is None:
            self._user_aliases = logmon.load_aliases(self._alias_path())
        merged = dict(logmon.DEFAULT_ALIASES)
        merged.update(self._user_aliases)
        return merged

    def _watchlist_path(self):
        return os.path.join(_cache_dir(), 'watchlist.json')

    def _load_watchlist(self):
        if self._watchlist is None:
            self._watchlist = logmon.load_watchlist(self._watchlist_path())
        return self._watchlist

    # ----- Silenced auctioneers (mute a spammer; keep showing them greyed) ----
    def _silenced_path(self):
        return os.path.join(_cache_dir(), 'silenced.json')

    def _load_silenced(self):
        """Set of lowercased auctioneer names whose HIGH matches should NOT
        ding/toast. Reuses the JSON-string-list loader; persists across restarts."""
        if self._silenced is None:
            self._silenced = {s.lower()
                              for s in logmon.load_watchlist(self._silenced_path())}
        return self._silenced

    def _lm_set_silenced(self, who, on):
        s = self._load_silenced()
        (s.add if on else s.discard)(who.lower())
        try:
            logmon.save_watchlist(self._silenced_path(), sorted(s))
        except OSError:
            pass
        self._lm_render()  # repaint so the speaker's rows grey/un-grey

    def _lm_feed_rightclick(self, event):
        """Right-click a row -> silence/unsilence that AUCTIONEER (mutes a trader
        spamming their whole set), plus a quick copy-tell."""
        iid = self.lm_feed.identify_row(event.y)
        if not iid:
            return
        vals = self.lm_feed.item(iid)['values']
        if not vals:
            return
        who = vals[3]
        silenced = who.lower() in self._load_silenced()
        menu = tk.Menu(self._lm_win, tearoff=0)
        if silenced:
            menu.add_command(label=f"Unsilence {who}",
                             command=lambda: self._lm_set_silenced(who, False))
        else:
            menu.add_command(label=f"Silence {who} (mute their dings)",
                             command=lambda: self._lm_set_silenced(who, True))
        menu.add_separator()
        menu.add_command(label=f"Copy  /tell {who}",
                         command=lambda: (self.lm_feed.selection_set(iid),
                                          self._lm_copy_tell()))
        # On a BUY lead the 'item' column is the exact watchlist entry that
        # matched (see logmon._watchlist_hits), so offer to drop it straight from
        # the feed — handy once you've bought the thing. Only shown when it's
        # still on the list; SELL rows show inventory items, not watchlist wants.
        item = vals[4] if len(vals) > 4 else ''
        if vals[2] == 'BUY' and item and item in self._load_watchlist():
            menu.add_separator()
            menu.add_command(label=f"Remove '{item}' from watchlist",
                             command=lambda: self._lm_remove_from_watchlist(item))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _lm_remove_from_watchlist(self, item):
        """Drop an item from the watchlist via the feed right-click menu. Mirrors
        the editor's exact-match removal and live-applies to the running matcher
        (and the editor, if it's open); works fine with the editor closed."""
        self._watchlist = [w for w in self._load_watchlist() if w != item]
        self._save_and_apply_watchlist()
        self.lm_status_var.set(f"Removed from watchlist: {item}")

    # ----- Settings dialog ----------------------------------------------------
    def _open_settings(self):
        """Tune the global knobs: krono fallback rate, the default values the
        macro-builder boxes start with, and extra inventory-filter names. Saved
        to settings.json and applied immediately."""
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("Settings")
        win.configure(bg='#1a1a1a')
        win.geometry("620x700")
        win.minsize(620, 600)
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        d = self.settings['defaults']
        self._set_krono_var = tk.StringVar(value=str(self.settings['krono_rate']))
        # Local edit vars, seeded from the stored settings (not the live boxes).
        self._set_vars = {
            'threshold': tk.StringVar(value=d['threshold']),
            'undercut': tk.StringVar(value=d['undercut']),
            'page': tk.StringVar(value=d['page']),
            'prefix': tk.StringVar(value=d['prefix']),
            'suffix': tk.StringVar(value=d['suffix']),
            'cha': tk.StringVar(value=d['cha']),
        }

        pad = {'padx': 10}
        ttk.Label(win, text="Saved values seed the boxes for new sessions and apply "
                            "to the current ones now.", foreground='#888888').pack(
            anchor='w', pady=(10, 4), **pad)

        grid = ttk.Frame(win)
        grid.pack(fill='x', **pad)
        rows = [
            ("Krono rate (plat):", self._set_krono_var,
             "Auto-pulled from TLP Auctions at startup. Hit Sync to refresh it now — "
             "that saves it instantly as your offline fallback (no need to Save). "
             "Used when offline, and to convert 'kr' in the boxes."),
            ("Default link threshold:", self._set_vars['threshold'],
             "Items priced ≥ this go out as clickable links; cheaper = compact text."),
            ("Default undercut %:", self._set_vars['undercut'],
             "Trims price-check results before posting (rounded to 5p)."),
            ("Default macro page:", self._set_vars['page'],
             "First action-bar page written to (page 1 is never touched)."),
            ("Default prefix:", self._set_vars['prefix'], "Leading text on each WTS line."),
            ("Default suffix:", self._set_vars['suffix'], "Trailing text on each WTS line."),
            ("Default Charisma:", self._set_vars['cha'],
             "Drives the NPC vendor-value estimate."),
        ]
        for i, (label, var, hint) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=i * 2, column=0, sticky='w', pady=(6, 0))
            ttk.Entry(grid, textvariable=var, width=12).grid(
                row=i * 2, column=1, sticky='w', padx=8, pady=(6, 0))
            hint_lbl = ttk.Label(grid, text=hint, foreground='#666666',
                                 font=('Consolas', 8), wraplength=560, justify='left')
            hint_lbl.grid(row=i * 2 + 1, column=0, columnspan=3, sticky='w')
            if var is self._set_krono_var:
                self._set_krono_sync = ttk.Button(
                    grid, text="⟳ Sync", width=8,
                    command=self._settings_sync_krono)
                self._set_krono_sync.grid(row=i * 2, column=2, sticky='w', pady=(6, 0))
                self._set_krono_hint = hint_lbl
                self._set_krono_hint_base = hint
                self._refresh_krono_hint()  # append "Last synced …" if we have it

        ttk.Label(win, text="Inventory filter — names dropped on load. Built-ins are "
                            "always on; add your own junk below.",
                  foreground='#888888').pack(anchor='w', pady=(14, 4), **pad)

        # Pack the bottom bars FIRST (side='bottom') so they reserve their height
        # before the excluded-items tree expands into whatever's left — otherwise
        # the tree eats the cavity and the Save/Cancel row gets clipped to nothing.
        bot = ttk.Frame(win)
        bot.pack(side='bottom', fill='x', pady=10, **pad)
        ttk.Button(bot, text="Save", command=self._settings_save).pack(side='left')
        ttk.Button(bot, text="Cancel", command=self._close_settings).pack(side='left', padx=6)
        self._set_status_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self._set_status_var,
                  foreground='#ff9900').pack(side='left', padx=10)

        addf = ttk.Frame(win)
        addf.pack(side='bottom', fill='x', pady=4, **pad)
        ttk.Label(addf, text="Add:").pack(side='left')
        self._excl_add_var = tk.StringVar()
        ent = ttk.Entry(addf, textvariable=self._excl_add_var)
        ent.pack(side='left', fill='x', expand=True, padx=4)
        ent.bind('<Return>', lambda e: self._excl_add())
        ttk.Button(addf, text="Add", command=self._excl_add).pack(side='left')
        ttk.Button(addf, text="Remove (yours only)",
                   command=self._excl_remove).pack(side='left', padx=4)

        efr = ttk.Frame(win)
        efr.pack(fill='both', expand=True, **pad)
        self._excl_tree = ttk.Treeview(efr, columns=('name', 'src'), show='headings',
                                       selectmode='browse', height=6)
        self._excl_tree.heading('name', text='Item name')
        self._excl_tree.heading('src', text='Source')
        self._excl_tree.column('name', width=380)
        self._excl_tree.column('src', width=90)
        self._excl_tree.tag_configure('custom', foreground='#00ff66')
        self._excl_tree.tag_configure('builtin', foreground='#888888')
        esb = ttk.Scrollbar(efr, orient='vertical', command=self._excl_tree.yview)
        self._excl_tree.configure(yscrollcommand=esb.set)
        self._excl_tree.pack(side='left', fill='both', expand=True)
        esb.pack(side='right', fill='y')
        self._excl_custom = list(self.settings['excluded'])  # working copy
        self._refresh_excl_tree()

    def _close_settings(self):
        if self._settings_win is not None:
            self._settings_win.destroy()
        self._settings_win = None

    def _refresh_excl_tree(self):
        self._excl_tree.delete(*self._excl_tree.get_children())
        for n in sorted(_DEFAULT_EXCLUDED):
            self._excl_tree.insert('', 'end', values=(n, 'built-in'), tags=('builtin',))
        for n in sorted(self._excl_custom, key=str.lower):
            self._excl_tree.insert('', 'end', values=(n, 'custom'), tags=('custom',))

    def _excl_add(self):
        name = self._excl_add_var.get().strip()
        if not name:
            return
        low = name.lower()
        if low in _DEFAULT_EXCLUDED or low in {c.lower() for c in self._excl_custom}:
            self._set_status_var.set(f'"{name}" is already filtered')
            return
        self._excl_custom.append(name)
        self._excl_add_var.set("")
        self._set_status_var.set("")
        self._refresh_excl_tree()

    def _excl_remove(self):
        sel = self._excl_tree.selection()
        if not sel:
            return
        vals = self._excl_tree.item(sel[0])['values']
        if not vals or vals[1] != 'custom':
            self._set_status_var.set("Built-in filters can't be removed")
            return
        name = str(vals[0]).lower()
        self._excl_custom = [c for c in self._excl_custom if c.lower() != name]
        self._refresh_excl_tree()

    def _settings_sync_krono(self):
        """Pull the live 1-day krono rate from TLP Auctions and drop it into the
        box. Runs threaded so the dialog doesn't freeze; best-effort."""
        self._set_krono_sync.configure(state='disabled')
        self._set_status_var.set("Syncing krono rate…")

        def work():
            rate = fetch_krono_rate(_config["server"])
            self.root.after(0, lambda: self._settings_sync_krono_done(rate))

        threading.Thread(target=work, daemon=True).start()

    def _settings_sync_krono_done(self, rate):
        # The dialog may have been closed while the fetch was in flight.
        if self._settings_win is None:
            return
        self._set_krono_sync.configure(state='normal')
        if not rate:
            self._set_status_var.set("Sync failed — TLP Auctions unreachable")
            return
        self._set_krono_var.set(str(rate))
        # Apply live + auto-persist the rate and its sync time as the offline
        # fallback (without committing the dialog's other, still-editable fields).
        self._apply_krono_rate(rate)
        self._set_status_var.set(f"Synced & saved: {rate}p/kr")
        self._refresh_krono_hint()

    def _refresh_krono_hint(self):
        """Append the last-synced timestamp to the krono hint in the Settings
        dialog (called when the dialog opens and after a manual Sync)."""
        if not getattr(self, '_set_krono_hint', None):
            return
        txt = self._set_krono_hint_base
        synced = self._fmt_synced(self.krono_synced_at)
        if synced:
            txt += f"  ·  Last synced {synced}."
        self._set_krono_hint.configure(text=txt)

    def _settings_save(self):
        try:
            rate = int(float(self._set_krono_var.get().strip().replace(',', '')))
            if rate <= 0:
                raise ValueError
        except ValueError:
            self._set_status_var.set("Krono rate must be a positive number")
            return
        new = {
            'krono_rate': rate,
            # Preserve the last live-sync timestamp; a manual Save isn't a sync.
            'krono_synced_at': self.settings.get('krono_synced_at'),
            'defaults': {k: v.get() for k, v in self._set_vars.items()},
            'excluded': sorted({c.strip() for c in self._excl_custom if c.strip()},
                               key=str.lower),
        }
        try:
            save_settings(new)
        except OSError as e:
            self._set_status_var.set(f"Save failed: {e}")
            return
        self.settings = new
        apply_runtime_settings(new)
        self.krono_rate = rate
        self._update_krono_label()  # reflect a hand-edited rate in the top-bar note
        # Push the saved defaults into the live boxes so they take effect now.
        dd = new['defaults']
        self.threshold_var.set(dd['threshold'])
        self.undercut_var.set(dd['undercut'])
        self.page_var.set(dd['page'])
        self.prefix_var.set(dd['prefix'])
        self.suffix_var.set(dd['suffix'])
        self.cha_var.set(dd['cha'])
        self._log("Settings saved. Filter changes apply on the next inventory load.")
        self._close_settings()

    # ----- Alias editor -------------------------------------------------------
    def _open_alias_editor(self):
        """Add/remove slang->item aliases. Built-ins are read-only; your own are
        editable and saved to aliases.json. Changes apply to a running monitor
        immediately."""
        if self._alias_win is not None and self._alias_win.winfo_exists():
            self._alias_win.lift()
            return
        self._effective_aliases()  # ensure self._user_aliases is loaded
        win = tk.Toplevel(self._lm_win or self.root)
        self._alias_win = win
        win.title("Aliases — slang → item name")
        win.configure(bg='#1a1a1a')
        win.geometry("560x460")
        win.protocol("WM_DELETE_WINDOW",
                     lambda: (win.destroy(), setattr(self, '_alias_win', None)))

        ttk.Label(win, text="When a WTB line contains the alias, the item name is "
                            "matched.\nBuilt-ins are read-only; your own can be removed.",
                  foreground='#888888').pack(anchor='w', padx=8, pady=(8, 4))

        fr = ttk.Frame(win)
        fr.pack(fill='both', expand=True, padx=8)
        self.alias_tree = ttk.Treeview(fr, columns=('alias', 'item', 'src'),
                                       show='headings', selectmode='browse')
        for c, t, w in (('alias', 'Alias', 120), ('item', 'Item name', 300),
                        ('src', 'Source', 90)):
            self.alias_tree.heading(c, text=t)
            self.alias_tree.column(c, width=w)
        self.alias_tree.tag_configure('custom', foreground='#00ff66')
        self.alias_tree.tag_configure('builtin', foreground='#888888')
        asb = ttk.Scrollbar(fr, orient='vertical', command=self.alias_tree.yview)
        self.alias_tree.configure(yscrollcommand=asb.set)
        self.alias_tree.pack(side='left', fill='both', expand=True)
        asb.pack(side='right', fill='y')

        add = ttk.Frame(win)
        add.pack(fill='x', padx=8, pady=6)
        ttk.Label(add, text="Alias:").pack(side='left')
        self.alias_key_var = tk.StringVar()
        ttk.Entry(add, textvariable=self.alias_key_var, width=12).pack(side='left', padx=(2, 8))
        ttk.Label(add, text="Item:").pack(side='left')
        self.alias_item_var = tk.StringVar()
        ttk.Entry(add, textvariable=self.alias_item_var).pack(
            side='left', fill='x', expand=True, padx=2)
        ttk.Button(add, text="Add / Update", command=self._alias_add).pack(side='left', padx=4)

        bot = ttk.Frame(win)
        bot.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(bot, text="Remove selected (yours only)",
                   command=self._alias_remove).pack(side='left')
        self.alias_status_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.alias_status_var,
                  foreground='#ff9900').pack(side='left', padx=10)

        self._alias_repopulate()

    def _alias_repopulate(self):
        if self._alias_win is None or not self._alias_win.winfo_exists():
            return
        self.alias_tree.delete(*self.alias_tree.get_children())
        for alias in sorted(self._effective_aliases()):
            item = self._effective_aliases()[alias]
            custom = alias in (self._user_aliases or {})
            self.alias_tree.insert('', 'end', values=(alias, item,
                                   'yours' if custom else 'built-in'),
                                   tags=('custom' if custom else 'builtin',))

    def _alias_add(self):
        alias = self.alias_key_var.get().strip().lower()
        item = self.alias_item_var.get().strip()
        if not alias or not item:
            self.alias_status_var.set("Enter both an alias and an item name.")
            return
        if self._user_aliases is None:
            self._user_aliases = {}
        self._user_aliases[alias] = item
        # Soft check: warn (but allow) if the item isn't an exact DB name.
        warn = "" if item in self.item_db else "  (note: not an exact DB item name)"
        self._save_and_apply_aliases()
        self.alias_key_var.set(""); self.alias_item_var.set("")
        self.alias_status_var.set(f"Saved  {alias} -> {item}{warn}")

    def _alias_remove(self):
        sel = self.alias_tree.selection()
        if not sel:
            return
        alias = self.alias_tree.item(sel[0])['values'][0]
        if alias not in (self._user_aliases or {}):
            self.alias_status_var.set(f"'{alias}' is a built-in - can't remove "
                                      f"(override it by adding your own).")
            return
        del self._user_aliases[alias]
        self._save_and_apply_aliases()
        self.alias_status_var.set(f"Removed  {alias}")

    def _save_and_apply_aliases(self):
        """Persist user aliases and push them to a running matcher live."""
        try:
            logmon.save_aliases(self._alias_path(), self._user_aliases)
        except OSError as e:
            self.alias_status_var.set(f"Could not save: {e}")
        if self._matcher is not None:
            self._matcher.set_aliases(self._effective_aliases())
        self._alias_repopulate()

    # ----- Watchlist editor ---------------------------------------------------
    def _open_watchlist_editor(self):
        """Items you WANT to buy. When someone posts one WTS, you get a BUY lead.
        Saved to watchlist.json; changes apply to a running monitor immediately."""
        if self._watch_win is not None and self._watch_win.winfo_exists():
            self._watch_win.lift()
            return
        self._load_watchlist()
        win = tk.Toplevel(self._lm_win or self.root)
        self._watch_win = win
        win.title("Watchlist — items you want to buy")
        win.configure(bg='#1a1a1a')
        win.geometry("460x440")
        win.protocol("WM_DELETE_WINDOW",
                     lambda: (win.destroy(), setattr(self, '_watch_win', None)))

        ttk.Label(win, text="When someone auctions one of these WTS, you get a "
                            "BUY lead.\nFull item name, an alias (CoF), OR a "
                            "distinctive keyword to catch a whole set\n"
                            "(e.g. 'Nathsar' matches Nathsar Gauntlets/Bracer/…; "
                            "generic words like 'Boots' won't fire).",
                  foreground='#888888').pack(anchor='w', padx=8, pady=(8, 4))

        fr = ttk.Frame(win)
        fr.pack(fill='both', expand=True, padx=8)
        self.watch_tree = ttk.Treeview(fr, columns=('item',), show='headings',
                                       selectmode='browse')
        self.watch_tree.heading('item', text='Item you want')
        self.watch_tree.column('item', width=400)
        wsb = ttk.Scrollbar(fr, orient='vertical', command=self.watch_tree.yview)
        self.watch_tree.configure(yscrollcommand=wsb.set)
        self.watch_tree.pack(side='left', fill='both', expand=True)
        wsb.pack(side='right', fill='y')

        add = ttk.Frame(win)
        add.pack(fill='x', padx=8, pady=6)
        ttk.Label(add, text="Item:").pack(side='left')
        self.watch_item_var = tk.StringVar()
        e = ttk.Entry(add, textvariable=self.watch_item_var)
        e.pack(side='left', fill='x', expand=True, padx=2)
        e.bind('<Return>', lambda ev: self._watch_add())
        ttk.Button(add, text="Add", command=self._watch_add).pack(side='left', padx=4)

        bot = ttk.Frame(win)
        bot.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(bot, text="Remove selected",
                   command=self._watch_remove).pack(side='left')
        self.watch_status_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.watch_status_var,
                  foreground='#ff9900').pack(side='left', padx=10)

        self._watch_repopulate()

    def _watch_repopulate(self):
        if self._watch_win is None or not self._watch_win.winfo_exists():
            return
        self.watch_tree.delete(*self.watch_tree.get_children())
        for item in sorted(self._load_watchlist(), key=str.lower):
            self.watch_tree.insert('', 'end', values=(item,))

    def _watch_add(self):
        item = self.watch_item_var.get().strip()
        if not item:
            return
        wl = self._load_watchlist()
        if item.lower() in (w.lower() for w in wl):
            self.watch_status_var.set(f"'{item}' is already on the list.")
            return
        wl.append(item)
        warn = "" if item in self.item_db else "  (note: not an exact DB item name)"
        self._save_and_apply_watchlist()
        self.watch_item_var.set("")
        self.watch_status_var.set(f"Added  {item}{warn}")

    def _watch_remove(self):
        sel = self.watch_tree.selection()
        if not sel:
            return
        item = self.watch_tree.item(sel[0])['values'][0]
        wl = self._load_watchlist()
        self._watchlist = [w for w in wl if w != item]
        self._save_and_apply_watchlist()
        self.watch_status_var.set(f"Removed  {item}")

    def _save_and_apply_watchlist(self):
        try:
            logmon.save_watchlist(self._watchlist_path(), self._load_watchlist())
        except OSError as e:
            # The feed right-click can remove items with the editor closed, so
            # watch_status_var may not exist yet — only set it when it does.
            if getattr(self, 'watch_status_var', None) is not None:
                self.watch_status_var.set(f"Could not save: {e}")
        if self._matcher is not None:
            self._matcher.set_watchlist(self._load_watchlist())
        self._watch_repopulate()

    def _lm_stop(self):
        if self._log_monitor is not None:
            self._log_monitor.stop()
            self._log_monitor = None
        if self._lm_win is not None and self._lm_win.winfo_exists():
            self.lm_toggle_btn.config(text="Start")
            self.lm_status_var.set("Stopped.")

    def _lm_on_alert(self, kind, tier, ts, speaker, item, line):
        """Called from the tailer thread — marshal onto the UI thread.
        kind is 'SELL' (they WTB your item) or 'BUY' (they WTS a watchlist item)."""
        self.root.after(0, lambda: self._lm_add(kind, tier, ts, speaker, item, line))

    def _lm_add(self, kind, tier, ts, speaker, item, line):
        if self._lm_win is None or not self._lm_win.winfo_exists():
            return
        # ts like 'Tue Jun 09 03:14:02 2026' -> just the clock part for display;
        # keep the full ts to rebuild the raw log line for the '+' popup.
        parts = ts.split()
        clock = parts[3] if len(parts) >= 4 else ts
        self._lm_rows.insert(0, (kind, tier, clock, speaker, item, line, ts))
        del self._lm_rows[200:]
        self._lm_render()
        # A silenced auctioneer still shows in the feed (greyed) but doesn't
        # ding/toast — kills a trader spamming their whole set.
        if tier == 'HIGH' and speaker.lower() not in self._load_silenced():
            if self.lm_sound_var.get():
                self._lm_beep(kind)
            self._lm_toast(kind, speaker, item, line)

    def _lm_render(self):
        """Repaint the feed from the backing list, honoring the 'Loud only'
        filter. Driven by new alerts and by toggling the filter."""
        if self._lm_win is None or not self._lm_win.winfo_exists():
            return
        loud_only = self.lm_loud_only_var.get()
        silenced = self._load_silenced()
        self.lm_feed.delete(*self.lm_feed.get_children())
        self._lm_raw_by_iid = {}
        for kind, tier, clock, speaker, item, line, ts in self._lm_rows:
            if loud_only and tier != 'HIGH':
                continue
            tag = 'MUTED' if speaker.lower() in silenced else f"{kind}_{tier}"
            iid = self.lm_feed.insert('', 'end',
                                      values=('+', clock, kind, speaker, item, line),
                                      tags=(tag,))
            self._lm_raw_by_iid[iid] = f"[{ts}] {speaker} auctions, '{line}'"

    def _lm_feed_click(self, event):
        """Click the '+' column -> raw log popup (and don't let it select/copy)."""
        if self.lm_feed.identify_region(event.x, event.y) != 'cell':
            return
        if self.lm_feed.identify_column(event.x) == '#1':  # the '+' column
            iid = self.lm_feed.identify_row(event.y)
            if iid:
                self._lm_show_raw(iid)
            return 'break'

    def _lm_show_raw(self, iid):
        raw = self._lm_raw_by_iid.get(iid, '')
        t = tk.Toplevel(self._lm_win or self.root)
        t.title("Raw auction log")
        t.configure(bg='#1a1a1a')
        t.geometry("640x150")
        t.attributes('-topmost', True)
        box = tk.Text(t, height=4, wrap='word', bg='#101010', fg='#cccccc',
                      font=('Consolas', 10), relief='flat')
        box.pack(fill='both', expand=True, padx=10, pady=10)
        box.insert('1.0', raw)
        box.config(state='disabled')
        ttk.Button(t, text="Close", command=t.destroy).pack(pady=(0, 10))

    def _lm_beep(self, kind='SELL'):
        """Distinct tones per lead so you know which without looking:
          SELL (someone wants YOUR item) -> bright rising 'cha-ching'
          BUY  (a watchlist item is for sale) -> two-note descending 'grab it'."""
        if winsound is None:
            return
        if kind == 'BUY':
            notes = [(1319, 110), (880, 170)]      # E6 -> A5, descending
        else:
            notes = [(988, 90), (1319, 90), (1760, 150)]  # B5-E6-A6, rising
        def play():
            try:
                for freq, dur in notes:
                    winsound.Beep(freq, dur)
            except RuntimeError:
                pass
        threading.Thread(target=play, daemon=True).start()

    def _lm_toast(self, kind, speaker, item, line):
        """Small, non-focus-stealing popup in the corner; auto-closes.
        kind 'SELL' = they want to buy your item; 'BUY' = they're selling one you want."""
        title = "Sell lead!" if kind == 'SELL' else "Buy lead!"
        head = f"WTB: {item}" if kind == 'SELL' else f"WTS: {item}"
        try:
            t = tk.Toplevel(self.root)
            t.title(title)
            t.configure(bg='#202a20')
            t.attributes('-topmost', True)
            t.geometry("360x110-20-60")  # bottom-right-ish
            tk.Label(t, text=head, bg='#202a20', fg='#00ff66',
                     font=('Consolas', 12, 'bold')).pack(anchor='w', padx=10, pady=(8, 0))
            tk.Label(t, text=speaker, bg='#202a20', fg='#cccccc',
                     font=('Consolas', 10, 'bold')).pack(anchor='w', padx=10)
            tk.Label(t, text=line[:80], bg='#202a20', fg='#aaaaaa',
                     font=('Consolas', 9), wraplength=340, justify='left').pack(
                         anchor='w', padx=10)
            t.bind('<Button-1>', lambda e: t.destroy())
            t.after(9000, lambda: t.winfo_exists() and t.destroy())
        except tk.TclError:
            pass

    def _lm_copy_tell(self, event=None):
        """Selecting a feed row copies '/tell <who> ' so you can alt-tab to EQ
        and just Ctrl-V (no squinting at a goofy auctioneer name to retype it)."""
        sel = self.lm_feed.selection()
        if not sel:
            return
        vals = self.lm_feed.item(sel[0])['values']
        if not vals:
            return
        who = vals[3]  # columns: (exp, time, type, who, item, line)
        self.root.clipboard_clear()
        self.root.clipboard_append(f"/tell {who} ")
        self.lm_copy_var.set(f"Copied  /tell {who}   to clipboard  ->  alt-tab to EQ, Ctrl-V")

    def _lm_clear(self):
        self._lm_rows = []
        self._lm_raw_by_iid = {}
        if self._lm_win is not None and self._lm_win.winfo_exists():
            self.lm_feed.delete(*self.lm_feed.get_children())

    def _lm_close(self):
        self._lm_stop()
        if self._lm_win is not None:
            self._lm_win.destroy()
            self._lm_win = None

    def _load_db(self):
        (self.item_db, self.item_ids, self.item_prices,
         self.item_by_id) = load_item_database(self.db_path)
        if self.item_db:
            self.db_count_var.set(f"{len(self.item_db)} items in DB")
            self.status_var.set("Ready")
        else:
            self.status_var.set("ERROR: items.txt.gz not found!")

    def _load_inventory(self):
        # /outputfile inventory dumps into the EQ ROOT (not Logs), so default the
        # picker there; remember the last folder used after that.
        init = self._last_inv_dir or _eq_install_dir() or None
        path = filedialog.askopenfilename(
            title="Select Inventory File", initialdir=init,
            filetypes=[("Text", "*.txt")])
        if not path:
            return
        self._last_inv_dir = os.path.dirname(path)
        self.inventory = load_inventory(path)
        self.inv_by_name = {it['name']: it for it in self.inventory}
        self.inv_loaded = True
        self.inv_only_var.set(True)
        self.status_var.set(f"Inventory: {len(self.inventory)} items")
        self._apply_filter()

    def _apply_filter(self):
        self.item_tree.delete(*self.item_tree.get_children())
        # Rebuilt each pass: maps the visible tree row -> the item's real id, so
        # add-to-auction / left price-check / Recent Postings act on the exact
        # item even when two rows share a display name.
        self.inv_row_id = {}
        search = self.filter_var.get().strip().lower()
        inv_only = self.inv_only_var.get()
        bags_only = self.bags_only_var.get()
        count = 0

        if inv_only and self.inv_loaded:
            # Sort alphabetically so items are easy to scan/find.
            for item in sorted(self.inventory, key=lambda i: i['name'].lower()):
                name = item['name']
                if bags_only and not self._is_bag_location(item.get('location', '')):
                    continue
                if search and search not in name.lower():
                    continue
                item_id = self._item_id(item)
                if name in self.item_db or item_id in self.item_by_id:
                    row = self.item_tree.insert(
                        '', 'end',
                        values=(name, self._qty_str(item.get('count', 1)),
                                item['location'], self._vendor_str(name, item_id)))
                    self.inv_row_id[row] = item_id
                    count += 1
                    if count >= 200:
                        break
        else:
            if not search or len(search) < 2:
                self.item_count_var.set("(type 2+ chars)")
                return
            # Collect matches first, then sort alphabetically before showing.
            matches = sorted((n for n in self.item_db if search in n.lower()),
                             key=str.lower)
            for name in matches[:200]:
                inv = self.inv_by_name.get(name, {})
                item_id = self._item_id(inv)
                row = self.item_tree.insert(
                    '', 'end',
                    values=(name, self._qty_str(inv.get('count', 1)),
                            inv.get('location', ""), self._vendor_str(name, item_id)))
                self.inv_row_id[row] = item_id
                count += 1
        self.item_count_var.set(f"({count})")

    def _on_cha_change(self, *_a):
        """CHA edited -> refresh the inventory Vendor column and re-flag the
        auction list (vendor values just changed)."""
        self._apply_filter()
        if hasattr(self, 'auc_tree'):
            self._refresh_vendor_flags()

    def _cha(self):
        """Player CHA from the box, or None if blank/invalid."""
        try:
            v = int(self.cha_var.get().strip())
            return v if v > 0 else None
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _item_id(item):
        """The real DB id carried on an item dict (from the inventory dump), or 0
        when unknown — 0 means 'fall back to name lookups'."""
        try:
            return int(item.get('id') or 0)
        except (TypeError, ValueError):
            return 0

    def _link_for(self, item):
        """itemlink (hash+name) for an auction item — by id when known (exact),
        else by name (ambiguous when the DB has duplicate names)."""
        iid = self._item_id(item)
        rec = self.item_by_id.get(iid) if iid else None
        if rec is not None:
            return rec['link']
        return self.item_db.get(item['name'])

    def _price_id_for(self, item):
        """Bulk-price-API item id for an auction item — the carried id when known
        (exact), else the name's first DB row."""
        iid = self._item_id(item)
        if iid and iid in self.item_by_id:
            return iid
        return self.item_ids.get(item['name'])

    def _base_copper(self, name, item_id=0):
        """Item base merchant value (copper) used for the NPC-vendor estimate.
        Prefers the exact id (disambiguates duplicate names), else the name's
        first DB row. None when the item has no DB price."""
        rec = self.item_by_id.get(item_id) if item_id else None
        if rec is not None:
            return rec['price']
        return self.item_prices.get(name)

    def _vendor_str(self, name, item_id=0):
        """Vendor-value cell for an item: base price x M(CHA), in plat. Blank
        when CHA isn't set or the item has no DB price."""
        cha = self._cha()
        price = self._base_copper(name, item_id)
        if cha is None or not price:
            return ""
        pp = vendor_value_pp(price, cha)
        return f"{pp:.0f}p" if pp >= 1 else "<1p"

    @staticmethod
    def _is_bag_location(loc):
        """True if an inventory location is a general-inventory bag slot
        ('General 1', 'General 2-Slot4'). Excludes equipped gear, Bank,
        SharedBank, KeyRing, Power Source, etc."""
        return (loc or '').strip().lower().startswith('general')

    @staticmethod
    def _natkey(s):
        """Natural-sort key: split digit runs into ints so 'General 2-Slot10'
        sorts after 'General 2-Slot9' instead of before it."""
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r'(\d+)', s or '')]

    def _location_sort_key(self, loc):
        """Location sort key that keeps the two kinds of slots in separate
        contiguous blocks instead of sandwiching the General bags between the
        equipped slots that sort before 'G' (Chest, Face...) and those after
        (Head, Neck, Bank...). Equipped/bank slots group first, General bags
        second; each block is natural-sorted within."""
        return (1 if self._is_bag_location(loc) else 0, self._natkey(loc))

    @staticmethod
    def _qty_sort_key(v):
        """Sort 'xN' quantity cells by N; blank (a lone item) counts as 1."""
        m = re.search(r'\d+', v or '')
        return int(m.group()) if m else 1

    @staticmethod
    def _price_sort_key(v):
        """Sort a displayed price cell ('500p', '2kr 500p', '1kr', '') by its
        plat value. Unpriced sorts below everything; krono is folded to plat at
        the default rate purely for ordering."""
        s = (v or '').strip().lower()
        if not s:
            return -1.0
        kr = re.search(r'(\d+(?:\.\d+)?)\s*kr', s)
        pp = re.search(r'(\d+(?:\.\d+)?)\s*p', s)
        if kr or pp:
            plat = (float(kr.group(1)) * DEFAULT_KRONO_RATE if kr else 0.0)
            plat += float(pp.group(1)) if pp else 0.0
            return float(plat)
        digits = re.sub(r'[^\d.]', '', s)
        try:
            return float(digits) if digits else 0.0
        except ValueError:
            return 0.0

    def _sort_column(self, tree, col):
        """Sort a tree by the clicked column header, toggling asc/desc. For the
        auction tree the backing auction_items list is reordered to match (the
        row<->index mapping that remove/set-price/select rely on), then the tree
        is rebuilt; the inventory tree is display-only so its rows just move."""
        key_funcs = {'price': self._price_sort_key, 'qty': self._qty_sort_key,
                     'location': self._location_sort_key,
                     'vendor': self._price_sort_key}  # 'Xp'/'<1p'/'' -> plat
        keyf = key_funcs.get(col, lambda s: (s or '').lower())
        state_key = (str(tree), col)
        descending = self._sort_state.get(state_key, False)
        rows = [(tree.index(iid), iid) for iid in tree.get_children('')]
        rows.sort(key=lambda r: keyf(tree.set(r[1], col)), reverse=descending)
        if tree is self.auc_tree:
            self.auction_items = [self.auction_items[i] for i, _ in rows
                                  if i < len(self.auction_items)]
            self._refresh_auction_tree()
        else:
            for pos, (_, iid) in enumerate(rows):
                tree.move(iid, '', pos)
        self._sort_state[state_key] = not descending

    def _row_tag(self, item):
        """Color tag for an auction row, priority krono > vendor-trash > diverge.
        Single source of truth so every rebuild (insert, sort, re-flag) colors
        rows the same way."""
        kind, _ = self._classify_price(item.get('price', ''))
        if kind == 'krono':
            return ('KRONO',)
        if self._is_vendor_trash(item):
            return ('VENDOR',)
        if item.get('diverge'):
            return ('DIVERGE',)
        return ()

    def _refresh_auction_tree(self):
        """Rebuild every auction row from self.auction_items (in list order),
        carrying the color tags so a sort/rebuild doesn't drop the flagging."""
        self.auc_tree.delete(*self.auc_tree.get_children())
        for item in self.auction_items:
            self.auc_tree.insert('', 'end', values=self._auc_values(item),
                                 tags=self._row_tag(item))

    def _bind_select_all(self, tree):
        """Wire Ctrl+A on a tree to select every row (explorer-style). Returns
        'break' so the binding doesn't fall through to other handlers."""
        def select_all(event):
            tree.selection_set(tree.get_children())
            return 'break'
        tree.bind('<Control-a>', select_all)
        tree.bind('<Control-A>', select_all)

    def _add_to_auction(self, event=None):
        """Double-click handler — add whatever is selected (one or many)."""
        self._add_rows_to_auction(self.item_tree.selection())

    def _add_selected_to_auction(self):
        """'Add Selected' button — add every highlighted item at once, so the
        user can shift/ctrl-select a batch instead of double-clicking each."""
        sel = self.item_tree.selection()
        if not sel:
            messagebox.showinfo("Add Selected", "Select one or more items first.")
            return
        self._add_rows_to_auction(sel)

    @staticmethod
    def _qty_str(count):
        """Display string for a quantity column — blank for a lone item, 'xN'
        for stacks/duplicates, so the column only draws attention when it
        matters."""
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
        return f"x{count}" if count > 1 else ""

    def _auc_values(self, item):
        """Tree row tuple for an auction item: (name, price, qty, vendor)."""
        return (item['name'], item.get('price', ''),
                self._qty_str(item.get('count', 1)),
                self._vendor_str(item['name'], self._item_id(item)))

    def _add_rows_to_auction(self, sel):
        """Add the given item-tree rows to the auction list."""
        price = self.price_var.get() or ""
        added = 0
        for iid in sel:
            name = str(self.item_tree.item(iid)['values'][0])
            # Prefer the exact id stashed on the row (disambiguates duplicate
            # names); fall back to the name's inventory entry / DB presence.
            item_id = self.inv_row_id.get(iid, 0)
            if not item_id and name not in self.item_db:
                continue
            count = self.inv_by_name.get(name, {}).get('count', 1)
            item = {'name': name, 'price': price, 'count': count, 'id': item_id}
            self.auction_items.append(item)
            self.auc_tree.insert('', 'end', values=self._auc_values(item))
            added += 1
        if added > 1:
            self._log(f"Added {added} items to auction")

    def _remove_from_auction(self, event=None):
        """Remove every selected auction row (double-click or Delete key)."""
        sel = self.auc_tree.selection()
        if not sel:
            return
        # Map each row to its list index, then delete highest-first so the
        # remaining indices stay valid as we pop.
        children = self.auc_tree.get_children()
        idx_of = {iid: i for i, iid in enumerate(children)}
        for iid in sorted(sel, key=lambda i: idx_of.get(i, -1), reverse=True):
            idx = idx_of.get(iid)
            self.auc_tree.delete(iid)
            if idx is not None and idx < len(self.auction_items):
                self.auction_items.pop(idx)

    def _on_auction_select(self, event):
        """Show the selected item's current price in the price field, and mirror
        the selection in the inventory list so Recent Postings / left-side Price
        Check act on the same item (those prefer the inventory selection)."""
        sel = self.auc_tree.selection()
        if not sel:
            return
        idx = self.auc_tree.index(sel[0])
        if idx < len(self.auction_items):
            self.price_var.set(self.auction_items[idx].get('price', ''))
            self._select_inventory_by_name(self.auction_items[idx]['name'])

    def _select_inventory_by_name(self, name):
        """Select the matching inventory row (and scroll it into view) if it's
        currently visible; otherwise clear the inventory selection so single-item
        lookups fall through to the auction selection instead of a stale one."""
        for iid in self.item_tree.get_children():
            vals = self.item_tree.item(iid)['values']
            if vals and str(vals[0]) == name:
                self.item_tree.selection_set(iid)
                self.item_tree.see(iid)
                return
        self.item_tree.selection_remove(*self.item_tree.selection())

    def _set_price(self):
        """Set the price box value on every selected auction row."""
        sel = self.auc_tree.selection()
        if not sel:
            return
        price = self.price_var.get()
        for iid in sel:
            idx = self.auc_tree.index(iid)
            if idx < len(self.auction_items):
                self.auction_items[idx]['price'] = price
                self.auction_items[idx].pop('diverge', None)  # you've repriced it
                self.auc_tree.item(iid, values=self._auc_values(self.auction_items[idx]))
        self._refresh_vendor_flags()  # re-color now that prices changed

    def _use_recent_median(self, name, price):
        """Set this item's auction price to the recent-median price string ('460p'
        or '1.5kr'). No undercut is applied on purpose — the recent median already
        reflects a competitive market, and undercutting it is the race-to-the-bottom
        we want to avoid. Applies to matching auction rows; if the item isn't in the
        list yet, the value loads into the price box so you can add it + Set Price."""
        hits = [i for i, it in enumerate(self.auction_items) if it['name'] == name]
        if hits:
            for i in hits:
                self.auction_items[i]['price'] = price
                self.auction_items[i].pop('diverge', None)  # repriced to recent
            self._refresh_auction_tree()
            self._refresh_vendor_flags()
            self._log(f"  {name}: priced at recent median {price}")
        else:
            self.price_var.set(price)
            self._log(f"  {name}: recent median {price} -> price box "
                      f"(not in auction list; add it, then Set Price)")

    def _undercut_pct(self):
        """Read the Undercut % box. Returns a float in [0, 100); 0 if blank or
        invalid, so a bad value just means 'no undercut' rather than an error."""
        raw = self.undercut_var.get().strip().rstrip('%')
        if not raw:
            return 0.0
        try:
            v = float(raw)
        except ValueError:
            return 0.0
        return v if 0 <= v < 100 else 0.0

    @staticmethod
    def _round_to_5(plat):
        """Round a plat value to the nearest 5 so undercut prices land on tidy
        numbers (300p - 2% = 294 -> 295, not a weird 294)."""
        return int(round(plat / 5.0)) * 5

    @staticmethod
    def _median(nums):
        """Median of a list (None if empty). Stdlib-free to keep the no-deps rule."""
        s = sorted(nums)
        n = len(s)
        if not n:
            return None
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    def _result_to_price(self, name, res, krono_rate, undercut=0.0):
        """Turn a single bulk-API result into (price, detail) strings.

        Posting prices render in **plain plat** — the API median is an effective
        plat value, and a kr/plat split (e.g. '1kr 1045p') is rate-dependent and
        awkward for posting. (krono_rate is unused here; kept for call symmetry.)
        With undercut > 0 the median is shaved that percent then rounded to 5."""
        if not res or not res.get('hasData'):
            return None, "No price data"
        median = int(round(res.get('medianPlatPrice', 0)))
        samples = res.get('sampleSize', 0)
        # Remember the raw median so Recent Postings can flag when live asks have
        # drifted away from this longer-window check median.
        self._last_median[name] = median
        if undercut:
            under = self._round_to_5(median * (1 - undercut / 100.0))
            detail = f"Median: {median}p ({samples} sales) -> -{undercut:g}% = {under}p"
            return f"{under}p", detail
        return f"{median}p", f"Median: {median}p ({samples} sales)"

    def _recent_market(self, sales):
        """Summarize recent WTS asks into a denomination-aware market read, or None
        if <2 priced asks. Splits plat asks (platPrice>0) from pure-krono asks
        (kronoPrice>0, no plat) and folds krono into effective plat at the live
        rate. Returns {eff_med, n, is_krono, price_str} — price_str is the postable
        string ('460p' or '1.5kr') in the item's dominant denomination."""
        rate = DEFAULT_KRONO_RATE
        wts = [s for s in sales if not s.get('transactionType')]
        # EC combines currencies: "1kr 6000p" means 1 krono PLUS 6000 plat (~1.5kr
        # total), NOT one-or-the-other — tlp-auctions records both components for
        # exactly this reason. So each ask's effective plat is the SUM of its parts.
        # A post that names any krono is krono-denominated (result shown in krono);
        # plat-only otherwise.
        eff, kr_count, plat_count = [], 0, 0
        for s in wts:
            p, k = s.get('platPrice') or 0, s.get('kronoPrice') or 0
            if k > 0:
                eff.append(p + k * rate)
                kr_count += 1
            elif p > 0:
                eff.append(p)
                plat_count += 1
        n = len(eff)
        eff_med = self._median(eff)
        if eff_med is None or n < 2:
            return None
        is_krono = kr_count > plat_count
        if is_krono:
            kr = max(round((eff_med / rate) * 2) / 2, 0.5)  # nearest 0.5 krono
            price_str = f"{kr:g}kr"
        else:
            price_str = f"{self._round_to_5(eff_med)}p"
        return {'eff_med': eff_med, 'n': n, 'is_krono': is_krono, 'price_str': price_str}

    def _resolve_price(self, name, res, krono_rate, undercut, server):
        """`_result_to_price`, then a recent-asks reading for higher-value items
        (bulk median >= RECENT_CHECK_FLOOR). Returns (price, detail, flag):
          - krono-dominant -> repriced into krono (no undercut), flag=None
          - plat asks running >=15% under the bulk median (>=3 asks) -> bulk price
            kept but flag set (a dict) so the row is marked for review
          - otherwise -> bulk price, flag=None
        Runs in a worker thread; the extra call only fires above the floor."""
        price, detail = self._result_to_price(name, res, krono_rate, undercut)
        median = self._last_median.get(name, 0)
        if not price or median < RECENT_CHECK_FLOOR:
            return price, detail, None
        sales, err = fetch_recent_sales(name, server)
        if err:
            return price, detail, None
        mk = self._recent_market(sales)
        if not mk:
            return price, detail, None
        if mk['is_krono']:
            return (mk['price_str'],
                    f"krono item -> {mk['price_str']} (recent median; "
                    f"bulk was {median:,}p plat)", None)
        pct = (mk['eff_med'] - median) / median * 100.0
        if pct <= -15 and mk['n'] >= 3:
            detail = f"{detail}  | recent asks ~{mk['price_str']} ({pct:.0f}%) — FLAGGED"
            return price, detail, {'recent': mk['price_str'], 'pct': pct, 'n': mk['n']}
        return price, detail, None

    def _price_check(self):
        """Price check the selected item via the bulk endpoint (single id)."""
        sel = self.auc_tree.selection()
        if not sel:
            sel = self.item_tree.selection()
            if not sel:
                return
            name = str(self.item_tree.item(sel[0])['values'][0])
            item_id = self._price_id_for(
                {'name': name, 'id': self.inv_row_id.get(sel[0], 0)})
        else:
            idx = self.auc_tree.index(sel[0])
            item = (self.auction_items[idx] if idx < len(self.auction_items)
                    else {'name': str(self.auc_tree.item(sel[0])['values'][0])})
            name = item['name']
            item_id = self._price_id_for(item)

        if item_id is None:
            self._log(f"  {name}: no item id in DB")
            return

        self._log(f"Checking price: {name}...")
        self.root.update()
        undercut = self._undercut_pct()

        def do_check():
            results, krono_rate, err = fetch_prices_bulk([item_id], _config["server"])
            if err:
                self.root.after(0, lambda: self._on_price_result(name, None, err))
                return
            price, detail, _flag = self._resolve_price(
                name, results.get(item_id), krono_rate, undercut, _config["server"])
            self.root.after(0, lambda: self._on_price_result(name, price, detail))

        threading.Thread(target=do_check, daemon=True).start()

    def _price_check_all(self):
        """Price check all auction items, 10 ids per bulk request."""
        if not self.auction_items:
            return
        undercut = self._undercut_pct()
        note = f" (undercut {undercut:g}%)" if undercut else ""
        self._log(f"Price checking {len(self.auction_items)} items "
                  f"(10 per batch){note}...")

        # Pair each auction row with its item id, flagging any missing from the DB.
        targets = []  # (idx, name, item_id)
        for idx, item in enumerate(self.auction_items):
            item_id = self._price_id_for(item)
            if item_id is None:
                self.root.after(0, lambda n=item['name']:
                                self._log(f"  {n}: no item id in DB"))
            else:
                targets.append((idx, item['name'], item_id))

        def do_all():
            server = _config["server"]
            for start in range(0, len(targets), BULK_PRICE_LIMIT):
                batch = targets[start:start + BULK_PRICE_LIMIT]
                results, krono_rate, err = fetch_prices_bulk(
                    [t[2] for t in batch], server)
                if err:
                    self.root.after(0, lambda e=err: self._log(f"  {e}"))
                    continue
                for idx, name, item_id in batch:
                    price, detail, flag = self._resolve_price(
                        name, results.get(item_id), krono_rate, undercut, server)
                    self.root.after(0, lambda n=name, p=price, d=detail, i=idx, f=flag:
                                    self._on_price_result_update(n, p, d, i, f))
            self.root.after(0, self._pc_all_done)

        threading.Thread(target=do_all, daemon=True).start()

    def _pc_all_done(self):
        """After PC All: log, color vendor-better rows, and alert which ones
        will sell to a vendor for more (and thus drop out of any macro)."""
        self._log("Price check complete!")
        flagged = self._refresh_vendor_flags()
        # Recent-asks divergence: items priced well above what's actually being
        # asked right now. We don't auto-reprice (could be a couple lowballers) —
        # just surface them (amber rows) so you can eyeball + reprice the real ones.
        diverged = [it for it in self.auction_items if it.get('diverge')]
        if diverged:
            self._log(f"{len(diverged)} item(s) priced above recent asks (amber) — "
                      "open Recent Postings to reprice the real ones:")
            for it in diverged:
                fl = it['diverge']
                self._log(f"    {it['name']}: you {it.get('price', '')} / "
                          f"recent ~{fl['recent']} ({fl['pct']:.0f}%)")
        if not flagged:
            return
        lines = []
        for it in flagged:
            vpp = self._vendor_pp(it['name'], self._item_id(it))
            vstr = f"{vpp:.0f}p" if vpp is not None else "?"
            lines.append(f"• {it['name']}: player {it.get('price', '')} / vendor {vstr}")
        shown = lines[:15]
        more = (f"\n…and {len(lines) - len(shown)} more (see log)."
                if len(lines) > len(shown) else "")
        messagebox.showinfo(
            "Worth more to a vendor",
            f"{len(flagged)} item(s) will sell to an NPC vendor for more than to "
            f"players (at CHA {self._cha()}). They're flagged orange and will be "
            f"left OUT of any macro you generate:\n\n" + "\n".join(shown) + more)

    def _refresh_vendor_flags(self):
        """Re-tag auction rows: purple when krono-priced, else orange when worth
        more to a vendor than to players (post-undercut). Krono items are never
        vendor-trash (can't compare), so the two tags never collide. Returns the
        vendor-flagged items. Cheap; safe to call often."""
        flagged = []
        for idx, iid in enumerate(self.auc_tree.get_children()):
            if idx >= len(self.auction_items):
                break
            item = self.auction_items[idx]
            tag = self._row_tag(item)
            # Re-render values too (refreshes the Vendor column on CHA change).
            self.auc_tree.item(iid, values=self._auc_values(item), tags=tag)
            if tag == ('VENDOR',):
                flagged.append(item)
        return flagged

    def _on_price_result(self, name, price, detail):
        self._log(f"  {name}: {detail}")
        if price:
            self.price_var.set(price)

    def _on_price_result_update(self, name, price, detail, idx, flag=None):
        self._log(f"  {name}: {detail}")
        if price and idx < len(self.auction_items):
            self.auction_items[idx]['price'] = price
            # Stash/clear the recent-asks divergence flag for row tagging + report.
            if flag:
                self.auction_items[idx]['diverge'] = flag
            else:
                self.auction_items[idx].pop('diverge', None)
            # Update treeview
            children = self.auc_tree.get_children()
            if idx < len(children):
                self.auc_tree.item(children[idx],
                                   values=self._auc_values(self.auction_items[idx]))

    def _price_check_left(self):
        """Price check the item(s) selected in the left/inventory list. Results
        are logged (and put in the Price box if a single item is selected); this
        does NOT touch the auction list, so you can check before adding."""
        sel = self.item_tree.selection()
        if not sel:
            messagebox.showinfo("Price Check", "Select one or more items first.")
            return
        targets = []  # (name, item_id)
        for iid in sel:
            name = str(self.item_tree.item(iid)['values'][0])
            item_id = self._price_id_for(
                {'name': name, 'id': self.inv_row_id.get(iid, 0)})
            if item_id is None:
                self._log(f"  {name}: no item id in DB")
            else:
                targets.append((name, item_id))
        if not targets:
            return
        undercut = self._undercut_pct()
        self._log(f"Price checking {len(targets)} item(s) from inventory...")

        def do_check():
            server = _config["server"]
            for start in range(0, len(targets), BULK_PRICE_LIMIT):
                batch = targets[start:start + BULK_PRICE_LIMIT]
                results, krono_rate, err = fetch_prices_bulk(
                    [t[1] for t in batch], server)
                if err:
                    self.root.after(0, lambda e=err: self._log(f"  {e}"))
                    continue
                for name, item_id in batch:
                    price, detail, _flag = self._resolve_price(
                        name, results.get(item_id), krono_rate, undercut, server)
                    # Single selection: also drop the price into the box so it's
                    # ready to use when adding the item.
                    setbox = price if len(targets) == 1 else None
                    self.root.after(0, lambda n=name, d=detail, p=setbox:
                                    self._on_left_price_result(n, d, p))
            self.root.after(0, lambda: self._log("Price check complete!"))

        threading.Thread(target=do_check, daemon=True).start()

    def _on_left_price_result(self, name, detail, price):
        self._log(f"  {name}: {detail}")
        if price:
            self.price_var.set(price)

    def _selected_single_name(self):
        """Name of the one item to act on for single-item lookups. Prefers the
        left/inventory selection, then the auction selection. Returns None if
        nothing is selected."""
        sel = self.item_tree.selection()
        if sel:
            return str(self.item_tree.item(sel[0])['values'][0])
        sel = self.auc_tree.selection()
        if sel:
            return str(self.auc_tree.item(sel[0])['values'][0])
        return None

    def _recent_postings(self):
        """Show the last few individual postings for ONE selected item, as a
        quick price reference. Works on a single item (left or auction list)."""
        name = self._selected_single_name()
        if not name:
            messagebox.showinfo("Recent Postings",
                                "Select an item (in either list) first.")
            return
        server = _config["server"]
        self._log(f"Fetching recent postings: {name}...")

        def do_fetch():
            sales, err = fetch_recent_sales(name, server)
            self.root.after(0, lambda: self._show_recent_postings(name, sales, err))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _show_recent_postings(self, name, sales, err):
        """Pop up a small window listing recent postings (newest first)."""
        if err:
            self._log(f"  {err}")
            messagebox.showerror("Recent Postings",
                                 f"Couldn't fetch postings:\n{err}")
            return
        if not sales:
            self._log(f"  {name}: no recent postings on {_config['server']}")
            messagebox.showinfo("Recent Postings",
                                f"No recent postings found for:\n{name}")
            return

        win = tk.Toplevel(self.root)
        win.title(f"Recent Postings — {name}")
        win.configure(bg='#1a1a1a')
        win.geometry("520x390")
        win.attributes('-topmost', True)

        ttk.Label(win, text=name, foreground='#00ff00',
                  font=('Consolas', 11, 'bold')).pack(anchor='w', padx=12, pady=(10, 0))
        ttk.Label(win, text=f"Last {len(sales)} postings on {_config['server']} "
                            f"(newest first)", foreground='#888888').pack(
            anchor='w', padx=12, pady=(0, 4))

        # Recent-asks divergence hint, kept at the BOTTOM (under the list). Close +
        # hint are packed side='bottom' FIRST so the expanding list can't shove them
        # off-screen at the default height.
        ttk.Button(win, text="Close", command=win.destroy).pack(side='bottom', pady=(4, 10))

        # Median the recent WTS asks (krono folded into plat at the live rate) and
        # show it in the item's natural denomination. _recent_market is shared with
        # the price-check krono auto-resolve, so the two always agree.
        mk = self._recent_market(sales)
        ref = self._last_median.get(name)
        if mk:
            eff_med, n, price_str = mk['eff_med'], mk['n'], mk['price_str']
            if mk['is_krono']:
                shown = f"{price_str} (≈{int(round(eff_med)):,}p @ {DEFAULT_KRONO_RATE:,}/kr)"
            else:
                shown = price_str
            if not ref:
                line = (f"Recent WTS median {shown} (over {n} asks) — "
                        f"price-check to compare vs the live median.")
                fg = '#cccccc'
            else:
                pct = (eff_med - ref) / ref * 100.0
                if pct <= -15:
                    line = (f"📉 Recent WTS median {shown} — ~{abs(pct):.0f}% UNDER your "
                            f"{ref:,}p check median. Median's lagging; consider repricing.")
                    fg = '#ff6666'
                elif pct >= 15:
                    line = (f"📈 Recent WTS median {shown} — ~{pct:.0f}% ABOVE your "
                            f"{ref:,}p check median. Asks are climbing.")
                    fg = '#00ff66'
                else:
                    line = (f"≈ Recent WTS median {shown} — in line with your {ref:,}p "
                            f"check median.")
                    fg = '#888888'
            # One-click price off the live market. Deliberately NO undercut: match
            # the recent median, don't undercut it (that's the spiral). Packed above
            # Close, below the hint line.
            ttk.Button(win, text=f"Set price → {price_str}  (match recent median)",
                       command=lambda v=price_str: self._use_recent_median(name, v)).pack(
                side='bottom', pady=(4, 0))
            ttk.Label(win, text=line, foreground=fg, font=('Segoe UI Emoji', 9, 'bold'),
                      wraplength=496, justify='left').pack(side='bottom', anchor='w',
                                                            padx=12, pady=(6, 0))

        txt = scrolledtext.ScrolledText(win, bg='#2a2a2a', fg='#cccccc',
                                        font=('Consolas', 9), wrap='none',
                                        padx=10, pady=8)
        txt.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        for s in sales:
            when = format_sale_age(s.get('datetime', ''))
            kind = 'WTB' if s.get('transactionType') else 'WTS'
            price = format_posting_price(s.get('platPrice', 0), s.get('kronoPrice', 0))
            who = s.get('auctioneer', '?')
            txt.insert('end', f"{when:<22} {kind}  {price:>9}  {who}\n")
        txt.config(state='disabled')

    def _clear(self):
        self.auction_items.clear()
        self.auc_tree.delete(*self.auc_tree.get_children())
        self.output_text.delete('1.0', 'end')

    def _save_auction(self):
        """Save auction list (names + prices) to JSON."""
        if not self.auction_items:
            messagebox.showwarning("Warning", "Nothing to save")
            return
        path = filedialog.asksaveasfilename(
            title="Save Auction List",
            defaultextension=".json",
            initialfile="auction_list.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            payload = {'threshold': self.threshold_var.get(),
                       'items': self.auction_items}
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            self._log(f"Saved {len(self.auction_items)} items to {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Save failed: {e}")

    def _load_auction(self):
        """Load auction list from JSON."""
        path = filedialog.askopenfilename(
            title="Load Auction List",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # New format: {threshold, items}. Old format: a bare item list.
            items = data.get('items', []) if isinstance(data, dict) else data
            # Clear current and load
            self._clear()
            if isinstance(data, dict) and 'threshold' in data:
                self.threshold_var.set(data.get('threshold', ''))
            # 'vendor' (old fixed floor) is ignored — superseded by the
            # CHA-based vendor-value comparison.
            for item in items:
                name = item.get('name', '')
                price = item.get('price', '')
                count = item.get('count', 1)
                # 'id' disambiguates duplicate names; legacy saves lack it (0 ->
                # name fallback, the old behavior).
                item_id = item.get('id', 0) if isinstance(item, dict) else 0
                if name:
                    entry = {'name': name, 'price': price, 'count': count,
                             'id': item_id}
                    self.auction_items.append(entry)
                    self.auc_tree.insert('', 'end', values=self._auc_values(entry))
            self._log(f"Loaded {len(self.auction_items)} items from {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Load failed: {e}")

    def _validate_eq_char_ini(self, path):
        """Sanity-check a chosen file before treating it as a character INI.

        Returns (level, message):
          'reject' - we're sure it's the wrong file (block it)
          'warn'   - probably wrong, but let the user override
          'ok'     - looks like a real character settings INI
        """
        base = os.path.basename(path)
        name = base.lower()

        if name in INI_BLOCKLIST:
            return ('reject',
                    f'"{base}" is {INI_BLOCKLIST[name]}, not a character file.\n\n'
                    f'You want the file named like  YourChar_server.ini')

        # UI_Charname_server.ini holds window layouts, not socials/macros.
        if name.startswith('ui_'):
            return ('reject',
                    f'"{base}" is a UI layout file, not your character settings.\n\n'
                    f'You want the same name WITHOUT the "UI_" prefix:\n'
                    f'{base[3:]}')

        # Peek at the contents for the tell-tale character sections.
        try:
            with open(path, 'r', encoding='latin-1') as f:
                head = f.read(65536).lower()
        except Exception as e:
            return ('reject', f"Couldn't read that file:\n{e}")

        if any(sec in head for sec in EQ_CHAR_SECTIONS):
            return ('ok', "")

        return ('warn',
                f'"{base}" doesn\'t look like an EverQuest character INI — '
                f'none of the usual sections ([Socials], [Hot Buttons], '
                f'[Key Mapping]) were found.\n\n'
                f'Character INIs are named like  YourChar_server.ini  and live '
                f'in your EverQuest folder.\n\n'
                f'Use it anyway?')

    def _select_ini_file(self):
        """Prompt for the character INI, re-prompting until a sane file is
        chosen (or the user cancels). Returns a path or None."""
        # Default the filter to *<server>*.ini so only this server's characters
        # show up (e.g. *Frostreaver*.ini -> Serelle_frostreaver_CLR.ini). The
        # match is case-insensitive in the Windows dialog. UI_*.ini files for
        # this server still slip through the glob, but validation rejects those.
        server = (self.server_var.get() or _config.get("server", "")).strip()
        filetypes = []
        if server:
            filetypes.append((f"{server} characters", f"*{server}*.ini"))
        filetypes += [("Character INI", "*_*.ini"),
                      ("INI files", "*.ini"),
                      ("All files", "*.*")]
        while True:
            path = filedialog.askopenfilename(
                title="Select Character INI File  (YourChar_server.ini)",
                initialdir=self._last_ini_dir or None,
                filetypes=filetypes)
            if not path:
                return None

            level, msg = self._validate_eq_char_ini(path)
            if level == 'ok':
                self._last_ini_dir = os.path.dirname(path)
                self._last_ini_path = path
                return path
            if level == 'reject':
                self._last_ini_dir = os.path.dirname(path)
                messagebox.showerror("Wrong file — pick again", msg)
                continue  # loop back to the picker
            # 'warn' — let the user override an uncertain pick
            self._last_ini_dir = os.path.dirname(path)
            if messagebox.askyesno("Doesn't look right", msg):
                return path
            # otherwise re-prompt

    def _write_ini(self):
        """Write generated macros directly to the EQ character INI file."""
        # Get the generated output
        content = self.output_text.get('1.0', 'end').strip()
        if not content or '--- PREVIEW' not in content:
            messagebox.showwarning("Warning", "Generate macros first!")
            return

        # Only the INI part (before preview)
        ini_content = content.split('--- PREVIEW')[0].strip()
        if not ini_content:
            return

        # Select INI file if not already selected
        if not self.ini_path:
            path = self._select_ini_file()
            if not path:
                return
            self.ini_path = path
            self.ini_var.set(os.path.basename(path))

        # Confirm
        if not messagebox.askyesno("Write to INI",
                                   f"Write macros to:\n{self.ini_path}\n\n"
                                   f"A backup will be created first.\n"
                                   f"Make sure EQ is CLOSED!\n\nProceed?"):
            return

        try:
            # Create backup
            backup_path = self.ini_path + ".bak"
            import shutil
            shutil.copy2(self.ini_path, backup_path)
            self._log(f"Backup: {os.path.basename(backup_path)}")

            # Read existing INI
            with open(self.ini_path, 'r', encoding='latin-1') as f:
                existing = f.read()

            # Parse the generated lines into key=value pairs
            new_entries = {}
            for line in ini_content.split('\n'):
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, val = line.split('=', 1)
                    new_entries[key.strip()] = val

            # Ensure [Socials] section exists
            if '[Socials]' not in existing:
                existing = existing.rstrip() + '\n\n[Socials]\n'

            # First, find buttons WE wrote on a previous run — their Name is
            # exactly WTS# or Rare#. We clear these out before writing the new
            # set so a shrunk list (or the old page-1 bug) doesn't leave
            # orphaned buttons behind. Hand-made socials are never matched.
            import re
            auto_name_re = re.compile(r'^(?:WTS|Rare)\d+$')
            drop_prefixes = set()  # e.g. 'Page2Button1' -> drop its Name/Color/Line*
            in_socials = False
            for line in existing.split('\n'):
                st = line.strip()
                if st == '[Socials]':
                    in_socials = True
                elif st.startswith('[') and st.endswith(']'):
                    in_socials = False
                elif in_socials and '=' in st:
                    k, v = st.split('=', 1)
                    k = k.strip()
                    if k.endswith('Name') and auto_name_re.match(v.strip()):
                        drop_prefixes.add(k[:-4])  # strip trailing 'Name'

            def _is_auto_button(key):
                """True if key belongs to a slot we previously auto-wrote."""
                for p in drop_prefixes:
                    if key in (p + 'Name', p + 'Color'):
                        return True
                    if key.startswith(p + 'Line') and key[len(p) + 4:].isdigit():
                        return True
                return False

            # Find the [Socials] section and update/add entries
            lines = existing.split('\n')
            output_lines = []
            in_socials = False
            written_keys = set()

            for line in lines:
                stripped = line.strip()
                if stripped == '[Socials]':
                    in_socials = True
                    output_lines.append(line)
                    continue
                elif stripped.startswith('[') and stripped.endswith(']'):
                    # Entering a new section — flush any unwritten keys first
                    if in_socials:
                        for key, val in new_entries.items():
                            if key not in written_keys:
                                output_lines.append(f"{key}={val}")
                                written_keys.add(key)
                    in_socials = False
                    output_lines.append(line)
                    continue

                if in_socials and '=' in stripped:
                    key = stripped.split('=', 1)[0].strip()
                    if key in new_entries:
                        output_lines.append(f"{key}={new_entries[key]}")
                        written_keys.add(key)
                        continue
                    if _is_auto_button(key):
                        continue  # orphaned auto-button from a previous run — drop it

                output_lines.append(line)

            # If we were still in [Socials] at EOF, flush remaining
            if in_socials:
                for key, val in new_entries.items():
                    if key not in written_keys:
                        output_lines.append(f"{key}={val}")

            # Write back as ANSI (latin-1)
            with open(self.ini_path, 'w', encoding='latin-1') as f:
                f.write('\n'.join(output_lines))

            self._log(f"Written to {os.path.basename(self.ini_path)}!")
            messagebox.showinfo("Success",
                                f"Macros written to INI!\n\n"
                                f"Backup saved as .bak\n"
                                f"Log in to EQ to use your new macros.")

        except Exception as e:
            messagebox.showerror("Error", f"Write failed: {e}\n\nINI restored from backup.")
            # Restore backup on failure
            try:
                shutil.copy2(backup_path, self.ini_path)
            except Exception:
                pass

    @staticmethod
    def _parse_plat_value(raw):
        """Parse a price box ('600', '600p', '1kr') into a plat int. 0 means
        OFF. A 'kr' unit is converted with the default krono rate. Blank or
        invalid -> 0."""
        raw = (raw or '').strip().lower().replace(',', '').replace(' ', '')
        if not raw:
            return 0
        mult = 1
        if 'kr' in raw:
            mult = DEFAULT_KRONO_RATE
            raw = raw.replace('kr', '')
        raw = raw.rstrip('p')
        if not raw:
            return mult if mult > 1 else 0  # bare "kr" -> one krono
        try:
            return max(int(float(raw) * mult), 0)
        except ValueError:
            return 0

    def _threshold_plat(self):
        """Plat floor at/above which items go out as clickable links.
        0 = link everything (classic behavior)."""
        return self._parse_plat_value(self.threshold_var.get())

    def _vendor_pp(self, name, item_id=0):
        """Estimated NPC buyback (plat float) for an item at the current CHA, or
        None if CHA isn't set / the item has no DB base price."""
        cha = self._cha()
        price = self._base_copper(name, item_id)
        if cha is None or not price:
            return None
        return vendor_value_pp(price, cha)

    def _is_vendor_trash(self, item):
        """True if this priced item is worth at least as much to an NPC vendor as
        you'd net from a player (post-undercut price already applied). Unpriced /
        krono items are never trash — we don't junk what we can't compare."""
        kind, plat = self._classify_price(item.get('price', ''))
        if kind != 'plat':
            return False
        vpp = self._vendor_pp(item['name'], self._item_id(item))
        return vpp is not None and vpp >= plat

    @staticmethod
    def _classify_price(price_str):
        """Classify an item's price for the link/text split. Returns
        (kind, plat) where kind is 'krono' | 'plat' | 'none'. 'krono' always
        links; 'plat' is compared to the threshold; 'none' is unpriced (goes
        to the text list flagged 'pst')."""
        s = (price_str or '').strip().lower()
        if not s:
            return ('none', 0)
        if 'kr' in s:
            return ('krono', 0)
        digits = ''.join(ch for ch in s if ch.isdigit() or ch == '.')
        if digits:
            try:
                return ('plat', int(float(digits)))
            except ValueError:
                return ('none', 0)
        return ('none', 0)

    def _pack_to_lines(self, tokens, prefix, suffix, sep):
        """Pack token strings (link or text) into <=255-char auction lines,
        each led by prefix and optionally tailed by suffix."""
        lines, cur = [], []
        base = len(prefix) + 1
        suffix_len = len(f" {suffix}") if suffix else 0
        cur_len = base
        for tok in tokens:
            add = (len(sep) if cur else 0) + len(tok)
            if cur and cur_len + add + suffix_len > 255:
                line = f"{prefix} " + sep.join(cur)
                lines.append(line + (f" {suffix}" if suffix else ""))
                cur, cur_len = [tok], base + len(tok)
            else:
                cur.append(tok)
                cur_len += add
        if cur:
            line = f"{prefix} " + sep.join(cur)
            lines.append(line + (f" {suffix}" if suffix else ""))
        return lines

    def _buttons_from_lines(self, lines, btn_name, start_page, max_lines_btn=5):
        """Lay packed lines into EQ social buttons starting at start_page,
        rolling to the next page after BUTTONS_PER_PAGE. btn_name is the label
        stem (WTS / Rare); buttons number from 1. Returns
        (ini_lines, preview, overflow_count, written, end_page)."""
        out, preview = [], []
        page, btn, written, overflow = start_page, 1, 0, 0
        chunks = list(range(0, len(lines), max_lines_btn))
        for bs in chunks:
            if btn > BUTTONS_PER_PAGE:
                page += 1
                btn = 1
            if page > MAX_PAGE:
                overflow = len(chunks) - written
                break
            bl = lines[bs:bs + max_lines_btn]
            label = f"{btn_name}{written + 1}"
            out.append(f"Page{page}Button{btn}Name={label}")
            out.append(f"Page{page}Button{btn}Color=0")
            for ln, line in enumerate(bl, 1):
                out.append(f"Page{page}Button{btn}Line{ln}={line}")
            out.append("")
            preview.append((label, bl))
            btn += 1
            written += 1
        return out, preview, overflow, written, page

    def _report_trash(self, trash):
        """Log + popup the vendor-trash items (worth more to an NPC than to
        players) with their bag locations, so you know what to go sell."""
        if not trash:
            return
        rows = []
        for it in trash:
            loc = self.inv_by_name.get(it['name'], {}).get('location', '?')
            vpp = self._vendor_pp(it['name'], self._item_id(it))
            vstr = f"{vpp:.0f}p" if vpp is not None else "?"
            rows.append((it['name'], it.get('price', ''), vstr, loc))
            self._log(f"  VENDOR ({vstr} vs {it.get('price', '')}): "
                      f"{it['name']} @ {loc}")
        shown = rows[:15]
        body = "\n".join(f"• {n}: player {p} / vendor {v} — {loc}"
                         for n, p, v, loc in shown)
        if len(rows) > len(shown):
            body += f"\n…and {len(rows) - len(shown)} more (see log)."
        messagebox.showinfo(
            "Go vendor these",
            f"{len(trash)} item(s) are worth more to a vendor than to players, "
            f"so they were left OUT of your macros:\n\n{body}")

    def _check_update(self):
        """Background check for a newer GitHub release. Non-blocking and silent
        on failure — never nags when offline, already current, or ahead."""
        def work():
            latest = check_latest_release()
            if not latest:
                return
            cur, new = _version_tuple(APP_VERSION), _version_tuple(latest)
            if new and cur and new > cur:
                self.root.after(0, lambda: self._show_update(latest))
        threading.Thread(target=work, daemon=True).start()

    def _show_update(self, latest):
        # Passive reminder: the orange clickable label stays in the toolbar.
        self.update_var.set(f"⬆ v{latest} available")
        self._log(f"Update available: v{latest} (you have v{APP_VERSION}) — "
                  f"{RELEASES_URL}")
        # Active nudge: one-time prompt at startup so the update isn't missed.
        # Declining leaves the label in place to click later.
        if messagebox.askyesno(
                "Update available",
                f"Version {latest} is now available "
                f"(you have v{APP_VERSION}).\n\n"
                "Do you want to open the download page?"):
            webbrowser.open(RELEASES_URL)

    def _refresh_krono_rate(self):
        """Pull the live krono->plat rate (tlp-auctions 1-day avg) in the
        background and apply it, so 'kr' conversions use the real number instead
        of the guessed fallback. Best-effort and silent on failure."""
        def work():
            rate = fetch_krono_rate(_config["server"])
            if rate:
                self.root.after(0, lambda: self._apply_krono_rate(rate))
        threading.Thread(target=work, daemon=True).start()

    def _apply_krono_rate(self, rate, persist=True):
        """Make a freshly-synced rate the effective krono rate everywhere, stamp
        the sync time, and (by default) persist both as the offline fallback so
        the value self-maintains across runs. Called from the startup auto-sync
        and the Settings 'Sync' button."""
        global DEFAULT_KRONO_RATE
        DEFAULT_KRONO_RATE = rate
        self.krono_rate = rate
        self.krono_synced_at = datetime.now().timestamp()
        self._log(f"Krono rate: {rate}p/kr (TLP Auctions 1-day avg)")
        self._update_krono_label()
        # Re-tag auction rows: any krono-priced items fold at the new rate.
        if self.auction_items:
            self._refresh_vendor_flags()
        if persist:
            self.settings['krono_rate'] = rate
            self.settings['krono_synced_at'] = self.krono_synced_at
            try:
                save_settings(self.settings)
            except OSError:
                pass  # best-effort; the live rate still applies this session

    @staticmethod
    def _fmt_synced(ts):
        """Format a sync epoch as a short local timestamp — time-only if it
        happened today, else 'mm/dd h:mmAM'. Empty string if never synced."""
        if not ts:
            return ""
        try:
            dt = datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return ""
        t = dt.strftime('%I:%M%p').lstrip('0').lower()  # 9:14am
        if dt.date() == datetime.now().date():
            return t
        return dt.strftime('%m/%d ') + t

    def _update_krono_label(self):
        """Refresh the top-bar krono note from the current rate + sync time."""
        if not hasattr(self, 'krono_status_var'):
            return
        synced = self._fmt_synced(self.krono_synced_at)
        if synced:
            self.krono_status_var.set(
                f"krono {self.krono_rate:,}p · synced @ {synced}")
        else:
            self.krono_status_var.set(f"krono {self.krono_rate:,}p (fallback)")

    def _generate(self):
        if not self.auction_items:
            messagebox.showwarning("Warning", "No items in auction")
            return
        try:
            page = int(self.page_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid page")
            return

        # Make sure the output is visible when the user generates
        if not self.macro_expanded:
            self._toggle_macro_panel()

        prefix = self.prefix_var.get()
        suffix = self.suffix_var.get()
        threshold = self._threshold_plat()

        # Band 1 (trash): items worth at least as much to an NPC vendor as you'd
        # net from a player (post-undercut). Krono/unpriced never trash. Dropped
        # from every macro and reported with bag locations so you can go vendor.
        trash, sellable = [], []
        for item in self.auction_items:
            (trash if self._is_vendor_trash(item) else sellable).append(item)

        if not sellable:
            self._report_trash(trash)
            messagebox.showinfo(
                "All vendor trash", "Every priced item is worth more to a vendor "
                "than to players — nothing to auction. Go vendor them!")
            return

        # Every sellable item needs a DB link for the link group — bail early
        # (and clearly) if one is missing rather than half-generating.
        for item in sellable:
            if not self._link_for(item):
                messagebox.showwarning("Missing", f"No link: {item['name']}")
                return

        def link_token(item):
            # Link by the item's real id when known (disambiguates duplicate
            # names like the two 'Mistmoore Battle Drums'); else by name.
            link = make_link(self._link_for(item), item['name'])
            # No "xN": tlp-auctions reads a trailing "x2" as two-for-price.
            return f"{link} {item['price']}" if item['price'] else link

        def text_token(item):
            # Exact DB-case name (tlp-auctions matches unlinked posts on exact
            # case); price as-is, or 'pst' when unpriced so it reads "ask me".
            return f"{item['name']} {item['price']}" if item['price'] \
                else f"{item['name']} pst"

        out, preview, overflow, unpriced = [], [], 0, []

        if threshold <= 0:
            # Split OFF -> classic behavior: everything links, WTS#, at `page`.
            lines = self._pack_to_lines(
                [link_token(i) for i in sellable], prefix, suffix, ", ")
            out, preview, overflow, _, _ = self._buttons_from_lines(lines, "WTS", page)
            self._log(f"Generated {len(preview)} button(s) (link everything)")
        else:
            # Split ON -> cheap items to compact text, movers/krono to links.
            text_items, link_items = [], []
            for item in sellable:
                kind, plat = self._classify_price(item['price'])
                if kind == 'krono' or (kind == 'plat' and plat >= threshold):
                    link_items.append(item)
                else:
                    if kind == 'none':
                        unpriced.append(item['name'])
                    text_items.append(item)

            text_lines = self._pack_to_lines(
                [text_token(i) for i in text_items], prefix, suffix, " | ")
            link_lines = self._pack_to_lines(
                [link_token(i) for i in link_items], prefix, suffix, ", ")

            # Both groups start at the Page box (default 2) and go up, so page 1
            # — the player's normal action buttons — is never touched. Text
            # fills first; the premium "Rare" links get a fresh page after it.
            text_out, text_pv, text_of, _, text_end = self._buttons_from_lines(
                text_lines, "WTS", page)
            link_start = max(page, text_end + 1) if text_items else page
            link_out, link_pv, link_of, _, _ = self._buttons_from_lines(
                link_lines, "Rare", link_start)

            out = text_out + link_out
            preview = text_pv + link_pv
            overflow = text_of + link_of
            self._log(f"Split @ {threshold}p: {len(text_items)} text (WTS, pg {page}), "
                      f"{len(link_items)} link (Rare, pg {link_start})")
            if unpriced:
                self._log(f"  no price -> 'pst': {', '.join(unpriced)}")

        self.output_text.delete('1.0', 'end')
        self.output_text.insert('1.0', '\n'.join(out))

        # Preview (only the buttons that actually fit)
        self.output_text.insert('end', '\n--- PREVIEW ---\n')
        for label, bl in preview:
            self.output_text.insert('end', f"\n{label}:\n")
            for i, line in enumerate(bl, 1):
                clean = line.replace(DC2, '|')
                self.output_text.insert('end', f"  L{i} ({len(line)}c): {clean}\n")

        self._report_trash(trash)

        if unpriced:
            messagebox.showinfo(
                "Unpriced items",
                f"{len(unpriced)} item(s) had no price and were listed as plain "
                f"text with 'pst':\n\n" + ", ".join(unpriced))

        if overflow:
            messagebox.showwarning(
                "Too many items",
                f"Your list is too big to fit in EQ's socials — "
                f"Page {MAX_PAGE} is the last page.\n\n"
                f"{overflow} macro button(s) didn't fit and were left out.\n\n"
                f"Lower the starting Page number, or split your items "
                f"across more than one character.")
            self._log(f"WARNING: {overflow} button(s) past Page {MAX_PAGE} were dropped")

    def _copy(self):
        content = self.output_text.get('1.0', 'end').strip()
        if not content:
            return
        if '--- PREVIEW' in content:
            content = content.split('--- PREVIEW')[0].strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.root.update()
        messagebox.showinfo("Copied", "Paste into INI (save as ANSI!)")

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=f"EQ Auction Forge v{APP_VERSION} - wangel")
    parser.add_argument("--db", default=ITEMS_DB)
    args = parser.parse_args()

    app = AuctionBuilder(db_path=args.db)
    app.run()


if __name__ == "__main__":
    main()