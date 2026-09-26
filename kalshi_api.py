"""Minimal read-only Kalshi public API client (no keys needed for market data)."""
import json, time, urllib.request, urllib.parse, urllib.error

BASE = "https://api.elections.kalshi.com/trade-api/v2"
_last = [0.0]
MIN_GAP = 0.08  # ~12 requests/second, under the basic 20/s read limit


def get(path, params=None, retries=5):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for k in range(retries):
        wait = MIN_GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "matrix-kalshi-scanner/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(0.5 * (k + 1)); continue
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(1.0 * (k + 1))
    raise RuntimeError(f"Kalshi API failed: {url}")


def f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def all_open_events():
    """Every open (non-combo) event with its markets. ~1 request per 200 events."""
    out, cursor = [], None
    while True:
        p = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        j = get("/events", p)
        if not j:
            break
        out += j.get("events", [])
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def market(ticker):
    j = get(f"/markets/{urllib.parse.quote(ticker)}")
    return j.get("market") if j else None


def _soonest(a, b):
    """Sports markets often have a scheduled close days after the game; the expected expiration
    is the better 'resolves by' time. Use whichever is earlier."""
    vals = [x for x in (a, b) if x]
    return min(vals) if vals else None


def norm_market(m, ev):
    """Flatten the fields the scanner uses."""
    return dict(
        ticker=m["ticker"], event=ev["event_ticker"], series=ev.get("series_ticker", ""),
        title=m.get("title") or ev.get("title", ""), sub=m.get("yes_sub_title", ""),
        ev_title=ev.get("title", ""), category=ev.get("category", ""),
        mutex=bool(ev.get("mutually_exclusive")), status=m.get("status"),
        bid=f(m.get("yes_bid_dollars"), 0.0), ask=f(m.get("yes_ask_dollars"), 1.0),
        bid_size=f(m.get("yes_bid_size_fp"), 0.0), ask_size=f(m.get("yes_ask_size_fp"), 0.0),
        last=f(m.get("last_price_dollars")), vol24=f(m.get("volume_24h_fp"), 0.0),
        oi=f(m.get("open_interest_fp"), 0.0), close_time=_soonest(m.get("close_time"), m.get("expected_expiration_time")),
        sched_close=m.get("close_time"),
        strike_type=m.get("strike_type") or "", floor=m.get("floor_strike"), cap=m.get("cap_strike"),
        tick=m.get("price_level_structure", "linear_cent"),
    )
