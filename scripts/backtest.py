#!/usr/bin/env python3
"""개미탕 지수 과거 검증 (확장판)

검증 기간을 늘리기 위해 세 가지 공식을 같이 돌린다.
  현행   : 지금 사이트 공식 그대로 (개인 순매수가 있는 2016년 이후)
  정규화 : 시대에 따라 시장 크기가 달라지는 걸 보정 (2016년 이후)
  장기   : 개인 순매수 없이 하락·공포·빚투만으로 (신용잔고가 있는 2000년대부터)
보는 기간도 1~4주에서 12주, 6개월, 1년까지 늘리고,
"끓기 시작한 날"과 "식기 시작한 날"에 샀을 때를 하락장별로 따로 본다.
결과는 data/backtest_long.json
"""
import bisect
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

VERSION = 5
HORIZONS = (5, 10, 15, 20, 60, 120, 250)   # 1·2·3·4주, 12주, 6개월, 1년 (거래일)
H_NAMES = ("1주", "2주", "3주", "4주", "12주", "6개월", "1년")
BANDS = [("냉탕", 0, 25), ("온탕", 25, 50), ("끓는 중", 50, 75), ("개미탕", 75, 101)]
FG_START = "2021-01-04"
SECTORS = [
    ("반도체", ["005930.KS", "000660.KS"]), ("에너지", ["034020.KS", "009830.KS"]),
    ("자동차", ["005380.KS", "000270.KS"]), ("조선", ["329180.KS", "042660.KS", "010140.KS"]),
    ("방산", ["012450.KS", "079550.KS"]), ("2차전지", ["373220.KS", "086520.KQ", "006400.KS"]),
    ("인터넷", ["035420.KS", "035720.KS"]), ("바이오", ["207940.KS", "068270.KS"]),
    ("금융", ["105560.KS", "055550.KS"]),
]


# ───────── 데이터 ─────────
def _yahoo(sym, query):
    j = json.loads(u.http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?{query}&interval=1d"))
    res = j["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    rows = {}
    for t, hi, cl in zip(res["timestamp"], q["high"], q["close"]):
        if hi is None or cl is None or cl <= 0:
            continue
        d = (dt.datetime.fromtimestamp(t, dt.timezone.utc) + dt.timedelta(seconds=off)).date().isoformat()
        rows[d] = (float(hi), float(cl))
    out = sorted((d, h, c) for d, (h, c) in rows.items())
    if len(out) < 300:
        raise ValueError("행이 너무 적음")
    return out


def yahoo_full(sym):
    try:
        return _yahoo(sym, "period1=852076800&period2=4102444800")   # 1997년부터
    except Exception:  # noqa: BLE001
        return _yahoo(sym, "range=10y")


def fg_history():
    j = json.loads(u.http(f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{FG_START}",
                          {"Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com"}))
    out = {}
    for p in j["fear_and_greed_historical"]["data"]:
        out[dt.datetime.fromtimestamp(p["x"] / 1000, dt.timezone.utc).date().isoformat()] = float(p["y"])
    if len(out) < 200:
        raise ValueError("공포탐욕 과거 값이 너무 적음")
    return out


def investor_history(market):
    out = {}
    for page in range(1, 150):
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
    """금융투자협회 신용공여 잔고 추이를 3년씩 끊어서 받는다. 날짜→억원"""
    screen = "https://freesis.kofia.or.kr/stat/FreeSIS.do?parentDivId=MSIS10000000000000&serviceId=STATSCU0100000070"
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = {"User-Agent": u.UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    op.open(urllib.request.Request(screen, headers=base), timeout=30).read()

    sample = []

    def chunk(a, b):
        body = json.dumps({"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D", "tmpV45": a, "tmpV46": b,
                                        "OBJ_NM": "STATSCU0100000070BO"}}).encode()
        req = urllib.request.Request("https://freesis.kofia.or.kr/meta/getMetaDataList.do", data=body, headers={
            **base, "Content-Type": "application/json; charset=UTF-8", "Accept": "application/json, text/javascript, */*",
            "X-Requested-With": "XMLHttpRequest", "Referer": screen, "Origin": "https://freesis.kofia.or.kr"})
        last = None
        for k in range(4):
            try:
                j = json.loads(op.open(req, timeout=40).read().decode("utf-8", "replace"))
                rows = j.get("ds1") or next((v for v in j.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
                got = {}
                if rows:
                    sample.clear()
                    sample.extend(rows[:2] + rows[-2:])
                for r in rows:
                    d = str(r.get("TMPV1", "")).replace("/", "").replace("-", "")
                    try:
                        v = float(str(r.get("TMPV3", "")).replace(",", "")) + float(str(r.get("TMPV4", "")).replace(",", ""))
                    except ValueError:
                        continue
                    if len(d) == 8 and v > 0:
                        got[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = v
                return got
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (k + 1))
        raise RuntimeError(f"{a}~{b}: {last}")

    raw, failed = {}, []
    this_year = dt.datetime.now(u.KST).year
    for y in range(1998, this_year + 1, 3):
        try:
            raw.update(chunk(f"{y}0101", f"{min(y + 2, this_year)}1231"))
        except Exception as e:  # noqa: BLE001
            failed.append(str(e)[:80])
        time.sleep(0.4)
    if len(raw) < 300:
        raise ValueError(f"신용잔고 과거 값이 부족함({len(raw)}일) {failed[:2]}")
    try:
        return clean_scale(raw, anchor_eok), failed
    except ValueError as e:
        raise ValueError(f"{e} / 응답 예: {json.dumps(sample, ensure_ascii=False)[:230]}")


def clean_scale(raw, anchor_eok):
    """맨 끝의 덜 채워진 행을 버리고, 최근 실제 값(anchor)에 단위를 맞춘다."""
    ds = sorted(raw)
    top = max(raw[d] for d in ds[-8:])
    while ds and raw[ds[-1]] < 0.75 * top:   # 코스피만 먼저 올라온 날 등
        raw.pop(ds.pop())
    latest = raw[ds[-1]]
    factor = 10 ** round(math.log10(anchor_eok / latest))
    if not 0.8 <= latest * factor / anchor_eok <= 1.25:
        raise ValueError(f"신용잔고 단위를 맞추지 못함(최근 {latest}, 기준 {anchor_eok})")
    return {d: v * factor for d, v in raw.items()}


# ───────── 도구 ─────────
def before(dates, d):
    """d 이전(미포함) 마지막 위치. 없으면 -1"""
    return bisect.bisect_left(dates, d) - 1


def stat(v):
    if not v:
        return None
    return [len(v), round(statistics.fmean(v), 1), round(100 * sum(1 for a in v if a > 0) / len(v)), round(min(v), 1)]


def main():
    errors, notes = {}, []

    def attempt(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors[name] = str(e)[:300]
            return None

    kospi = attempt("코스피", lambda: yahoo_full("^KS11"))
    nasdaq = attempt("나스닥", lambda: yahoo_full("^IXIC"))
    vix = attempt("VIX", lambda: yahoo_full("^VIX"))
    fg = attempt("공포탐욕", fg_history) or {}
    inv_k = attempt("개인:코스피", lambda: investor_history("KOSPI"))
    inv_q = attempt("개인:코스닥", lambda: investor_history("KOSDAQ"))
    anchor = u.load("store.json", {}).get("credit", {}).get("value", 265_000)
    cr = attempt("신용잔고", lambda: credit_history(anchor))
    credit, cr_failed = cr if cr else ({}, [])
    if cr_failed:
        notes.append(f"신용잔고 일부 구간 누락 {len(cr_failed)}건")
    if not credit:
        notes.append("신용잔고 과거 값을 못 받아 물린 개미는 개인 누적 순매수만으로 계산, 장기 공식은 생략")

    now = dt.datetime.now(u.KST).isoformat(timespec="seconds")
    if not (kospi and nasdaq and vix):
        u.save("backtest.json", {"ok": False, "v": VERSION, "at": now, "errors": errors})
        return 1

    kd = [r[0] for r in kospi]
    kcl = [r[2] for r in kospi]
    n = len(kospi)
    # 코스피 1년 고점
    peak, peak_date = [0.0] * n, [""] * n
    for i in range(n):
        start = (dt.date.fromisoformat(kd[i]) - dt.timedelta(days=365)).isoformat()
        j = max(0, i - 270)
        best, bd = 0.0, ""
        for k in range(j, i + 1):
            if kd[k] >= start and kospi[k][1] > best:
                best, bd = kospi[k][1], kd[k]
        peak[i], peak_date[i] = best, bd
    # 나스닥 1년 고점 대비
    nd = [r[0] for r in nasdaq]
    ndd = [0.0] * len(nasdaq)
    for i in range(len(nasdaq)):
        start = (dt.date.fromisoformat(nd[i]) - dt.timedelta(days=365)).isoformat()
        hi = max(nasdaq[k][1] for k in range(max(0, i - 270), i + 1) if nd[k] >= start)
        ndd[i] = nasdaq[i][2] / hi - 1
    # 공포: CNN 값이 없는 기간은 VIX로 대신한다(겹치는 기간의 분포를 맞춰서 환산)
    vd = [r[0] for r in vix]
    vcl = [r[2] for r in vix]
    fgd = sorted(fg)
    pairs = []
    for d in fgd:
        k = before(vd, d)
        if k >= 0:
            pairs.append((vcl[k], 100 - fg[d]))
    if len(pairs) > 200:
        vs = sorted(p[0] for p in pairs)
        fs = sorted(p[1] for p in pairs)

        def fear_proxy(v):
            pos = bisect.bisect_left(vs, v)
            return fs[min(pos, len(fs) - 1)]
        mv, mf = statistics.fmean(p[0] for p in pairs), statistics.fmean(p[1] for p in pairs)
        cov = sum((a - mv) * (b - mf) for a, b in pairs)
        corr = cov / math.sqrt(sum((a - mv) ** 2 for a, _ in pairs) * sum((b - mf) ** 2 for _, b in pairs))
    else:
        pts = [(10, 0), (13, 20), (17, 50), (22, 70), (28, 85), (40, 100)]

        def fear_proxy(v):
            if v <= pts[0][0]:
                return 0.0
            for (a, fa), (b, fb) in zip(pts, pts[1:]):
                if v <= b:
                    return fa + (fb - fa) * (v - a) / (b - a)
            return 100.0
        corr = None

    # 개인 누적 순매수
    inv_ok = bool(inv_k and inv_q)
    if inv_ok:
        idates = sorted(set(inv_k) & set(inv_q))
        acc, ipref = 0.0, []
        for d in idates:
            acc += inv_k[d] + inv_q[d]
            ipref.append(acc)
        inv_min = idates[0]
    crd = sorted(credit)
    crv = [credit[d] for d in crd]

    # 날짜별 재료
    rows = []
    trapped_hist, cred_hist = [], []
    for i in range(n):
        d = kd[i]
        jn = before(nd, d)
        jv = before(vd, d)
        if jn < 250 or jv < 0 or i < 250:
            continue
        k_dd = kcl[i] / peak[i] - 1
        proxy = fear_proxy(vcl[jv])
        jf = before(fgd, d) if fgd else -1
        cnn = 100 - fg[fgd[jf]] if jf >= 0 and (dt.date.fromisoformat(d) - dt.date.fromisoformat(fgd[jf])).days <= 10 else None
        base = u.W_FIRE[0] * u.clip01(-k_dd / u.FULL_KOSPI_DROP) * 100 + u.W_FIRE[2] * u.clip01(-ndd[jn] / u.FULL_NASDAQ_DROP) * 100
        fire_live = base + u.W_FIRE[1] * (cnn if cnn is not None else proxy)
        fire_proxy = base + u.W_FIRE[1] * proxy

        jc = before(crd, d)
        c = crv[jc] if jc >= 0 else None
        m_abs = m_norm = None
        if c is not None:
            m_abs = u.clip01((c - u.CREDIT_ZERO) / (u.CREDIT_FULL - u.CREDIT_ZERO)) * 100
            lo = max(0, jc - 750)
            if jc - lo >= 250:
                m_norm = u.clip01((c / max(crv[lo:jc + 1]) - 0.6) / 0.4) * 100

        t_abs = t_norm = None
        if inv_ok and peak_date[i] >= inv_min:
            a = before(idates, peak_date[i])
            b = bisect.bisect_right(idates, d) - 1
            if b >= 0:
                cum = max(0.0, ipref[b] - (ipref[a] if a >= 0 else 0.0))
                t_abs = u.clip01(cum / u.FULL_TRAPPED) * 100
                win = trapped_hist[-750:]
                t_norm = (100 * sum(1 for w in win if w <= cum) / len(win)) if len(win) >= 120 and cum > 0 else (0.0 if cum <= 0 else t_abs)
                trapped_hist.append(cum)

        def sc(fire, pot):
            return math.sqrt(max(fire, 0) * max(pot, 0)) if pot is not None else None

        if not crd:   # 신용잔고를 못 받았으면 개인 누적 순매수만으로
            s_now = sc(fire_live, t_abs)
            s_norm = sc(fire_live, t_norm)
        else:
            s_now = sc(fire_live, u.W_POT[0] * t_abs + u.W_POT[1] * m_abs) if t_abs is not None and m_abs is not None else None
            s_norm = sc(fire_live, u.W_POT[0] * t_norm + u.W_POT[1] * m_norm) if t_norm is not None and m_norm is not None else None
        s_long = sc(fire_proxy, m_norm)
        rows.append((i, d, s_now, s_norm, s_long))

    def fwd(i, h):
        return (kcl[i + h] / kcl[i] - 1) * 100 if i + h < n else None

    def analyse(col, label):
        sel = [(r[0], r[1], r[col]) for r in rows if r[col] is not None]
        if len(sel) < 200:
            return None
        res = {"name": label, "from": sel[0][1], "to": sel[-1][1], "days": len(sel), "bands": [], "all": None}

        def table(sub):
            return [stat([w for w in (fwd(i, h) for i, _, _ in sub) if w is not None]) for h in HORIZONS]
        for bn, lo, hi in BANDS:
            sub = [x for x in sel if lo <= x[2] < hi]
            res["bands"].append({"band": bn, "days": len(sub), "h": table(sub)})
        res["all"] = table(sel)

        # 하락장(개미탕 구간)별: 끓기 시작한 날 vs 식기 시작한 날
        eps, cur = [], None
        for i, d, s in sel:
            if s >= 75:
                if cur and i - cur["e"] <= 20:
                    cur["e"], cur["ed"], cur["n"] = i, d, cur["n"] + 1
                    cur["max"] = max(cur["max"], s)
                else:
                    cur = {"s": i, "sd": d, "e": i, "ed": d, "n": 1, "max": s}
                    eps.append(cur)
        ep_out, hs = [], (20, 60, 120, 250)
        for e in eps:
            if e["n"] < 3:
                continue
            x = e["e"] + 1
            ep_out.append({"start": e["sd"], "end": e["ed"], "days": e["n"], "max": round(e["max"]),
                           "drop_in": round((kcl[e["e"]] / kcl[e["s"]] - 1) * 100, 1),
                           "from_start": [None if fwd(e["s"], h) is None else round(fwd(e["s"], h), 1) for h in hs],
                           "from_exit": [None if x >= n or fwd(x, h) is None else round(fwd(x, h), 1) for h in hs]})
        res["episodes"] = ep_out
        res["episode_avg"] = {
            "from_start": [stat([e["from_start"][k] for e in ep_out if e["from_start"][k] is not None]) for k in range(4)],
            "from_exit": [stat([e["from_exit"][k] for e in ep_out if e["from_exit"][k] is not None]) for k in range(4)]}
        return res

    variants = [v for v in (analyse(2, "현행 공식"), analyse(3, "정규화 공식"), analyse(4, "장기 공식(개인 순매수 제외)")) if v]

    # 섹터: 정규화 공식 기준, 4주·12주
    sectors = []
    sel = [(r[0], r[1], r[3]) for r in rows if r[3] is not None]
    if len(sel) >= 200:
        for sname, syms in SECTORS:
            tabs = []
            for sym in syms:
                try:
                    rr = _yahoo(sym, "range=10y")
                    tabs.append(([x[0] for x in rr], [x[2] for x in rr]))
                except Exception as e:  # noqa: BLE001
                    errors["종목:" + sym] = str(e)[:120]
                time.sleep(0.4)
            if not tabs:
                continue

            def sfwd(d, h, tabs=tabs):
                v = []
                for ds, cs in tabs:
                    k = bisect.bisect_left(ds, d)
                    if k < len(ds) and ds[k] == d and k + h < len(ds):
                        v.append((cs[k + h] / cs[k] - 1) * 100)
                return statistics.fmean(v) if v else None
            item = {"sector": sname, "bands": []}
            for bn, lo, hi in BANDS + [("전체", 0, 101)]:
                sub = [x for x in sel if lo <= x[2] < hi]
                item["bands"].append({"band": bn, "h": [stat([w for w in (sfwd(d, h) for _, d, _ in sub) if w is not None])
                                                        for h in (20, 60)]})
            sectors.append(item)

    out = {"at": now, "v": VERSION, "horizons": H_NAMES, "stat_fields": ["일수", "평균%", "상승비율%", "최악%"],
           "episode_horizons": ["4주", "12주", "6개월", "1년"], "fear_proxy_corr": None if corr is None else round(corr, 2),
           "credit_from": crd[0] if crd else None, "investor_from": inv_min if inv_ok else None,
           "variants": variants, "sectors": sectors, "notes": notes, "errors": errors}
    os.makedirs(u.DATA, exist_ok=True)
    with open(os.path.join(u.DATA, "backtest_long.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    u.save("backtest.json", {"ok": bool(variants), "v": VERSION, "at": now, "errors": errors})
    print("과거 검증(확장) 완료:", [(v["name"], v["from"], v["days"]) for v in variants], errors or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
