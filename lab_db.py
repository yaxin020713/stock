#!/usr/bin/env python3
"""trading_lab.db: a local SQLite record of the paper-trading simulation. Standard library only.

  python3 lab_db.py init            # create tables and seed accounts, costs, rule versions
  python3 lab_db.py summary         # latest equity per market and closed-trade stats
  python3 lab_db.py add-nav DATE MARKET CASH EQUITY RETURN_PCT POSITIONS DATA_DATE [NOTE]
  python3 lab_db.py add-trade JSON  # one row as a JSON object using the trades column names

The cloud routines write their ledger to Notion (they cannot push to this repository), so this database
is a mirror: it is filled when a session copies the Notion rows into it, not by the routines themselves.
"""
import json
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_lab.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  market TEXT PRIMARY KEY, currency TEXT, start_capital REAL, mode TEXT,
  max_positions INTEGER, pos_value_min REAL, pos_value_max REAL, start_date TEXT, end_date TEXT);
CREATE TABLE IF NOT EXISTS cost_model (
  market TEXT, item TEXT, value REAL, note TEXT, PRIMARY KEY (market, item));
CREATE TABLE IF NOT EXISTS rule_versions (
  version TEXT PRIMARY KEY, effective_date TEXT, params_json TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, ticker TEXT, action TEXT, status TEXT,
  decision_date TEXT, fill_date TEXT, signal_date TEXT, shares REAL, price REAL, amount REAL, fees REAL,
  stop REAL, target REAL, realized_pnl REAL, r_multiple REAL, held_sessions INTEGER,
  setup TEXT, sources TEXT, source_says TEXT, reason TEXT, rule_version TEXT, notion_url TEXT);
CREATE TABLE IF NOT EXISTS positions (
  market TEXT, ticker TEXT, shares REAL, avg_cost REAL, stop REAL, target REAL, open_date TEXT,
  last_price REAL, unrealized_pnl REAL, status TEXT, PRIMARY KEY (market, ticker, open_date));
CREATE TABLE IF NOT EXISTS nav_daily (
  date TEXT, market TEXT, cash REAL, equity REAL, return_pct REAL, positions INTEGER, data_date TEXT, note TEXT,
  PRIMARY KEY (date, market));
CREATE TABLE IF NOT EXISTS reviews (
  date TEXT, kind TEXT, rule_version TEXT, summary TEXT, notion_url TEXT);
"""

SEED = {
    "accounts": [
        ("TW", "TWD", 10000, "odd-lot (whole shares)", 2, 4000, 5000, "2026-11-19", "2026-12-19"),
        ("US", "USD", 300, "fractional shares (4 decimals)", 3, 90, 135, "2026-11-19", "2026-12-19"),
    ],
    "cost_model": [
        ("TW", "buy_fee_rate", 0.001425, "no discount"), ("TW", "sell_fee_rate", 0.001425, "no discount"),
        ("TW", "min_fee", 20, "NT$ per order"), ("TW", "sale_tax_rate", 0.003, "sell side"),
        ("TW", "slippage", 0.0015, "assumption, odd-lot spread"),
        ("US", "commission", 0, "commission-free broker assumed"),
        ("US", "sec_fee_rate", 0.0000278, "estimate, sell side, min $0.01"),
        ("US", "finra_taf_per_share", 0.000166, "estimate, sell side, min $0.01"),
        ("US", "slippage", 0.0005, "assumption, fractional execution"),
    ],
}

PARAMS_V2 = {"min_rsi": 45, "max_rsi": 75, "atr_mult": 1.5, "target_r": 2.0, "time_stop_days": 10,
             "min_stop_pct": 0.02, "max_cost_to_reward": 0.25,
             "TW": {"risk_pct": 0.03, "pos_value_min": 4000, "pos_value_max": 5000, "max_positions": 2,
                    "max_exposure": 0.95, "max_new_per_day": 1, "fractional": False, "slippage": 0.0015},
             "US": {"risk_pct": 0.03, "pos_value_min": 90, "pos_value_max": 135, "max_positions": 3,
                    "max_exposure": 0.9, "max_new_per_day": 1, "fractional": True, "slippage": 0.0005}}


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    con = connect()
    con.executescript(SCHEMA)
    con.executemany("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?)", SEED["accounts"])
    con.executemany("INSERT OR REPLACE INTO cost_model VALUES (?,?,?,?)", SEED["cost_model"])
    con.execute("INSERT OR REPLACE INTO rule_versions VALUES (?,?,?,?)",
                ("v1", "2026-11-19", "{}", "first version, capital 100,000 each, risk 1%; superseded the same day"))
    con.execute("INSERT OR REPLACE INTO rule_versions VALUES (?,?,?,?)",
                ("v2", "2026-11-19", json.dumps(PARAMS_V2, ensure_ascii=False),
                 "small account: TW NT$10,000 odd-lot, US US$300 fractional, real fees and tax, slippage, cost check"))
    # the one order v1 placed before the reset (cancelled, kept for the record)
    con.execute("DELETE FROM trades WHERE market='TW' AND ticker='2412.TW' AND status='取消' AND rule_version='v1'")
    con.execute("INSERT INTO trades (market,ticker,action,status,decision_date,signal_date,shares,price,amount,stop,target,"
                "setup,sources,source_says,reason,rule_version,notion_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("TW", "2412.TW", "買進", "取消", "2026-11-19", "2026-11-19", 89, 166.0, 14774, 160.38, 177.23,
                 "新高突破", "台股掃描器日線 2026-11-19; sim.py plan(ATR14)",
                 "scan: breakout_20d + ma_bull_align, RSI 72.8, 28,876 lots", "cancelled: capital reset to NT$10,000 (v2)",
                 "v1", "https://app.notion.com/p/41ddf88eee9781c19363c5e8a05ab5f8"))
    for market, cap in (("TW", 10000), ("US", 300)):
        con.execute("INSERT OR REPLACE INTO nav_daily VALUES (?,?,?,?,?,?,?,?)",
                    ("2026-11-19", market, cap, cap, 0, 0, "start", "start capital (v2 reset)"))
    con.commit()
    print("initialised", DB)


def summary():
    con = connect()
    for market in ("TW", "US"):
        row = con.execute("SELECT * FROM nav_daily WHERE market=? ORDER BY date DESC LIMIT 1", (market,)).fetchone()
        acc = con.execute("SELECT * FROM accounts WHERE market=?", (market,)).fetchone()
        if not row:
            continue
        closed = con.execute("SELECT realized_pnl, r_multiple FROM trades WHERE market=? AND action LIKE '賣出%' "
                             "AND status='已成交'", (market,)).fetchall()
        wins = sum(1 for r in closed if (r["realized_pnl"] or 0) > 0)
        avg_r = sum((r["r_multiple"] or 0) for r in closed) / len(closed) if closed else 0
        print("%s %s: equity %.2f (%+.2f%%) start %.0f, closed %d, wins %d, avg R %.2f" % (
            market, acc["currency"], row["equity"], row["return_pct"], acc["start_capital"], len(closed), wins, avg_r))


def add_nav(args):
    date, market, cash, equity, ret, npos, data_date = args[:7]
    note = args[7] if len(args) > 7 else ""
    con = connect()
    con.execute("INSERT OR REPLACE INTO nav_daily VALUES (?,?,?,?,?,?,?,?)",
                (date, market, float(cash), float(equity), float(ret), int(npos), data_date, note))
    con.commit()


def add_trade(text):
    row = json.loads(text)
    con = connect()
    cols = ",".join(row)
    con.execute("INSERT INTO trades (%s) VALUES (%s)" % (cols, ",".join("?" * len(row))), list(row.values()))
    con.commit()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "init":
        init()
    elif cmd == "summary":
        summary()
    elif cmd == "add-nav":
        add_nav(sys.argv[2:])
    elif cmd == "add-trade":
        add_trade(sys.argv[2])
    else:
        print(__doc__)
