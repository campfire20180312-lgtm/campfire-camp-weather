#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
營火部落 — 營地天氣資料產生器

流程
  1. 從 campfiretw.com/camp-database/ 取出營地清單（名稱／縣市／鄉鎮／海拔／座標）
  2. 呼叫中央氣象署 F-D0047-091「臺灣未來1週天氣預報」
  3. 每個營地對應最近的鄉鎮預報點，用「營地海拔 − 預報點海拔」的高度差套遞減率修正氣溫
  4. 輸出 weather.json 給前端頁面讀取

用法
  export CWA_API_KEY=CWA-XXXXXXXX
  python3 build_weather.py                # 正式產生 weather.json
  python3 build_weather.py --demo         # 不連網，產生示範資料（給沒有金鑰時預覽版面）
  python3 build_weather.py --inspect      # 只印出 API 回傳結構，用來核對欄位名稱

備註
  預報點海拔取自 DEM（預設 opentopodata SRTM 30m），結果快取在 town_elev.json，
  只有第一次執行會連 DEM，之後都直接讀快取。
"""

import argparse
import base64
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TPE = timezone(timedelta(hours=8))

DB_URL = "https://campfiretw.com/camp-database/"
CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091"
DEM_URL = "https://api.opentopodata.org/v1/srtm30m"

# 環境氣溫遞減率（°C / 100m）。營火部落資料庫既有說明用 0.6，這裡保持一致。
# 乾空氣約 1.0、飽和空氣約 0.5，台灣山區多濕，0.55~0.65 都算合理。
LAPSE = float(os.environ.get("LAPSE", "0.6"))

HERE = os.path.dirname(os.path.abspath(__file__))
ELEV_CACHE = os.path.join(HERE, "town_elev.json")

NEEDED_ELEMENTS = [
    "最高溫度",
    "最低溫度",
    "12小時降雨機率",
    "風速",
    "平均相對濕度",
    "天氣現象",
]


# ---------------------------------------------------------------- 營地清單

def load_camps(url=DB_URL):
    """從資料庫頁面內嵌的 base64 腳本裡取出 DATA 與 GEO。"""
    html = requests.get(url, timeout=60).text
    m = re.search(r'var\s+S\s*=\s*"([A-Za-z0-9+/=]+)"', html)
    if not m:
        raise RuntimeError("找不到資料庫頁面的內嵌資料（var S）")
    code = base64.b64decode(m.group(1)).decode("utf-8")

    data = _js_literal(code, "DATA")
    geo = _js_literal(code, "GEO")

    camps = []
    for r in data:
        g = geo.get(r["name"])
        if not g:
            continue
        camps.append({
            "n": r["name"],
            "ct": r.get("county", ""),
            "d": r.get("dist", ""),
            "e": r.get("e"),
            "la": round(float(g[0]), 5),
            "lo": round(float(g[1]), 5),
            "cat": r.get("c", ""),
            "price": r.get("price", ""),
            "moto": 1 if r.get("moto") else 0,
            "hd": 1 if r.get("hd") else 0,
            "closed": 1 if r.get("closed") else 0,
            "pw": _power_flag(r.get("note", "")),
        })
    if len(camps) < 100:
        raise RuntimeError("營地筆數異常（%d），資料庫頁面格式可能改了" % len(camps))
    return camps


def _js_literal(code, name):
    """把 `NAME = [...]` 或 `NAME = {...}` 這段 JS 物件字面值轉成 Python 物件。"""
    k = re.search(name + r"\s*=\s*[\[{]", code)
    if not k:
        raise RuntimeError("找不到 %s" % name)
    s0 = k.end() - 1
    opening = code[s0]
    closing = "]" if opening == "[" else "}"
    depth, end, in_str, quote = 0, -1, False, ""
    p = s0
    while p < len(code):
        c = code[p]
        if in_str:
            if c == "\\":
                p += 2
                continue
            if c == quote:
                in_str = False
        elif c in "\"'`":
            in_str, quote = True, c
        elif c == opening:
            depth += 1
        elif c == closing:
            depth -= 1
            if depth == 0:
                end = p
                break
        p += 1
    if end < 0:
        raise RuntimeError("%s 括號不完整" % name)
    raw = code[s0:end + 1]
    # 只替換 ASCII 的物件鍵名，中文字串內容不會被誤傷
    raw = re.sub(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return json.loads(raw)


def _power_flag(note):
    if re.search(r"無電|不供電|沒有電", note):
        return -1
    if re.search(r"供電|有電|電源|插座|電力", note):
        return 1
    return 0


# ---------------------------------------------------------------- 氣象署

def fetch_cwa(key, elements=NEEDED_ELEMENTS):
    params = {"Authorization": key, "format": "JSON", "ElementName": list(elements)}
    r = requests.get(CWA_URL, params=params, timeout=180)
    r.raise_for_status()
    js = r.json()
    if str(js.get("success")).lower() not in ("true", "1"):
        raise RuntimeError("氣象署回傳 success != true：%s" % str(js)[:200])
    return js


def locations_of(js):
    """回傳 [{name, lat, lon, elements:{名稱:[Time...]}}]，容忍大小寫與層級差異。"""
    rec = js.get("records", js)
    locs_wrap = rec.get("Locations") or rec.get("locations") or []
    if isinstance(locs_wrap, dict):
        locs_wrap = [locs_wrap]
    out = []
    for wrap in locs_wrap:
        for loc in wrap.get("Location") or wrap.get("location") or []:
            name = loc.get("LocationName") or loc.get("locationName")
            lat = _f(loc.get("Latitude") or loc.get("lat"))
            lon = _f(loc.get("Longitude") or loc.get("lon"))
            els = {}
            for el in loc.get("WeatherElement") or loc.get("weatherElement") or []:
                en = el.get("ElementName") or el.get("elementName")
                els[en] = el.get("Time") or el.get("time") or []
            out.append({"name": name, "lat": lat, "lon": lon, "el": els})
    if not out:
        raise RuntimeError("解析不到 Location，請用 --inspect 看實際結構")
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _value(t, prefer):
    """從一個 Time 區段取出要素值；prefer 是優先的鍵名清單。"""
    evs = t.get("ElementValue") or t.get("elementValue") or []
    if isinstance(evs, dict):
        evs = [evs]
    for ev in evs:
        for k in prefer:
            if k in ev:
                return ev[k]
    for ev in evs:
        for v in ev.values():
            return v
    return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # 氣象署缺值常見以 -99 表示
    return None if f <= -90 else f


def _start(t):
    s = t.get("StartTime") or t.get("startTime") or t.get("DataTime")
    return datetime.fromisoformat(s) if s else None


# ---------------------------------------------------------------- 預報點海拔

def town_elevations(locs, refresh=False):
    cache = {}
    if os.path.exists(ELEV_CACHE) and not refresh:
        cache = json.load(open(ELEV_CACHE, encoding="utf-8"))
    missing = [l for l in locs if l["name"] not in cache and l["lat"] and l["lon"]]
    for i in range(0, len(missing), 90):
        batch = missing[i:i + 90]
        q = "|".join("%.5f,%.5f" % (l["lat"], l["lon"]) for l in batch)
        r = requests.post(DEM_URL, data={"locations": q}, timeout=120)
        r.raise_for_status()
        res = r.json().get("results", [])
        for l, item in zip(batch, res):
            e = item.get("elevation")
            cache[l["name"]] = round(e) if e is not None else 0
        time.sleep(1.2)
    json.dump(cache, open(ELEV_CACHE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    return cache


# ---------------------------------------------------------------- 換算

def wind_chill(t, ws_ms):
    """加拿大／美國風寒公式；僅在 10°C 以下、風速 1.3 m/s 以上適用。"""
    if t is None or ws_ms is None or t > 10 or ws_ms < 1.3:
        return t
    v = (ws_ms * 3.6) ** 0.16
    return 13.12 + 0.6215 * t - 11.37 * v + 0.3965 * t * v


def adjust(t, dh):
    if t is None or dh is None:
        return None
    return t - dh / 100.0 * LAPSE


def haversine(a, b, c, d):
    R = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def build_days(loc):
    """把 12 小時區段整理成以日期為單位的白天／夜間。"""
    days = {}

    def slot(t):
        st = _start(t)
        if not st:
            return None, None
        if 5 <= st.hour < 12:
            return st.date().isoformat(), "day"
        return st.date().isoformat(), "night"

    def put(el_name, prefer, field, numeric=True):
        for t in loc["el"].get(el_name, []):
            key, part = slot(t)
            if not key:
                continue
            v = _value(t, prefer)
            v = _num(v) if numeric else v
            days.setdefault(key, {})[part + "_" + field] = v

    put("最高溫度", ["溫度", "MaxTemperature"], "hi")
    put("最低溫度", ["溫度", "MinTemperature"], "lo")
    put("12小時降雨機率", ["降雨機率", "ProbabilityOfPrecipitation"], "pop")
    put("風速", ["風速", "WindSpeed"], "ws")
    put("平均相對濕度", ["相對濕度", "RelativeHumidity"], "rh")
    put("天氣現象", ["天氣現象", "Weather"], "wx", numeric=False)

    out = []
    for date in sorted(days):
        d = days[date]
        out.append({
            "date": date,
            "hi": d.get("day_hi", d.get("night_hi")),
            "lo": d.get("night_lo", d.get("day_lo")),
            "popd": d.get("day_pop"),
            "popn": d.get("night_pop"),
            "wsd": d.get("day_ws"),
            "wsn": d.get("night_ws"),
            "rh": d.get("night_rh", d.get("day_rh")),
            "wxd": d.get("day_wx"),
            "wxn": d.get("night_wx"),
        })
    return out


def compose(camps, locs, elev, generated):
    pts = [l for l in locs if l["lat"] and l["lon"]]
    out_camps = []
    for c in camps:
        near = min(pts, key=lambda l: haversine(c["la"], c["lo"], l["lat"], l["lon"]))
        base = elev.get(near["name"], 0)
        dh = (c["e"] or 0) - base
        days = []
        for d in build_days(near):
            lo = d["lo"]
            hi = d["hi"]
            clo = adjust(lo, dh)
            chi = adjust(hi, dh)
            wsn = d["wsn"] if d["wsn"] is not None else d["wsd"]
            feel = wind_chill(clo, wsn)
            days.append({
                "dt": d["date"],
                "hi": _r(hi), "lo": _r(lo),
                "chi": _r(chi), "clo": _r(clo), "feel": _r(feel),
                "popd": _i(d["popd"]), "popn": _i(d["popn"]),
                "wsd": _r(d["wsd"], 1), "wsn": _r(wsn, 1),
                "rh": _i(d["rh"]),
                "wxd": d["wxd"], "wxn": d["wxn"],
            })
        # 只輸出頁面真的會用到的欄位。座標、價格、分類、機車友善這些留在資料庫頁，
        # 不要在公開的 weather.json 裡再複製一份，避免整包被輕鬆抓走。
        out_camps.append({
            "n": c["n"], "ct": c["ct"], "d": c["d"], "e": c["e"], "pw": c["pw"],
            "tw": near["name"], "te": base, "dh": dh,
            "km": round(haversine(c["la"], c["lo"], near["lat"], near["lon"]), 1),
            "days": days,
        })
    check(out_camps)
    return {
        "generated": generated,
        "source": "中央氣象署開放資料 F-D0047-091 鄉鎮天氣預報（未來1週）",
        "lapse": LAPSE,
        "count": len(out_camps),
        "camps": out_camps,
    }


def check(camps):
    """寧可整批失敗，也不要安靜產出一包空值蓋掉好的資料。"""
    if len(camps) < 300:
        raise RuntimeError("營地筆數只有 %d，低於預期" % len(camps))
    with_days = [c for c in camps if len(c["days"]) >= 5]
    if len(with_days) < len(camps) * 0.9:
        raise RuntimeError("只有 %d/%d 筆有 5 天以上的預報，回傳結構可能變了"
                           % (len(with_days), len(camps)))
    filled = sum(1 for c in camps for d in c["days"] if d["clo"] is not None)
    total = sum(len(c["days"]) for c in camps)
    if total == 0 or filled < total * 0.8:
        raise RuntimeError("溫度欄位缺值太多（%d/%d），請用 --inspect 檢查要素名稱"
                           % (filled, total))
    rain = sum(1 for c in camps for d in c["days"]
               if d["popd"] is not None or d["popn"] is not None)
    if rain < total * 0.5:
        raise RuntimeError("降雨機率缺值太多（%d/%d）" % (rain, total))


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def _i(v):
    return None if v is None else int(round(float(v)))


# ---------------------------------------------------------------- 主程式

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "weather.json"))
    ap.add_argument("--demo", action="store_true", help="不連網，產生示範資料")
    ap.add_argument("--inspect", action="store_true", help="只印出 API 結構")
    ap.add_argument("--refresh-elev", action="store_true", help="重新計算預報點海拔")
    args = ap.parse_args()

    if args.demo:
        from demo_data import make_demo
        payload = make_demo(LAPSE, adjust, wind_chill)
    else:
        key = os.environ.get("CWA_API_KEY")
        if not key:
            sys.exit("請先設定環境變數 CWA_API_KEY")
        js = fetch_cwa(key)
        if args.inspect:
            rec = js.get("records", {})
            wrap = (rec.get("Locations") or [{}])[0]
            loc = (wrap.get("Location") or [{}])[0]
            slim = dict(loc)
            we = slim.pop("WeatherElement", [])[:2]
            print(json.dumps({"wrapKeys": list(wrap.keys()), "location": slim,
                              "elementSample": we[:1]},
                             ensure_ascii=False, indent=2)[:4000])
            return
        locs = locations_of(js)
        elev = town_elevations(locs, refresh=args.refresh_elev)
        camps = load_camps()
        payload = compose(camps, locs, elev,
                          datetime.now(TPE).isoformat(timespec="minutes"))

    out = os.path.abspath(args.out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("已寫入 %s（%d 筆營地，%.1f KB）" %
          (out, payload["count"], os.path.getsize(out) / 1024))


if __name__ == "__main__":
    main()
