#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐 3 小時預報（2026-10-24 達哥：天氣頁加「逐 3 小時預報」）

氣象署各縣市「鄉鎮天氣預報－未來3天」（F-D0047-001、005 … 085，每個縣市一支）
整理成每個鄉鎮從現在起每 3 小時一格，輸出 docs/wx3h.json 給天氣頁的營地天氣用。
溫度是鄉鎮預報點的值，頁面再用營地海拔修正（跟 weather.json 同一套遞減率）。

這支程式出錯不會影響 weather.json：workflow 裡這一步失敗只記警告，網頁讀不到就不顯示逐 3 小時。

用法
  export CWA_API_KEY=CWA-XXXXXXXX
  python3 build_wx3h.py --out ../docs/wx3h.json
  python3 build_wx3h.py --inspect          # 只印出第一支的結構，核對要素名稱
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_weather import (CWA_BASE, TPE, _get, _value, _num, _num_gte,   # noqa: E402
                           _norm_county, _f)

# 各縣市未來 3 天（逐 3 小時）；未來 1 週是 003、007 … 087
DATASETS = ["F-D0047-%03d" % n for n in range(1, 86, 4)]
HOURS = 72          # 從現在起抓幾小時
STEP = 3

# 要素名稱（氣象署 2024 改版後的中文名稱）；比對時容許前後有別的字
EL = {
    "t": (re.compile(r"^溫度$"), ["Temperature", "溫度"]),
    "a": (re.compile(r"體感溫度"), ["ApparentTemperature", "體感溫度"]),
    "p": (re.compile(r"降雨機率"), ["ProbabilityOfPrecipitation", "降雨機率"]),
    "w": (re.compile(r"^天氣現象$"), ["Weather", "天氣現象"]),
    "c": (re.compile(r"^天氣現象$"), ["WeatherCode", "天氣現象代碼"]),
    "s": (re.compile(r"^風速$"), ["WindSpeed", "風速"]),
    "b": (re.compile(r"^風速$"), ["BeaufortScale", "蒲福風級"]),
}


def _times(t):
    """回傳 (起, 迄)；只有單一時間點（DataTime）的，迄＝None。"""
    s = t.get("StartTime") or t.get("startTime")
    e = t.get("EndTime") or t.get("endTime")
    d = t.get("DataTime") or t.get("dataTime")
    try:
        if s:
            return datetime.fromisoformat(s), (datetime.fromisoformat(e) if e else None)
        if d:
            return datetime.fromisoformat(d), None
    except ValueError:
        pass
    return None, None


def _strict(t, prefer):
    """跟 _value 一樣，但找不到指定的鍵就回 None（代碼、風級不能拿別的值頂替）。"""
    evs = t.get("ElementValue") or t.get("elementValue") or []
    if isinstance(evs, dict):
        evs = [evs]
    for ev in evs:
        for k in prefer:
            if k in ev:
                return ev[k]
    return None


def _pick(els, rx):
    for name, arr in els.items():
        if rx.search(name or ""):
            return arr
    return []


def town_slots(els, t0, n):
    """把一個鄉鎮的各要素放進 n 個 3 小時格子。"""
    out = {k: [None] * n for k in EL}
    for k, (rx, prefer) in EL.items():
        best = [None] * n        # 單點資料：記下離格子起點最近的那筆
        for t in _pick(els, rx):
            st, en = _times(t)
            if st is None:
                continue
            st = st.astimezone(TPE)
            raw = _strict(t, prefer) if k in ("c", "b") else _value(t, prefer)
            if k in ("w",):
                v = raw if raw not in (None, "") else None
            elif k == "c":
                v = str(raw) if raw not in (None, "") else None
            elif k == "s":
                v, _ = _num_gte(raw)
            else:
                v = _num(raw)
            if v is None:
                continue
            if en is not None:
                en = en.astimezone(TPE)
                for i in range(n):
                    a = t0 + timedelta(hours=STEP * i)
                    if st <= a < en or (a <= st < a + timedelta(hours=STEP)):
                        if k in ("s", "b"):
                            out[k][i] = v if out[k][i] is None else max(out[k][i], v)
                        elif out[k][i] is None:
                            out[k][i] = v
            else:
                i = int((st - t0).total_seconds() // (STEP * 3600))
                if 0 <= i < n:
                    off = (st - (t0 + timedelta(hours=STEP * i))).total_seconds()
                    if k in ("s", "b"):
                        out[k][i] = v if out[k][i] is None else max(out[k][i], v)
                    elif best[i] is None or off < best[i]:
                        best[i] = off
                        out[k][i] = v
    return out


def build(key):
    now = datetime.now(TPE)
    t0 = now.replace(minute=0, second=0, microsecond=0)
    t0 = t0.replace(hour=t0.hour - t0.hour % STEP)
    n = HOURS // STEP + 1
    towns, wxs, total = {}, [], 0
    for ds in DATASETS:
        js = _get(CWA_BASE + ds, {"Authorization": key, "format": "JSON"}, 180).json()
        rec = js.get("records", js)
        wraps = rec.get("Locations") or rec.get("locations") or []
        if isinstance(wraps, dict):
            wraps = [wraps]
        for wrap in wraps:
            ct = _norm_county(wrap.get("LocationsName") or wrap.get("locationsName") or "")
            for loc in wrap.get("Location") or wrap.get("location") or []:
                name = loc.get("LocationName") or loc.get("locationName")
                els = {}
                for el in loc.get("WeatherElement") or loc.get("weatherElement") or []:
                    en = el.get("ElementName") or el.get("elementName")
                    els[en] = el.get("Time") or el.get("time") or []
                sl = town_slots(els, t0, n)
                if not any(v is not None for v in sl["t"]):
                    continue
                row = {}
                for k, arr in sl.items():
                    if k == "w":
                        idx = []
                        for v in arr:
                            if v is None:
                                idx.append(None)
                                continue
                            if v not in wxs:
                                wxs.append(v)
                            idx.append(wxs.index(v))
                        row["w"] = idx
                    elif k == "c":
                        row["c"] = [int(v) if v and str(v).isdigit() else None for v in arr]
                    elif k in ("p", "b"):
                        row[k] = [None if v is None else int(round(v)) for v in arr]
                    else:
                        row[k] = [None if v is None else round(v, 1) for v in arr]
                # 全部是空的欄位不輸出
                row = {k: v for k, v in row.items() if any(x is not None for x in v)}
                towns[ct + "|" + name] = row
                total += 1
        time.sleep(0.4)
    # 最前面幾格如果全台都沒有值（發布時間在格子之後），整排往後移
    first = 0
    while first < n - 1 and not any(r.get("t") and r["t"][first] is not None for r in towns.values()):
        first += 1
    if first:
        for r in towns.values():
            for k in r:
                r[k] = r[k][first:]
        t0 = t0 + timedelta(hours=STEP * first)
    if total < 300:
        raise RuntimeError("逐 3 小時預報只整理到 %d 個鄉鎮，預期 300 以上" % total)
    return {
        "generated": now.isoformat(timespec="minutes"),
        "source": "中央氣象署開放資料 鄉鎮天氣預報－各縣市未來3天（F-D0047 系列）",
        "t0": t0.isoformat(timespec="minutes"),
        "step": STEP,
        "wx": wxs,
        "count": total,
        "towns": towns,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "docs", "wx3h.json"))
    ap.add_argument("--inspect", action="store_true")
    args = ap.parse_args()
    key = (os.environ.get("CWA_API_KEY") or "").strip()
    if not key:
        sys.exit("請先設定環境變數 CWA_API_KEY")
    if args.inspect:
        js = _get(CWA_BASE + DATASETS[0], {"Authorization": key, "format": "JSON"}, 180).json()
        rec = js.get("records", js)
        wrap = (rec.get("Locations") or [{}])[0]
        loc = (wrap.get("Location") or [{}])[0]
        for el in loc.get("WeatherElement") or []:
            tt = el.get("Time") or []
            print(el.get("ElementName"), len(tt), json.dumps(tt[:2], ensure_ascii=False)[:400])
        return
    data = build(key)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    one = next(iter(data["towns"].items()))
    print("已寫入 %s（%d 個鄉鎮，從 %s 起 %d 格，%.1f KB）" % (
        args.out, data["count"], data["t0"], len(one[1].get("t", [])), os.path.getsize(args.out) / 1024))
    print("範例 %s：%s" % (one[0], json.dumps(one[1], ensure_ascii=False)[:600]))


if __name__ == "__main__":
    main()
