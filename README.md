# Odyssey

Your own app for flipping Danish design. It finds underpriced listings, shows the
full profit picture (fees, costs, tax, days to sell), tracks your stock and learns
from your sales. It runs free: GitHub runs the scanner every 15 minutes and hosts
the app, and Telegram pings you when a real deal appears.

```
saved-search e-mails (DBA, Lauritz, Catawiki…) ─┐
eBay official API ──────────────────────────────┼─► scanner (GitHub, every 15 min) ─► Telegram alert
RSS feeds ──────────────────────────────────────┘          │
                                                             └─► docs/data/deals.json ─► Odyssey app on your phone
```

## Setup, all from your phone's browser (about 30 minutes)

**1. GitHub account and empty repository**
- Sign up at github.com (free).
- Tap **+ → New repository**, name it `odyssey`, set it to **Public**, leave
  everything else unticked, and tap **Create**.
- Claude can push the code into it for you. Otherwise, upload this folder.

**2. Turn on the app's web address**
- Repository → **Settings → Pages** → Source: *Deploy from a branch* → Branch
  `main`, folder `/docs` → **Save**.
- After a minute your app lives at `https://schopenhauerrr.github.io/Odyssey-/`.

**3. Telegram bot**
- In Telegram, message **@BotFather**, send `/newbot` and pick a name. Copy the token it gives you.
- Send any message to your new bot.
- Message **@userinfobot** and copy your **Id**.

**4. Give GitHub the secrets**
Repository → **Settings → Secrets and variables → Actions → New repository secret**.
Add these one at a time:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | your Id |
| `APP_URL` | `https://schopenhauerrr.github.io/Odyssey-` |

**5. Start the scanner**
Repository → **Actions** → if asked, *I understand… enable them* → **Odyssey scan**
→ **Run workflow**. From then on it runs every 15 minutes by itself.

**6. Install the app**
Open your app address in Chrome on the phone → **⋮ → Add to home screen**
(or *Install app*). Odyssey now has its own icon and opens full screen.

**7. Connect deal sources** (this is what makes alerts appear)
- **Saved searches with e-mail alerts** on DBA, Lauritz, Catawiki, Trendsales and
  others, for brands like *kay bojesen, holmegaard, ph5, wegner*.
- In Gmail, create a filter that puts those alert mails under the label
  **DealAlerts**.
- Create a Gmail **app password** (Google account → Security → App passwords),
  then add the secrets `IMAP_USERNAME` (your Gmail address) and `IMAP_PASSWORD`
  (the app password). E-mail scanning switches on by itself.
- Optional: eBay keys from developer.ebay.com go in `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.

## Using the app
- **Deals**: every listing that matched your item list. *Buy* means it clears
  your profit target after tax. *Watch* means it's profitable but below target.
  Tap a deal for the full breakdown.
- **Profit**: paste any listing title and price to get the full breakdown:
  expected sale, platform fee, pickup, cleaning, packing, tax, profit after tax,
  margin, return on money, days to sell, profit per hour, and the most you
  should pay.
- **Stock**: log what you buy, list and sell. Once you've sold 2 or more of the
  same item type, Odyssey uses *your* real sale prices and selling times
  instead of the starting guesses.
- **Stats**: profit after tax by month, best item types, speed, profit per hour.
- **Settings** (top right): your tax rate, profit target, usual costs, seller fees.

## Things to know
- **The starting prices in `scanner/reference_prices.csv` are rough guesses.**
  Edit them on GitHub (open the file → ✏️) with real sold prices, and set
  `verified` to `yes`. The scanner re-runs automatically after you save.
- **The seller fees in Settings are assumptions.** Check each site's current fees.
- **Tax** defaults to 50%. Set your own marginal rate. Reselling things you bought
  to resell is taxable. Selling your own used things usually isn't.
- **Privacy**: the repository is public (free hosting needs that), so the deal
  list and price list can be seen by anyone who finds the address. Your stock,
  sales and settings stay only on your phone. Use **Stock → Back up** now and then.
- GitHub can run scheduled scans a few minutes late. If it ever stops after a
  long quiet period, open **Actions** and re-enable it.

## For later
- Change the app: edit `app/odyssey.html`, then run `python tools/build_app.py`.
- Tests: `python scanner/tests/test_scanner.py` and `python scanner/tests/test_finance_parity.py`
  (the second checks that the app and the scanner calculate the same numbers).
