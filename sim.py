#!/usr/bin/env python3
"""Paper-trading engine for the small-account simulation. Standard library only. No real orders, ever.

  python3 sim.py plan --market US --equity 300 --cash 300 --exposure 0 --positions 0 --tickers NVDA,AMD
  python3 sim.py settle --params '<rules json>' < state.json

Fill model (identical for US and TW, nothing looks ahead):
  * an order is decided after a session closed (signal_date = that close) and fills at the OPEN of the
    first session after signal_date, plus slippage against us;
  * stop and target are checked on every daily high/low from the fill bar on; a gap through the level
    exits at that day's open; if one bar touches both, the stop is assumed to hit first;
  * stop / time-stop / gap exits pay slippage; a target (limit) fill pays none;
  * a position still open after `time_stop_days` sessions exits at that session's close.
Markets:
  TW  odd-lot (whole shares). Costs: 0.1425% each way with a NT$20 minimum, plus 0.3% sale tax.
  US  fractional shares (4 decimals). Costs: no commission; sells pay SEC + FINRA TAF fees (estimate).
Sizing (plan): value band pos_value_min..pos_value_max, risk cap risk_pct x equity, exposure and cash caps.
A plan is rejected when round-trip costs exceed `max_cost_to_reward` of the reward at the target.
Rule parameters: flat keys apply to both markets; a "US"/"TW" sub-object overrides them for that market.
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
DEFAULTS = {"risk_pct": 0.03, "max_pos_pct": 0.5, "pos_value_min": 0.0, "pos_value_max": 0.0,
            "max_positions": 5, "max_exposure": 0.9, "atr_mult": 1.5, "target_r": 2.0,
            "time_stop_days": 10, "min_stop_pct": 0.02, "max_cost_to_reward": 0.25, "slippage": 0.0,
            "fractional": False}
MARKET_DEFAULTS = {"US": {"fractional": True, "slippage": 0.0005},
                   "TW": {"fractional": False, "slippage": 0.0015}}
# exchange close (local) + 15 minutes: a bar dated today is dropped until then
CLOSE_LOCAL = {"US": (16, 15), "TW": (13, 45)}
TW_RATE, TW_TAX, TW_MIN = 0.001425, 0.003, 20.0
US_SEC, US_TAF = 0.0000278, 0.000166  # estimates; the real rates change, a broker's numbers rule


def resolve(market, params):
    p = dict(DEFAULTS)
    p.update(MARKET_DEFAULTS[market])
    p.update({k: v for k, v in params.items() if not isinstance(v, dict)})
    p.update(params.get("common", {}))
    p.update(params.get(market, {}))
    return p


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


def fee(market, side, amount, shares=0.0):
    if amount <= 0:
        return 0.0
    if market == "TW":
        rate = TW_RATE if side == "buy" else TW_RATE + TW_TAX
        return round(max(TW_MIN, amount * rate), 2)
    if side == "buy":
        return 0.0
    return round(max(0.01, amount * US_SEC) + max(0.01, shares * US_TAF), 2)


def floor_shares(x, fractional):
    return math.floor(x * 10000 + 1e-9) / 10000 if fractional else math.floor(x + 1e-9)


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
    p = resolve(market, params)
    frac, slip = p["fractional"], p["slippage"]
    room = max(0.0, p["max_exposure"] * equity - exposure)
    vmax = p["pos_value_max"] or p["max_pos_pct"] * equity
    rows = []
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
        entry = c * (1 + slip)
        stop = min(c - p["atr_mult"] * a, c * (1 - p["min_stop_pct"]))
        risk = entry - stop
        target = entry + p["target_r"] * risk
        fee_room = TW_MIN if market == "TW" else 0.0
        caps = (("risk", p["risk_pct"] * equity / risk), ("value cap", vmax / entry),
                ("cash", (cash - fee_room) / (entry * 1.002)), ("exposure", min(room, cash) / entry))
        limit, raw = min(caps, key=lambda x: x[1])
        n = max(0.0, floor_shares(raw, frac))
        value = n * entry
        rt_cost = fee(market, "buy", value) + fee(market, "sell", n * target, n) + value * slip
        reward = n * (target - entry)
        cost_ratio = rt_cost / reward if reward > 0 else 9.99
        reason = ""
        if npos >= p["max_positions"]:
            reason = "max positions"
        elif n <= 0:
            reason = "size is zero"
        elif value < p["pos_value_min"]:
            reason = "position %.2f below minimum %.2f (limited by %s)" % (value, p["pos_value_min"], limit)
        elif cost_ratio > p["max_cost_to_reward"]:
            reason = "costs are %.0f%% of the reward at target (max %.0f%%)" % (
                cost_ratio * 100, p["max_cost_to_reward"] * 100)
        ok = reason == ""
        row.update(ok=ok, signal_date=bars[-1]["d"], ref_close=round(c, 2), est_entry=round(entry, 4),
                   atr=round(a, 3), stop=round(stop, 2), target=round(target, 2), shares=n,
                   est_cost=round(value, 2), risk_at_stop=round(n * risk, 2), limited_by=limit,
                   roundtrip_cost=round(rt_cost, 2), cost_to_reward=round(cost_ratio, 3), reason=reason)
        if ok:
            room -= value
            cash -= value + fee("TW", "buy", value) if market == "TW" else value
            npos += 1
        rows.append(row)
    return rows


# ---------------------------------------------------------------- settle

def settle(state, params, bars_for=fetch_bars):
    market = state["market"]
    p = resolve(market, params)
    frac, slip = p["fractional"], p["slippage"]
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
            px, action, limit_fill = None, None, False
            if k > 0 and b["o"] <= stop:
                px, action = b["o"], "賣出-停損"
            elif k > 0 and b["o"] >= target:
                px, action, limit_fill = b["o"], "賣出-停利", True
            elif b["l"] <= stop:
                px, action = stop, "賣出-停損"
            elif b["h"] >= target:
                px, action, limit_fill = target, "賣出-停利", True
            elif k + 1 >= p["time_stop_days"]:
                px, action = b["c"], "賣出-時間停損"
            if px is not None:
                if not limit_fill:
                    px *= (1 - slip)
                gross = round(sh * px, 2)
                f = fee(market, "sell", gross, sh)
                buy_fee = pos.get("buy_fee", fee(market, "buy", sh * entry))
                pnl = round(gross - f - sh * entry - buy_fee, 2)
                risk_total = sh * (entry - stop)
                ev = {"type": "exit", "action": action, "ticker": pos["ticker"], "date": b["d"],
                      "shares": sh, "price": round(px, 4), "amount": gross, "fees": f,
                      "realized_pnl": pnl, "r_multiple": round(pnl / risk_total, 2) if risk_total > 0 else None,
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
        px = b["o"] * (1 + slip)
        if px <= o["stop"] or b["o"] >= o["target"]:
            events.append({"type": "cancel", "ticker": sym, "date": b["d"], "ref": o.get("ref"),
                           "detail": "gap through stop/target at open %.2f" % b["o"]})
            continue
        sh = floor_shares(float(o["shares"]), frac)
        unit = 0.0001 if frac else 1
        while sh > 0 and sh * px + fee(market, "buy", sh * px) > cash:
            sh = floor_shares(sh - unit, frac) if sh > unit else 0
        if sh <= 0:
            events.append({"type": "cancel", "ticker": sym, "date": b["d"], "ref": o.get("ref"),
                           "detail": "not enough cash"})
            continue
        cost = round(sh * px, 2)
        bf = fee(market, "buy", cost)
        cash -= cost + bf
        events.append({"type": "fill", "action": "買進", "ticker": sym, "date": b["d"], "shares": sh,
                       "price": round(px, 4), "amount": cost, "fees": bf, "stop": o["stop"],
                       "target": o["target"], "ref": o.get("ref")})
        new_positions.append(({"ticker": sym, "shares": sh, "avg_cost": px, "stop": o["stop"],
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
    pl.add_argument("--params", default="{}", help="JSON rule parameters")
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
