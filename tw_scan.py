#!/usr/bin/env python3
"""台股日線掃描器 (Taiwan daily scanner). Standard library only.

  python3 tw_scan.py universe                 # 建立上市櫃股票清單 -> tw/universe.txt
  python3 tw_scan.py daily                    # 掃描全市場 -> tw/watchlist.json
  python3 tw_scan.py daily --symbols 2330,2317,6488
  python3 tw_scan.py daily --min-price 10 --min-lots 1000 --show 30

Data: 公司清單來自證交所 / 櫃買中心 OpenAPI; 日線收盤來自 Yahoo spark
(免費、非官方、可能延遲; 上市 .TW、上櫃 .TWO). 只用收盤價, 規則與美股版相同:
  breakout_20d    今天收盤高於前 20 個交易日的每一個收盤
  bull_flag       前段 10 日漲幅 > 10%, 最近 5 日收盤收斂在 5% 內
  reclaim_ma20    前一日收盤在 20 日均線下, 今天站上
  oversold        RSI14 (Wilder) < 30
  oversold_bounce 昨天 RSI14 < 30, 今天收紅
  ma_bull_align   5 日 > 10 日 > 20 日均線, 且收盤在 5 日均線上 (台股常用的多頭排列)
符合規則的圖表不是買進建議。
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (tw-scanner)"
TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
SPARK_MAX = 20
MIN_BARS = 30
TPE = datetime.timezone(datetime.timedelta(hours=8))
CLOSE_TIME = datetime.time(13, 30)
OUT_DIR = "tw"


class ScanError(Exception):
    pass


def http_get(url, timeout=25, tries=4):
    """GET with retries: connection resets, timeouts, 429 and 5xx wait 2, 4, 8 seconds
    and try again. Other 4xx answers are final."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                e.close()
            except Exception:
                pass
            if code != 429 and code < 500:
                raise
            err = e
        except (urllib.error.URLError, OSError) as e:
            err = e
        if attempt == tries - 1:
            raise err
        time.sleep(2 * 2 ** attempt)


def yahoo_get(path):
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path, tries=2)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                e.close()
                saw_429 = saw_429 or e.code == 429
                continue
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
        if attempt == 0 and saw_429:
            time.sleep(5)
            continue
        break
    raise ScanError("Yahoo answered %s" % last)


# ---------------------------------------------------------------- universe

def build_universe():
    rows = {}
    for url, code_k, name_k, suffix in (
            (TWSE_URL, "公司代號", "公司簡稱", ".TW"),
            (TPEX_URL, "SecuritiesCompanyCode", "CompanyAbbreviation", ".TWO")):
        try:
            docs = json.loads(http_get(url))
        except (ValueError, urllib.error.URLError, OSError) as e:
            raise ScanError("cannot read %s: %s" % (url, e))
        for d in docs:
            code = str(d.get(code_k, "")).strip()
            if re.fullmatch(r"[1-9]\d{3}", code):  # 4 位數普通股, 排除權證/ETF/特別代碼
                rows[code] = (d.get(name_k, "").strip(), suffix)
    if not rows:
        raise ScanError("the company lists came back empty")
    return rows


def cmd_universe(a):
    rows = build_universe()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "universe.txt")
    with open(path, "w", encoding="utf-8") as f:
        for code in sorted(rows):
            name, suf = rows[code]
            f.write("%s|%s|%s\n" % (code, name, suf))
    twse = sum(1 for v in rows.values() if v[1] == ".TW")
    print("上市 %d 檔 + 上櫃 %d 檔 = %d 檔普通股 (4 位數代號). 已寫入 %s" % (
        twse, len(rows) - twse, len(rows), path))
    return 0


def read_universe():
    path = os.path.join(OUT_DIR, "universe.txt")
    if not os.path.exists(path):
        cmd_universe(argparse.Namespace())
    rows = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("|")
            if len(p) == 3:
                rows[p[0]] = (p[1], p[2])
    return rows


# ---------------------------------------------------------------- math

def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = [max(0.0, c[i] - c[i - 1]) for i in range(1, len(c))]
    lo = [max(0.0, c[i - 1] - c[i]) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def sma(c, n, end=None):
    seg = c[:end] if end else c
    return sum(seg[-n:]) / n


def find_setups(c):
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20, ma20_prev = sma(c, 20), sma(c[:-1], 20)
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = rsi(c), rsi(c[:-1])
    ma5, ma10 = sma(c, 5), sma(c, 10)
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    if ma5 > ma10 > ma20 and last > ma5:
        tags.append("ma_bull_align")
    return tags, r


LABEL = {"breakout_20d": "20日新高突破", "bull_flag": "多頭旗形", "reclaim_ma20": "站上20日線",
         "oversold": "超賣(RSI<30)", "oversold_bounce": "超賣反彈", "ma_bull_align": "均線多頭排列"}


def daily_bars(series, now):
    """[(date, close)] complete bars only; today's bar dropped until 13:30 Taipei."""
    today = now.date()
    done = now.time() >= CLOSE_TIME and now.weekday() < 5
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = datetime.datetime.fromtimestamp(ts, TPE).date()
        if d > today or (d == today and not done):
            continue
        out[d] = float(c)
    return sorted(out.items())


def fetch_spark(codes, uni, pause, quiet):
    data, failed = {}, {}
    batches = [codes[i:i + SPARK_MAX] for i in range(0, len(codes), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k:
            time.sleep(pause)
        ymap = {c + uni[c][1]: c for c in batch}
        path = "/v7/finance/spark?symbols=%s&range=3mo&interval=1d" % ",".join(ymap)
        try:
            doc = json.loads(yahoo_get(path))
            res = (doc.get("spark") or {}).get("result") or []
        except (ScanError, ValueError) as e:
            for c in batch:
                failed[c] = str(e)
            continue
        got = {}
        for item in res:
            resp = item.get("response") or []
            if not resp:
                continue
            code = ymap.get((item.get("symbol") or "").upper())
            if not code:
                continue
            r0 = resp[0]
            q = ((r0.get("indicators") or {}).get("quote") or [{}])[0] or {}
            t = r0.get("timestamp") or []
            cl = q.get("close") or []
            got[code] = {"t": t, "c": list(cl) + [None] * (len(t) - len(cl)), "meta": r0.get("meta") or {}}
        for c in batch:
            if c in got:
                data[c] = got[c]
            else:
                failed[c] = "no data from Yahoo"
        if not quiet and (k + 1) % 25 == 0:
            print("  spark %d of %d requests" % (k + 1, len(batches)), file=sys.stderr)
    return data, failed


def cmd_daily(a):
    uni = read_universe()
    if a.symbols:
        want = [s.strip().upper().split(".")[0] for s in a.symbols.split(",") if s.strip()]
        missing = [s for s in want if s not in uni]
        if missing:
            print("不在清單內, 略過: " + ", ".join(missing), file=sys.stderr)
        codes = [s for s in want if s in uni]
    else:
        codes = sorted(uni)
    if a.limit:
        codes = codes[:a.limit]
    if not codes:
        print("沒有可掃描的代號.")
        return 1
    now = datetime.datetime.now(TPE)
    if not a.quiet:
        print("抓取 %d 檔日線 (%d 次請求)..." % (len(codes), -(-len(codes) // SPARK_MAX)), file=sys.stderr)
    data, failed = fetch_spark(codes, uni, a.pause, a.quiet)

    counts = {"requested": len(codes), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    hits, dates = [], {}
    for code in codes:
        s = data.get(code)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_bars(s, now)
        meta = s["meta"]
        price = meta.get("regularMarketPrice") or (bars[-1][1] if bars else None)
        vol = meta.get("regularMarketVolume")  # 股; 1 張 = 1000 股
        lots = (vol or 0) / 1000.0
        if price is None or price < a.min_price or lots < a.min_lots:
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        dates[bars[-1][0]] = dates.get(bars[-1][0], 0) + 1
        closes = [c for _, c in bars]
        tags, r = find_setups(closes)
        if tags:
            hits.append({"symbol": code, "yahoo": code + uni[code][1], "name": uni[code][0],
                         "market": "上市" if uni[code][1] == ".TW" else "上櫃", "setups": tags,
                         "price": round(closes[-1], 2), "close_date": bars[-1][0].isoformat(),
                         "rsi": round(r, 1) if r is not None else None, "lots": int(lots),
                         "turnover_twd": int(closes[-1] * (vol or 0))})
    hits.sort(key=lambda h: (-h["turnover_twd"], h["symbol"]))
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    by = {k: sum(1 for h in hits if k in h["setups"]) for k in LABEL}
    counts["hits"] = len(hits)

    if not counts["scanned"]:
        print("沒有任何圖表可掃描 (取得 %d 檔, 失敗 %d 檔)." % (counts["fetched"], len(failed)))
        return 1
    doc = {"mode": "daily", "market": "TW", "as_of": now.isoformat(timespec="minutes"),
           "close_date": last_date.isoformat() if last_date else None,
           "universe": len(uni), "scanned": counts["scanned"], "failed": len(failed), "hits": hits}
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print("台股日線掃描, %s (台北時間), 收盤資料至 %s (Yahoo spark, 僅日收盤價)." % (
            now.strftime("%a %Y-%m-%d %H:%M"), last_date))
        print("取得 %s 檔中的 %s 檔圖表 (%s 檔失敗或無資料); %s 檔通過流動性篩選 (股價 >= %g 元且成交量 >= %d 張); "
              "歷史不足 30 天 %d 檔; 實際掃描 %s 檔." % (
                  format(counts["requested"], ","), format(counts["fetched"], ","), format(len(failed), ","),
                  format(counts["fetched"] - counts["illiquid"], ","), a.min_price, a.min_lots,
                  counts["short"], format(counts["scanned"], ",")))
        print("%d 檔符合某個規則: %s" % (len(hits), ", ".join("%s %d" % (LABEL[k], n) for k, n in by.items())))
        print("依成交金額排序:")
        for h in hits[:a.show]:
            print("  %-5s %-8s %-3s %9.2f 元  RSI %5s  %6s 張  %s" % (
                h["symbol"], h["name"], h["market"], h["price"], h["rsi"], format(h["lots"], ","),
                "、".join(LABEL[t] for t in h["setups"])))
        if len(hits) > a.show:
            print("  ... 其餘 %d 檔在 watchlist 內 (--show N 顯示更多)." % (len(hits) - a.show))
    if not a.no_write:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "watchlist.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        if not a.json:
            print("已寫入 %s (%d 檔)." % (path, len(hits)))
    if not a.json:
        print("這是符合規則的圖表清單, 不是買進清單. 價格移動後訊號就過時了.")
    return 0 if hits else 1


def main():
    p = argparse.ArgumentParser(description="台股日線掃描器")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("universe")
    d = sub.add_parser("daily")
    d.add_argument("--symbols", help="逗號分隔, 例如 2330,2317")
    d.add_argument("--limit", type=int, default=0)
    d.add_argument("--min-price", type=float, default=10.0)
    d.add_argument("--min-lots", type=int, default=1000, help="最近一日成交量 (張)")
    d.add_argument("--pause", type=float, default=0.3)
    d.add_argument("--show", type=int, default=20)
    d.add_argument("--json", action="store_true")
    d.add_argument("--no-write", action="store_true")
    d.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    try:
        return cmd_universe(a) if a.cmd == "universe" else cmd_daily(a)
    except ScanError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
