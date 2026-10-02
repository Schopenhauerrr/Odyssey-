"""Offline tests: run with  python -m pytest tests/  (or python tests/test_scanner.py)"""
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scanner as s  # noqa: E402

CFG = s.load_config()
for k in ("bot_token", "chat_id"):
    CFG["telegram"][k] = ""
REFS = s.load_refs()

# Two different alert layouts (classifieds-style table and auction-style cards)
CLASSIFIEDS_HTML = """
<html><body><table>
<tr><td><a href="https://click.example.dk/r?u=1"><img alt="Kay Bojesen abe, mellem, teak" src="x.jpg"></a></td>
    <td><a href="https://click.example.dk/r?u=1">Kay Bojesen abe, mellem, teak</a><br>Aarhus C<br><b>60 kr.</b></td></tr>
<tr><td><a href="https://click.example.dk/r?u=2">PH5 lampe, Louis Poulsen, original</a><p>1.450 kr.</p></td></tr>
<tr><td><a href="https://click.example.dk/r?u=3">Wegner Y-stol kopi</a><p>400 kr.</p></td></tr>
<tr><td><a href="https://click.example.dk/r?u=4">Billy reol</a><p>200 kr.</p></td></tr>
<tr><td><a href="https://click.example.dk/r?u=5">PH 5 pendel, hvid</a><p>kr. 3.900</p></td></tr>
</table><a href="https://example.dk/unsubscribe">Afmeld</a></body></html>"""

AUCTION_HTML = """
<div class="lot"><a href="https://auction.example.com/lot/77">Holmegaard gulvvase, Otto Brauer, grøn, 56 cm</a>
  <span>Aktuelt bud: 300 DKK</span></div>
<div class="lot"><a href="https://auction.example.com/lot/78">Georg Jensen Konge sølv middagsgaffel</a>
  <span>Current bid € 20</span></div>"""

EBAY_ITEMS = [
    {"title": "Arne Jacobsen Myren Ant Chair Fritz Hansen", "price": {"value": "45.00", "currency": "EUR"},
     "itemWebUrl": "https://www.ebay.de/itm/1", "buyingOptions": ["FIXED_PRICE"],
     "shippingOptions": [{"shippingCost": {"value": "15.00", "currency": "EUR"}}],
     "itemLocation": {"postalCode": "8000", "country": "DK"}},
    {"title": "Le Klint 101 pendant", "price": {"value": "120.00", "currency": "EUR"},
     "itemWebUrl": "https://www.ebay.de/itm/2", "buyingOptions": ["AUCTION"]},
]

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Royal Copenhagen Flora Danica fad</title><link>https://feed.example/1</link>
<description>Pris: 600 kr</description></item>
<item><title>Tom pris</title><link>https://feed.example/2</link><description>ingen pris</description></item>
</channel></rss>"""


def test_prices():
    assert s.find_price("1.450 kr.") == (1450, "DKK")
    assert s.find_price("kr. 3.900") == (3900, "DKK")
    assert s.find_price("1 250,-") == (1250, "DKK")
    assert s.find_price("€ 20") == (20, "EUR")
    assert s.find_price("DKK 1,250.00") == (1250, "DKK")
    assert s.find_price("12,50 kr") == (12, "DKK")
    assert s.find_price("450 kr", "SEK") == (450, "SEK")
    assert s.find_price("ingen pris") is None


def test_normalise_and_match():
    assert s.norm("Kähler Gulvvase SØLV") == "kahler gulvvase solv"
    ref = next(r for r in REFS if r.ref_id == "kb_monkey_m")
    assert ref.matches(s.norm("Kay Bojesen abe teak"))
    assert not ref.matches(s.norm("Kay Bojesen abe mini"))
    assert not ref.matches(s.norm("Kay Bojesen abent hus"))


def test_classifieds_email():
    ls = s.parse_alert_html(CLASSIFIEDS_HTML, "dba.dk")
    titles = {l.title: l for l in ls}
    assert len(ls) == 5, [l.title for l in ls]
    assert titles["Kay Bojesen abe, mellem, teak"].price == 60
    verdicts = {l.title: s.evaluate(l, REFS, CFG) for l in ls}
    assert verdicts["Kay Bojesen abe, mellem, teak"].is_deal
    assert verdicts["PH5 lampe, Louis Poulsen, original"].is_deal
    assert not verdicts["Wegner Y-stol kopi"].is_deal            # copy
    assert verdicts["Billy reol"].ref is None                     # unrelated
    assert not verdicts["PH 5 pendel, hvid"].is_deal              # fair price, not a deal


def test_auction_email():
    ls = s.parse_alert_html(AUCTION_HTML, "lauritz.com")
    assert len(ls) == 2
    vase = next(l for l in ls if "gulvvase" in l.title)
    assert vase.is_auction and vase.price == 300
    assert s.evaluate(vase, REFS, CFG).is_deal
    fork = next(l for l in ls if "Konge" in l.title)
    assert fork.currency == "EUR"
    v = s.evaluate(fork, REFS, CFG)
    assert v.ref.ref_id == "gj_cutlery" and abs(v.price_dkk - 149.2) < 0.1
    assert v.fin["verdict"] == "maybe"   # small profit after tax


def test_full_mime_message():
    msg = MIMEMultipart("alternative")
    msg["From"] = "DBA <noreply@notifications.dba.dk>"
    msg["Subject"] = "Nye annoncer"
    msg.attach(MIMEText("plain fallback", "plain", "utf-8"))
    msg.attach(MIMEText(CLASSIFIEDS_HTML, "html", "utf-8"))
    ls = s.parse_message(msg)
    assert len(ls) == 5 and ls[0].site == "dba.dk"


def test_plain_text_alert():
    body = "Nye annoncer\n\nKay Bojesen elefant\n150 kr.\nhttps://x.dk/1\n\nAndet\nhttps://x.dk/2\n"
    ls = s.parse_alert_text(body, "x.dk")
    assert len(ls) == 1 and ls[0].title == "Kay Bojesen elefant"


def test_ebay_and_rss():
    ls = s.parse_ebay_items(EBAY_ITEMS, "EBAY_DE")
    assert ls[0].extra_cost_dkk == 15 and ls[0].location == "8000 DK"
    ls[0].extra_cost_dkk *= 7.46
    assert s.evaluate(ls[0], REFS, CFG).ref.ref_id == "aj_ant"
    assert ls[1].is_auction
    rs = s.parse_feed(RSS, "feed")
    assert len(rs) == 1 and s.evaluate(rs[0], REFS, CFG).is_deal


def test_price_cap():
    l = s.Listing("m", "m", "PH Artichoke koglen kobber", 9000, "DKK", "")
    v = s.evaluate(l, REFS, CFG)
    assert not v.is_deal and "over max price" in v.reasons[0]
    l.price = 6500
    assert s.evaluate(l, REFS, CFG).is_deal


def test_store_and_export(monkeypatch=None):
    import json, tempfile
    tmp = Path(tempfile.mkdtemp())
    st = s.Store(tmp / "state.json")
    l = s.Listing("m", "dba.dk", "Kay Bojesen abe", 60, "DKK", "u")
    assert st.is_new(l.key())
    st.remember(s.evaluate(l, REFS, CFG))
    st.remember(s.evaluate(s.Listing("m", "dba.dk", "Billy reol", 50, "DKK", "u2"), REFS, CFG))
    st.save()
    st2 = s.Store(tmp / "state.json")
    assert not st2.is_new(l.key())
    assert len(st2.state["matches"]) == 1 and st2.state["matches"][0]["is_deal"]
    old = s.APP_DATA
    s.APP_DATA = tmp / "app"
    try:
        s.export_app_data(REFS, st2)
        deals = json.loads((tmp / "app" / "deals.json").read_text())
        refs = json.loads((tmp / "app" / "refs.json").read_text())
    finally:
        s.APP_DATA = old
    assert deals["items"][0]["ref_id"] == "kb_monkey_m" and len(refs) == len(REFS)


def test_alert_text():
    v = s.evaluate(s.Listing("m", "dba.dk", "Holmegaard gulvvase grøn", 300, "DKK", "https://x"), REFS, CFG)
    msg = s.format_alert(v, "https://me.github.io/odyssey")
    assert "profit after tax ~708" in msg and "#deal-" in msg and "rough guess" in msg


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
