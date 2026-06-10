"""
Log monitor for EQ Auction Forge  (BETA — feature/log-monitor, local only).

Tails the player's own EverQuest chat log (`/log on`) and raises an alert when
someone posts a **WTB** for an item in the player's inventory — i.e. a live
sell lead. Pure-logic module: no tkinter, stdlib only, so the matcher stays
unit-testable and the app owns the UI + sound.

ToS-safe by design: this reads OUR OWN log file (the same thing GINA/parsers do),
is alert-only, and never injects input or automates the client.

Pipeline:
    LogTailer (thread) --new lines--> LogMonitor.handle_line()
        parse "[ts] Speaker auctions, '...'"  ->  skip our own lines
        Matcher.match_line(msg):  buy_segments -> score vs inventory -> tier
        pick best, dedup per (speaker, item) with a cooldown
        -> on_alert(tier, ts, speaker, item, line)   [called from the worker thread]

The matcher was calibrated against a real 22h EC-tunnel log; see the
`log-monitor-wip` memory and prototypes/ for the derivation of every knob below.
"""
import json
import math
import os
import re
import threading
import time
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Tokenization + IDF matcher  (ported from prototypes/wtb_match_proto.py)
# ---------------------------------------------------------------------------

# Grammar words + the universal spell/song prefix: dropped from BOTH item and
# query tokens. 'spell'/'song' lead almost every spell/tune name but buyers
# routinely omit them ("WTB Bane of Nife"), so counting them only penalized
# item-coverage on real matches; they carry no discriminative signal.
STOPWORDS = {'of', 'the', 'a', 'an', 'and', 'for', 'to', 'with',
             'spell', 'spells', 'song', 'songs'}
# Auction boilerplate: stripped from the QUERY only.
BOILERPLATE = {'wtb', 'wts', 'wtt', 'iso', 'lf', 'pst', 'pstme', 'plat', 'pp',
               'kr', 'krono', 'kronos', 'ea', 'each', 'cash', 'price', 'offer'}

TOKEN_RE = re.compile(r"[a-z0-9'`]+")
PRICEY_RE = re.compile(r"^\d+(?:\.\d+)?[kpm]*$")  # 5k, 500p, 1.5k, 2000, ...

# Tunables — calibrated against real data (do not tweak without re-running
# prototypes/logmon_calibrate.py against a captured log).
T_HIGH = 6.0          # min total score to be considered for the loud tier
T_MAYBE = 3.0         # min total score to register at all
ITEM_COV_HIGH = 0.85  # query must hit ~all of MY item's idf mass -> loud
ITEM_COV_MAYBE = 0.55  # strong partial (same set / different slot) -> quiet feed
EXACT_ANCHOR = 4.0    # loud also needs ONE exact match on a distinctive word, so
                      # a typo/abbrev on the rest still fires (bane of 'knife',
                      # cobalt 'gaunts') but a pure coincidental substring with no
                      # exact anchor (frost <- 'frostbringer') cannot.


def tokenize(text, drop_boilerplate=False):
    toks = []
    for t in TOKEN_RE.findall(text.lower()):
        if t in STOPWORDS:
            continue
        if drop_boilerplate and (t in BOILERPLATE or PRICEY_RE.match(t)):
            continue
        if len(t) < 2:
            continue
        toks.append(t)
    return toks


def build_idf(all_names):
    """Inverse document frequency over every DB item name, so rare words (Nife,
    Karana, Thyxl) carry the signal and common ones (helm, scale) weigh ~0."""
    df = {}
    for name in all_names:
        for t in set(tokenize(name)):
            df[t] = df.get(t, 0) + 1
    n = len(all_names)
    return {t: math.log(n / c) for t, c in df.items()}, n


def _common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def token_sim(q, c):
    """Similarity of two tokens, tuned for EQ item names. Prefix/substring beats
    raw edit-distance, but a contained token must be >=4 chars so 'bo'/'ro' can't
    ride in on 'symbol'/'necro'."""
    if q == c:
        return 1.0
    if min(len(q), len(c)) >= 4 and (q in c or c in q):
        return 0.92
    cpl = _common_prefix_len(q, c)
    if cpl >= 4:
        return 0.8
    if cpl >= 2:
        r = SequenceMatcher(None, q, c).ratio()
        if r >= 0.85:
            return 0.5 * r
    return 0.0


def score(query, cand_name, idf):
    """Score a query string against one candidate item name.

    Returns (total, q_coverage, best_single, item_cov, item_cov_exact, hits).
    item_cov is the fraction of the candidate's own idf mass the query EARNED
    (crediting earned contribution, not raw idf, so a weak substring can't fake
    a whole item); item_cov_exact counts only exact (sim==1.0) word matches and
    gates the loud tier."""
    q_toks = tokenize(query, drop_boilerplate=True)
    c_toks = tokenize(cand_name)
    total = 0.0
    best_single = 0.0
    hits = []
    earned = {}        # candidate token -> best contribution credited to it
    earned_exact = {}  # same, exact (sim==1.0) matches only
    for q in q_toks:
        best = 0.0
        best_c = None
        best_sim = 0.0
        for c in c_toks:
            sim = token_sim(q, c)
            if sim <= 0:
                continue
            w = idf.get(c, 0.0)
            if sim < 1.0:
                # Cap inexact weight by the QUERY token's own rarity so a common
                # word embedded in a rare compound (ring->peacebringer) can't
                # inherit the compound's heavy idf.
                qw = idf.get(q)
                if qw is not None:
                    w = min(w, qw)
            contrib = sim * w
            if contrib > best:
                best, best_c, best_sim = contrib, c, sim
        if best > 0:
            total += best
            best_single = max(best_single, best)
            hits.append((q, best_c, round(best, 1)))
            earned[best_c] = max(earned.get(best_c, 0.0), best)
            if best_sim >= 1.0:
                earned_exact[best_c] = max(earned_exact.get(best_c, 0.0), best)
    coverage = len(hits) / len(q_toks) if q_toks else 0.0
    uniq_c = set(c_toks)
    item_idf_total = sum(idf.get(c, 0.0) for c in uniq_c)
    item_cov = (sum(earned.values()) / item_idf_total) if item_idf_total > 0 else 0.0
    # Strongest single EXACT word match — the "anchor" that proves this is really
    # my item (not a coincidental substring). 0 when nothing matched exactly.
    exact_anchor = max(earned_exact.values()) if earned_exact else 0.0
    return total, coverage, best_single, item_cov, exact_anchor, hits


def confidence(total, coverage, best_single, item_cov, exact_anchor):
    """Two tiers:
      HIGH  = query covers ~all of my item's idf mass AND anchors on >=1 exact
              distinctive word -> loud (sound + toast). The coverage may include
              a fuzzy/typo word (bane of 'knife', cobalt 'gaunts') as long as an
              exact anchor is present; a pure coincidental substring with no exact
              anchor (frost <- 'frostbringer') is rejected.
      MAYBE = strong partial — distinctive words matched but a slot/word differs
              (Darkwood Helm vs Bracer) -> quiet feed entry, no sound.
    """
    if total < T_MAYBE:
        return None
    if item_cov >= ITEM_COV_HIGH and exact_anchor >= EXACT_ANCHOR:
        return "HIGH"
    if item_cov >= ITEM_COV_MAYBE and total >= T_HIGH:
        return "MAYBE"
    return None


# ---------------------------------------------------------------------------
# Auction-line parsing + buy-segment extraction
# ---------------------------------------------------------------------------

# [Tue Jun 09 03:14:02 2026] Soandso auctions, 'WTB fungi tunic pst'
LINE_RE = re.compile(r"^\[(.*?)\]\s+(\S+)\s+auctions,\s+'(.*)'\s*$")

# Direction markers used to slice a line into intent segments.
DIR_MARK = re.compile(r'\b(wtb|iso|lf|buying|wts|wtt|selling)\b', re.I)
_BUY_MARK = {'wtb', 'iso', 'lf', 'buying'}      # the poster wants to BUY
_SELL_MARK = {'wts', 'wtt', 'selling'}          # the poster is SELLING/trading

# A buy segment that LEADS with krono (after an optional quantity) is a currency
# purchase, not an item — "WTB Krono paying 10.5k at the wood pillar" must not
# match Spell: Pillar of Flame on the incidental location word. Krono is never
# inventory, so such segments are dropped entirely.
_KRONO_LEAD_RE = re.compile(
    r"^\W*(?:\d+(?:\.\d+)?[kpm]*\W+)*(?:krono|kronos|kr)\b", re.I)


def is_krono_trade(seg):
    """True if a buy segment is a Krono currency purchase (leads with krono)."""
    return bool(_KRONO_LEAD_RE.match(seg.strip()))


# ---------------------------------------------------------------------------
# Aliases: slang/abbreviation -> canonical item name. A buyer types "CoF" or
# "fungi", which can't fuzzy-match the real name; we expand the buy text with the
# canonical name before scoring, then the normal matcher handles it. Only fires
# if the canonical item is actually in inventory, so a big list is low-risk —
# the one thing to avoid is an alias that maps to a COMMON word (e.g. rage).
# Keys are lowercase; canonical values are exact DB item names (verified).
DEFAULT_ALIASES = {
    "fbss": "Flowing Black Silk Sash",
    "cof": "Cloak of Flames",
    "acof": "Ancient Cloak of Flames",
    "fungi": "Fungus Covered Scale Tunic",
    "fungi tunic": "Fungus Covered Scale Tunic",
    "jboots": "Journeyman's Boots",
    "j boots": "Journeyman's Boots",
    "guise": "Guise of the Deceiver",
    "ssoy": "Short Sword of the Ykesha",
    "ykesha": "Short Sword of the Ykesha",
    "eot": "Spell: Eye of Tallon",
    # Mined + DB-verified from the EQ acronym list (item-only, unambiguous).
    "aon": "Amulet of Necropotence",
    "bcg": "Bone-Clasped Girdle",
    "boc": "Blade of Carnage",
    "css": "Crystalline Short Sword",
    "gebs": "Golden Efreeti Boots",
    "lami": "Lamentation",
    "lammy": "Lamentation",
    "oss": "Obtenebrate Short Sword",
    "pgt": "Polished Granite Tomahawk",
    "sbs": "Sarnak Battle Shield",
    "bfg": "Breezeboot's Frigid Gnasher",
    "alex": "Aged Left Eye of Xygoz",
}


def load_aliases(path):
    """Load the user's alias overrides (JSON {alias: item}) from path, or {}."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {str(k).strip().lower(): str(v).strip()
                for k, v in data.items() if str(k).strip() and str(v).strip()}
    except (OSError, ValueError):
        return {}


def save_aliases(path, aliases):
    """Persist the user's alias overrides to JSON."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(aliases, f, indent=2, sort_keys=True)


def load_watchlist(path):
    """Load the user's watchlist (JSON list of item names) from path, or []."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return [str(x).strip() for x in data if str(x).strip()]
    except (OSError, ValueError):
        return []


def save_watchlist(path, items):
    """Persist the watchlist to JSON."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(sorted(items), f, indent=2)


def parse_auction_line(raw):
    """('timestamp', 'Speaker', 'message') for an auctions line, else None."""
    m = LINE_RE.match(raw.rstrip('\n\r'))
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _segments(msg, want):
    """Return only the portion(s) of a line governed by markers in `want`
    (a set of direction words). Traders post both directions in one line
    ("WTS a // WTB b"); slicing on markers keeps each intent separate so a WTS
    item can't masquerade as a buy lead (or vice versa). Krono-purchase segments
    are dropped (currency, not inventory). '' when nothing matches."""
    marks = list(DIR_MARK.finditer(msg))
    if not marks:
        return ''
    segs = []
    for i, m in enumerate(marks):
        if m.group(1).lower() in want:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(msg)
            seg = msg[m.end():end]
            if is_krono_trade(seg):
                continue
            segs.append(seg)
    return ' '.join(segs)


def buy_segments(msg):
    """The BUY portion(s) of a line — what the poster wants to purchase."""
    return _segments(msg, _BUY_MARK)


def sell_segments(msg):
    """The SELL/trade portion(s) of a line — what the poster is offering."""
    return _segments(msg, _SELL_MARK)


# ---------------------------------------------------------------------------
# Matcher: holds the prebuilt IDF + the player's inventory as candidates
# ---------------------------------------------------------------------------

def _dedupe(names):
    seen, out = set(), []
    for nm in names:
        if nm and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


class Matcher:
    def __init__(self, db_names, inventory_names, aliases=None, watchlist=None):
        self.idf, self.n = build_idf(list(db_names))
        self.set_inventory(inventory_names)
        self.set_watchlist(watchlist or [])
        self.set_aliases(aliases or {})

    def set_inventory(self, inventory_names):
        self.candidates = _dedupe(inventory_names)

    def set_watchlist(self, watchlist_names):
        self.watchlist = _dedupe(watchlist_names)

    def set_aliases(self, aliases):
        """aliases: {alias_lower: canonical_item_name}. Precompile word-boundary
        patterns so expansion is a quick scan per line."""
        self.aliases = dict(aliases)
        self._alias_pats = [
            (re.compile(r'\b' + re.escape(k) + r'\b', re.I), v)
            for k, v in self.aliases.items()
        ]

    def _expand_aliases(self, seg):
        """Append the canonical item name for any alias found in the buy text,
        so 'WTB CoF' -> '... Cloak of Flames' and the matcher catches it."""
        if not self._alias_pats:
            return seg
        extra = [canon for pat, canon in self._alias_pats if pat.search(seg)]
        return (seg + ' ' + ' '.join(extra)) if extra else seg

    def _best(self, seg, candidates):
        """Best (tier, score, item) of seg vs candidates, or None. Top-1: a post
        is usually one item, and best-only suppresses a weak MAYBE when a real
        HIGH is on the same line."""
        if not seg or not candidates:
            return None
        seg = self._expand_aliases(seg)
        best = None  # (score, tier, item)
        for name in candidates:
            tot, cov, bs, icov, icovx, _hits = score(seg, name, self.idf)
            conf = confidence(tot, cov, bs, icov, icovx)
            if conf and (best is None or tot > best[0]):
                best = (tot, conf, name)
        return (best[1], round(best[0], 1), best[2]) if best else None

    def match_line(self, msg):
        """Return a list of leads for a line: each is (kind, tier, score, item).
          kind 'SELL' = poster WTBs an item I OWN  (I can sell to them)
          kind 'BUY'  = poster WTSs an item on my WATCHLIST  (I can buy from them)
        Usually 0 or 1; a mixed line can yield both."""
        out = []
        sell = self._best(buy_segments(msg), self.candidates)   # their WTB vs my inv
        if sell:
            out.append(('SELL',) + sell)
        buy = self._best(sell_segments(msg), self.watchlist)    # their WTS vs my wishlist
        if buy:
            out.append(('BUY',) + buy)
        return out


# ---------------------------------------------------------------------------
# LogTailer: follow a growing file from the end, like `tail -f`
# ---------------------------------------------------------------------------

class LogTailer(threading.Thread):
    """Daemon thread that emits new full lines appended to `path`.

    Starts at end-of-file (no history replay). Handles the file not existing
    yet (waits for it), partial last lines (buffers until the newline lands),
    and truncation/replacement (re-opens from 0 if the file shrinks)."""

    def __init__(self, path, on_line, poll=1.0):
        super().__init__(daemon=True)
        self.path = path
        self.on_line = on_line
        self.poll = poll
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _open_at_end(self):
        f = open(self.path, 'r', encoding='latin-1', errors='ignore')
        f.seek(0, os.SEEK_END)
        return f

    def run(self):
        # Wait for the log to exist (player may not have toggled /log on yet).
        while not self._stop.is_set() and not os.path.isfile(self.path):
            if self._stop.wait(self.poll):
                return
        try:
            f = self._open_at_end()
        except OSError:
            return
        try:
            while not self._stop.is_set():
                pos = f.tell()
                line = f.readline()
                if line and line.endswith(('\n', '\r')):
                    self.on_line(line)
                    continue
                if line:                      # partial line — rewind, wait for more
                    f.seek(pos)
                # EOF: detect truncation/rotation, then sleep
                try:
                    if os.path.getsize(self.path) < pos:
                        f.close()
                        f = open(self.path, 'r', encoding='latin-1', errors='ignore')
                except OSError:
                    pass
                if self._stop.wait(self.poll):
                    break
        finally:
            f.close()


# ---------------------------------------------------------------------------
# LogMonitor: ties tailer + matcher + dedup, fires on_alert
# ---------------------------------------------------------------------------

class LogMonitor:
    """Coordinator. Construct with the log path, your own character name (to skip
    your own auctions), a Matcher, and an
    on_alert(kind, tier, ts, speaker, item, line) callback ('SELL'|'BUY' lead).
    on_alert fires from the tailer thread — marshal to your UI thread.

    Dedup: the same (kind, speaker, item) won't re-alert within `cooldown`
    seconds, so a trader re-posting every 30s alerts once."""

    def __init__(self, log_path, self_char, matcher, on_alert,
                 cooldown=300.0, poll=1.0):
        self.log_path = log_path
        self.self_char = (self_char or '').lower()
        self.matcher = matcher
        self.on_alert = on_alert
        self.cooldown = cooldown
        self._seen = {}            # (speaker_lc, item) -> monotonic time
        self._tailer = LogTailer(log_path, self.handle_line, poll=poll)

    @staticmethod
    def guess_log_path(char_ini_path, server):
        """Best-guess eqlog path from the chosen character INI
        (<EQ>\\<Char>_<server>.ini) -> <EQ>\\Logs\\eqlog_<Char>_<server>.txt.
        EQ lowercases the server in the log filename. Returns None if it can't
        be derived."""
        if not char_ini_path:
            return None
        eq_dir = os.path.dirname(char_ini_path)
        base = os.path.basename(char_ini_path)
        m = re.match(r'(.+?)_([^_]+)\.ini$', base, re.I)
        if not m:
            return None
        char = m.group(1)
        srv = (server or m.group(2)).lower()
        return os.path.join(eq_dir, 'Logs', f'eqlog_{char}_{srv}.txt')

    def handle_line(self, raw):
        parsed = parse_auction_line(raw)
        if not parsed:
            return
        ts, speaker, msg = parsed
        if self.self_char and speaker.lower() == self.self_char:
            return
        now = time.monotonic()
        for kind, tier, _scr, item in self.matcher.match_line(msg):
            key = (kind, speaker.lower(), item)
            last = self._seen.get(key)
            if last is not None and now - last < self.cooldown:
                continue
            self._seen[key] = now
            self.on_alert(kind, tier, ts, speaker, item, msg)

    def start(self):
        self._tailer.start()

    def stop(self):
        self._tailer.stop()
