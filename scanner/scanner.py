#!/usr/bin/env python3
"""
Odyssey scanner.

Watches several second-hand sources for new listings, matches them against
reference_prices.csv, works out the full profit picture (fees, costs, tax,
days to sell) and sends a Telegram alert for real deals. It also writes
docs/data/*.json, which the Odyssey app reads.

Sources (all within the sites' normal use):
  * email_alerts : saved-search alert e-mails (DBA, Lauritz, Catawiki, Trendsales,
                   Auctionet, Tradera ...) read over IMAP
  * ebay         : eBay's official Browse API
  * rss          : any RSS/Atom feed

Commands:
  python scanner/scanner.py once                 # one pass (what GitHub Actions runs)
  python scanner/scanner.py run                  # loop forever (if you host it yourself)
  python scanner/scanner.py check "title" PRICE  # evaluate one listing
  python scanner/scanner.py parse-email FILE     # what the parser extracts from a saved alert
  python scanner/scanner.py export               # just rebuild docs/data/refs.json
  python scanner/scanner.py test-telegram
"""
from __future__ import annotations

import csv
import email
import hashlib
import imaplib
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

from finance import compute_deal

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = HERE / "config.yaml"
REFS_PATH = HERE / "reference_prices.csv"
APP_DATA = ROOT / "docs" / "data"
SETTINGS_PATH = APP_DATA / "settings.json"
STATE_PATH = ROOT / "data" / "state.json"

DEALS_KEEP_DAYS = 14
DEALS_KEEP_MAX = 300

# Words that almost always mean "not the real thing" or "damaged".
GLOBAL_EXCLUDE = [
    "kopi", "replika", "replica", "copy", "reproduktion", "reproduction",
    "inspireret", "inspired", "i stil med", "style", "a la", "look alike",
    "lignende", "defekt", "skar", "beskadiget", "damaged", "broken", "limet",
    "glued", "reservedel", "spare part", "plakat", "poster", "bog", "book",
]
AUCTION_WORDS = ["bud", "bid", "auktion", "auction", "vurdering", "estimate", "lot "]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def norm(text: str) -> str:
    """Lowercase, fold Danish/German letters, strip accents, squash spaces."""
    t = text.lower()
    for a, b in (("æ", "ae"), ("ø", "o"), ("å", "a"), ("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip()


def has_word(text_norm: str, phrase: str) -> bool:
    phrase = norm(phrase)
    if not phrase:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text_norm) is not None


_NUM = r"(\d{1,3}(?:[.,\s ]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)"
_CUR = r"(kr\.?|dkk|eur|€|sek|nok)"
PRICE_AFTER = re.compile(_NUM + r"\s*(?:" + _CUR + r"|,-)", re.I)
PRICE_BEFORE = re.compile(_CUR + r"\s*" + _NUM, re.I)


def _to_number(int_part: str) -> float:
    return float(re.sub(r"[.,\s ]", "", int_part))


def find_price(text: str, default_currency: str = "DKK") -> tuple[float, str] | None:
    """Return the first price in text as (amount, currency)."""
    best = None
    for rx, num_g, cur_g in ((PRICE_AFTER, 1, 3), (PRICE_BEFORE, 2, 1)):
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            cur = (m.group(cur_g) or "").lower().rstrip(".")
            cur = {"kr": default_currency, "dkk": "DKK", "€": "EUR", "eur": "EUR",
                   "sek": "SEK", "nok": "NOK", "": default_currency}[cur]
            best = (m.start(), _to_number(m.group(num_g)), cur)
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Listing:
    source: str
    site: str
    title: str
    price: float
    currency: str
    url: str
    extra_cost_dkk: float = 0.0     # shipping etc. if known
    is_auction: bool = False
    location: str = ""

    def key(self) -> str:
        raw = f"{self.site}|{norm(self.title)}|{round(self.price)}"
        return hashlib.sha1(raw.encode()).hexdigest()


@dataclass
class Ref:
    ref_id: str
    name: str
    include: list[list[str]]
    exclude: list[str]
    search_query: str
    resale_dkk: float
    days_to_sell: float
    verified: bool
    notes: str = ""

    def matches(self, text_norm: str) -> bool:
        if any(has_word(text_norm, w) for w in self.exclude):
            return False
        return all(any(has_word(text_norm, alt) for alt in group) for group in self.include)


@dataclass
class Verdict:
    listing: Listing
    ref: Ref | None
    price_dkk: float = 0.0
    fin: dict = field(default_factory=dict)
    is_deal: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def net_profit(self) -> float:
        return self.fin.get("after_tax", 0.0)


def load_refs(path: Path = REFS_PATH) -> list[Ref]:
    refs = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("ref_id") or not row.get("resale_dkk"):
                continue
            refs.append(Ref(
                ref_id=row["ref_id"].strip(),
                name=row["name"].strip(),
                include=[[a.strip() for a in g.split("|") if a.strip()]
                         for g in row["include"].split(";") if g.strip()],
                exclude=[w.strip() for w in (row.get("exclude") or "").split("|") if w.strip()],
                search_query=(row.get("search_query") or row["name"]).strip(),
                resale_dkk=float(row["resale_dkk"]),
                days_to_sell=float(row.get("days_to_sell") or 21),
                verified=(row.get("verified") or "").strip().lower() in ("yes", "y", "true", "1"),
                notes=(row.get("notes") or "").strip(),
            ))
    return refs


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["settings"] = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    # aliases used by the source fetchers
    cfg["currency_to_dkk"] = cfg["settings"]["currency_to_dkk"]
    cfg["rules"] = {"max_price_dkk": cfg["settings"]["max_price_dkk"]}
    env = os.environ.get
    cfg["app_url"] = (env("APP_URL") or cfg.get("app_url") or "").rstrip("/")
    cfg["telegram"]["bot_token"] = env("TELEGRAM_BOT_TOKEN") or cfg["telegram"].get("bot_token", "")
    cfg["telegram"]["chat_id"] = env("TELEGRAM_CHAT_ID") or cfg["telegram"].get("chat_id", "")
    s = cfg["sources"]
    s["email_alerts"]["username"] = env("IMAP_USERNAME") or s["email_alerts"].get("username", "")
    s["email_alerts"]["password"] = env("IMAP_PASSWORD") or s["email_alerts"].get("password", "")
    s["ebay"]["client_id"] = env("EBAY_CLIENT_ID") or s["ebay"].get("client_id", "")
    s["ebay"]["client_secret"] = env("EBAY_CLIENT_SECRET") or s["ebay"].get("client_secret", "")
    # a source turns on automatically once its secret is present (handy on GitHub)
    if env("IMAP_PASSWORD"):
        s["email_alerts"]["enabled"] = True
    if env("EBAY_CLIENT_ID"):
        s["ebay"]["enabled"] = True
    return cfg


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def match_ref(title: str, refs: list[Ref]) -> Ref | None:
    text = norm(title)
    if any(has_word(text, w) for w in GLOBAL_EXCLUDE):
        return None
    candidates = [r for r in refs if r.matches(text)]
    # most specific match first (more keyword groups), then highest value
    return max(candidates, key=lambda r: (len(r.include), r.resale_dkk)) if candidates else None


def evaluate(listing: Listing, refs: list[Ref], cfg: dict) -> Verdict:
    st = cfg["settings"]
    v = Verdict(listing=listing, ref=None)
    if any(has_word(norm(listing.title), w) for w in GLOBAL_EXCLUDE):
        v.reasons.append("excluded word (copy/damaged/etc.)")
        return v
    v.ref = ref = match_ref(listing.title, refs)
    if not ref:
        v.reasons.append("no reference match")
        return v
    rate = st["currency_to_dkk"].get(listing.currency.upper())
    if rate is None:
        v.reasons.append(f"unknown currency {listing.currency}")
        return v
    v.price_dkk = listing.price * rate
    if v.price_dkk <= 0:
        v.reasons.append("no price / free")
        return v
    if v.price_dkk > st["max_price_dkk"]:
        v.reasons.append(f"over max price ({v.price_dkk:,.0f} > {st['max_price_dkk']:,})")
        return v

    v.fin = compute_deal(ref.resale_dkk, ref.days_to_sell, v.price_dkk, st,
                         extra_cost=listing.extra_cost_dkk)
    if v.fin["verdict"] != "buy":
        if v.price_dkk > v.fin["max_buy"]:
            v.reasons.append(f"price above max buy ({v.price_dkk:,.0f} > {v.fin['max_buy']:,.0f})")
        if v.fin["after_tax"] < st["min_profit_after_tax"]:
            v.reasons.append(f"profit after tax {v.fin['after_tax']:,.0f} < {st['min_profit_after_tax']}")
    v.is_deal = v.fin["verdict"] == "buy"
    return v


def short_key(listing: Listing) -> str:
    return listing.key()[:12]


def format_alert(v: Verdict, app_url: str = "") -> str:
    l, r, f = v.listing, v.ref, v.fin
    lines = [
        f"🧭 {r.name}",
        f"Asking {v.price_dkk:,.0f} DKK" + (f" ({l.price:,.0f} {l.currency})" if l.currency != "DKK" else "")
        + f" · {l.site}",
        f"Sells for ~{f['sell']:,.0f} → profit after tax ~{f['after_tax']:,.0f} DKK ({f['margin']:.0%})",
        f"Max buy {f['max_buy']:,.0f} · sells in ~{f['days']:.0f} days",
    ]
    if not r.verified:
        lines.append("⚠️ Reference price is a rough guess")
    if l.is_auction:
        lines.append("⏳ Auction: current bid, final price will be higher")
    if l.location:
        lines.append(f"📍 {l.location}")
    lines.append(f"“{l.title[:160]}”")
    if l.url:
        lines.append(l.url)
    if app_url:
        lines.append(f"Full breakdown: {app_url}/#deal-{short_key(l)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# State (a JSON file, so it can live in the GitHub repo between runs)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.state = {}
        self.state.setdefault("seen", {})      # listing key -> first seen (iso)
        self.state.setdefault("mails", {})     # message-id -> processed (iso)
        self.state.setdefault("runs", {})      # source -> unix time of last run
        self.state.setdefault("matches", [])   # recent matched listings for the app

    def is_new(self, key: str) -> bool:
        return key not in self.state["seen"]

    def remember(self, v: Verdict):
        l = v.listing
        self.state["seen"][l.key()] = now_iso()
        if v.ref and v.fin:  # matched a reference item and was priced → show in the app
            self.state["matches"].insert(0, {
                "id": short_key(l), "first_seen": now_iso(), "source": l.source, "site": l.site,
                "title": l.title, "price": l.price, "currency": l.currency,
                "price_dkk": round(v.price_dkk, 2), "url": l.url, "ref_id": v.ref.ref_id,
                "is_auction": l.is_auction, "extra_cost_dkk": round(l.extra_cost_dkk, 2),
                "location": l.location, "is_deal": v.is_deal,
            })

    def mail_done(self, msg_id: str) -> bool:
        return msg_id in self.state["mails"]

    def mark_mail(self, msg_id: str):
        self.state["mails"][msg_id] = now_iso()

    def due(self, source: str, every_minutes: float) -> bool:
        last = self.state["runs"].get(source)
        return last is None or time.time() - last >= every_minutes * 60 - 30

    def ran(self, source: str):
        self.state["runs"][source] = time.time()

    def prune(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        for k in ("seen", "mails"):
            self.state[k] = {x: t for x, t in self.state[k].items() if t >= cutoff}
        deal_cut = (datetime.now(timezone.utc) - timedelta(days=DEALS_KEEP_DAYS)).isoformat()
        self.state["matches"] = [m for m in self.state["matches"] if m["first_seen"] >= deal_cut][:DEALS_KEEP_MAX]

    def save(self):
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def export_app_data(refs: list[Ref], store: Store | None = None):
    """Write the files the app reads: refs.json always, deals.json when a store is given."""
    APP_DATA.mkdir(parents=True, exist_ok=True)
    (APP_DATA / "refs.json").write_text(json.dumps([
        {"id": r.ref_id, "name": r.name, "resale_dkk": r.resale_dkk, "days_to_sell": r.days_to_sell,
         "verified": r.verified, "keywords": [g for g in r.include], "exclude": r.exclude, "notes": r.notes}
        for r in refs], ensure_ascii=False, indent=1), encoding="utf-8")
    if store is not None:
        (APP_DATA / "deals.json").write_text(json.dumps({
            "updated": now_iso(), "sample": False, "items": store.state["matches"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Notifier
# --------------------------------------------------------------------------- #
def notify(cfg: dict, text: str):
    token, chat = cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"]
    if not token or not chat:
        print("\n[ALERT — Telegram not configured]\n" + text + "\n")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text, "disable_web_page_preview": False},
                          timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:  # never let a failed alert kill the loop
        print(f"[telegram error] {e}\n{text}")
        return False


# --------------------------------------------------------------------------- #
# Source 1: saved-search alert e-mails
# --------------------------------------------------------------------------- #
SITE_CURRENCY = {"tradera": "SEK", "blocket": "SEK", "finn.no": "NOK"}
SKIP_LINK_TEXT = re.compile(
    r"^(se (annonce|mere|alle)|vis|view|open|abn|unsubscribe|afmeld|log ind|login|"
    r"indstillinger|settings|privacy|help|hjaelp|app store|google play|facebook|instagram)",
    re.I)


def _site_from_sender(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender)
    dom = (m.group(1) if m else sender).lower()
    parts = dom.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else dom


def parse_alert_html(html: str, site: str, currency: str = "DKK") -> list[Listing]:
    """Extract (title, price, link) blocks from an alert e-mail's HTML.

    Alert mails differ per site, so instead of per-site templates this looks
    for small blocks that contain both a link and a price."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["style", "script", "head"]):
        t.decompose()
    blocks: dict[int, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        node, container = a, None
        for _ in range(7):
            node = node.parent
            if node is None:
                break
            txt = node.get_text(" ", strip=True)
            if len(txt) > 500:
                break
            if find_price(txt, currency):
                container = node
                break
        if container is None:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            img = a.find("img", alt=True)
            title = img["alt"].strip() if img else ""
        b = blocks.setdefault(id(container), {"node": container, "titles": [], "href": href})
        if title and not SKIP_LINK_TEXT.search(norm(title)) and not find_price(title, currency):
            b["titles"].append(title)
            if len(title) >= max((len(t) for t in b["titles"]), default=0):
                b["href"] = href

    # Keep only innermost blocks: an outer container (a table row holding an
    # image link, or the whole mail body) that wraps another block is noise.
    nodes = [b["node"] for b in blocks.values()]
    inner = [b for b in blocks.values()
             if not any(n is not b["node"] and any(p is b["node"] for p in n.parents) for n in nodes)]

    out = []
    for b in inner:
        text = b["node"].get_text(" ", strip=True)
        price = find_price(text, currency)
        title = max(b["titles"], key=len) if b["titles"] else ""
        if not title:  # fall back to the block text minus the price
            title = PRICE_AFTER.sub("", text)[:150].strip()
        if not price or len(title) < 4:
            continue
        out.append(Listing(source="email", site=site, title=title, price=price[0],
                           currency=price[1], url=b["href"],
                           is_auction=any(has_word(norm(text), w) for w in AUCTION_WORDS)))
    return out


def parse_alert_text(body: str, site: str, currency: str = "DKK") -> list[Listing]:
    """Fallback for plain-text alerts: blocks separated by blank lines."""
    out = []
    for block in re.split(r"\n\s*\n", body):
        url = re.search(r"https?://\S+", block)
        price = find_price(block, currency)
        if not (url and price):
            continue
        lines = [ln.strip() for ln in block.splitlines()
                 if ln.strip() and "http" not in ln and not find_price(ln, currency)]
        if lines:
            out.append(Listing("email", site, lines[0], price[0], price[1], url.group(0).rstrip(">)")))
    return out


def parse_message(msg: email.message.Message) -> list[Listing]:
    sender = str(make_header(decode_header(msg.get("From", ""))))
    site = _site_from_sender(sender)
    currency = next((c for k, c in SITE_CURRENCY.items() if k in site), "DKK")
    html, text = None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/html" and html is None:
            html = decoded
        elif ctype == "text/plain" and text is None:
            text = decoded
    listings = parse_alert_html(html, site, currency) if html else []
    if not listings and text:
        listings = parse_alert_text(text, site, currency)
    return listings


def fetch_email_alerts(cfg: dict, store: Store) -> list[Listing]:
    c = cfg["sources"]["email_alerts"]
    if not (c["username"] and c["password"]):
        print("[email] username/password not set, skipping")
        return []
    since = (datetime.now() - timedelta(days=c.get("lookback_days", 2))).strftime("%d-%b-%Y")
    found = []
    with imaplib.IMAP4_SSL(c["imap_host"]) as imap:
        imap.login(c["username"], c["password"])
        imap.select(f'"{c["folder"]}"', readonly=True)
        _, data = imap.search(None, "SINCE", since)
        for num in data[0].split():
            _, hdr = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])")
            h = email.message_from_bytes(hdr[0][1])
            msg_id = h.get("Message-ID", f"num-{num.decode()}")
            sender = (h.get("From") or "").lower()
            if store.mail_done(msg_id) or not any(s.lower() in sender for s in c["senders"]):
                continue
            _, full = imap.fetch(num, "(BODY.PEEK[])")
            found += parse_message(email.message_from_bytes(full[0][1]))
            store.mark_mail(msg_id)
    print(f"[email] {len(found)} listings extracted")
    return found


# --------------------------------------------------------------------------- #
# Source 2: eBay Browse API (official)
# --------------------------------------------------------------------------- #
_ebay_token: dict = {}


def _ebay_auth(c: dict) -> str:
    if _ebay_token.get("exp", 0) > time.time() + 60:
        return _ebay_token["token"]
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
                      auth=(c["client_id"], c["client_secret"]),
                      data={"grant_type": "client_credentials",
                            "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    _ebay_token.update(token=j["access_token"], exp=time.time() + j["expires_in"])
    return _ebay_token["token"]


def parse_ebay_items(items: list[dict], marketplace: str) -> list[Listing]:
    out = []
    for it in items:
        price = it.get("price") or {}
        if "value" not in price:
            continue
        ship = 0.0
        for opt in it.get("shippingOptions") or []:
            cost = (opt.get("shippingCost") or {})
            if cost.get("value") and cost.get("currency") == price.get("currency"):
                ship = float(cost["value"])
                break
        loc = it.get("itemLocation") or {}
        out.append(Listing(
            source="ebay", site=marketplace, title=it.get("title", ""),
            price=float(price["value"]), currency=price.get("currency", "EUR"),
            url=it.get("itemWebUrl", ""), extra_cost_dkk=ship,  # converted below
            is_auction="AUCTION" in (it.get("buyingOptions") or []),
            location=" ".join(x for x in (loc.get("postalCode"), loc.get("country")) if x)))
    return out


def fetch_ebay(cfg: dict, refs: list[Ref]) -> list[Listing]:
    c = cfg["sources"]["ebay"]
    if not (c["client_id"] and c["client_secret"]):
        print("[ebay] API keys not set, skipping")
        return []
    fx = cfg["currency_to_dkk"]
    max_eur = cfg["rules"]["max_price_dkk"] / fx["EUR"]
    token, found = _ebay_auth(c), []
    for mkt in c["marketplaces"]:
        for country in c["item_location_countries"]:
            for ref in refs:
                params = {"q": ref.search_query, "sort": "newlyListed",
                          "limit": c.get("results_per_query", 50),
                          "filter": f"price:[..{max_eur:.0f}],priceCurrency:EUR,itemLocationCountry:{country}"}
                try:
                    r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
                                     params=params, timeout=20,
                                     headers={"Authorization": f"Bearer {token}",
                                              "X-EBAY-C-MARKETPLACE-ID": mkt})
                    r.raise_for_status()
                except Exception as e:
                    print(f"[ebay] {ref.ref_id}/{country}: {e}")
                    continue
                for l in parse_ebay_items(r.json().get("itemSummaries", []), mkt):
                    l.extra_cost_dkk *= fx.get(l.currency, 1.0)
                    found.append(l)
                time.sleep(0.3)
    print(f"[ebay] {len(found)} listings fetched")
    return found


# --------------------------------------------------------------------------- #
# Source 3: RSS / Atom
# --------------------------------------------------------------------------- #
def parse_feed(xml_text: str, site: str) -> list[Listing]:
    root = ET.fromstring(xml_text)
    out = []
    items = root.iter("item") if root.find(".//item") is not None else root.iter("{http://www.w3.org/2005/Atom}entry")
    for it in items:
        def g(tag):
            el = it.find(tag)
            if el is None:
                el = it.find("{http://www.w3.org/2005/Atom}" + tag)
            if el is None:
                return ""
            return el.get("href") if tag == "link" and el.get("href") else (el.text or "")
        title = g("title").strip()
        desc = BeautifulSoup(g("description") or g("summary"), "html.parser").get_text(" ")
        price = find_price(f"{title} {desc}")
        if title and price:
            out.append(Listing("rss", site, title, price[0], price[1], g("link").strip()))
    return out


def fetch_rss(cfg: dict) -> list[Listing]:
    found = []
    for feed in cfg["sources"]["rss"].get("feeds") or []:
        try:
            r = requests.get(feed["url"], timeout=20, headers={"User-Agent": "personal-deal-scanner"})
            r.raise_for_status()
            found += parse_feed(r.text, feed.get("name", feed["url"]))
        except Exception as e:
            print(f"[rss] {feed.get('name')}: {e}")
    print(f"[rss] {len(found)} listings fetched")
    return found


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def scan_once(cfg: dict, refs: list[Ref], store: Store, force: bool = False) -> int:
    s, listings = cfg["sources"], []
    jobs = [
        ("email_alerts", lambda: fetch_email_alerts(cfg, store)),
        ("ebay", lambda: fetch_ebay(cfg, refs)),
        ("rss", lambda: fetch_rss(cfg)),
    ]
    for name, fn in jobs:
        sc = s.get(name, {})
        every = sc.get("every_minutes", cfg["scan_interval_minutes"])
        if not sc.get("enabled") or not (force or store.due(name, every)):
            continue
        try:
            listings += fn()
        except Exception as e:
            print(f"[{name}] failed: {e}")
        store.ran(name)

    # one-time confirmation that Telegram is set up correctly
    tg = cfg["telegram"]
    if tg["bot_token"] and tg["chat_id"] and not store.state.get("telegram_ok"):
        if notify(cfg, "🧭 Odyssey is connected. Deals will arrive here.\n" + (cfg["app_url"] or "")):
            store.state["telegram_ok"] = now_iso()

    deals = 0
    for l in listings:
        if not store.is_new(l.key()):
            continue
        v = evaluate(l, refs, cfg)
        store.remember(v)
        if v.is_deal:
            deals += 1
            notify(cfg, format_alert(v, cfg["app_url"]))
    store.save()
    export_app_data(refs, store)
    print(f"[{datetime.now():%H:%M}] {len(listings)} listings checked, {deals} deal(s)")
    return deals


def main(argv: list[str]):
    cmd = argv[1] if len(argv) > 1 else "once"
    cfg, refs = load_config(), load_refs()

    if cmd == "check":
        l = Listing("manual", "manual", argv[2], float(argv[3]), argv[4] if len(argv) > 4 else "DKK", "")
        v = evaluate(l, refs, cfg)
        print(format_alert(v) if v.is_deal else f"No deal: {'; '.join(v.reasons)}"
              + (f"  [matched {v.ref.ref_id}]" if v.ref else ""))
    elif cmd == "parse-email":
        raw = Path(argv[2]).read_bytes()
        if argv[2].lower().endswith((".html", ".htm")):
            listings = parse_alert_html(raw.decode("utf-8", "replace"), argv[3] if len(argv) > 3 else "unknown")
        else:
            listings = parse_message(email.message_from_bytes(raw))
        for l in listings:
            v = evaluate(l, refs, cfg)
            tag = "DEAL" if v.is_deal else ("match" if v.ref else "-")
            print(f"[{tag:5}] {l.price:>8,.0f} {l.currency}  {l.title[:70]}"
                  + (f"  -> {v.ref.ref_id}" if v.ref else "") + (f"  ({'; '.join(v.reasons)})" if v.reasons else ""))
        print(f"{len(listings)} listings extracted")
    elif cmd == "export":
        export_app_data(refs)
        print("docs/data/refs.json written")
    elif cmd == "test-telegram":
        notify(cfg, "🧭 Odyssey is connected. Deals will arrive here.")
    elif cmd == "once":
        scan_once(cfg, refs, Store())
    elif cmd == "run":
        store = Store()
        while True:
            try:
                scan_once(cfg, load_refs(), store)
            except Exception as e:
                print(f"[scan] {e}")
            time.sleep(cfg["scan_interval_minutes"] * 60)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
