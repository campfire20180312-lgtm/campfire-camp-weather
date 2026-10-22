# -*- coding: utf-8 -*-
"""事先算好「出發地 → 每個營地」的真實開車路線（2026-10-22 達哥選做法二）

路線卡原本只知道經過哪幾個縣市，溫度拿整個縣市最糟的營地來代表，
花蓮就被合歡山遊客中心停車場（3127 公尺）拉成低溫 4.7°C。
這支程式用 OpenStreetMap＋OSRM 算出每條路線實際經過的鄉鎮，
再查那段路的最高、最低海拔，頁面用氣象署鄉鎮預報換算到路段高度。

輸出 docs/routes/<代碼>.json，一個出發地一個檔：
{"v":1, "gen":"…", "o":"台北", "towns":["新北|坪林區", …],
 "g":{"<營地 fid>": 地點序號},
 "r":[[總公里, 分鐘, [鄉鎮序號, 最低海拔, 最高海拔, 公里], …], …]}   ← 第幾個地點的路線，算不出來是 null
只存鄉鎮代碼與高度，不存營地座標與路線幾何。

需要：本機 OSRM 服務（預設 http://127.0.0.1:5000）、docs/towns.json、Open-Meteo 海拔 API。
用法：python3 scripts/build_routes.py [--osrm URL] [--only 台北] [--limit N]
"""
import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, HERE)

TPE = timezone(timedelta(hours=8))
SAMPLE_KM = 1.0          # 沿路每 1 公里取一個點
ELEV_ROUND = 0.004       # 海拔快取格點（約 400 公尺）
ELEV_URL = "https://api.open-meteo.com/v1/elevation"
ELEV_CACHE = os.path.join(HERE, "route_elev.json")
GROUP_DEG = 0.015        # 約 1.5 公里內的營地當成同一個地點，只算一條路（2026-10-22 達哥：同一區域的營區合併算比較快）

# 出發地：跟天氣頁「出發地」下拉選單同一組縣市（離島沒有陸路，不算）。座標取主要車站或市區。
ORIGINS = {
    "基隆": ("KEL", 25.1319, 121.7398),
    "台北": ("TPE", 25.0478, 121.5170),
    "新北": ("NTP", 25.0136, 121.4637),
    "桃園": ("TYN", 24.9893, 121.3135),
    "新竹": ("HSZ", 24.8016, 120.9716),
    "苗栗": ("MIA", 24.5701, 120.8224),
    "台中": ("TXG", 24.1372, 120.6866),
    "彰化": ("CHW", 24.0817, 120.5381),
    "南投": ("NTO", 23.9124, 120.6844),
    "雲林": ("YUN", 23.7114, 120.5414),
    "嘉義": ("CYI", 23.4791, 120.4410),
    "台南": ("TNN", 22.9971, 120.2127),
    "高雄": ("KHH", 22.6394, 120.3025),
    "屏東": ("PIF", 22.6694, 120.4862),
    "宜蘭": ("ILA", 24.7546, 121.7580),
    "花蓮": ("HUA", 23.9932, 121.6011),
    "台東": ("TTT", 22.7932, 121.1236),
}
ISLANDS = ("澎湖", "金門", "連江")


def hav(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def sample_line(coords, step=SAMPLE_KM):
    """GeoJSON 座標（經度, 緯度）每 step 公里取一點，回傳 [(緯度, 經度, 累積公里)]，含起點終點。"""
    if not coords:
        return []
    out = [(coords[0][1], coords[0][0], 0.0)]
    acc, nxt = 0.0, step
    for (lo1, la1), (lo2, la2) in zip(coords, coords[1:]):
        d = hav(la1, lo1, la2, lo2)
        while d > 0 and acc + d >= nxt:
            f = (nxt - acc) / d
            out.append((la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f, nxt))
            nxt += step
        acc += d
    last = coords[-1]
    if acc - out[-1][2] > 0.05:
        out.append((last[1], last[0], acc))
    return out


class TownIndex:
    """最近的氣象署鄉鎮預報點。用等距圓柱近似，台灣尺度夠用。"""

    def __init__(self, towns):
        self.keys, self.pts = [], []
        for t in towns:
            if t.get("lat") is None or t.get("lng") is None:
                continue
            self.keys.append("%s|%s" % (t["ct"], t["n"]))
            self.pts.append((float(t["lat"]), float(t["lng"])))
        self.k = math.cos(math.radians(23.7))

    def nearest(self, la, lo):
        best, bi = 1e18, -1
        for i, (a, b) in enumerate(self.pts):
            d = (a - la) ** 2 + ((b - lo) * self.k) ** 2
            if d < best:
                best, bi = d, i
        return bi

    def nearest_many(self, keys):
        """一次算很多格點（"緯,經" 字串）的最近預報點，用 numpy 分批。"""
        import numpy as np
        P = np.array(self.pts)
        out = {}
        keys = list(keys)
        for i in range(0, len(keys), 4000):
            ch = keys[i:i + 4000]
            Q = np.array([[float(x) for x in k.split(",")] for k in ch])
            d = (Q[:, None, 0] - P[None, :, 0]) ** 2 + ((Q[:, None, 1] - P[None, :, 1]) * self.k) ** 2
            for k, j in zip(ch, d.argmin(axis=1)):
                out[k] = int(j)
        return out


def ekey(la, lo):
    return "%.3f,%.3f" % (round(la / ELEV_ROUND) * ELEV_ROUND, round(lo / ELEV_ROUND) * ELEV_ROUND)


def fill_elev(keys, cache, fetch=None):
    """補齊缺的海拔。fetch(lats, lons) -> [公尺]；預設打 Open-Meteo，每次 100 點。"""
    miss = sorted(k for k in keys if k not in cache)
    print("海拔：需要 %d 點，快取已有 %d，要查 %d" % (len(keys), len(keys) - len(miss), len(miss)))

    def om(lats, lons):
        for t in range(4):
            try:
                r = requests.get(ELEV_URL, params={"latitude": ",".join(lats), "longitude": ",".join(lons)}, timeout=60)
                if r.status_code == 429:
                    time.sleep(20 * (t + 1))
                    continue
                r.raise_for_status()
                return r.json()["elevation"]
            except Exception as e:              # noqa: BLE001
                print("海拔查詢失敗（第 %d 次）：%s" % (t + 1, str(e)[:120]))
                time.sleep(5 * (t + 1))
        return None

    fetch = fetch or om
    for i in range(0, len(miss), 100):
        chunk = miss[i:i + 100]
        lats = [k.split(",")[0] for k in chunk]
        lons = [k.split(",")[1] for k in chunk]
        ev = fetch(lats, lons)
        if ev is None:
            continue
        for k, v in zip(chunk, ev):
            if v is not None:
                cache[k] = int(round(float(v)))
        time.sleep(0.12)
        if i and i % 5000 == 0:
            print("  …%d/%d" % (i, len(miss)), flush=True)
    return cache


def segments(samples, town_of, elev):
    """路線樣本點 [(格點鍵, 公里)] → 依序經過的鄉鎮段 [鄉鎮序號, 最低海拔, 最高海拔, 公里]。
    town_of：格點鍵 → 鄉鎮序號。只有一個點的夾心段（A、B、A）視為誤差，併回 A。"""
    rows = []
    for k, km in samples:
        rows.append((town_of[k], elev.get(k), km))
    segs = []
    for ti, e, km in rows:
        if segs and segs[-1]["t"] == ti:
            s = segs[-1]
        else:
            s = {"t": ti, "es": [], "k0": km, "k1": km, "n": 0}
            segs.append(s)
        if e is not None:
            s["es"].append(e)
        s["k1"] = km
        s["n"] += 1
    # 夾心段併回
    changed = True
    while changed and len(segs) >= 3:
        changed = False
        for i in range(1, len(segs) - 1):
            if segs[i]["n"] == 1 and segs[i - 1]["t"] == segs[i + 1]["t"]:
                a, b, c = segs[i - 1], segs[i], segs[i + 1]
                a["es"] += b["es"] + c["es"]
                a["k1"] = c["k1"]
                a["n"] += b["n"] + c["n"]
                del segs[i:i + 2]
                changed = True
                break
    out = []
    for s in segs:
        es = s["es"] or [None]
        lo = min(x for x in es if x is not None) if s["es"] else None
        hi = max(x for x in es if x is not None) if s["es"] else None
        out.append([s["t"], lo, hi, round(max(s["k1"] - s["k0"], SAMPLE_KM if s["n"] == 1 else 0), 1)])
    return out


def osrm_route(base, o, c, session=None):
    s = session or requests
    url = "%s/route/v1/driving/%.5f,%.5f;%.5f,%.5f" % (base, o[1], o[0], c[1], c[0])
    for t in range(3):
        try:
            r = s.get(url, params={"overview": "full", "geometries": "geojson", "steps": "false"}, timeout=60)
            js = r.json()
            if js.get("code") != "Ok" or not js.get("routes"):
                return None
            rt = js["routes"][0]
            return rt["geometry"]["coordinates"], rt["distance"] / 1000.0, rt["duration"] / 60.0
        except Exception as e:                  # noqa: BLE001
            print("OSRM 失敗（第 %d 次）：%s" % (t + 1, str(e)[:120]))
            time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osrm", default="http://127.0.0.1:5000")
    ap.add_argument("--towns", default=os.path.join(ROOT, "docs", "towns.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "routes"))
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    import build_weather as bw
    camps, src = bw.load_camps_safe()
    camps = [c for c in camps if c.get("ct") not in ISLANDS]
    if a.limit:
        camps = camps[:a.limit]
    towns = json.load(open(a.towns, encoding="utf-8"))["towns"]
    tix = TownIndex(towns)
    print("營地 %d（%s）、鄉鎮預報點 %d" % (len(camps), src or "資料庫頁", len(tix.keys)))

    origins = {k: v for k, v in ORIGINS.items() if not a.only or k in a.only.split(",")}
    sess = requests.Session()
    geo = {}       # (出發地, 營地鍵) -> (樣本點, 公里, 分鐘)
    # 同一個小區域（約 1.5 公里）的營地共用一條路線，路線用區域裡第一個營地的座標（一定在路邊）
    groups, camp_g = [], {}
    gidx = {}
    for c in camps:
        key = str(c["id"]) if c.get("id") is not None else c["n"]
        gk = (round(c["la"] / GROUP_DEG), round(c["lo"] / GROUP_DEG))
        if gk not in gidx:
            gidx[gk] = len(groups)
            groups.append((c["la"], c["lo"]))
        camp_g[key] = gidx[gk]
    jobs = []
    for on, (code, ola, olo) in origins.items():
        for gi, d in enumerate(groups):
            jobs.append((on, gi, (ola, olo), d))
    print("營地 %d 個併成 %d 個地點，路線 %d 條" % (len(camp_g), len(groups), len(jobs)))
    t0 = time.time()

    def run(j):
        on, key, o, d = j
        r = osrm_route(a.osrm, o, d, sess)
        if not r:
            return j, None
        coords, km, mins = r
        return j, ([(ekey(la, lo), round(k, 2)) for la, lo, k in sample_line(coords)], km, mins)

    with ThreadPoolExecutor(a.workers) as ex:
        for n, (j, res) in enumerate(ex.map(run, jobs)):
            if res:
                geo[(j[0], j[1])] = res
            if n and n % 2000 == 0:
                print("  路線 %d/%d（%.0f 秒）" % (n, len(jobs), time.time() - t0), flush=True)
    print("算出 %d 條，失敗 %d 條" % (len(geo), len(jobs) - len(geo)))

    cache = {}
    if os.path.exists(ELEV_CACHE):
        cache = json.load(open(ELEV_CACHE, encoding="utf-8"))
    need = set()
    for smp, _, _ in geo.values():
        for k, _ in smp:
            need.add(k)
    fill_elev(need, cache)
    with open(ELEV_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)

    town_of = tix.nearest_many(need)
    os.makedirs(a.out, exist_ok=True)
    gen = datetime.now(TPE).isoformat(timespec="minutes")
    for on, (code, _, _) in origins.items():
        r, used = [None] * len(groups), {}
        for (o2, gi), (smp, km, mins) in geo.items():
            if o2 != on:
                continue
            segs = segments(smp, town_of, cache)
            rows = []
            for ti, lo, hi, skm in segs:
                if ti not in used:
                    used[ti] = len(used)
                rows.append([used[ti], lo, hi, skm])
            r[gi] = [round(km, 1), int(round(mins))] + rows
        tl = [None] * len(used)
        for ti, j in used.items():
            tl[j] = tix.keys[ti]
        out = {"v": 1, "gen": gen, "o": on, "towns": tl, "g": camp_g, "r": r,
               "note": "路線：OpenStreetMap 貢獻者＋OSRM；海拔：Open-Meteo（Copernicus DEM 90m）"}
        p = os.path.join(a.out, code + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print("%s %s：%d 條、%d 個鄉鎮、%d KB" % (on, code, sum(1 for x in r if x), len(tl), os.path.getsize(p) // 1024))
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"v": 1, "gen": gen, "o": {k: v[0] for k, v in ORIGINS.items()}}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
