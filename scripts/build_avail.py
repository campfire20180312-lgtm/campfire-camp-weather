#!/usr/bin/env python3
"""週末還訂得到的營地（2026-10-23 達哥要求，每天自動更新）

1. 從 campfiretw.com/camp-database/ 頁面內嵌的 DATA 取出每家營地的訂位平台店家（plat 欄位）
   plat = {ic 愛露營, ec 露營樂, ac 亞洲露營, ct 玩露TRIP}；同平台兩家店時值是陣列
2. 查未來三個週末的週五、週六晚還剩幾帳
   - 露營樂：POST /ajax_store/daily_stocks_query，回每天剩餘帳數（Sum_Stocks，含所有營位型態）
   - 亞洲露營：API enable-rooms 取營位名稱（自備帳篷＝自搭帳），rooms-period-price-availability 取每晚剩餘
   - 玩露TRIP：營區頁拿 CSRF 再 POST checkAvailability，回可訂營位；房間、露營車、狩獵帳不算
   - 愛露營：月曆資料走 Firestore 即時連線，用 Playwright 開營區頁讀月曆（剩幾帳／滿／公休）
3. 輸出 docs/avail.json：
   {"generated": "...", "nights": ["2026-10-24", ...],
    "c": {"fid": {"ic": [3, 0, null, ...], "ec": [...]}}}
   陣列跟 nights 對齊：數字＝剩幾帳（0＝滿），-1＝公休，null＝查不到

這些是平台網頁自己用的查詢，不是公開 API：一天一次、每家間隔，平台改版就要跟著修。
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_weather import DB_URL, _js_literal  # noqa: E402

TPE = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
GAP = 1.2          # 每家之間隔幾秒，壓低頻率
IC_BUDGET = 40 * 60   # 愛露營最多跑 40 分鐘，超過就停（workflow 上限 75 分鐘）
T0 = time.time()
LOG = {"ic": [0, 0], "ec": [0, 0], "ac": [0, 0], "ct": [0, 0]}   # [成功, 失敗]


def ids(v):
    return v if isinstance(v, list) else [v]


def nights_ahead(today, weekends=3):
    """未來三個週末的週五、週六晚（今天是週六就從今天算）。"""
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    out = []
    for w in range(weekends):
        s = sat + timedelta(days=7 * w)
        if s - timedelta(days=1) >= today:
            out.append(s - timedelta(days=1))
        out.append(s)
    return out


def load_camps():
    import base64
    html = requests.get(DB_URL, headers={"User-Agent": UA}, timeout=60).text
    m = re.search(r'var\s+S\s*=\s*"([A-Za-z0-9+/=]+)"', html)
    if not m:
        raise RuntimeError("找不到資料庫頁面的內嵌資料（var S）")
    code = base64.b64decode(m.group(1)).decode("utf-8")
    data = _js_literal(code, "DATA")
    camps = [r for r in data if r.get("plat") and not r.get("closed") and not r.get("bo") and not r.get("cp")]
    print("有訂位平台的營地 %d 家" % len(camps))
    return camps


# ---------------------------------------------------------------- 露營樂

def q_easycamp(sess, sid, nights):
    url = "https://www.easycamp.com.tw/Store_%s.html" % sid
    sess.get(url, timeout=40)
    r = sess.post("https://www.easycamp.com.tw/ajax_store/daily_stocks_query",
                  data={"start_date": nights[0].isoformat(),
                        "end_date": (nights[-1] + timedelta(days=1)).isoformat(),
                        "store_id": sid},
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": url}, timeout=40)
    js = r.json()
    d = js.get("data") or {}
    out = []
    for n in nights:
        v = d.get(n.isoformat())
        out.append(int(v["Sum_Stocks"]) if v and str(v.get("Sum_Stocks", "")).lstrip("-").isdigit() else None)
    return out


# ---------------------------------------------------------------- 亞洲露營

AC = "https://asiacamp.bvtrip.tw/api/v1/asiacamp/camp-page/"


def q_asiacamp(sess, cid, nights):
    rooms = sess.get(AC + "enable-rooms", params={"camp_id": cid}, timeout=40).json().get("data") or []
    self_ids = set()
    for rm in rooms:
        beds = " ".join(b.get("type", "") for b in (rm.get("beds") or []))
        if "自備帳篷" in beds or re.search(r"自搭|車宿|營位", rm.get("name", "")):
            self_ids.add(rm.get("id"))
    js = sess.get(AC + "rooms-period-price-availability",
                  params={"camp_id": cid, "start": nights[0].isoformat(),
                          "end": (nights[-1] + timedelta(days=1)).isoformat(), "person_count": 2},
                  timeout=40).json()
    per = {}
    for x in js.get("data") or []:
        cd = x.get("customData") or {}
        if self_ids and cd.get("room_id") not in self_ids:
            continue
        per.setdefault(x.get("dates"), 0)
        if cd.get("available"):
            per[x["dates"]] += int(cd.get("remain") or 0)
    return [per.get(n.isoformat()) for n in nights]


# ---------------------------------------------------------------- 玩露TRIP

CT_NOT_SELF = re.compile(r"房|民宿|小木屋|木屋|艙|露營車|狩獵|豪華|懶人|免搭|貨櫃|套房|通鋪|帳篷屋|蒙古包")


def q_camptrip(sess, slug, nights):
    url = "https://booking.camptrip.com.tw/hotel/" + urllib.parse.quote(slug, safe="")
    html = sess.get(url, timeout=40).text
    tok = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    hid = re.search(r'bravo_booking_data\s*=\s*\{"id":(\d+)', html)
    if not tok or not hid:
        raise RuntimeError("沒有 CSRF 或 hotel id")
    out = []
    for n in nights:
        r = sess.post("https://booking.camptrip.com.tw/hotel/checkAvailability",
                      data={"hotel_id": hid.group(1), "start_date": n.isoformat(),
                            "end_date": (n + timedelta(days=1)).isoformat(),
                            "adults": 1, "children": 0, "_token": tok.group(1)},
                      headers={"X-CSRF-TOKEN": tok.group(1), "X-Requested-With": "XMLHttpRequest",
                               "Referer": url}, timeout=40)
        js = r.json()
        rooms = js.get("rooms") or []
        cnt = 0
        for rm in rooms:
            if CT_NOT_SELF.search(rm.get("title", "")):
                continue
            try:
                cnt += max(int(rm.get("number") or 1), 0)
            except (TypeError, ValueError):
                cnt += 1
        out.append(cnt)
        time.sleep(0.6)
    return out


# ---------------------------------------------------------------- 愛露營（Playwright）

def parse_ic_calendar(text, year, month):
    """月曆文字 → {date: 值}。月曆開頭可能帶上個月最後幾天、結尾帶下個月頭幾天，日期歸零就換月。"""
    m = re.search(r"日\s+一\s+二\s+三\s+四\s+五\s+六\s+(.*?)\s+\d{4}-\d{2}-\d{2}\s*空位狀況", text, re.S)
    if not m:
        return {}
    tok = m.group(1).split()
    vals, i = [], 0
    while i + 1 < len(tok):
        if tok[i].isdigit() and 1 <= int(tok[i]) <= 31:
            vals.append((int(tok[i]), tok[i + 1]))
            i += 2
        else:
            i += 1
    out = {}
    if not vals:
        return out
    # 第一格不是 1 號就是上個月
    y, mo = (year, month) if vals[0][0] == 1 else ((year, month - 1) if month > 1 else (year - 1, 12))
    prev = 0
    for day, v in vals:
        if day < prev:            # 換月
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        prev = day
        try:
            dt = datetime(y, mo, day).date()
        except ValueError:
            continue
        if v.isdigit():
            out[dt] = int(v)
        elif v == "滿":
            out[dt] = 0
        elif "公休" in v:
            out[dt] = -1
    return out


async def ic_one(page, sid, nights):
    await page.goto("https://m.icamping.app/store/" + sid, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_function("document.body.innerText.includes('空位狀況')", timeout=25000)
    await page.wait_for_timeout(1500)
    got = {}
    for _ in range(3):
        text = await page.inner_text("body")
        hm = re.search(r"(\d{1,2})月\s*(\d{4})\s*空位狀況", text)
        if not hm:
            break
        got.update(parse_ic_calendar(text, int(hm.group(2)), int(hm.group(1))))
        if all(n in got for n in nights):
            break
        btn = page.get_by_text("下個月", exact=True)
        if await btn.count() == 0:
            break
        await btn.first.click()
        await page.wait_for_timeout(2500)
    return [got.get(n) for n in nights]


async def run_icamping(jobs, nights, conc=3):
    from playwright.async_api import async_playwright
    res = {}
    q = asyncio.Queue()
    for j in jobs:
        q.put_nowait(j)
    async with async_playwright() as p:
        br = await p.chromium.launch()
        ctx = await br.new_context(user_agent=UA, locale="zh-TW", viewport={"width": 420, "height": 900})

        t_ic = time.time()

        async def worker():
            page = await ctx.new_page()
            while not q.empty():
                # 前 12 家全部失敗＝平台擋掉或改版，不要硬跑；超過時間預算也停
                if LOG["ic"][0] == 0 and LOG["ic"][1] >= 12:
                    print("::warning::愛露營前 12 家都失敗，停止查詢")
                    break
                if time.time() - t_ic > IC_BUDGET:
                    print("::warning::愛露營超過時間預算，剩 %d 家沒查" % q.qsize())
                    break
                sid = q.get_nowait()
                done = LOG["ic"][0] + LOG["ic"][1]
                if done and done % 25 == 0:
                    print("愛露營 已查 %d 家（成功 %d）" % (done, LOG["ic"][0]), flush=True)
                try:
                    res[sid] = await ic_one(page, sid, nights)
                    LOG["ic"][0] += 1
                except Exception as e:      # noqa: BLE001
                    LOG["ic"][1] += 1
                    print("愛露營 %s 失敗：%s" % (sid, str(e)[:120]))
                    res[sid] = [None] * len(nights)
                    try:
                        await page.close()
                    except Exception:       # noqa: BLE001
                        pass
                    page = await ctx.new_page()
                await asyncio.sleep(GAP)
            await page.close()

        await asyncio.gather(*[worker() for _ in range(conc)])
        await br.close()
    return res


# ---------------------------------------------------------------- 主程式

def add(a, b):
    """同平台兩家店的剩餘帳數相加（任一家查不到就當另一家的值）。"""
    if a is None:
        return b
    if b is None:
        return a
    if a < 0 and b < 0:
        return -1
    return max(a, 0) + max(b, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "docs", "avail.json"))
    ap.add_argument("--limit", type=int, default=0, help="測試用：每個平台只查前幾家")
    args = ap.parse_args()

    now = datetime.now(TPE)
    nights = nights_ahead(now.date())
    camps = load_camps()

    jobs = {"ic": set(), "ec": set(), "ac": set(), "ct": set()}
    for c in camps:
        for k, v in c["plat"].items():
            if k in jobs:
                jobs[k].update(str(x) for x in ids(v))
    if args.limit:
        jobs = {k: set(sorted(v)[:args.limit]) for k, v in jobs.items()}
    print("要查：" + "、".join("%s %d" % (k, len(v)) for k, v in jobs.items()))

    res = {k: {} for k in jobs}
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"})
    for k, fn in (("ec", q_easycamp), ("ac", q_asiacamp), ("ct", q_camptrip)):
        for n_, sid in enumerate(sorted(jobs[k])):
            if n_ and n_ % 25 == 0:
                print("%s 已查 %d 家（成功 %d）" % (k, n_, LOG[k][0]), flush=True)
            if n_ >= 8 and LOG[k][0] == 0:
                print("::warning::%s 前 8 家都失敗，停止查詢" % k)
                break
            try:
                res[k][sid] = fn(sess, sid, nights)
                LOG[k][0] += 1
            except Exception as e:          # noqa: BLE001
                LOG[k][1] += 1
                print("%s %s 失敗：%s" % (k, sid, str(e)[:120]))
            time.sleep(GAP)
    if jobs["ic"]:
        res["ic"] = asyncio.run(run_icamping(sorted(jobs["ic"]), nights))

    out = {}
    for c in camps:
        row = {}
        for k, v in c["plat"].items():
            if k not in res:
                continue
            arr = None
            for sid in ids(v):
                r = res[k].get(str(sid))
                if r is None:
                    continue
                arr = r if arr is None else [add(a, b) for a, b in zip(arr, r)]
            if arr is not None and any(x is not None for x in arr):
                row[k] = arr
        if row:
            out[str(c["fid"])] = row

    doc = {"generated": now.isoformat(timespec="minutes"),
           "nights": [n.isoformat() for n in nights],
           "log": LOG,
           "c": out}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print("完成：%d 家有空位資料；各平台 成功/失敗：%s" % (len(out), json.dumps(LOG)))
    # 某個平台全部失敗（多半是平台改版或擋了 GitHub 的連線）就讓這次執行失敗，GitHub 會寄信通知
    dead = [k for k, (ok, ng) in LOG.items() if ng and not ok]
    if dead:
        print("::error::這些平台全部查詢失敗：%s" % "、".join(dead))
        sys.exit(1)


if __name__ == "__main__":
    main()
