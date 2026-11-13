#!/usr/bin/env python3
"""개미탕 지수 과거 검증

과거 매 거래일의 개미탕 지수를 지금 공식 그대로 계산하고,
점수 구간별로 "그날 코스피를 샀다면 1·2·3·4주 뒤 수익률"을 모아 data/backtest.json 에 쓴다.
update.py 가 일주일에 한 번 자동으로 호출한다.
"""
import datetime as dt
import http.cookiejar
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update as u  # noqa: E402

HORIZONS = (5, 10, 15, 20)          # 거래일 기준 1·2·3·4주
BANDS = [("냉탕 (0~25)", 0, 25), ("온탕 (25~50)", 25, 50), ("끓는 중 (50~75)", 50, 75), ("개미탕 (75~100)", 75, 101)]
FG_START = "2021-01-04"
VERSION = 3
SECTORS = [
    ("반도체", [("삼성전자", "005930.KS"), ("SK하이닉스", "000660.KS")]),
    ("에너지", [("두산에너빌리티", "034020.KS"), ("한화솔루션", "009830.KS")]),
    ("자동차", [("현대차", "005380.KS"), ("기아", "000270.KS")]),
    ("조선", [("HD현대중공업", "329180.KS"), ("한화오션", "042660.KS"), ("삼성중공업", "010140.KS")]),
    ("방산", [("한화에어로스페이스", "012450.KS"), ("LIG넥스원", "079550.KS")]),
    ("2차전지", [("LG에너지솔루션", "373220.KS"), ("에코프로", "086520.KQ"), ("삼성SDI", "006400.KS")]),
    ("인터넷", [("네이버", "035420.KS"), ("카카오", "035720.KS")]),
    ("바이오", [("삼성바이오로직스", "207940.KS"), ("셀트리온", "068270.KS")]),
    ("금융", [("KB금융", "105560.KS"), ("신한지주", "055550.KS")]),
]

def yahoo_long(sym):
    j = json.loads(u.http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=10y&interval=1d"))
    res = j["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    rows = {}
    for t, hi, cl in zip(res["timestamp"], q["high"], q["close"]):
        if hi is None or cl is None:
            continue
        d = (dt.datetime.fromtimestamp(t, dt.timezone.utc) + dt.timedelta(seconds=off)).date().isoformat()
        rows[d] = (float(hi), float(cl))
    return sorted((d, h, c) for d, (h, c) in rows.items())


def fg_history():
    j = json.loads(u.http(f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{FG_START}",
                          {"Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com"}))
    out = {}
    for p in j["fear_and_greed_historical"]["data"]:
        d = dt.datetime.fromtimestamp(p["x"] / 1000, dt.timezone.utc).date().isoformat()
        out[d] = float(p["y"])
    if len(out) < 200:
        raise ValueError("공포탐욕 과거 값이 너무 적음")
    return out


def investor_history(market):
    """다음 금융에서 가능한 만큼 과거로 거슬러 올라간다. 날짜→억원"""
    out = {}
    for page in range(1, 120):
        j = json.loads(u.http(f"https://finance.daum.net/api/investor/{market}/days?page={page}&perPage=100&pagination=true",
                              {"Referer": f"https://finance.daum.net/domestic/investors/{market}"}))
        got = {}
        for r in j.get("data") or []:
            v, d = r.get("individualStraightPurchasePrice"), str(r.get("date", ""))[:10]
            if v is not None and len(d) == 10:
                got[d] = v / 1e8
        if not got or all(d in out for d in got):
            break
        out.update(got)
        time.sleep(0.3)
    if len(out) < 300:
        raise ValueError(f"개인 순매수 과거 값이 부족함({len(out)}일)")
    return out


def credit_history(anchor_eok):
    """금융투자협회 신용공여 잔고 추이. 날짜→억원. anchor(최근 실제 값)로 단위를 맞춘다."""
    screen = "https://freesis.kofia.or.kr/stat/FreeSIS.do?parentDivId=MSIS10000000000000&serviceId=STATSCU0100000070"
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = {"User-Agent": u.UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    op.open(urllib.request.Request(screen, headers=base), timeout=30).read()
    body = json.dumps({"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D", "tmpV45": "20200101",
                                    "tmpV46": dt.datetime.now(u.KST).strftime("%Y%m%d"),
                                    "OBJ_NM": "STATSCU0100000070BO"}}).encode()
    req = urllib.request.Request("https://freesis.kofia.or.kr/meta/getMetaDataList.do", data=body, headers={
        **base, "Content-Type": "application/json; charset=UTF-8", "Accept": "application/json, text/javascript, */*",
        "X-Requested-With": "XMLHttpRequest", "Referer": screen, "Origin": "https://freesis.kofia.or.kr"})
    j = json.loads(op.open(req, timeout=40).read().decode("utf-8", "replace"))
    rows = j.get("ds1") or next((v for v in j.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
    raw = {}
    for r in rows:
        d = str(r.get("TMPV1", "")).replace("/", "").replace("-", "")
        try:
            v = float(str(r.get("TMPV3", "")).replace(",", "")) + float(str(r.get("TMPV4", "")).replace(",", ""))
        except ValueError:
            continue
        if len(d) == 8 and v > 0:
            raw[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = v
    if len(raw) < 300:
        raise ValueError(f"신용잔고 과거 값이 부족함({len(raw)}일), 응답 키: {list(j)[:5]}, 첫 행: {str(rows[:1])[:200]}")
    latest = raw[max(raw)]
    factor = 10 ** round(math.log10(anchor_eok / latest))
    if not 0.8 <= latest * factor / anchor_eok <= 1.25:
        raise ValueError(f"신용잔고 단위를 맞추지 못함(최근 {latest}, 기준 {anchor_eok})")
    return {d: v * factor for d, v in raw.items()}


def asof(series_dates, series_vals, d):
    """d 이전(미포함) 가장 최근 값"""
    lo, hi = 0, len(series_dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if series_dates[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    return series_vals[lo - 1] if lo else None


def main():
    notes, errors = [], {}

    def attempt(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors[name] = str(e)[:400]
            return None

    kospi = attempt("코스피", lambda: yahoo_long("^KS11"))
    nasdaq = attempt("나스닥", lambda: yahoo_long("^IXIC"))
    fg = attempt("공포탐욕", fg_history)
    inv_k = attempt("개인:코스피", lambda: investor_history("KOSPI"))
    inv_q = attempt("개인:코스닥", lambda: investor_history("KOSDAQ"))
    store = u.load("store.json", {})
    anchor = store.get("credit", {}).get("value", 265_000)
    credit = attempt("신용잔고", lambda: credit_history(anchor))

    out = {"at": dt.datetime.now(u.KST).isoformat(timespec="seconds"), "errors": errors, "ok": False}
    if not (kospi and nasdaq and fg and inv_k and inv_q):
        u.save("backtest.json", out)
        print("과거 검증: 필수 데이터 부족", errors)
        return 1
    if not credit:
        notes.append("신용잔고 과거 값을 받지 못해 물린 개미는 개인 누적 순매수만으로 계산")

    inv_dates = sorted(set(inv_k) & set(inv_q))
    inv_min = inv_dates[0]
    pref, acc = {}, 0.0
    for d in inv_dates:
        acc += inv_k[d] + inv_q[d]
        pref[d] = acc
    inv_d = inv_dates
    inv_p = [pref[d] for d in inv_dates]

    n_d = [r[0] for r in nasdaq]
    fg_d = sorted(fg)
    fg_v = [fg[d] for d in fg_d]
    cr_d = sorted(credit) if credit else []
    cr_v = [credit[d] for d in cr_d]

    days = []
    kd = [r[0] for r in kospi]
    for i, (d, hi, cl) in enumerate(kospi):
        if d < fg_d[0]:
            continue
        start = (dt.date.fromisoformat(d) - dt.timedelta(days=365)).isoformat()
        win = [r for r in kospi[max(0, i - 270):i + 1] if r[0] >= start]
        peak_date, peak = max(((r[0], r[1]) for r in win), key=lambda x: x[1])
        if peak_date < inv_min:
            continue
        # 나스닥: 전날까지의 1년 고점 대비
        j = 0
        lo, hi2 = 0, len(n_d)
        while lo < hi2:
            mid = (lo + hi2) // 2
            if n_d[mid] < d:
                lo = mid + 1
            else:
                hi2 = mid
        j = lo - 1
        if j < 200:
            continue
        nwin = [r for r in nasdaq[max(0, j - 270):j + 1] if r[0] >= start]
        n_dd = nasdaq[j][2] / max(r[1] for r in nwin) - 1
        f = asof(fg_d, fg_v, d)
        if f is None:
            continue
        before_peak = asof(inv_d, inv_p, peak_date) or 0.0
        upto = asof(inv_d, inv_p, (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat())
        if upto is None:
            continue
        cum = upto - before_peak
        fire = (u.W_FIRE[0] * u.clip01(-(cl / peak - 1) / u.FULL_KOSPI_DROP) * 100 + u.W_FIRE[1] * (100 - f)
                + u.W_FIRE[2] * u.clip01(-n_dd / u.FULL_NASDAQ_DROP) * 100)
        s_trap = u.clip01(cum / u.FULL_TRAPPED) * 100
        c = asof(cr_d, cr_v, d) if credit else None
        if c is not None:
            pot = u.W_POT[0] * s_trap + u.W_POT[1] * u.clip01((c - u.CREDIT_ZERO) / (u.CREDIT_FULL - u.CREDIT_ZERO)) * 100
        else:
            pot = s_trap
        score = math.sqrt(max(fire, 0) * max(pot, 0))
        fwd = [(kospi[i + h][2] / cl - 1) * 100 if i + h < len(kospi) else None for h in HORIZONS]
        days.append({"date": d, "score": score, "fwd": fwd, "i": i})

    if len(days) < 200:
        out["errors"]["계산"] = f"계산 가능한 날이 너무 적음({len(days)}일)"
        u.save("backtest.json", out)
        return 1

    def stats(sel):
        cols = []
        for hi_, _ in enumerate(HORIZONS):
            v = [x["fwd"][hi_] for x in sel if x["fwd"][hi_] is not None]
            if not v:
                cols.append(None)
                continue
            cols.append({"n": len(v), "mean": round(statistics.fmean(v), 2), "median": round(statistics.median(v), 2),
                         "win": round(100 * sum(1 for a in v if a > 0) / len(v)), "worst": round(min(v), 1),
                         "best": round(max(v), 1)})
        episodes, last = 0, None
        for x in sel:
            if last is None or x["i"] - last > 20:
                episodes += 1
            last = x["i"]
        return {"days": len(sel), "episodes": episodes, "h": cols}

    bands = []
    for name, lo, hi_ in BANDS:
        sel = [x for x in days if lo <= x["score"] < hi_]
        bands.append({"name": name, "lo": lo, **stats(sel)})
    out.update({"ok": True, "from": days[0]["date"], "to": days[-1]["date"], "total_days": len(days),
                "bands": bands, "all": stats(days), "notes": notes, "credit_used": bool(credit),
                "investor_from": inv_min,
                "monthly": [{"date": x["date"], "score": round(x["score"], 1)} for x in days[::5]]})
    out["v"] = VERSION
    out.pop("monthly", None)

    # 섹터별: 같은 날 그 섹터 대장주들을 똑같은 비중으로 샀다면
    groups = [(b[0], [x for x in days if b[1] <= x["score"] < b[2]]) for b in BANDS] + [("전체", days)]

    def summarize(fwd_of):
        res = []
        for bname, sel in groups:
            cols = []
            for hi_ in range(len(HORIZONS)):
                v = [w for w in (fwd_of(x["date"], hi_) for x in sel) if w is not None]
                cols.append({"n": len(v), "mean": round(statistics.fmean(v), 1),
                             "win": round(100 * sum(1 for a in v if a > 0) / len(v)),
                             "worst": round(min(v), 1)} if v else None)
            res.append({"name": bname, "h": cols})
        return res

    sectors = []
    for sname, members in SECTORS:
        tables = []
        for name, sym in members:
            try:
                rows = yahoo_long(sym)
                pos = {r[0]: k for k, r in enumerate(rows)}
                tables.append((name, rows, pos))
            except Exception as e:  # noqa: BLE001
                errors["종목:" + name] = str(e)[:200]
            time.sleep(0.5)
        if not tables:
            continue

        def one(rows, pos, d, hi_):
            k = pos.get(d)
            h = HORIZONS[hi_]
            return (rows[k + h][2] / rows[k][2] - 1) * 100 if k is not None and k + h < len(rows) else None

        def sector_fwd(d, hi_, tables=tables):
            v = [w for w in (one(r, p, d, hi_) for _, r, p in tables) if w is not None]
            return statistics.fmean(v) if v else None

        sectors.append({"sector": sname, "bands": summarize(sector_fwd),
                        "stocks": [{"name": n, "bands": summarize(lambda d, hi_, r=r, p=p: one(r, p, d, hi_))}
                                   for n, r, p in tables]})
    u.save("backtest_stocks.json", {"at": out["at"], "from": out["from"], "to": out["to"],
                                    "sectors": sectors, "errors": errors})

    u.save("backtest.json", out)
    print(f"과거 검증 완료: {out['from']} ~ {out['to']}, {len(days)}일", errors or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
