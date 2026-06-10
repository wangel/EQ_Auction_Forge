# EQ-Auction_Forge.py
# EQ Auction Macro Builder with price checking
#
# Features:
#   - Uses items.txt.gz database for pre-computed link hashes
#   - Per-item pricing (edit price per item in auction list)
#   - Price check via TLP Auctions API (Frostreaver)
#   - Auto-packs items across lines (max 255 chars/line, 5 lines/button)
#   - Load inventory to filter to items you own
#
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
from urllib.parse import urlencode

import ssl

# A windowed PyInstaller build has no console, so sys.stdout/stderr are None.
# Guard against stray print()/traceback writes crashing the app.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

DC2 = '\x12'


def _app_dir():
    """Directory the app lives in — works whether run as a .py or a
    PyInstaller-frozen .exe. Used to locate items.txt.gz regardless of
    the current working directory the app was launched from."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _cache_dir():
    """User-writable folder for the extracted items.txt, so the app works
    even when installed somewhere read-only (e.g. Program Files)."""
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    d = os.path.join(base, 'EQAuctionForge')
    os.makedirs(d, exist_ok=True)
    return d


ITEMS_DB = os.path.join(_app_dir(), "items.txt.gz")
SERVER = "Frostreaver"
# Servers selectable in the UI dropdown. The box is editable, so a server
# not listed here can still be typed in for price checks. Only Frostreaver is
# listed for now — once a TLP gets Bazaar, tlp-auctions data dries up. Add new
# TLPs (or EQ Legends) here when they launch.
SERVERS = ["Frostreaver"]
_config = {"server": "Frostreaver"}
API_BASE = "https://api.tlp-auctions.com"

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

# SSL context — their API cert doesn't match the hostname
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def load_item_database(gz_path):
    """Load the item DB. Returns (links, ids):
      links: name -> itemlink (hash+name) for building clickable links
      ids:   name -> item id (int) for the bulk price API
    """
    items = {}
    ids = {}
    txt_path = os.path.join(_cache_dir(), 'items.txt')
    if not os.path.isfile(txt_path):
        if not os.path.isfile(gz_path):
            return items, ids
        print(f"  Extracting {gz_path}...")
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
                    items[name] = link
                    item_id = row.get('id', '').strip()
                    if item_id.isdigit() and name not in ids:
                        ids[name] = int(item_id)
            except Exception:
                continue
    print(f"  {len(items)} items loaded")
    return items, ids


# Worthless newbie/starter items that clutter the inventory list but never get
# sold in EC tunnel. Matched by exact (case-insensitive) name so we don't nuke
# named gear that happens to share a word (e.g. "Dagger" the newbie item vs.
# "Ceremonial Dagger"). Add more here as you spot them.
EXCLUDED_ITEMS = frozenset(n.lower() for n in (
    "Backpack",
    "Small Box",
    "Dagger",
    "Skin of Milk",
    "Bread Cakes",
    "Gloomingdeep Lantern",
    "Ethereal Dreamweave Satchel",
    "Dreamweave Satchel",
))


def load_inventory(filepath):
    """Read an EQ /outputfile inventory dump (tab-separated).

    The dump has a Count column, and stackable items (spells, potions, etc.)
    or duplicate gear show up on separate lines per slot. We combine entries
    with the same name into one, summing their counts, so a stack of 2 scrolls
    in two slots becomes a single 'x2' entry. Worthless newbie/starter items
    (see EXCLUDED_ITEMS) are dropped. Returns a list of
    {'name', 'location', 'count'} in first-seen order.
    """
    combined = {}  # name -> {'name', 'location', 'count'}
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
            if name in combined:
                combined[name]['count'] += count
            else:
                combined[name] = {'name': name, 'location': loc, 'count': count}
                order.append(name)
    return [combined[n] for n in order]


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

APP_VERSION = "1.3.7"

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


def format_plat_price(median_pp, krono_rate):
    """Format an effective plat price as kr/pp using the server's krono rate.

    The bulk API already folds krono-priced sales into plat, so median_pp is a
    straight plat value. We only use krono_rate to display big numbers in kr.
    """
    median_pp = int(round(median_pp))
    if krono_rate and krono_rate > 0:
        kr = int(median_pp // krono_rate)
        rem = int(median_pp % krono_rate)
        if kr >= 1:
            return f"{kr}kr" if rem < 500 else f"{kr}kr {rem}p"
    return f"{median_pp}p"


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


class AuctionBuilder:
    def __init__(self, db_path=ITEMS_DB):
        self.db_path = db_path
        self.item_db = {}
        self.item_ids = {}
        self.inventory = []
        self.inv_by_name = {}  # name -> inventory entry, for count/location lookups
        self.auction_items = []  # list of {'name', 'price', 'count'}
        self.inv_loaded = False
        # Per-(tree, column) ascending/descending toggle for header sorting.
        self._sort_state = {}

        self.root = tk.Tk()
        self.root.title("EQ Auction Forge v1.3.7 — by wangel")
        self.root.configure(bg='#1a1a1a')
        self.root.geometry("1000x800")
        self._build_ui()
        self.root.after(100, self._load_db)
        self.root.after(800, self._check_update)

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
        self.status_var = tk.StringVar(value="Loading...")
        ttk.Label(top, textvariable=self.status_var, foreground='#888888').pack(side='left', padx=10)

        # Server selector (drives price checks). Editable so any server can be typed.
        ttk.Label(top, text="Server:").pack(side='left', padx=(10, 2))
        self.server_var = tk.StringVar(value=_config["server"])
        self.server_var.trace_add(
            'write', lambda *a: _config.__setitem__('server', self.server_var.get().strip()))
        ttk.Combobox(top, textvariable=self.server_var, values=SERVERS,
                     width=13).pack(side='left')

        self.db_count_var = tk.StringVar()
        ttk.Label(top, textvariable=self.db_count_var, foreground='#00ff00').pack(side='right')
        ttk.Button(top, text="Help", command=self._show_help).pack(side='right', padx=5)

        # Update nudge — stays blank/invisible unless a newer release exists.
        # Click it to open the releases page.
        self.update_var = tk.StringVar(value="")
        self._update_lbl = ttk.Label(top, textvariable=self.update_var,
                                     foreground='#FFA500',
                                     font=('Consolas', 9, 'bold'), cursor='hand2')
        self._update_lbl.pack(side='right', padx=8)
        self._update_lbl.bind('<Button-1>', lambda e: webbrowser.open(RELEASES_URL))

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

        # Tree + scrollbar live in their own frame so a button row can sit
        # below them (mixing pack sides in one parent gets messy otherwise).
        tree_frame = ttk.Frame(left)
        tree_frame.pack(side='top', fill='both', expand=True)
        cols = ('name', 'qty', 'location')
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
        self.item_tree.column('name', width=280)
        self.item_tree.column('qty', width=40, anchor='center')
        self.item_tree.column('location', width=120)
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
        self.auc_tree = ttk.Treeview(auc_frame, columns=('name', 'price', 'qty'),
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
        self.auc_tree.column('name', width=190)
        self.auc_tree.column('price', width=80)
        self.auc_tree.column('qty', width=40, anchor='center')
        auc_sb = ttk.Scrollbar(auc_frame, orient='vertical', command=self.auc_tree.yview)
        self.auc_tree.configure(yscrollcommand=auc_sb.set)
        self.auc_tree.pack(side='left', fill='both', expand=True)
        auc_sb.pack(side='right', fill='y')
        self.auc_tree.bind('<Double-1>', self._remove_from_auction)
        self.auc_tree.bind('<Delete>', self._remove_from_auction)
        self.auc_tree.bind('<<TreeviewSelect>>', self._on_auction_select)
        self._bind_select_all(self.auc_tree)

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
        self.undercut_var = tk.StringVar(value="0")
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
        self.prefix_var = tk.StringVar(value="/auc WTS")
        ttk.Entry(sf, textvariable=self.prefix_var, width=12).pack(side='left', padx=5)
        ttk.Label(sf, text="Page:").pack(side='left', padx=(10, 0))
        self.page_var = tk.StringVar(value="2")
        ttk.Entry(sf, textvariable=self.page_var, width=3).pack(side='left', padx=5)
        ttk.Label(sf, text="Suffix:").pack(side='left', padx=(10, 0))
        self.suffix_var = tk.StringVar(value="")
        ttk.Entry(sf, textvariable=self.suffix_var, width=8).pack(side='left', padx=(5, 2))
        ttk.Button(sf, text="?", width=2, command=self._suffix_help).pack(side='left')

        # Macro pricing threshold: items priced at/above this go out as
        # clickable links (the high-ticket "movers"); cheaper items go out as
        # compact plain text. Krono-priced items always link. Blank/0 links
        # everything (the classic behavior).
        tf = ttk.Frame(self.macro_panel)
        tf.pack(fill='x', pady=3)
        ttk.Label(tf, text="Link if ≥:").pack(side='left')
        self.threshold_var = tk.StringVar(value="600p")
        ttk.Entry(tf, textvariable=self.threshold_var, width=6).pack(side='left', padx=(5, 8))
        ttk.Label(tf, text="Vendor <:").pack(side='left')
        self.vendor_var = tk.StringVar(value="100p")
        ttk.Entry(tf, textvariable=self.vendor_var, width=6).pack(side='left', padx=5)
        ttk.Label(tf, text="≥ link · < vendor = trash (skipped) · blank/0 = off",
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

        help_text = """EQ Auction Forge v1.3.7
by wangel

HOW TO USE:

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
     TLP Auctions (uses median, not average)
   - Set "Undercut %" to shave that % off every
     price-checked median (e.g. 5 = 5% under)
   - "Price Check" under the items list checks the
     selected inventory item(s) without adding them
   - "Recent Postings" lists the last few sales of
     one item, with time, for a quick reference
5. Click "Generate" to build the macros
6. Click "Write to INI" to save directly to your
   character INI file (auto-backup created)
7. Log into EQ — your macro buttons have clickable
   purple item links!

TIPS:
- Uncheck "Inv only" to search ALL 133k+ items
  (useful for WTB macros)
- Check "Bags only" to hide equipped/bank gear and
  show just what's sitting in your inventory bags
- Click any column header to sort. Sort the item
  list by Location to grab a whole bag at once; sort
  the auction list by Price (after PC All) to find
  and delete the cheap stuff. Click again to reverse.
- Items auto-pack 2+ per line when they fit under
  the 255 character limit
- Each macro button supports up to 5 lines
- Save/Load preserves your auction list as JSON
- Price Check uses median pricing for accuracy

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

    def _load_db(self):
        self.item_db, self.item_ids = load_item_database(self.db_path)
        if self.item_db:
            self.db_count_var.set(f"{len(self.item_db)} items in DB")
            self.status_var.set("Ready")
        else:
            self.status_var.set("ERROR: items.txt.gz not found!")

    def _load_inventory(self):
        path = filedialog.askopenfilename(
            title="Select Inventory File", filetypes=[("Text", "*.txt")])
        if not path:
            return
        self.inventory = load_inventory(path)
        self.inv_by_name = {it['name']: it for it in self.inventory}
        self.inv_loaded = True
        self.inv_only_var.set(True)
        self.status_var.set(f"Inventory: {len(self.inventory)} items")
        self._apply_filter()

    def _apply_filter(self):
        self.item_tree.delete(*self.item_tree.get_children())
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
                if name in self.item_db:
                    self.item_tree.insert(
                        '', 'end',
                        values=(name, self._qty_str(item.get('count', 1)),
                                item['location']))
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
                self.item_tree.insert(
                    '', 'end',
                    values=(name, self._qty_str(inv.get('count', 1)),
                            inv.get('location', "")))
                count += 1
        self.item_count_var.set(f"({count})")

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
        kr = re.search(r'(\d+)\s*kr', s)
        pp = re.search(r'(\d+)\s*p', s)
        if kr or pp:
            plat = (int(kr.group(1)) * DEFAULT_KRONO_RATE if kr else 0)
            plat += int(pp.group(1)) if pp else 0
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
                     'location': self._location_sort_key}
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

    def _refresh_auction_tree(self):
        """Rebuild every auction row from self.auction_items (in list order)."""
        self.auc_tree.delete(*self.auc_tree.get_children())
        for item in self.auction_items:
            self.auc_tree.insert('', 'end', values=self._auc_values(item))

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
        """Tree row tuple for an auction item: (name, price, qty)."""
        return (item['name'], item.get('price', ''),
                self._qty_str(item.get('count', 1)))

    def _add_rows_to_auction(self, sel):
        """Add the given item-tree rows to the auction list."""
        price = self.price_var.get() or ""
        added = 0
        for iid in sel:
            name = str(self.item_tree.item(iid)['values'][0])
            if name not in self.item_db:
                continue
            count = self.inv_by_name.get(name, {}).get('count', 1)
            item = {'name': name, 'price': price, 'count': count}
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
        """Show the selected item's current price in the price field."""
        sel = self.auc_tree.selection()
        if not sel:
            return
        idx = self.auc_tree.index(sel[0])
        if idx < len(self.auction_items):
            current_price = self.auction_items[idx].get('price', '')
            self.price_var.set(current_price)

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
                self.auc_tree.item(iid, values=self._auc_values(self.auction_items[idx]))

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

    def _result_to_price(self, name, res, krono_rate, undercut=0.0):
        """Turn a single bulk-API result into (price, detail) strings.

        When undercut > 0, the price is the median shaved by that percent so it
        lands just under the going rate; detail still shows the raw median."""
        if not res or not res.get('hasData'):
            return None, "No price data"
        median = res.get('medianPlatPrice', 0)
        samples = res.get('sampleSize', 0)
        median_str = format_plat_price(median, krono_rate)
        if undercut:
            price = format_plat_price(median * (1 - undercut / 100.0), krono_rate)
            detail = f"Median: {median_str} ({samples} sales) -> -{undercut:g}% = {price}"
            return price, detail
        return median_str, f"Median: {median_str} ({samples} sales)"

    def _price_check(self):
        """Price check the selected item via the bulk endpoint (single id)."""
        sel = self.auc_tree.selection()
        if not sel:
            sel = self.item_tree.selection()
            if not sel:
                return
            name = self.item_tree.item(sel[0])['values'][0]
        else:
            name = self.auc_tree.item(sel[0])['values'][0]

        item_id = self.item_ids.get(name)
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
            price, detail = self._result_to_price(
                name, results.get(item_id), krono_rate, undercut)
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
            item_id = self.item_ids.get(item['name'])
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
                    price, detail = self._result_to_price(
                        name, results.get(item_id), krono_rate, undercut)
                    self.root.after(0, lambda n=name, p=price, d=detail, i=idx:
                                    self._on_price_result_update(n, p, d, i))
            self.root.after(0, lambda: self._log("Price check complete!"))

        threading.Thread(target=do_all, daemon=True).start()

    def _on_price_result(self, name, price, detail):
        self._log(f"  {name}: {detail}")
        if price:
            self.price_var.set(price)

    def _on_price_result_update(self, name, price, detail, idx):
        self._log(f"  {name}: {detail}")
        if price and idx < len(self.auction_items):
            self.auction_items[idx]['price'] = price
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
            item_id = self.item_ids.get(name)
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
                    price, detail = self._result_to_price(
                        name, results.get(item_id), krono_rate, undercut)
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
        win.geometry("520x320")
        win.attributes('-topmost', True)

        ttk.Label(win, text=name, foreground='#00ff00',
                  font=('Consolas', 11, 'bold')).pack(anchor='w', padx=12, pady=(10, 0))
        ttk.Label(win, text=f"Last {len(sales)} postings on {_config['server']} "
                            f"(newest first)", foreground='#888888').pack(
            anchor='w', padx=12, pady=(0, 4))

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

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

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
                       'vendor': self.vendor_var.get(),
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
            if isinstance(data, dict) and 'vendor' in data:
                self.vendor_var.set(data.get('vendor', ''))
            for item in items:
                name = item.get('name', '')
                price = item.get('price', '')
                count = item.get('count', 1)
                if name:
                    entry = {'name': name, 'price': price, 'count': count}
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

    def _vendor_plat(self):
        """Plat floor BELOW which a priced item is vendor trash, dropped from
        the macros and reported. 0 = keep everything."""
        return self._parse_plat_value(self.vendor_var.get())

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

    def _report_trash(self, trash, vendor_min):
        """Log + popup the vendor-trash items with their bag locations, so the
        user knows exactly what to go sell to a vendor."""
        if not trash:
            return
        rows = []
        for it in trash:
            loc = self.inv_by_name.get(it['name'], {}).get('location', '?')
            rows.append((it['name'], it.get('price', ''), loc))
            self._log(f"  TRASH < {vendor_min}p: {it['name']} "
                      f"({it.get('price', '')}) @ {loc}")
        shown = rows[:15]
        body = "\n".join(f"• {n} ({p}) — {loc}" for n, p, loc in shown)
        if len(rows) > len(shown):
            body += f"\n…and {len(rows) - len(shown)} more (see log)."
        messagebox.showinfo(
            "Go vendor these — they're trash",
            f"{len(trash)} item(s) priced under {vendor_min}p were left OUT of "
            f"your macros. Go vendor them:\n\n{body}")

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
        self.update_var.set(f"⬆ v{latest} available")
        self._log(f"Update available: v{latest} (you have v{APP_VERSION}) — "
                  f"{RELEASES_URL}")

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
        vendor_min = self._vendor_plat()

        # Band 1 (trash): priced strictly under the vendor floor. Krono and
        # unpriced items are never trash — we won't call something junk we
        # can't price. Trash is dropped from every macro and reported with
        # bag locations so you can go vendor it.
        trash, sellable = [], []
        for item in self.auction_items:
            kind, plat = self._classify_price(item['price'])
            if vendor_min > 0 and kind == 'plat' and plat < vendor_min:
                trash.append(item)
            else:
                sellable.append(item)

        if not sellable:
            self._report_trash(trash, vendor_min)
            messagebox.showinfo(
                "All trash", "Every priced item is below the vendor floor — "
                "nothing to auction. Go vendor them!")
            return

        # Every sellable item needs a DB link for the link group — bail early
        # (and clearly) if one is missing rather than half-generating.
        for item in sellable:
            if not self.item_db.get(item['name']):
                messagebox.showwarning("Missing", f"No link: {item['name']}")
                return

        def link_token(item):
            link = make_link(self.item_db[item['name']], item['name'])
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

        self._report_trash(trash, vendor_min)

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
    parser = argparse.ArgumentParser(description="EQ Auction Forge v1.3.7 - wangel")
    parser.add_argument("--db", default=ITEMS_DB)
    args = parser.parse_args()

    app = AuctionBuilder(db_path=args.db)
    app.run()


if __name__ == "__main__":
    main()