# EQ Auction Builder

Generate EverQuest social macros with **clickable item links** for EC Tunnel trading. No more manually shift-clicking items into macros — search your inventory, set prices, and export directly to your character INI file.

![EQ Auction Builder Screenshot](screenshots/main.png)

## Features

- **Item Link Generation** — Generates proper EQ item links with clickable purple text using pre-computed link hashes from the [items.sodeq.org](https://items.sodeq.org) database
- **Inventory Integration** — Load your `/outputfile inventory` dump to see only items you actually have
- **Price Checking** — Fetch real-time pricing from [TLP Auctions](https://www.tlp-auctions.com) with median-based pricing (more accurate than averages)
- **Auto-Packing** — Automatically fits as many items per line as possible while staying under EQ's 255-character limit, with up to 5 lines per macro button
- **Krono Pricing** — High-value items are automatically formatted in Krono (e.g., `2kr 500pp`)
- **Direct INI Writing** — Write macros directly to your character INI file with automatic backup
- **Save/Load** — Save your auction lists to JSON for quick reloading

## Requirements

- Python 3.8+
- No external dependencies (stdlib only — tkinter, json, gzip, csv, urllib)
- `items.txt.gz` item database (included, sourced from [items.sodeq.org](https://items.sodeq.org))

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/eq-auction-builder.git
cd eq-auction-builder
python eq_auction_builder.py
```

No `pip install` needed — runs entirely on Python's standard library.

## Usage

### Quick Start

1. In-game: `/outputfile inventory` to dump your inventory
2. Run: `python eq_auction_builder.py`
3. Click **Load Inventory** and select your inventory file
4. Check **Inv only** to filter to items you own
5. **Double-click** items to add them to your auction list
6. Click **PC All** to price check everything via TLP Auctions
7. Adjust prices as needed (select item → type price → **Set Price**)
8. Click **Generate** to build the macros
9. Click **Write to INI** to write directly to your character file

### Command Line Options

```bash
python eq_auction_builder.py                          # Default
python eq_auction_builder.py --server Frostreaver     # Set server for price checks
python eq_auction_builder.py --db /path/to/items.txt.gz  # Custom item database
```

### Supported Servers (Price Checking)

Price checking works with any server supported by [TLP Auctions](https://www.tlp-auctions.com):
- Frostreaver, Teek, Oakwynd, Yelinak, Mischief, Thornblade, and more

### How Item Links Work

EQ item links use a special character (DC2, hex `0x12`) as a delimiter around the item's hash data. The hash includes the item ID and a checksum that the client validates. This tool uses a pre-built database of item hashes so you don't need to extract them manually.

**Important:** When writing to your INI file manually (not using Write to INI), save as **ANSI encoding**, not UTF-8. In Notepad++: `Encoding → ANSI → Save`.

## Item Database

The `items.txt.gz` file contains item data from [items.sodeq.org](https://items.sodeq.org). To update it:

1. Download the latest item dump from [items.sodeq.org](https://items.sodeq.org)
2. Replace `items.txt.gz` in the project folder
3. Delete `items.txt` if it exists (will be re-extracted on next run)

## Screenshots

*Add your screenshots to a `screenshots/` folder*

## Credits

- Item data: [items.sodeq.org](https://items.sodeq.org)
- Pricing data: [TLP Auctions](https://www.tlp-auctions.com) ([API Docs](https://api.tlp-auctions.com/swagger/index.html))

## License

MIT License — see [LICENSE](LICENSE)

## Support

If you find this useful, consider buying me a coffee!

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/YOUR_KOFI)
