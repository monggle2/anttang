#!/usr/bin/env python3
"""개미탕 지수 자동 갱신 (파이썬 기본 기능만 사용, 설치할 것 없음)

지수 = √(불 세기 × 물린 개미)
  불 세기   = 코스피 1년 고점 대비 하락 50% + 공포(100−공포탐욕) 30% + 나스닥 고점 대비 하락 20%
  물린 개미 = 고점 이후 개인 누적 순매수 60% + 신용융자 잔고 40%

출처
  지수           Yahoo Finance 차트 API (코스피·코스닥은 실패 시 네이버 금융)
  신용융자 잔고   금융투자협회 FreeSIS 메인 화면
  공포탐욕지수    CNN
  개인 순매수     네이버 금융 투자자별 매매동향
어느 출처가 실패해도 직전 값을 유지하고, 결과는 data/status.json 에 남긴다.
"""
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# ───────── 설정 (숫자만 바꾸면 됨) ─────────
FULL_KOSPI_DROP = 0.40      # 코스피 고점 대비 −40% 이상이면 만점
FULL_NASDAQ_DROP = 0.20     # 나스닥 고점 대비 −20% 이상이면 만점
FULL_TRAPPED = 200_000      # 개인 누적 순매수 20조(억원 단위) 이상이면 만점
CREDIT_ZERO, CREDIT_FULL = 150_000, 300_000   # 신용잔고 15조 이하 0점, 30조 이상 만점 (억원)
W_FIRE = (0.5, 0.3, 0.2)    # 코스피 손실, 공포, 나스닥 손실
W_POT = (0.6, 0.4)          # 물린 물량, 빚투
OVERFLOW_DROP = -3.0        # 코스피 하루 −3% 이하면 넘침
# 첫 실행 전 기준값: 2026-11-12까지의 개인 누적 순매수 추정치(억원). 실제 수집이 되면 자동으로 대체됨
SEED = {"base_date": "2026-11-12", "base_cum": 400_000}
# ─────────────────────────────────────────

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
INDICES = [("코스피", "^KS11", "KOSPI"), ("코스닥", "^KQ11", "KOSDAQ"), ("나스닥", "^IXIC", None),
           ("닛케이", "^N225", None), ("항셍", "^HSI", None), ("상하이종합", "000001.SS", None)]


def http(url, headers=None, tries=3, timeout=25):
    h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8", "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                raw = r.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp949", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def num(s):
    s = (s or "").replace(",", "").replace("−", "-").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def clip01(x):
    return max(0.0, min(1.0, x))


# ───────── 지수 ─────────
def yahoo(sym):
    err = None
    for host in ("query1", "query2"):
        try:
            j = json.loads(http(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                                f"{urllib.parse.quote(sym)}?range=1y&interval=1d"))
            res = j["chart"]["result"][0]
            off = res["meta"].get("gmtoffset", 0)
            q = res["indicators"]["quote"][0]
            rows = []
            for t, hi, cl in zip(res["timestamp"], q["high"], q["close"]):
                if hi is None or cl is None:
                    continue
                d = (dt.datetime.fromtimestamp(t, dt.timezone.utc) + dt.timedelta(seconds=off)).date().isoformat()
                rows.append((d, float(hi), float(cl)))
            if len(rows) < 30:
                raise ValueError("행이 너무 적음")
            return rows
        except Exception as e:  # noqa: BLE001
            err = e
    raise RuntimeError(f"yahoo {sym}: {err}")


def naver_index(code):
    today = dt.datetime.now(KST).date()
    txt = http("https://api.finance.naver.com/siseJson.naver?symbol=%s&requestType=1&startTime=%s&endTime=%s&timeframe=day"
               % (code, (today - dt.timedelta(days=400)).strftime("%Y%m%d"), today.strftime("%Y%m%d")),
               {"Referer": "https://finance.naver.com/"})
    rows = []
    for m in re.finditer(r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', txt):
        d = m.group(1)
        rows.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(m.group(3)), float(m.group(5))))
    if len(rows) < 30:
        raise ValueError("naver index: 행이 너무 적음")
    return rows


def index_rows(sym, naver_code):
    """[(날짜, 고가, 종가)] 과거→최근. 국내 지수는 네이버 우선."""
    order = ([lambda: naver_index(naver_code)] if naver_code else []) + [lambda: yahoo(sym)]
    err = None
    for f in order:
        try:
            rows = sorted(f())
            cutoff = (dt.datetime.now(KST).date() - dt.timedelta(days=366)).isoformat()
            return [r for r in rows if r[0] >= cutoff]
        except Exception as e:  # noqa: BLE001
            err = e
    raise RuntimeError(str(err))


# ───────── 신용융자 잔고 (금융투자협회) ─────────
def freesis_credit():
    """(기준일, 신용융자 잔고 억원). 메인 화면에 서버가 직접 넣어주는 값을 읽는다."""
    html = http("https://freesis.kofia.or.kr/stat/main.do")
    val = date = None
    for block in html.split("<li"):
        if "clickJisuMenu('OS0026')" not in block and 'clickJisuMenu("OS0026")' not in block:
            continue
        v = re.search(r'class="num1"[^>]*>\s*([\d,]+)', block)
        d = re.search(r'class="date"[^>]*>\s*(\d{1,2})/(\d{1,2})', block)
        if v:
            val, date = num(v.group(1)), d
            break
    if val is None:  # 화면 구조가 바뀐 경우: 글자 기준으로 한 번 더 시도
        text = re.sub(r"<[^>]+>", " ", html)
        m = re.search(r"신용융자\D{0,40}?(\d{1,2})/(\d{1,2})\D{0,40}?(\d{1,3}(?:,\d{3}){2,})", text)
        if not m:
            raise ValueError("신용융자 값을 찾지 못함")
        val, date = num(m.group(3)), m
    eok = val / 100.0  # 백만원 → 억원
    if not 30_000 <= eok <= 800_000:
        raise ValueError(f"신용융자 값이 범위를 벗어남: {eok}")
    today = dt.datetime.now(KST).date()
    if date:
        mo, da = int(date.group(1)), int(date.group(2))
        y = today.year - 1 if mo > today.month + 1 else today.year
        d = dt.date(y, mo, da).isoformat()
    else:
        d = today.isoformat()
    return d, round(eok)


# ───────── 공포탐욕지수 (CNN) ─────────
def fear_greed():
    j = json.loads(http("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                        {"Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com"}))
    s = float(j["fear_and_greed"]["score"])
    if not 0 <= s <= 100:
        raise ValueError("범위 밖")
    return round(s, 1)


# ───────── 개인 순매수 (네이버 금융) ─────────
class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell = [], None, None

    def _end_cell(self):
        if self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def _end_row(self):
        self._end_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._end_row()
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._end_cell()
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._end_cell()
        elif tag in ("tr", "table"):
            self._end_row()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def investor_page(sosok, bizdate, page):
    html = http(f"https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate={bizdate}&sosok={sosok}&page={page}",
                {"Referer": "https://finance.naver.com/sise/sise_trans_style.naver"})
    p = _Rows()
    p.feed(html)
    out, checked, good = {}, 0, 0
    for r in p.rows:
        m = re.fullmatch(r"(\d{2}|\d{4})\.(\d{2})\.(\d{2})", r[0]) if r else None
        if not m:
            continue
        n = [num(c) for c in r[1:]]
        if len(n) < 10 or any(v is None for v in n[:10]):
            continue
        y = m.group(1)
        d = f"{'20' + y if len(y) == 2 else y}-{m.group(2)}-{m.group(3)}"
        # 자기 검증: 개인 + 외국인 + 기관계 + 기타법인 ≈ 0 이어야 열 위치가 맞는 것
        checked += 1
        if abs(n[0] + n[1] + n[2] + n[9]) <= 0.03 * (abs(n[0]) + abs(n[1]) + abs(n[2]) + abs(n[9])) + 30:
            good += 1
        out[d] = n[0]
    if not out:
        raise ValueError("표에서 행을 찾지 못함")
    if good < 0.7 * checked:
        raise ValueError("표 구조가 예상과 다름(합계 검증 실패)")
    return out


def collect_investor(sosok, known, until):
    """known(날짜→억원)을 최신 값으로 채운다. until 날짜까지 거슬러 올라감."""
    bizdate = dt.datetime.now(KST).strftime("%Y%m%d")
    newest_known = max(known) if known else ""
    covered = bool(known) and min(known) <= until
    for page in range(1, 60):
        got = investor_page(sosok, bizdate, page)
        known.update(got)
        oldest = min(got)
        if oldest <= until or (covered and oldest <= newest_known):
            break
        time.sleep(0.4)
    return known


def probe(url, headers=None):
    try:
        return {"ok": True, "sample": http(url, headers, tries=1)[:400]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


# ───────── 계산 ─────────
def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    now = dt.datetime.now(KST)
    store = load("store.json", {})
    store.setdefault("investor", {"kospi": {}, "kosdaq": {}})
    store.setdefault("seed", SEED)
    status, stale = {}, []

    def attempt(name, fn):
        try:
            v = fn()
            status[name] = {"ok": True}
            return v
        except Exception as e:  # noqa: BLE001
            status[name] = {"ok": False, "error": str(e)[:300]}
            return None

    # 1) 지수 6개
    indices, hist = [], {}
    for name, sym, ncode in INDICES:
        rows = attempt(f"지수:{name}", lambda s=sym, c=ncode: index_rows(s, c))
        if rows:
            hist[name] = rows
            last, prev = rows[-1], rows[-2]
            item = {"name": name, "close": round(last[2], 2), "chg": round((last[2] / prev[2] - 1) * 100, 2),
                    "date": last[0], "note": ""}
            store.setdefault("indices", {})[name] = item
        else:
            item = store.get("indices", {}).get(name)
            stale.append(name)
        if item:
            indices.append(item)

    if "코스피" not in hist:
        save("status.json", {"at": now.isoformat(timespec="seconds"), "sources": status})
        print("코스피 지수를 가져오지 못해 이번 갱신은 건너뜁니다.", status, file=sys.stderr)
        return 1

    k = hist["코스피"]
    peak_date, peak = max(((r[0], r[1]) for r in k), key=lambda x: x[1])
    k_dd = k[-1][2] / peak - 1
    k_chg = (k[-1][2] / k[-2][2] - 1) * 100
    date = k[-1][0]

    if "나스닥" in hist:
        n = hist["나스닥"]
        n_dd = n[-1][2] / max(r[1] for r in n) - 1
        store["nasdaq_dd"] = n_dd
    else:
        n_dd = store.get("nasdaq_dd", 0.0)

    # 2) 공포탐욕지수
    fg = attempt("공포탐욕지수", fear_greed)
    if fg is None:
        fg = store.get("fear_greed", 50.0)
        stale.append("공포탐욕지수")
    store["fear_greed"] = fg

    # 3) 신용융자 잔고
    cr = attempt("신용융자 잔고", freesis_credit)
    if cr:
        store["credit"] = {"date": cr[0], "value": cr[1]}
    else:
        stale.append("신용융자 잔고")
    credit = store.get("credit", {"date": "2026-11-10", "value": 265_494})

    # 4) 개인 순매수 (고점 이후 누적)
    inv = store["investor"]
    ok_all = True
    for key, sosok in (("kospi", "01"), ("kosdaq", "02")):
        r = attempt(f"개인 순매수:{key}", lambda kk=key, ss=sosok: collect_investor(ss, inv[kk], peak_date))
        ok_all = ok_all and r is not None
    seed = store["seed"]
    full = all(inv[m] and min(inv[m]) <= peak_date for m in ("kospi", "kosdaq"))
    if full or peak_date > seed["base_date"]:
        cum = sum(v for m in ("kospi", "kosdaq") for d, v in inv[m].items() if d >= peak_date)
        estimated = False
    else:
        cum = seed["base_cum"] + sum(v for m in ("kospi", "kosdaq") for d, v in inv[m].items() if d > seed["base_date"])
        estimated = True
    if not ok_all:
        stale.append("개인 순매수")
        status["probe:naver_new"] = probe("https://stock.naver.com/api/domestic/market/trend/daily?marketType=KOSPI",
                                          {"Referer": "https://stock.naver.com/"})
        status["probe:daum"] = probe("https://finance.daum.net/api/investor/KOSPI/days?page=1&perPage=5&pagination=true",
                                     {"Referer": "https://finance.daum.net/domestic/investors/KOSPI"})
    ind_today = sum(inv[m].get(date, 0) for m in ("kospi", "kosdaq"))
    if not any(date in inv[m] for m in ("kospi", "kosdaq")):
        ind_today = store.get("ind_today", 0)
    store["ind_today"] = ind_today
    cutoff = (now.date() - dt.timedelta(days=420)).isoformat()
    for m in ("kospi", "kosdaq"):
        inv[m] = {d: v for d, v in sorted(inv[m].items()) if d >= cutoff}

    # 5) 점수
    s_loss = clip01(-k_dd / FULL_KOSPI_DROP) * 100
    s_fear = 100 - fg
    s_nas = clip01(-n_dd / FULL_NASDAQ_DROP) * 100
    fire = W_FIRE[0] * s_loss + W_FIRE[1] * s_fear + W_FIRE[2] * s_nas
    s_trap = clip01(cum / FULL_TRAPPED) * 100
    s_margin = clip01((credit["value"] - CREDIT_ZERO) / (CREDIT_FULL - CREDIT_ZERO)) * 100
    pot = W_POT[0] * s_trap + W_POT[1] * s_margin
    score = math.sqrt(max(fire, 0) * max(pot, 0))
    trigger = "발동" if k_chg <= OVERFLOW_DROP else "없음"

    gauge = {"date": date, "score": round(score, 1), "fire": round(fire, 1), "pot": round(pot, 1),
             "fear_greed": fg, "ind_net": round(ind_today), "trigger": trigger,
             "kospi_from_high": round(k_dd * 100, 1), "nasdaq_from_high": round(n_dd * 100, 1),
             "trapped_tn": round(cum / 10000, 1), "margin_tn": round(credit["value"] / 10000, 2),
             "kospi_peak": round(peak, 2), "kospi_peak_date": peak_date}
    meta = {"updated_at": now.isoformat(timespec="seconds"), "credit_date": credit["date"],
            "trapped_estimated": estimated, "stale": stale}
    save("latest.json", {"gauge": [gauge], "indices": indices, "meta": meta})
    save("store.json", store)
    save("status.json", {"at": meta["updated_at"], "sources": status})

    hist_rows = load("history.json", [])
    hist_rows = [h for h in hist_rows if h.get("date") != date] + [
        {"date": date, "score": gauge["score"], "fire": gauge["fire"], "pot": gauge["pot"]}]
    save("history.json", sorted(hist_rows, key=lambda h: h["date"]))

    print(f"[{date}] 개미탕 지수 {gauge['score']} (불 {gauge['fire']}, 물린 개미 {gauge['pot']})")
    for name, st in status.items():
        print(("  OK   " if st.get("ok") else "  실패 ") + name + ("" if st.get("ok") else " → " + st.get("error", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
