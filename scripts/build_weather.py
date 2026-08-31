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
CWA_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"
# 各縣市「未來1週天氣預報」是鄉鎮層級；全臺彙整版 F-D0047-091 只有縣市層級，不能用
DATASETS = ["F-D0047-%03d" % n for n in range(3, 88, 4)]
DEM_URL = "https://api.opentopodata.org/v1/srtm30m"
RAIN_DATASET = "O-A0002-001"   # 雨量觀測站-雨量資料（觀測，不是預報）
# 地震報告：E-A0015-001 是顯著有感（規模較大、全臺性），E-A0016-001 是小區域有感。
# 兩支都抓，合併後去重，用來判斷「七天內這一帶有沒有搖過」。
EQ_DATASETS = ["E-A0015-001", "E-A0016-001"]
EQ_DAYS = 7          # 只看幾天內的地震
EQ_RADIUS_KM = 120   # 震央離營地多遠以內才納入
EQ_STATION_KM = 25   # 測站離營地多近才拿它的震度當作營地的震度
# 天氣特報：W-C0033-001 是各縣市目前的警特報總表（JSON），一次涵蓋豪大雨、陸上強風、
# 低溫、高溫。W-C0033-003 那幾支只有 CAP 格式，解析麻煩且內容是同一批，不用。
WARN_DATASET = "W-C0033-001"
# 氣象署不發布逐日雨量（毫米）預報，只有機率與天氣現象文字。要給「大概會下幾毫米」
# 只能用國際模式，這裡用 Open-Meteo（免金鑰）。全球模式對台灣山區地形雨會低估，
# 所以頁面上單獨標成「參考雨量」，不拿它決定風險等級。
QPF_URL = "https://api.open-meteo.com/v1/forecast"
QPF_BATCH = 80        # 一次帶幾個座標
# 氣象署只發一週，第八天之後只剩國際模式，所以抓 14 天，後面那幾天標成參考值
QPF_DAYS = 14

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


# ---------------------------------------------------------------- 連線

def _get(url, params=None, timeout=120, tries=3):
    """氣象署偶爾會連線逾時，重試三次再放棄。"""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:                  # noqa: BLE001
            last = e
            print("第 %d 次連線失敗：%s" % (i + 1, str(e)[:120]))
            time.sleep(3 * (i + 1))
    raise last


# ---------------------------------------------------------------- 營地清單

def load_camps(url=DB_URL):
    """從資料庫頁面內嵌的 base64 腳本裡取出 DATA 與 GEO。"""
    html = _get(url, None, 60).text
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

def fetch_cwa(key, dataset, elements=NEEDED_ELEMENTS):
    params = {"Authorization": key, "format": "JSON", "ElementName": list(elements)}
    js = _get(CWA_BASE + dataset, params, 180).json()
    if str(js.get("success")).lower() not in ("true", "1"):
        raise RuntimeError("氣象署回傳 success != true：%s" % str(js)[:200])
    return js


def fetch_rain(key):
    """抓全台雨量站的累積雨量。抓不到就回空的，不讓整批失敗。"""
    params = {
        "Authorization": key, "format": "JSON",
        "RainfallElement": ["Past1hr", "Past3hr", "Past24hr", "Past3days"],
        "GeoInfo": ["Coordinates", "StationAltitude", "CountyName", "TownName"],
    }
    try:
        js = _get(CWA_BASE + RAIN_DATASET, params, 120).json()
    except Exception as e:                      # noqa: BLE001
        print("雨量站抓取失敗，這次略過：%s" % e)
        return [], None

    rec = js.get("records", js)
    raw = rec.get("Station") or rec.get("station") or []
    out, newest = [], None
    for st in raw:
        geo = st.get("GeoInfo") or {}
        lat = lon = None
        for c in geo.get("Coordinates") or []:
            if "WGS84" in str(c.get("CoordinateName", "")):
                lat = _f(c.get("StationLatitude"))
                lon = _f(c.get("StationLongitude"))
        if lat is None or lon is None:
            continue
        el = st.get("RainfallElement") or {}

        def mm(name):
            v = (el.get(name) or {}).get("Precipitation")
            f = _num(v)
            return None if f is None or f < 0 else round(f, 1)

        t = st.get("ObsTime", {}).get("DateTime") if isinstance(st.get("ObsTime"), dict) else None
        if t and (newest is None or t > newest):
            newest = t
        out.append({
            "name": st.get("StationName") or st.get("stationName"),
            "lat": lat, "lon": lon,
            "alt": _num((geo.get("StationAltitude") or {}).get("StationAltitude")
                        if isinstance(geo.get("StationAltitude"), dict)
                        else geo.get("StationAltitude")),
            "r1": mm("Past1hr"), "r3": mm("Past3hr"),
            "r24": mm("Past24hr"), "r72": mm("Past3days"),
        })
    print("雨量站 %d 站，觀測時間 %s" % (len(out), newest))
    return out, newest


def fetch_qpf(camps):
    """逐日預估雨量（毫米）。抓不到就回空的，不讓整批失敗。"""
    out = {}
    for i in range(0, len(camps), QPF_BATCH):
        chunk = camps[i:i + QPF_BATCH]
        params = {
            "latitude": ",".join("%.4f" % c["la"] for c in chunk),
            "longitude": ",".join("%.4f" % c["lo"] for c in chunk),
            "daily": ("precipitation_sum,precipitation_probability_max,"
                      "temperature_2m_max,temperature_2m_min,wind_speed_10m_max"),
            "timezone": "Asia/Taipei",
            "forecast_days": QPF_DAYS,
        }
        try:
            js = _get(QPF_URL, params, 90).json()
        except Exception as e:                  # noqa: BLE001
            print("預估雨量抓取失敗，這次略過：%s" % e)
            return {}
        rows = js if isinstance(js, list) else [js]
        if len(rows) != len(chunk):
            print("預估雨量筆數對不上（%d vs %d），這批略過" % (len(rows), len(chunk)))
            continue
        for c, r in zip(chunk, rows):
            d = r.get("daily") or {}
            days = d.get("time") or []
            if not days:
                continue

            def col(name):
                v = d.get(name) or []
                return v + [None] * (len(days) - len(v))

            mm, pop = col("precipitation_sum"), col("precipitation_probability_max")
            tmx, tmn = col("temperature_2m_max"), col("temperature_2m_min")
            ws = col("wind_speed_10m_max")
            rec = {}
            for k, day in enumerate(days):
                rec[day] = {
                    "mm": _r(mm[k], 1),
                    "pop": _i(pop[k]),
                    "hi": _r(tmx[k], 1),
                    "lo": _r(tmn[k], 1),
                    # Open-Meteo 的風速預設是 km/h，換成 m/s 才跟氣象署一致
                    "ws": None if ws[k] is None else round(float(ws[k]) / 3.6, 1),
                }
            out[c["n"]] = {"e": _i(r.get("elevation")), "d": rec}
        time.sleep(0.6)
    print("預估雨量：%d/%d 個營地" % (len(out), len(camps)))
    return out


def _norm_county(s):
    """臺→台、去掉縣市後綴，讓營地資料庫的「苗栗」對得上氣象署的「苗栗縣」。"""
    if not s:
        return ""
    s = str(s).strip().replace("臺", "台")
    if s.endswith("縣") or s.endswith("市"):
        s = s[:-1]
    return s


def _norm_town(s):
    if not s:
        return ""
    s = str(s).strip().replace("臺", "台")
    for suf in ("鄉", "鎮", "市", "區"):
        if s.endswith(suf):
            return s[:-1]
    return s


def fetch_warnings(key):
    """各縣市目前生效中的警特報。抓不到就回空的，不讓整批失敗。"""
    try:
        js = _get(CWA_BASE + WARN_DATASET, {"Authorization": key, "format": "JSON"}, 120).json()
    except Exception as e:                      # noqa: BLE001
        print("天氣特報抓取失敗，這次略過：%s" % e)
        return {}
    rec = js.get("records", js)
    locs = rec.get("location") or rec.get("Location") or []
    out = {}
    for L in locs:
        county = _norm_county(L.get("locationName") or L.get("LocationName"))
        hz = L.get("hazardConditions") or L.get("HazardConditions") or {}
        hazards = hz.get("hazards") or hz.get("Hazards") or []
        if isinstance(hazards, dict):
            hazards = hazards.get("hazard") or hazards.get("Hazard") or []
        items = []
        for h in hazards or []:
            info = h.get("info") or h.get("Info") or {}
            ph = info.get("phenomena") or info.get("Phenomena")
            if not ph:
                continue
            vt = h.get("validTime") or h.get("ValidTime") or {}
            aa = info.get("affectedAreas") or info.get("AffectedAreas") or {}
            areas = []
            for a in (aa.get("location") or aa.get("Location") or []):
                n = a.get("locationName") or a.get("LocationName")
                if n:
                    areas.append(_norm_town(n))
            items.append({
                "p": ph,
                "s": info.get("significance") or info.get("Significance"),
                "st": vt.get("startTime") or vt.get("StartTime"),
                "et": vt.get("endTime") or vt.get("EndTime"),
                "areas": areas,
            })
        if items:
            out[county] = items
    if out:
        print("天氣特報：%s" % json.dumps(
            {k: [i["p"] for i in v] for k, v in out.items()}, ensure_ascii=False))
    else:
        print("天氣特報：目前沒有生效中的特報（%d 個縣市回傳）" % len(locs))
        if locs:
            print("第一筆結構供核對：%s"
                  % json.dumps(locs[0], ensure_ascii=False)[:900])
    return out


# 只有這幾種對露營有意義，其他（濃霧、颱風等）不進風險模型
WARN_KEEP = ("雨", "強風", "低溫", "高溫")


def camp_warnings(c, warns):
    items = warns.get(_norm_county(c.get("ct")))
    if not items:
        return None
    town = _norm_town(c.get("d"))
    out = []
    for it in items:
        if not any(k in it["p"] for k in WARN_KEEP):
            continue
        if it["areas"] and town and town not in it["areas"]:
            continue
        out.append({"p": it["p"], "s": it["s"], "et": it["et"]})
    return out or None


INTENSITY_MAP = {
    "0級": 0, "1級": 1, "2級": 2, "3級": 3, "4級": 4,
    "5弱": 5.0, "5強": 5.5, "6弱": 6.0, "6強": 6.5, "7級": 7.0,
    # 舊制寫法，保險起見一併吃進來
    "5級": 5.0, "6級": 6.0,
}


def _intensity(s):
    if not s:
        return None
    s = str(s).strip()
    if s in INTENSITY_MAP:
        return INTENSITY_MAP[s]
    m = re.match(r"^(\d)", s)
    if not m:
        return None
    base = float(m.group(1))
    if "弱" in s:
        return base
    if "強" in s:
        return base + 0.5
    return base


def _eq_time(s):
    """氣象署回傳的是台灣時間字串，格式偶爾帶 T，兩種都吃。"""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TPE)
        except ValueError:
            continue
    return None


def fetch_quakes(key, now=None):
    """抓最近幾天的有感地震。抓不到就回空的，不讓整批失敗。"""
    now = now or datetime.now(TPE)
    since = now - timedelta(days=EQ_DAYS)
    out, seen = [], set()
    for ds in EQ_DATASETS:
        params = {
            "Authorization": key, "format": "JSON", "limit": 100,
            "timeFrom": since.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            js = _get(CWA_BASE + ds, params, 120).json()
        except Exception as e:                  # noqa: BLE001
            print("地震 %s 抓取失敗，略過：%s" % (ds, e))
            continue
        rec = js.get("records", js)
        raw = rec.get("Earthquake") or rec.get("earthquake") or []
        for q in raw:
            info = q.get("EarthquakeInfo") or q.get("earthquakeInfo") or {}
            t = _eq_time(info.get("OriginTime"))
            if t is None or t < since or t > now + timedelta(hours=1):
                continue
            epi = info.get("Epicenter") or {}
            lat = _f(epi.get("EpicenterLatitude"))
            lon = _f(epi.get("EpicenterLongitude"))
            mag = _num((info.get("EarthquakeMagnitude") or {}).get("MagnitudeValue"))
            if lat is None or lon is None:
                continue
            no = q.get("EarthquakeNo") or (info.get("OriginTime"), lat, lon)
            if no in seen:
                continue
            seen.add(no)
            stations = []
            areas = ((q.get("Intensity") or {}).get("ShakingArea")) or []
            for a in areas:
                for st in a.get("EqStation") or []:
                    slat = _f(st.get("StationLatitude"))
                    slon = _f(st.get("StationLongitude"))
                    iv = _intensity(st.get("SeismicIntensity"))
                    if slat is None or slon is None or iv is None:
                        continue
                    stations.append((slat, slon, iv))
            out.append({
                "t": t, "lat": lat, "lon": lon,
                "mag": None if mag is None else round(mag, 1),
                "dep": _num(info.get("FocalDepth")),
                "loc": epi.get("Location"),
                "stations": stations,
            })
        time.sleep(0.4)
    out.sort(key=lambda q: q["t"], reverse=True)
    print("七天內有感地震 %d 筆%s" %
          (len(out), ("，最大規模 %.1f" % max(q["mag"] or 0 for q in out)) if out else ""))
    return out


def camp_quake(c, quakes, now):
    """挑出對這個營地最有意義的一筆：先看營地實際震度，沒有測站就看規模與距離。"""
    best = None
    for q in quakes:
        km = haversine(c["la"], c["lo"], q["lat"], q["lon"])
        if km > EQ_RADIUS_KM:
            continue
        local = None
        for slat, slon, iv in q["stations"]:
            if haversine(c["la"], c["lo"], slat, slon) <= EQ_STATION_KM:
                local = iv if local is None else max(local, iv)
        # 排序鍵：先比營地震度，再比規模，最後比距離近的
        rank = (local if local is not None else -1, q["mag"] or 0, -km)
        if best is None or rank > best[0]:
            best = (rank, q, km, local)
    if best is None:
        return None
    _, q, km, local = best
    hours = (now - q["t"]).total_seconds() / 3600.0
    return {
        "mag": q["mag"],
        "km": round(km, 1),
        "dep": None if q["dep"] is None else round(q["dep"], 1),
        "int": local,
        "hr": int(round(hours)),
        "t": q["t"].strftime("%Y-%m-%d %H:%M"),
        "loc": q["loc"],
    }


def all_locations(key):
    """把 22 個縣市的鄉鎮預報全部抓下來合併。"""
    out = []
    for ds in DATASETS:
        out.extend(locations_of(fetch_cwa(key, ds)))
        time.sleep(0.4)
    if len(out) < 300:
        raise RuntimeError("鄉鎮數只有 %d，預期 300 以上" % len(out))
    return out


def locations_of(js):
    """回傳 [{name, lat, lon, elements:{名稱:[Time...]}}]，容忍大小寫與層級差異。"""
    rec = js.get("records", js)
    locs_wrap = rec.get("Locations") or rec.get("locations") or []
    if isinstance(locs_wrap, dict):
        locs_wrap = [locs_wrap]
    out = []
    for wrap in locs_wrap:
        ct = _norm_county(wrap.get("LocationsName") or wrap.get("locationsName") or "")
        for loc in wrap.get("Location") or wrap.get("location") or []:
            name = loc.get("LocationName") or loc.get("locationName")
            lat = _f(loc.get("Latitude") or loc.get("lat"))
            lon = _f(loc.get("Longitude") or loc.get("lon"))
            els = {}
            for el in loc.get("WeatherElement") or loc.get("weatherElement") or []:
                en = el.get("ElementName") or el.get("elementName")
                els[en] = el.get("Time") or el.get("time") or []
            out.append({"name": name, "lat": lat, "lon": lon, "ct": ct, "el": els})
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


def compose(camps, locs, elev, generated, rain=None, rain_time=None, quakes=None, warns=None,
            qpf=None):
    pts = [l for l in locs if l["lat"] and l["lon"]]
    rain = rain or []
    quakes = quakes or []
    warns = warns or {}
    qpf = qpf or {}
    now = datetime.now(TPE)
    out_camps = []
    for c in camps:
        near = min(pts, key=lambda l: haversine(c["la"], c["lo"], l["lat"], l["lon"]))
        base = elev.get(near["name"], 0)
        dh = (c["e"] or 0) - base
        q = qpf.get(c["n"]) or {}
        qdays = q.get("d") or {}
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
                "mm": (qdays.get(d["date"]) or {}).get("mm"),
            })
        # 只輸出頁面真的會用到的欄位。座標、價格、分類、機車友善這些留在資料庫頁，
        # 不要在公開的 weather.json 裡再複製一份，避免整包被輕鬆抓走。
        out_camps.append({
            "n": c["n"], "ct": c["ct"], "d": c["d"], "e": c["e"], "pw": c["pw"],
            "tw": near["name"], "te": base, "dh": dh,
            "km": round(haversine(c["la"], c["lo"], near["lat"], near["lon"]), 1),
            "days": days,
        })
        if q.get("e") is not None:
            out_camps[-1]["qe"] = q["e"]
        # 氣象署預報之後的日子，用國際模式補上去，標記 src=om，頁面上會標成參考值。
        # 模式格點有自己的高度，所以遞減率是從格點高度修到營地高度，不是用鄉鎮預報點。
        if qdays:
            have = set(d["dt"] for d in days)
            dh2 = (c["e"] or 0) - (q.get("e") if q.get("e") is not None else (c["e"] or 0))
            for dt in sorted(qdays):
                if dt in have:
                    continue
                v = qdays[dt]
                hi, lo = v.get("hi"), v.get("lo")
                chi = adjust(hi, dh2) if hi is not None else None
                clo = adjust(lo, dh2) if lo is not None else None
                ws = v.get("ws")
                days.append({
                    "dt": dt, "src": "om",
                    "hi": None, "lo": None,
                    "chi": _r(chi), "clo": _r(clo),
                    "feel": _r(wind_chill(clo, ws)) if clo is not None else None,
                    "popd": v.get("pop"), "popn": None,
                    "wsd": ws, "wsn": ws,
                    "rh": None, "wxd": None, "wxn": None,
                    "mm": v.get("mm"),
                })
        if rain:
            st = min(rain, key=lambda s: haversine(c["la"], c["lo"], s["lat"], s["lon"]))
            out_camps[-1]["rain"] = {
                "st": st["name"],
                "km": round(haversine(c["la"], c["lo"], st["lat"], st["lon"]), 1),
                "alt": None if st["alt"] is None else int(st["alt"]),
                "r1": st["r1"], "r3": st["r3"], "r24": st["r24"], "r72": st["r72"],
            }
        if quakes:
            eq = camp_quake(c, quakes, now)
            if eq:
                out_camps[-1]["eq"] = eq
        if warns:
            w = camp_warnings(c, warns)
            if w:
                out_camps[-1]["warn"] = w
    check(out_camps)
    return {
        "generated": generated,
        "source": "中央氣象署開放資料 鄉鎮天氣預報（各縣市未來1週，F-D0047 系列）",
        "lapse": LAPSE,
        "rainObsTime": rain_time,
        "eqDays": EQ_DAYS,
        "eqCount": len(quakes),
        "warnCount": sum(1 for c in out_camps if c.get("warn")),
        "qpfSource": "Open-Meteo（ECMWF/GFS 等全球模式），非中央氣象署預報",
        "qpfCount": sum(1 for c in out_camps if any(d.get("mm") is not None for d in c["days"])),
        "cwaDays": max((sum(1 for d in c["days"] if d.get("src") != "om") for c in out_camps),
                       default=0),
        "count": len(out_camps),
        "camps": out_camps,
    }


def compose_towns(locs, elev, generated, rain=None, rain_time=None,
                  quakes=None, warns=None):
    """全台鄉鎮預報點另存一份，給「搜尋的地方沒有營地」時當備援。

    欄位刻意跟營地同一個形狀，頁面可以直接沿用同一套渲染；差別是 dh=0
    （預報點就是它自己，不做海拔修正）、沒有 Open-Meteo 的毫米數，
    並且多一個 town=1 讓頁面知道這不是營地。
    """
    rain = rain or []
    quakes = quakes or []
    warns = warns or {}
    now = datetime.now(TPE)
    out = []
    for l in locs:
        if not (l.get("lat") and l.get("lon")):
            continue
        base = elev.get(l["name"], 0)
        days = []
        for d in build_days(l):
            wsn = d["wsn"] if d["wsn"] is not None else d["wsd"]
            days.append({
                "dt": d["date"],
                "hi": _r(d["hi"]), "lo": _r(d["lo"]),
                "chi": _r(d["hi"]), "clo": _r(d["lo"]),
                "feel": _r(wind_chill(d["lo"], wsn)),
                "popd": _i(d["popd"]), "popn": _i(d["popn"]),
                "wsd": _r(d["wsd"], 1), "wsn": _r(wsn, 1),
                "rh": _i(d["rh"]),
                "wxd": d["wxd"], "wxn": d["wxn"],
            })
        rec = {"n": l["name"], "ct": l.get("ct") or "", "d": l["name"],
               "e": base, "tw": l["name"], "te": base, "dh": 0, "km": 0,
               "town": 1, "days": days}
        if rain:
            st = min(rain, key=lambda s: haversine(l["lat"], l["lon"], s["lat"], s["lon"]))
            rec["rain"] = {
                "st": st["name"],
                "km": round(haversine(l["lat"], l["lon"], st["lat"], st["lon"]), 1),
                "alt": None if st["alt"] is None else int(st["alt"]),
                "r1": st["r1"], "r3": st["r3"], "r24": st["r24"], "r72": st["r72"],
            }
        if quakes:
            eq = camp_quake({"la": l["lat"], "lo": l["lon"]}, quakes, now)
            if eq:
                rec["eq"] = eq
        if warns:
            w = camp_warnings({"ct": rec["ct"], "d": l["name"]}, warns)
            if w:
                rec["warn"] = w
        out.append(rec)
    if len(out) < 300:
        raise RuntimeError("鄉鎮預報點只有 %d 筆，預期 300 以上" % len(out))
    return {
        "generated": generated,
        "source": "中央氣象署開放資料 鄉鎮天氣預報（各縣市未來1週，F-D0047 系列）",
        "rainObsTime": rain_time,
        "note": "鄉鎮預報點本身的預報值，沒有做海拔修正，也沒有國際模式的預估雨量。",
        "count": len(out),
        "towns": out,
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
    if rain < total * 0.25:
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
    ap.add_argument("--inspect-eq", action="store_true", help="只印出地震 API 結構")
    ap.add_argument("--refresh-elev", action="store_true", help="重新計算預報點海拔")
    args = ap.parse_args()

    towns = None
    if args.demo:
        from demo_data import make_demo
        payload = make_demo(LAPSE, adjust, wind_chill)
    else:
        key = os.environ.get("CWA_API_KEY")
        if not key:
            sys.exit("請先設定環境變數 CWA_API_KEY")
        if args.inspect_eq:
            since = (datetime.now(TPE) - timedelta(days=EQ_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
            for ds in EQ_DATASETS:
                r = _get(CWA_BASE + ds, {"Authorization": key, "format": "JSON",
                                         "limit": 3, "timeFrom": since}, 120).json()
                rec = r.get("records", r)
                arr = rec.get("Earthquake") or rec.get("earthquake") or []
                print("=== %s success=%s recordKeys=%s 筆數=%d"
                      % (ds, r.get("success"), list(rec.keys()), len(arr)))
                if arr:
                    q = arr[0]
                    info = q.get("EarthquakeInfo") or {}
                    areas = ((q.get("Intensity") or {}).get("ShakingArea")) or []
                    st = (areas[0].get("EqStation") or [{}])[0] if areas else {}
                    print(json.dumps({"topKeys": list(q.keys()), "info": info,
                                      "areaKeys": list(areas[0].keys()) if areas else None,
                                      "stationSample": st},
                                     ensure_ascii=False, indent=2)[:2500])
            print("解析結果：%s" % json.dumps(
                [{k: v for k, v in q.items() if k != "stations"} | {"stn": len(q["stations"])}
                 for q in fetch_quakes(key)][:5], ensure_ascii=False, default=str))
            return
        js = fetch_cwa(key, DATASETS[0])
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
        locs = all_locations(key)
        elev = town_elevations(locs, refresh=args.refresh_elev)
        camps = load_camps()
        rain, rain_time = fetch_rain(key)
        quakes = fetch_quakes(key)
        warns = fetch_warnings(key)
        qpf = fetch_qpf(camps)
        stamp = datetime.now(TPE).isoformat(timespec="minutes")
        payload = compose(camps, locs, elev, stamp,
                          rain, rain_time, quakes, warns, qpf)
        towns = compose_towns(locs, elev, stamp, rain, rain_time, quakes, warns)

    out = os.path.abspath(args.out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("已寫入 %s（%d 筆營地，%.1f KB）" %
          (out, payload["count"], os.path.getsize(out) / 1024))
    if towns:
        tout = os.path.join(os.path.dirname(out), "towns.json")
        with open(tout, "w", encoding="utf-8") as f:
            json.dump(towns, f, ensure_ascii=False, separators=(",", ":"))
        print("已寫入 %s（%d 個鄉鎮預報點，%.1f KB）" %
              (tout, towns["count"], os.path.getsize(tout) / 1024))


if __name__ == "__main__":
    main()
