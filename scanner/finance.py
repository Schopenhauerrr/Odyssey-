"""Profit model shared with the app (docs/index.html → `computeDeal`). Keep the two in sync;
tests/test_finance_parity.py runs both on the same inputs and compares the results."""
from __future__ import annotations


def compute_deal(resale_dkk: float, ref_days: float, asking_dkk: float, s: dict, *,
                 platform: str | None = None, condition: str | None = None, ship: bool | None = None,
                 transport: float | None = None, cleaning: float | None = None,
                 extra_cost: float = 0.0, learned_days: float | None = None) -> dict:
    p = s["platforms"][platform or s["default_platform"]]
    cond = s["conditions"][condition or s["default_condition"]]["factor"]
    ship = s["ship_by_default"] if ship is None else ship
    c = s["costs"]
    transport = c["transport"] if transport is None else transport
    cleaning = c["cleaning"] if cleaning is None else cleaning
    packing = c["packing"] if ship else 0.0
    tax_rate = s["tax_rate"]

    sell = resale_dkk * cond
    fee = sell * p["fee_pct"] + (p["fee_fixed"] if sell > 0 else 0)
    direct = transport + cleaning + packing + extra_cost
    invested = asking_dkk + direct
    before_tax = sell - fee - invested
    tax = max(0.0, before_tax) * tax_rate
    after_tax = before_tax - tax

    if learned_days:
        days, days_source = learned_days, "your sales"
    elif p.get("fixed_days"):
        days, days_source = p["fixed_days"], "auction cycle"
    else:
        days, days_source = ref_days * p["days_factor"], "estimate"

    max_buy = min(sell - fee - direct - s["min_profit_after_tax"] / (1 - tax_rate),
                  sell * s["max_buy_ratio"])
    max_buy = max(0.0, max_buy)

    if asking_dkk > s["max_price_dkk"]:
        verdict = "skip"
    elif after_tax >= s["min_profit_after_tax"] and asking_dkk <= max_buy + 0.5:
        verdict = "buy"
    elif after_tax > 0:
        verdict = "maybe"
    else:
        verdict = "skip"

    return {
        "sell": sell, "fee": fee, "transport": transport, "cleaning": cleaning, "packing": packing,
        "extra": extra_cost, "invested": invested, "before_tax": before_tax, "tax": tax,
        "after_tax": after_tax, "margin": after_tax / sell if sell else 0.0,
        "roi": after_tax / invested if invested else 0.0, "days": days, "days_source": days_source,
        "per_hour": after_tax / s["hours_per_item"] if s["hours_per_item"] else 0.0,
        "max_buy": max_buy, "verdict": verdict,
    }
