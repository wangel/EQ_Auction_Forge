# eq_auction_builder.py v4
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
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from urllib.request import urlopen, Request
from urllib.parse import quote

import ssl

DC2 = '\x12'
ITEMS_DB = "items.txt.gz"
SERVER = "Frostreaver"
_config = {"server": "Frostreaver"}
API_BASE = "https://api.tlp-auctions.com"

# SSL context — their API cert doesn't match the hostname
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def load_item_database(gz_path):
    items = {}
    txt_path = gz_path.replace('.gz', '')
    if not os.path.isfile(txt_path):
        if not os.path.isfile(gz_path):
            return items
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
            except Exception:
                continue
    print(f"  {len(items)} items loaded")
    return items


def load_inventory(filepath):
    items = []
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
            name = parts[ni].strip().rstrip('*')
            loc = parts[li].strip()
            if name.lower() in ('', 'empty'):
                continue
            items.append({'name': name, 'location': loc})
    return items


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


def fetch_price(item_name, server=SERVER):
    """Fetch average price from TLP Auctions API."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        # Get krono price
        kr_url = f"{API_BASE}/api/krono-prices/{server}"
        req = Request(kr_url, headers=headers)
        with urlopen(req, timeout=5, context=_ssl_ctx) as r:
            kr_data = json.loads(r.read().decode())
        krono_price = int(kr_data.get('averagePrice', 4000))

        # Get item price
        search = quote(item_name)
        pc_url = f"{API_BASE}/api/prices/pricecheck?serverName={server}&searchTerm={search}"
        req = Request(pc_url, headers=headers)
        with urlopen(req, timeout=5, context=_ssl_ctx) as r:
            data = json.loads(r.read().decode())

        avg = data.get('sellAverage')
        sales = data.get('recentSellSales', [])

        # Get actual prices from recent sales (include krono sales)
        recent_prices = []
        for sale in sales:
            pp = float(sale.get('platPrice', 0) or 0)
            kr = float(sale.get('kronoPrice', 0) or 0)
            total_pp = int(pp + (kr * krono_price))
            if total_pp > 0:
                recent_prices.append(total_pp)

        if not recent_prices:
            return None, "No price data"

        # Calculate median (more reliable than average with outliers)
        sorted_prices = sorted(recent_prices)
        mid = len(sorted_prices) // 2
        if len(sorted_prices) % 2 == 0:
            median_pp = (sorted_prices[mid - 1] + sorted_prices[mid]) // 2
        else:
            median_pp = sorted_prices[mid]

        avg_pp = int(float(avg)) if avg else median_pp

        # Format median as the suggested price (use kr for expensive items)
        kr = median_pp // krono_price
        pp = median_pp % krono_price
        if kr >= 1:
            price = f"{kr}kr" if pp < 500 else f"{kr}kr {pp}pp"
        else:
            price = f"{median_pp}pp"

        # Build detail string showing recent prices in kr or pp
        def fmt(p):
            k = p // krono_price
            r = p % krono_price
            if k >= 1:
                return f"{k}kr{f' {r}pp' if r >= 500 else ''}"
            return f"{p}pp"

        prices_str = ", ".join(fmt(p) for p in sorted_prices)
        detail = f"Median: {fmt(median_pp)} | Recent: {prices_str} ({len(sales)} sales)"
        return price, detail
    except Exception as e:
        return None, f"Error: {e}"


class AuctionBuilder:
    def __init__(self, db_path=ITEMS_DB):
        self.db_path = db_path
        self.item_db = {}
        self.inventory = []
        self.auction_items = []  # list of {'name', 'price'}
        self.inv_loaded = False

        self.root = tk.Tk()
        self.root.title("EQ Auction Builder v4 — by Wangel")
        self.root.configure(bg='#1a1a1a')
        self.root.geometry("1000x800")
        self._build_ui()
        self.root.after(100, self._load_db)

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

        # === Top ===
        top = ttk.Frame(self.root)
        top.pack(fill='x', padx=10, pady=5)
        ttk.Button(top, text="Load Inventory", command=self._load_inventory).pack(side='left')
        self.status_var = tk.StringVar(value="Loading...")
        ttk.Label(top, textvariable=self.status_var, foreground='#888888').pack(side='left', padx=10)
        self.db_count_var = tk.StringVar()
        ttk.Label(top, textvariable=self.db_count_var, foreground='#00ff00').pack(side='right')
        ttk.Button(top, text="Help", command=self._show_help).pack(side='right', padx=5)

        # === Paned ===
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=10, pady=5)

        # --- Left: Item browser ---
        left = ttk.Frame(paned)
        paned.add(left, weight=2)

        lf = ttk.Frame(left)
        lf.pack(fill='x')
        ttk.Label(lf, text="Items (double-click to add)").pack(side='left')
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

        cols = ('name', 'location')
        self.item_tree = ttk.Treeview(left, columns=cols, show='headings', height=20)
        self.item_tree.heading('name', text='Item Name')
        self.item_tree.heading('location', text='Location')
        self.item_tree.column('name', width=300)
        self.item_tree.column('location', width=120)
        sb = ttk.Scrollbar(left, orient='vertical', command=self.item_tree.yview)
        self.item_tree.configure(yscrollcommand=sb.set)
        self.item_tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.item_tree.bind('<Double-1>', self._add_to_auction)

        # --- Right: Auction builder ---
        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        ttk.Label(right, text="Auction (double-click to remove)").pack(anchor='w')

        auc_frame = ttk.Frame(right)
        auc_frame.pack(fill='both', expand=True)
        self.auc_tree = ttk.Treeview(auc_frame, columns=('name', 'price'),
                                     show='headings', height=8)
        self.auc_tree.heading('name', text='Item')
        self.auc_tree.heading('price', text='Price')
        self.auc_tree.column('name', width=220)
        self.auc_tree.column('price', width=80)
        auc_sb = ttk.Scrollbar(auc_frame, orient='vertical', command=self.auc_tree.yview)
        self.auc_tree.configure(yscrollcommand=auc_sb.set)
        self.auc_tree.pack(side='left', fill='both', expand=True)
        auc_sb.pack(side='right', fill='y')
        self.auc_tree.bind('<Double-1>', self._remove_from_auction)
        self.auc_tree.bind('<<TreeviewSelect>>', self._on_auction_select)

        # Price controls
        pf = ttk.Frame(right)
        pf.pack(fill='x', pady=3)
        ttk.Label(pf, text="Price:").pack(side='left')
        self.price_var = tk.StringVar(value="")
        ttk.Entry(pf, textvariable=self.price_var, width=10).pack(side='left', padx=5)
        ttk.Button(pf, text="Set Price", command=self._set_price).pack(side='left', padx=3)
        ttk.Button(pf, text="Price Check", command=self._price_check).pack(side='left', padx=3)
        ttk.Button(pf, text="PC All", command=self._price_check_all).pack(side='left', padx=3)

        # Settings
        sf = ttk.Frame(right)
        sf.pack(fill='x', pady=3)
        ttk.Label(sf, text="Prefix:").pack(side='left')
        self.prefix_var = tk.StringVar(value="/auc WTS")
        ttk.Entry(sf, textvariable=self.prefix_var, width=12).pack(side='left', padx=5)
        ttk.Label(sf, text="Page:").pack(side='left', padx=(10, 0))
        self.page_var = tk.StringVar(value="2")
        ttk.Entry(sf, textvariable=self.page_var, width=3).pack(side='left', padx=5)
        ttk.Label(sf, text="Suffix:").pack(side='left', padx=(10, 0))
        self.suffix_var = tk.StringVar(value="PST")
        ttk.Entry(sf, textvariable=self.suffix_var, width=8).pack(side='left', padx=5)

        # Buttons row 1
        bf = ttk.Frame(right)
        bf.pack(fill='x', pady=3)
        ttk.Button(bf, text="Generate", command=self._generate).pack(side='left')
        ttk.Button(bf, text="Copy", command=self._copy).pack(side='left', padx=5)
        ttk.Button(bf, text="Clear", command=self._clear).pack(side='left')
        ttk.Button(bf, text="Save", command=self._save_auction).pack(side='left', padx=(15, 3))
        ttk.Button(bf, text="Load", command=self._load_auction).pack(side='left')

        # Buttons row 2 - INI writing
        bf2 = ttk.Frame(right)
        bf2.pack(fill='x', pady=2)
        ttk.Button(bf2, text="Write to INI", command=self._write_ini).pack(side='left')
        ttk.Label(bf2, text="INI:", foreground='#888888').pack(side='left', padx=(10, 2))
        self.ini_var = tk.StringVar(value="(select file)")
        ttk.Label(bf2, textvariable=self.ini_var, foreground='#666666',
                  font=('Consolas', 8)).pack(side='left')
        self.ini_path = None

        # Output
        ttk.Label(right, text="INI Output:").pack(anchor='w', pady=(3, 0))
        self.output_text = scrolledtext.ScrolledText(
            right, height=8, bg='#2a2a2a', fg='#00ff00',
            font=('Consolas', 9), insertbackground='#00ff00')
        self.output_text.pack(fill='both', expand=True)

        # Log/Console
        ttk.Label(right, text="Log:").pack(anchor='w', pady=(3, 0))
        self.console = scrolledtext.ScrolledText(
            right, height=5, bg='#1e1e1e', fg='#cccccc',
            font=('Consolas', 8), insertbackground='#cccccc')
        self.console.pack(fill='x')

        ttk.Label(right,
                  text="Save INI as ANSI encoding! (Notepad++ > Encoding > ANSI)",
                  foreground='#FF4500', font=('Consolas', 8, 'bold')).pack(pady=(2, 0))

    def _log(self, msg):
        self.console.insert('end', f"{msg}\n")
        self.console.see('end')

    def _show_help(self):
        """Show help/about dialog."""
        help_win = tk.Toplevel(self.root)
        help_win.title("Help — EQ Auction Builder")
        help_win.configure(bg='#1a1a1a')
        help_win.geometry("500x520")
        help_win.attributes('-topmost', True)

        txt = scrolledtext.ScrolledText(
            help_win, bg='#2a2a2a', fg='#cccccc',
            font=('Consolas', 9), wrap='word', padx=10, pady=10)
        txt.pack(fill='both', expand=True, padx=10, pady=10)

        help_text = """EQ Auction Builder v4
by Wangel

HOW TO USE:

1. In-game: /outputfile inventory
2. Click "Load Inventory" and select the file
3. Double-click items to add to your auction list
4. Set prices:
   - Type a price, select an item, click "Set Price"
   - Or click "PC All" to auto-fetch prices from
     TLP Auctions (uses median, not average)
5. Click "Generate" to build the macros
6. Click "Write to INI" to save directly to your
   character INI file (auto-backup created)
7. Log into EQ — your macro buttons have clickable
   purple item links!

TIPS:
- Uncheck "Inv only" to search ALL 133k+ items
  (useful for WTB macros)
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
        self.item_db = load_item_database(self.db_path)
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
        self.inv_loaded = True
        self.inv_only_var.set(True)
        self.status_var.set(f"Inventory: {len(self.inventory)} items")
        self._apply_filter()

    def _apply_filter(self):
        self.item_tree.delete(*self.item_tree.get_children())
        search = self.filter_var.get().strip().lower()
        inv_only = self.inv_only_var.get()
        count = 0

        if inv_only and self.inv_loaded:
            for item in self.inventory:
                name = item['name']
                if search and search not in name.lower():
                    continue
                if name in self.item_db:
                    self.item_tree.insert('', 'end', values=(name, item['location']))
                    count += 1
                    if count >= 200:
                        break
        else:
            if not search or len(search) < 2:
                self.item_count_var.set("(type 2+ chars)")
                return
            for name in self.item_db:
                if search in name.lower():
                    loc = ""
                    for inv in self.inventory:
                        if inv['name'] == name:
                            loc = inv['location']
                            break
                    self.item_tree.insert('', 'end', values=(name, loc))
                    count += 1
                    if count >= 200:
                        break
        self.item_count_var.set(f"({count})")

    def _add_to_auction(self, event):
        sel = self.item_tree.selection()
        if not sel:
            return
        name = self.item_tree.item(sel[0])['values'][0]
        if name not in self.item_db:
            return
        price = self.price_var.get() or ""
        self.auction_items.append({'name': name, 'price': price})
        self.auc_tree.insert('', 'end', values=(name, price))

    def _remove_from_auction(self, event):
        sel = self.auc_tree.selection()
        if not sel:
            return
        idx = self.auc_tree.index(sel[0])
        self.auc_tree.delete(sel[0])
        if idx < len(self.auction_items):
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
        """Set price for selected item in auction list."""
        sel = self.auc_tree.selection()
        if not sel:
            return
        price = self.price_var.get()
        idx = self.auc_tree.index(sel[0])
        if idx < len(self.auction_items):
            self.auction_items[idx]['price'] = price
            self.auc_tree.item(sel[0], values=(self.auction_items[idx]['name'], price))

    def _price_check(self):
        """Price check selected item in auction list."""
        sel = self.auc_tree.selection()
        if not sel:
            sel = self.item_tree.selection()
            if not sel:
                return
            name = self.item_tree.item(sel[0])['values'][0]
        else:
            name = self.auc_tree.item(sel[0])['values'][0]

        self._log(f"Checking price: {name}...")
        self.root.update()

        def do_check():
            price, detail = fetch_price(name, _config["server"])
            self.root.after(0, lambda: self._on_price_result(name, price, detail))

        threading.Thread(target=do_check, daemon=True).start()

    def _price_check_all(self):
        """Price check all items in auction list."""
        if not self.auction_items:
            return
        self._log("Price checking all items...")

        def do_all():
            for i, item in enumerate(self.auction_items):
                price, detail = fetch_price(item["name"], _config["server"])
                self.root.after(0, lambda n=item['name'], p=price, d=detail, idx=i:
                self._on_price_result_update(n, p, d, idx))
                import time
                time.sleep(0.5)  # Don't spam the API
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
                                   values=(self.auction_items[idx]['name'], price))

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
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.auction_items, f, indent=2)
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
                items = json.load(f)
            # Clear current and load
            self._clear()
            for item in items:
                name = item.get('name', '')
                price = item.get('price', '')
                if name:
                    self.auction_items.append({'name': name, 'price': price})
                    self.auc_tree.insert('', 'end', values=(name, price))
            self._log(f"Loaded {len(self.auction_items)} items from {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Load failed: {e}")

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
            path = filedialog.askopenfilename(
                title="Select Character INI File",
                filetypes=[("INI files", "*.ini"), ("All files", "*.*")])
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

    def _generate(self):
        if not self.auction_items:
            messagebox.showwarning("Warning", "No items in auction")
            return
        try:
            page = int(self.page_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid page")
            return

        prefix = self.prefix_var.get()
        suffix = self.suffix_var.get()
        max_line = 255
        max_lines_btn = 5

        # Build link strings with per-item prices
        link_strings = []
        for item in self.auction_items:
            itemlink = self.item_db.get(item['name'])
            if not itemlink:
                messagebox.showwarning("Missing", f"No link: {item['name']}")
                return
            link = make_link(itemlink, item['name'])
            if item['price']:
                link_str = f"{link} {item['price']}"
            else:
                link_str = link
            link_strings.append(link_str)

        # Auto-pack into lines under 255 chars
        all_lines = []
        current_parts = []
        current_len = len(prefix) + 1
        suffix_len = len(f" {suffix}") if suffix else 0

        for ls in link_strings:
            sep = ", " if current_parts else ""
            addition = len(sep) + len(ls)
            if current_len + addition + suffix_len > max_line and current_parts:
                line = f"{prefix} " + ", ".join(current_parts)
                if suffix:
                    line += f" {suffix}"
                all_lines.append(line)
                current_parts = [ls]
                current_len = len(prefix) + 1 + len(ls)
            else:
                current_parts.append(ls)
                current_len += addition

        if current_parts:
            line = f"{prefix} " + ", ".join(current_parts)
            if suffix:
                line += f" {suffix}"
            all_lines.append(line)

        # Pack into buttons (5 lines each)
        out = []
        btn = 1
        for bs in range(0, len(all_lines), max_lines_btn):
            bl = all_lines[bs:bs + max_lines_btn]
            if btn > 10:
                page += 1
                btn = 1
            out.append(f"Page{page}Button{btn}Name=WTS{(bs // max_lines_btn) + 1}")
            out.append(f"Page{page}Button{btn}Color=0")
            for ln, line in enumerate(bl, 1):
                out.append(f"Page{page}Button{btn}Line{ln}={line}")
            out.append("")
            btn += 1

        self.output_text.delete('1.0', 'end')
        self.output_text.insert('1.0', '\n'.join(out))

        # Preview
        self.output_text.insert('end', '\n--- PREVIEW ---\n')
        bn = 0
        for bs in range(0, len(all_lines), max_lines_btn):
            bn += 1
            bl = all_lines[bs:bs + max_lines_btn]
            self.output_text.insert('end', f"\nButton {bn}:\n")
            for i, line in enumerate(bl, 1):
                clean = line.replace(DC2, '|')
                self.output_text.insert('end', f"  L{i} ({len(line)}c): {clean}\n")

        self._log(f"Generated {len(all_lines)} lines across {bn} button(s)")

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
    parser = argparse.ArgumentParser(description="EQ Auction Macro Builder v4")
    parser.add_argument("--db", default=ITEMS_DB)
    parser.add_argument("--server", default=SERVER)
    args = parser.parse_args()

    _config["server"] = args.server
    app = AuctionBuilder(db_path=args.db)
    app.run()


if __name__ == "__main__":
    main()