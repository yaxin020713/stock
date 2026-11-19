#!/usr/bin/env python3
"""Paper-trading engine for the 30-day simulation. Standard library only. No real orders, ever.

  python3 sim.py plan --market US --equity 100000 --cash 100000 --exposure 0 --positions 0 --tickers NVDA,AMD
  python3 sim.py settle < state.json

Fill model (identical for US and TW, so nothing looks ahead):
  * an order is decided after a session closed (signal_date = that close) and fills at the OPEN
    of the first session after signal_date;
  * stop and target are checked on every daily high/low from the fill bar on; a gap through the
    level exits at that day's open; if one bar touches both, the stop is assumed to hit first;
  * a position still open after `time_stop_days` sessions exits at that session's close.
Costs: US 0 (commission-free broker assumed); TW 0.1425% each way (min NT$20) + 0.3% sale tax.
Data: Yahoo daily bars (free, unofficial, can lag or be wrong). Prices are simulated fills, not real ones.
"""
import argparse
import datetime
import json
import math
import sys
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (sim)"
HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
DEFAULTS = {"risk_pct": 0.01, "max_pos_pct": 0.20, "max_positions": 5, "max_exposure": 0.80,
            "atr_mult": 1.5, "target_r": 2.0, "time_stop_days": 10, "min_stop_pct": 0.02}
FEE = {"US": {"buy": 0.0, "sell": 0.0, "min": 0.0},
       "TW": {"buy": 0.001425, "sell": 0.001425 + 0.003, "min": 20.0}}
# exchange close (local) + 15 minutes: a bar dated today is dropped until then
CLOSE_LOCAL = {"US": (16, 15), "TW": (13, 45)}


def get(url):
    import time
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def fetch_bars(sym, market):
    """Complete daily bars, oldest first: [{'d','o','h','l','c'}]."""
    doc = None
    for host in HOSTS:
        try:
            doc = json.loads(get("%s/v8/finance/chart/%s?range=6mo&interval=1d" % (host, sym)))
            if (doc.get("chart") or {}).get("result"):
                break
        except (ValueError, urllib.error.URLError, OSError):
            continue
    res = ((doc or {}).get("chart") or {}).get("result")
    if not res:
        raise RuntimeError("no data for %s" % sym)
    r = res[0]
    off = (r.get("meta") or {}).get("gmtoffset", 0)
    q = r["indicators"]["quote"][0]
    out = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        d = (datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(seconds=off))
        out[d.date().isoformat()] = {"d": d.date().isoformat(), "o": float(o), "h": float(h),
                                     "l": float(l), "c": float(c)}
    bars = [out[k] for k in sorted(out)]
    now_local = datetime.datetime.utcnow() + datetime.timedelta(seconds=off)
    hh, mm = CLOSE_LOCAL[market]
    if bars and bars[-1]["d"] == now_local.date().isoformat() and (now_local.hour, now_local.minute) < (hh, mm):
        bars.pop()  # today's session is not finished
    return bars


def fee(market, side, amount):
    f = FEE[market]
    return round(max(f["min"], amount * f[side]), 2) if amount > 0 else 0.0


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


# ---------------------------------------------------------------- plan

def plan(market, equity, cash, exposure, npos, tickers, params, bars_for=fetch_bars):
    p = dict(DEFAULTS, **params)
    rows = []
    room = max(0.0, p["max_exposure"] * equity - exposure)
    for sym in tickers:
        row = {"ticker": sym}
        try:
            bars = bars_for(sym, market)
            a = atr(bars)
            if a is None:
                raise RuntimeError("less than 15 daily bars")
        except Exception as e:  # noqa: BLE001 - report per ticker, keep going
            row.update(ok=False, reason=str(e))
            rows.append(row)
            continue
        c = bars[-1]["c"]
        stop = min(c - p["atr_mult"] * a, c * (1 - p["min_stop_pct"]))
        risk = c - stop
        n_risk = math.floor(p["risk_pct"] * equity / risk)
        n_cap = math.floor(p["max_pos_pct"] * equity / c)
        n_cash = math.floor(cash / (c * 1.01))
        n_room = math.floor(min(room, cash) / c)
        n = max(0, min(n_risk, n_cap, n_cash, n_room))
        limit = min((("risk", n_risk), ("position cap", n_cap), ("cash", n_cash), ("exposure", n_room)), key=lambda x: x[1])[0]
        ok = n >= 1 and npos < p["max_positions"]
        row.update(ok=ok, signal_date=bars[-1]["d"], ref_close=round(c, 2), atr=round(a, 3),
                   stop=round(stop, 2), target=round(c + p["target_r"] * risk, 2), shares=n,
                   est_cost=round(n * c, 2), risk_at_stop=round(n * risk, 2), limited_by=limit,
                   reason="" if ok else ("max positions" if npos >= p["max_positions"] else "size < 1 share"))
        if ok:
            room -= n * c
            cash -= n * c
            npos += 1
        rows.append(row)
    return rows


# ---------------------------------------------------------------- settle

def settle(state, params, bars_for=fetch_bars):
    market = state["market"]
    p = dict(DEFAULTS, **params)
    cash = float(state["cash"])
    events, still_open, cache = [], [], {}

    def bars(sym):
        if sym not in cache:
            cache[sym] = bars_for(sym, market)
        return cache[sym]

    def run(pos, series, start_i, cash):
        """Walk bars from start_i. Returns (cash, exit_event|None)."""
        sh, entry, stop, target = pos["shares"], pos["avg_cost"], pos["stop"], pos["target"]
        for k, b in enumerate(series[start_i:]):
            px, action = None, None
            if k > 0 and b["o"] <= stop:
                px, action = b["o"], "賣出-停損"
            elif k > 0 and b["o"] >= target:
                px, action = b["o"], "賣出-停利"
            elif b["l"] <= stop:
                px, action = stop, "賣出-停損"
            elif b["h"] >= target:
                px, action = target, "賣出-停利"
            elif k + 1 >= p["time_stop_days"]:
                px, action = b["c"], "賣出-時間停損"
            if px is not None:
                gross = round(sh * px, 2)
                f = fee(market, "sell", gross)
                buy_fee = pos.get("buy_fee", fee(market, "buy", sh * entry))
                pnl = round(gross - f - sh * entry - buy_fee, 2)
                risk_total = sh * (entry - pos["stop0"] if "stop0" in pos else entry - stop)
                ev = {"type": "exit", "action": action, "ticker": pos["ticker"], "date": b["d"],
                      "shares": sh, "price": round(px, 2), "amount": gross, "fees": f,
                      "realized_pnl": pnl, "r_multiple": round(pnl / risk_total, 2) if risk_total else None,
                      "held_sessions": k + 1}
                return cash + gross - f, ev
        return cash, None

    # 1) pending buys -> fills at the first open after signal_date
    new_positions = []
    for o in state.get("pending", []):
        sym = o["ticker"]
        try:
            series = bars(sym)
        except Exception as e:  # noqa: BLE001
            events.append({"type": "error", "ticker": sym, "detail": str(e), "ref": o.get("ref")})
            continue
        i = next((j for j, b in enumerate(series) if b["d"] > o["signal_date"]), None)
        if i is None:
            still_open.append({"type": "pending", "ticker": sym, "ref": o.get("ref")})
            continue
        b = series[i]
        if b["o"] <= o["stop"] or b["o"] >= o["target"]:
            events.append({"type": "cancel", "ticker": sym, "date": b["d"], "ref": o.get("ref"),
                           "detail": "gap through stop/target at open %.2f" % b["o"]})
            continue
        sh = int(o["shares"])
        while sh >= 1 and sh * b["o"] + fee(market, "buy", sh * b["o"]) > cash:
            sh -= 1
        if sh < 1:
            events.append({"type": "cancel", "ticker": sym, "date": b["d"], "ref": o.get("ref"),
                           "detail": "not enough cash"})
            continue
        cost = round(sh * b["o"], 2)
        bf = fee(market, "buy", cost)
        cash -= cost + bf
        events.append({"type": "fill", "action": "買進", "ticker": sym, "date": b["d"], "shares": sh,
                       "price": round(b["o"], 2), "amount": cost, "fees": bf, "stop": o["stop"],
                       "target": o["target"], "ref": o.get("ref")})
        new_positions.append(({"ticker": sym, "shares": sh, "avg_cost": b["o"], "stop": o["stop"],
                               "target": o["target"], "buy_fee": bf, "open_date": b["d"]}, i))

    # 2) walk every position (new ones from their fill bar, old ones from open_date)
    held = []
    for pos, i in new_positions:
        cash, ev = run(pos, bars(pos["ticker"]), i, cash)
        (events.append(ev) if ev else held.append(pos))
    for pos in state.get("positions", []):
        series = bars(pos["ticker"])
        i = next((j for j, b in enumerate(series) if b["d"] >= pos["open_date"]), None)
        if i is None:
            held.append(pos)
            continue
        cash, ev = run(pos, series, i, cash)
        (events.append(ev) if ev else held.append(pos))

    # 3) mark to market
    marks, mv = [], 0.0
    for pos in held:
        last = bars(pos["ticker"])[-1]
        v = pos["shares"] * last["c"]
        mv += v
        marks.append({"ticker": pos["ticker"], "shares": pos["shares"], "avg_cost": round(pos["avg_cost"], 4),
                      "stop": pos["stop"], "target": pos["target"], "open_date": pos["open_date"],
                      "last_price": round(last["c"], 2), "last_date": last["d"],
                      "unrealized_pnl": round(v - pos["shares"] * pos["avg_cost"] - pos.get("buy_fee", 0), 2)})
    return {"market": market, "cash": round(cash, 2), "market_value": round(mv, 2),
            "equity": round(cash + mv, 2), "events": events, "positions": marks, "unfilled": still_open}


def main():
    ap = argparse.ArgumentParser(description="paper-trading engine (simulation only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--market", choices=("US", "TW"), required=True)
    pl.add_argument("--equity", type=float, required=True)
    pl.add_argument("--cash", type=float, required=True)
    pl.add_argument("--exposure", type=float, default=0.0, help="market value of open positions")
    pl.add_argument("--positions", type=int, default=0, help="number of open positions")
    pl.add_argument("--tickers", required=True, help="Yahoo symbols, comma separated (TW: 2330.TW)")
    pl.add_argument("--params", default="{}", help="JSON overrides of the rule parameters")
    st = sub.add_parser("settle")
    st.add_argument("--params", default="{}")
    a = ap.parse_args()
    params = json.loads(a.params)
    if a.cmd == "plan":
        out = plan(a.market, a.equity, a.cash, a.exposure, a.positions, a.tickers.split(","), params)
    else:
        out = settle(json.load(sys.stdin), params)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
