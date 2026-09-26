"""MATRIX // KALSHI SCANNER - scans every open Kalshi market, flags opportunities and paper-trades them.

Run:  python scanner_bot.py              (scanner + dashboard at http://localhost:8788)
      python scanner_bot.py --once       (single scan, print results, exit)
Paper trading only: read-only public data, no API keys, no real orders.
"""
import json, os, sys, time, math, threading, sqlite3, argparse, traceback, webbrowser, urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import kalshi_api as K
import detectors as D

CFG = json.load(open(os.path.join(HERE, "config.json")))
DB_PATH = os.path.join(os.environ.get("STATE_DIR", HERE), "kalshi_paper.db")
LIVE = {"status": "starting", "last_scan": None, "next_scan": None, "scan": {}, "errors": [], "marks": {}}


def now():
    return datetime.now(timezone.utc)


def log(msg):
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "scanner.log"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ------------------------------------------------------------------ storage
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT, grp TEXT, strategy TEXT,
          ticker TEXT, event TEXT, title TEXT, side TEXT, qty REAL, price REAL, fee REAL, cost REAL,
          opened_at TEXT, close_time TEXT, status TEXT, result TEXT, payout REAL, pnl REAL, settled_at TEXT);
        CREATE TABLE IF NOT EXISTS opps(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, type TEXT, event TEXT,
          title TEXT, edge REAL, info TEXT, legs TEXT, taken INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS scans(ts TEXT PRIMARY KEY, events INTEGER, markets INTEGER, secs REAL, counts TEXT);
        CREATE TABLE IF NOT EXISTS equity(ts TEXT PRIMARY KEY, equity REAL, cash REAL, open_value REAL);
        """)


def cash():
    with db() as c:
        spent = c.execute("SELECT COALESCE(SUM(cost),0) s FROM positions").fetchone()["s"]
        paid = c.execute("SELECT COALESCE(SUM(payout),0) s FROM positions WHERE status='settled'").fetchone()["s"]
    return CFG["bankroll"] - spent + paid


def open_positions():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM positions WHERE status='open'")]


# ------------------------------------------------------------------ paper execution
def paper_take(opp, markets_by_ticker):
    """Simulate taker fills at the displayed top of book. Returns (taken, note)."""
    S = CFG["strategies"]
    typ = opp["type"]
    pos = open_positions()
    held = {p["ticker"] for p in pos}
    if any(l["ticker"] in held for l in opp["legs"]):
        return False, "already holding"
    open_cost = sum(p["cost"] for p in pos)
    free = cash()
    if typ not in ("ARB_NO_BASKET", "ARB_LADDER"):
        p = next(r for r in S["band_rules"] if r["name"] == typ)
        if sum(1 for x in pos if x["event"] == opp["event"] and x["strategy"] == typ) >= p["max_per_event"]:
            return False, "event limit"
        if sum(1 for x in pos if x["strategy"] == typ) >= p["max_open"]:
            return False, "max open positions"
        leg = opp["legs"][0]
        qty = math.floor(min(p["stake"] / leg["price"], leg["size"]))
    else:
        p = S["arb_no_basket" if typ == "ARB_NO_BASKET" else "arb_ladder"]
        per_set = sum(l["price"] for l in opp["legs"])
        qty = math.floor(min(min(l["size"] for l in opp["legs"]), p["max_cost"] / per_set))
    if qty < 1:
        return False, "size too small"
    fills = [(l, qty, D.fee(l["price"], qty)) for l in opp["legs"]]
    cost = sum(l["price"] * q + f for l, q, f in fills)
    if cost > free:
        return False, "not enough paper cash"
    if open_cost + cost > CFG["max_exposure"] * CFG["bankroll"]:
        return False, "exposure limit"
    grp = f"{typ[:4]}-{int(time.time()*1000)}"
    with db() as c:
        for l, q, f in fills:
            m = markets_by_ticker.get(l["ticker"], {})
            c.execute("""INSERT INTO positions(grp,strategy,ticker,event,title,side,qty,price,fee,cost,opened_at,close_time,status)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                      (grp, typ, l["ticker"], opp["event"], (m.get("title") or l.get("title") or "")[:160], l["side"], q,
                       l["price"], f, l["price"] * q + f, now().isoformat(), m.get("close_time")))
    return True, f"{qty} x {len(fills)} leg(s), cost ${cost:,.2f}"


def settle(open_by_ticker_in_scan):
    """Settle paper positions whose market has resolved."""
    for p in open_positions():
        if p["ticker"] in open_by_ticker_in_scan:
            continue  # still trading
        m = K.market(p["ticker"])
        if not m:
            continue
        res = (m.get("result") or "").lower()
        if res not in ("yes", "no") and not m.get("settlement_value_dollars"):
            continue
        if res in ("yes", "no"):
            val_yes = 1.0 if res == "yes" else 0.0
        else:
            val_yes = K.f(m.get("settlement_value_dollars"), 0.0)
        per = val_yes if p["side"] == "yes" else 1 - val_yes
        payout = per * p["qty"]
        pnl = payout - p["cost"]
        with db() as c:
            c.execute("UPDATE positions SET status='settled', result=?, payout=?, pnl=?, settled_at=? WHERE id=?",
                      (res or f"{val_yes:.2f}", payout, pnl, now().isoformat(), p["id"]))
        log(f"{'✅' if pnl >= 0 else '❌'} SETTLED {p['strategy']} {p['side'].upper()} {p['ticker']} -> {res.upper() or val_yes}  P&L ${pnl:+.2f}")


def notify(text):
    hook = (os.environ.get("DISCORD_WEBHOOK") or CFG.get("discord_webhook", "")).strip()
    if not hook:
        return
    try:
        req = urllib.request.Request(hook, data=json.dumps({"content": text[:1900]}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "matrix-kalshi"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log(f"discord failed: {e}")


# ------------------------------------------------------------------ scan cycle
def scan_once(trade=True):
    t0 = time.time()
    events = K.all_open_events()
    markets = [K.norm_market(m, e) for e in events for m in e.get("markets", [])]
    by_t = {m["ticker"]: m for m in markets}
    S = CFG["strategies"]
    opps = []
    if S["arb_no_basket"]["enabled"]:
        opps += D.no_basket(markets, S["arb_no_basket"]["min_edge"])
    if S["arb_ladder"]["enabled"]:
        opps += D.ladder(markets, S["arb_ladder"]["min_edge"])
    for r in S["band_rules"]:
        if r["enabled"]:
            opps += D.band_rule(markets, r)
    watch = D.wide_spreads(markets, S["watch_wide_spread"]["min_spread"], S["watch_wide_spread"]["min_vol24"]) if S["watch_wide_spread"]["enabled"] else []
    counts = {}
    for o in opps + watch:
        counts[o["type"]] = counts.get(o["type"], 0) + 1
    secs = time.time() - t0
    ts = now().isoformat()
    LIVE["marks"] = {t: (m["bid"], m["ask"]) for t, m in by_t.items()}
    LIVE["scan"] = dict(ts=ts, events=len(events), markets=len(markets), secs=round(secs, 1), counts=counts,
                        watch=sorted(watch, key=lambda w: -float(w["info"].split("24h vol ")[1].split(" ")[0]))[:25])
    with db() as c:
        c.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?,?)", (ts, len(events), len(markets), secs, json.dumps(counts)))
    # take the best opportunities first (arbs before statistical trades)
    rank = {"ARB_NO_BASKET": 0, "ARB_LADDER": 1}
    opps.sort(key=lambda o: (rank.get(o["type"], 2), -o["edge"]))
    recent = set()
    with db() as c:
        for r in c.execute("SELECT type, event, legs FROM opps WHERE ts > datetime('now','-6 hours')"):
            recent.add((r["type"], r["legs"]))
    for o in opps:
        legs_key = json.dumps([l["ticker"] for l in o["legs"]])
        taken, note = (paper_take(o, by_t) if trade else (False, "scan only"))
        if not taken and (not o["type"].startswith("ARB") or (o["type"], legs_key) in recent):
            continue  # feed shows every arb, but only the rule trades actually taken
        with db() as c:
            c.execute("INSERT INTO opps(ts,type,event,title,edge,info,legs,taken,note) VALUES(?,?,?,?,?,?,?,?,?)",
                      (ts, o["type"], o["event"], o["title"][:160], o["edge"], o["info"], legs_key, int(taken), note))
        if taken:
            msg = f"📡 {o['type']} {o['title'][:80]} | edge {o['edge']*100:.1f}c/set | {note} | {o['info']}"
            log(msg); notify(msg)
    if trade:
        settle(set(by_t))
    snapshot()
    log(f"scan: {len(events)} events / {len(markets)} markets in {secs:.0f}s  {counts}")
    return opps, watch


def snapshot():
    pos = open_positions()
    val = 0.0
    for p in pos:
        bid, ask = LIVE["marks"].get(p["ticker"], (None, None))
        if bid is None:
            val += p["qty"] * p["price"]
        else:
            val += p["qty"] * (bid if p["side"] == "yes" else 1 - ask)
    cs = cash()
    with db() as c:
        c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?,?)", (now().replace(second=0, microsecond=0).isoformat(), cs + val, cs, val))


def loop():
    every = CFG["scan_minutes"] * 60
    while True:
        LIVE["status"] = "scanning"
        try:
            scan_once(trade=True)
            LIVE["last_scan"] = now().isoformat()
            LIVE["status"] = "running"
        except Exception as e:
            log("scan error: " + repr(e)); traceback.print_exc()
            LIVE["errors"] = (LIVE["errors"] + [f"{now().isoformat()} {e!r}"])[-5:]
            LIVE["status"] = "error - retrying"
        nxt = time.time() + every
        LIVE["next_scan"] = datetime.fromtimestamp(nxt, timezone.utc).isoformat()
        while time.time() < nxt:
            time.sleep(2)


# ------------------------------------------------------------------ dashboard API
def payload():
    with db() as c:
        pos = [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY id DESC LIMIT 2000")]
        opps = [dict(r) for r in c.execute("SELECT * FROM opps ORDER BY id DESC LIMIT 80")]
        eq = [dict(r) for r in c.execute("SELECT ts, equity FROM equity ORDER BY ts")]
        nscan = c.execute("SELECT COUNT(*) n FROM scans").fetchone()["n"]
        arbs = c.execute("SELECT COUNT(*) n FROM opps WHERE type LIKE 'ARB%'").fetchone()["n"]
    openp = [p for p in pos if p["status"] == "open"]
    settled = [p for p in pos if p["status"] == "settled"]
    for p in openp:
        bid, ask = LIVE["marks"].get(p["ticker"], (None, None))
        p["mark"] = None if bid is None else (bid if p["side"] == "yes" else round(1 - ask, 4))
        p["upnl"] = None if p["mark"] is None else p["mark"] * p["qty"] - p["cost"]
    strat = {}
    for p in pos:
        s = strat.setdefault(p["strategy"], dict(strategy=p["strategy"], open=0, settled=0, wins=0, pnl=0.0, cost=0.0))
        if p["status"] == "open":
            s["open"] += 1
        else:
            s["settled"] += 1; s["wins"] += p["pnl"] > 0; s["pnl"] += p["pnl"]; s["cost"] += p["cost"]
    cs = cash()
    open_val = sum((p["mark"] if p["mark"] is not None else p["price"]) * p["qty"] for p in openp)
    realized = sum(p["pnl"] for p in settled)
    if len(eq) > 1500:
        k = len(eq) // 1500 + 1
        eq = eq[::k] + [eq[-1]]
    bt = None
    if os.path.exists(os.path.join(HERE, "backtest.json")):
        bt = json.load(open(os.path.join(HERE, "backtest.json")))
    return dict(
        config=dict(bankroll=CFG["bankroll"], scan_minutes=CFG["scan_minutes"], max_exposure=CFG["max_exposure"],
                    rules=[dict(name=r["name"], enabled=r["enabled"], side=r["side"], min_price=r["min_price"], max_price=r["max_price"])
                           for r in CFG["strategies"]["band_rules"]]),
        live=dict(status=LIVE["status"], last_scan=LIVE["last_scan"], next_scan=LIVE["next_scan"], errors=LIVE["errors"],
                  scan=LIVE["scan"], n_scans=nscan, arbs_seen=arbs, server_time=now().isoformat()),
        stats=dict(equity=cs + open_val, cash=cs, open_value=open_val, open_cost=sum(p["cost"] for p in openp),
                   realized=realized, settled=len(settled), wins=sum(p["pnl"] > 0 for p in settled),
                   roi=(realized / sum(p["cost"] for p in settled)) if settled else None),
        strategies=list(strat.values()), open=openp, settled=settled[:300], opps=opps, equity=eq, backtest=bt)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ct):
        self.send_response(code); self.send_header("Content-Type", ct); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, open(os.path.join(HERE, "dashboard.html"), "rb").read(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(payload(), default=str).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._send(500, repr(e).encode(), "text/plain")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one scan, print, exit (no trades)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--cron", metavar="SITE_DIR", help="one scan WITH paper trades, then write a static dashboard to SITE_DIR (for GitHub Actions)")
    a = ap.parse_args()
    init_db()
    if a.cron:
        os.makedirs(a.cron, exist_ok=True)
        try:
            scan_once(trade=True)
            LIVE["status"] = "running"
        except Exception as e:
            log("scan error: " + repr(e)); traceback.print_exc()
            LIVE["status"] = "error - will retry next run"; LIVE["errors"] = [f"{now().isoformat()} {e!r}"]
        LIVE["last_scan"] = now().isoformat()
        LIVE["next_scan"] = datetime.fromtimestamp(time.time() + CFG["scan_minutes"] * 60, timezone.utc).isoformat()
        with open(os.path.join(a.cron, "data.json"), "w") as fh:
            json.dump(payload(), fh, default=str)
        import shutil
        shutil.copy(os.path.join(HERE, "dashboard.html"), os.path.join(a.cron, "index.html"))
        with db() as c:  # keep the state file small
            c.execute("DELETE FROM opps WHERE id NOT IN (SELECT id FROM opps ORDER BY id DESC LIMIT 2000)")
            c.execute("DELETE FROM scans WHERE ts NOT IN (SELECT ts FROM scans ORDER BY ts DESC LIMIT 500)")
        sys.exit(0)
    if a.once:
        opps, watch = scan_once(trade=False)
        for o in sorted(opps, key=lambda o: -o["edge"])[:40]:
            print(f"{o['type']:14s} edge {o['edge']*100:5.1f}c  {o['title'][:70]}  | {o['info']}")
        print(f"{len(watch)} wide-spread markets flagged")
        sys.exit(0)
    port = CFG.get("dashboard_port", 8788)
    url = f"http://localhost:{port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        print(f"Port {port} busy: scanner already running. Opening {url}"); webbrowser.open(url); sys.exit(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"dashboard at {url}")
    if not a.no_browser:
        webbrowser.open(url)
    loop()
