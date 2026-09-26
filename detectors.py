"""Opportunity detectors. Input: list of normalized markets (kalshi_api.norm_market).
Every detector returns a list of opportunity dicts:
  {type, event, title, edge (expected $ per 1-contract set, after fees), legs:[{ticker, side, price, size}], info}
"""
import math, re, collections
from datetime import datetime, timezone

TAKER = 0.07


def fee(price, contracts=1, mult=1.0):
    """Kalshi taker fee: 0.07 x C x P x (1-P), rounded up to the cent per order."""
    raw = mult * TAKER * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


def fee_per_contract(price, mult=1.0):
    return mult * TAKER * price * (1 - price)


def hours_left(m, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
    except Exception:
        return None
    return (ct - now).total_seconds() / 3600


# ------------------------------------------------------------------ 1) mutually exclusive NO basket
def no_basket(markets, min_edge=0.01):
    """In a mutually exclusive event at most one market resolves YES, so buying NO on k markets pays at
    least k-1 dollars. Cost = sum(1 - yes_bid). Risk-free profit if sum(yes_bid) - 1 - fees > 0."""
    by_ev = collections.defaultdict(list)
    for m in markets:
        if m["mutex"] and m["status"] == "active":
            by_ev[m["event"]].append(m)
    out = []
    for ev, ms in by_ev.items():
        legs = [m for m in ms if m["bid"] > 0 and m["bid_size"] > 0 and m["bid"] - fee_per_contract(1 - m["bid"]) > 0]
        if len(legs) < 2:
            continue
        edge = sum(m["bid"] for m in legs) - 1 - sum(fee_per_contract(1 - m["bid"]) for m in legs)
        if edge >= min_edge:
            out.append(dict(type="ARB_NO_BASKET", event=ev, title=legs[0]["ev_title"], edge=edge,
                            legs=[dict(ticker=m["ticker"], side="no", price=round(1 - m["bid"], 4), size=m["bid_size"], title=m["sub"] or m["title"]) for m in legs],
                            info=f"sum of YES bids {sum(m['bid'] for m in legs):.3f} across {len(legs)} outcomes"))
    return out


# ------------------------------------------------------------------ 2) threshold ladder monotonicity
def _ladder_key(ticker):
    pre, last = ticker.rsplit("-", 1)
    return pre + "-" + re.sub(r"[-\d.]+$", "", last)


_UP = re.compile(r"(above|over|or more|or above|at least|\+|greater|higher|exceed|>)", re.I)
_DN = re.compile(r"(below|under|or less|or fewer|or below|at most|less than|lower|<)", re.I)


def ladder(markets, min_edge=0.01):
    """'Above X' must be at least as likely as 'Above Y' when X < Y (same underlying). If YES on the
    easier strike is cheaper than the YES bid on the harder strike: buy YES easy + buy NO hard = pays >= $1.
    Only markets whose label literally says above/below are used: strike fields alone are not reliable
    (e.g. year-based 'by 2025 / by 2030' markets also use strike_type='greater')."""
    g = collections.defaultdict(list)
    for m in markets:
        if m["status"] != "active":
            continue
        st, label = m["strike_type"], (m["sub"] or m["title"] or "")
        if st in ("greater", "greater_or_equal") and m["floor"] is not None and m["cap"] is None and _UP.search(label):
            g[(m["event"], _ladder_key(m["ticker"]), st, 1)].append(m)
        elif st in ("less", "less_or_equal") and m["cap"] is not None and m["floor"] is None and _DN.search(label):
            g[(m["event"], _ladder_key(m["ticker"]), st, -1)].append(m)
    out = []
    for (ev, _, _, d), ms in g.items():
        if len(ms) < 2:
            continue
        key = (lambda m: m["floor"]) if d == 1 else (lambda m: m["cap"])
        ms.sort(key=key)
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                lo, hi = ms[i], ms[j]
                if key(lo) == key(hi):
                    continue
                A, B = (lo, hi) if d == 1 else (hi, lo)
                a, b = A["ask"], B["bid"]
                if not (0 < a < 1 and 0 < b < 1) or A["ask_size"] <= 0 or B["bid_size"] <= 0:
                    continue
                edge = b - a - fee_per_contract(a) - fee_per_contract(1 - b)
                if edge >= min_edge:
                    out.append(dict(type="ARB_LADDER", event=ev, title=A["ev_title"], edge=edge,
                                    legs=[dict(ticker=A["ticker"], side="yes", price=a, size=A["ask_size"], title=A["sub"] or A["title"]),
                                          dict(ticker=B["ticker"], side="no", price=round(1 - b, 4), size=B["bid_size"], title=B["sub"] or B["title"])],
                                    info=f"YES {A['sub']} @ {a:.2f} < YES bid {B['sub']} @ {b:.2f}"))
    return out


# ------------------------------------------------------------------ 3) price-band rules (statistical, not risk-free)
def band_rule(markets, r):
    """Buy `side` at the taker price when that price is inside [min_price, max_price] and the market resolves
    within the hours window. Examples: FAVORITE_YES (buy YES at 90-95c), LONGSHOT_FADE (buy NO at 88-98c).
    Whether a band has an edge is an empirical question: see research/backtest_bands.py."""
    out = []
    for m in markets:
        if m["status"] != "active":
            continue
        h = hours_left(m)
        if h is None or h < r["min_hours_left"] or h > r["max_hours_left"]:
            continue
        b, a = m["bid"], m["ask"]
        if not (0 < b < a < 1) or a - b > r["max_spread"] or m["vol24"] < r["min_vol24"]:
            continue
        if r["side"] == "yes":
            px, size = a, m["ask_size"]
        else:
            px, size = round(1 - b, 4), m["bid_size"]
        if not (r["min_price"] <= px <= r["max_price"]) or size < r["min_size"]:
            continue
        if any(m["series"].startswith(x) for x in r.get("exclude_series", [])):
            continue
        roi = r.get("expected_roi", 0.0)
        for lo_h, hi_h, v in r.get("roi_by_hours", []):  # backtested return by time-to-resolution, used for ranking
            if lo_h <= h < hi_h:
                roi = v
        edge = roi * px + min(m["vol24"], 1e5) * 1e-9  # $ per contract; volume only breaks ties
        out.append(dict(type=r["name"], event=m["event"], title=m["title"], edge=edge,
                        legs=[dict(ticker=m["ticker"], side=r["side"], price=px, size=size, title=m["sub"] or m["title"])],
                        info=f"buy {r['side'].upper()} @ {px:.2f} (YES {b:.2f}/{a:.2f}), resolves in {h:.1f}h, 24h vol {m['vol24']:.0f}"))
    return out


# ------------------------------------------------------------------ 4) watchlist flags (no trades)
def wide_spreads(markets, min_spread=0.10, min_vol24=200):
    out = []
    for m in markets:
        if m["status"] == "active" and m["bid"] > 0 and m["ask"] < 1 and m["ask"] - m["bid"] >= min_spread and m["vol24"] >= min_vol24:
            out.append(dict(type="WIDE_SPREAD", event=m["event"], title=m["title"], edge=0.0, legs=[],
                            info=f"{m['ticker']}: bid {m['bid']:.2f} / ask {m['ask']:.2f}, 24h vol {m['vol24']:.0f} - maker opportunity"))
    return out
