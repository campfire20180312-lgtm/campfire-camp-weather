#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""風場圖資料：氣象署區域預報模式 WRF 3 公里（開放資料 M-A0064）的 10 米風 → 台灣周邊的小圖檔。

輸出（預設 docs/wind/）：
  wind.json            起算時間、範圍、每一格的時間與數值範圍
  w000.png … w084.png  每 6 小時一張。紅色＝東西向風 U、綠色＝南北向風 V（各自線性對到 0–255），第 0 列是最北邊

來源檔一個約 180 MB，這裡只用 HTTP Range 抓 10 米 U、V 兩段（各約 2.3 MB），不下載整個檔。
資料是 GRIB2 簡單壓縮（template 5.0）、Lambert 正形投影（template 3.30），只用 numpy 解，不需要 eccodes。

用法：
  python3 build_wind.py                      從氣象署開放資料的公開儲存空間抓（不需要授權碼）
  python3 build_wind.py --skip-same          跟資料夾裡現有的是同一批（起算時間一樣）就不重抓；排程用這個
  python3 build_wind.py --local 檔案.grb2     讀本機檔（裡面是幾段 10 米 U、V 訊息接在一起），測試用
  python3 build_wind.py --out 資料夾
授權：氣象資料開放平臺使用規範（政府資料開放授權條款），頁面上要標示「交通部中央氣象署」。"""
import argparse, http.client, io, json, math, os, struct, sys, time, urllib.parse
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image

BASE = os.environ.get('WIND_BASE') or 'https://cwaopendata.s3.ap-northeast-1.amazonaws.com/Model/M-A0064-%03d.grb2'   # WIND_BASE 是測試用
HOURS = list(range(0, 85, 6))
# 輸出範圍：台灣本島、澎湖、蘭嶼綠島、金門、馬祖，四周多留一圈海面；0.03 度一格（約 3 公里）
W, S, E, N, STEP = 117.0, 20.0, 124.5, 27.2, 0.03
R_EARTH = 6371229.0
TW = timezone(timedelta(hours=8))


# ---------- GRIB2 ----------
def parse_message(buf, need_data=True):
    """解一段 GRIB2 訊息。回傳 dict：ref、fh、cat、num、stype、sval、grid、values（need_data 時）"""
    if buf[:4] != b'GRIB' or buf[7] != 2:
        raise ValueError('不是 GRIB2')
    total = struct.unpack('>Q', buf[8:16])[0]
    m = {'len': total, 'disc': buf[6]}
    p = 16
    while p + 5 <= len(buf):
        if buf[p:p + 4] == b'7777':
            break
        sl, sn = struct.unpack('>IB', buf[p:p + 5])
        if sl < 5:
            break
        s = buf[p:p + sl]
        if sn == 1:
            y, mo, d, h, mi, se = struct.unpack('>HBBBBB', s[12:19])
            m['ref'] = datetime(y, mo, d, h, mi, se, tzinfo=timezone.utc)
        elif sn == 3:
            tpl = struct.unpack('>H', s[12:14])[0]
            m['gtpl'] = tpl
            if tpl == 30 and len(s) >= 73:
                def i4(o):   # GRIB 的有號整數是「符號位＋數值」
                    v = struct.unpack('>I', s[o:o + 4])[0]
                    return -(v & 0x7fffffff) if v & 0x80000000 else v
                m['grid'] = {
                    'nx': struct.unpack('>I', s[30:34])[0], 'ny': struct.unpack('>I', s[34:38])[0],
                    'la1': i4(38) / 1e6, 'lo1': i4(42) / 1e6, 'flags': s[46],
                    'lad': i4(47) / 1e6, 'lov': i4(51) / 1e6,
                    'dx': struct.unpack('>I', s[55:59])[0] / 1e3, 'dy': struct.unpack('>I', s[59:63])[0] / 1e3,
                    'scan': s[64], 'latin1': i4(65) / 1e6, 'latin2': i4(69) / 1e6}
        elif sn == 4:
            m['cat'], m['num'] = s[9], s[10]
            m['tunit'] = s[17]
            m['fh'] = struct.unpack('>I', s[18:22])[0]
            m['stype'] = s[22]
            sf = s[23]
            sf = -(sf & 0x7f) if sf & 0x80 else sf
            m['sval'] = struct.unpack('>I', s[24:28])[0] * 10.0 ** (-sf)
        elif sn == 5:
            m['npts'] = struct.unpack('>I', s[5:9])[0]
            m['dtpl'] = struct.unpack('>H', s[9:11])[0]
            m['R'] = struct.unpack('>f', s[11:15])[0]
            e, d = struct.unpack('>HH', s[15:19])
            m['E'] = -(e & 0x7fff) if e & 0x8000 else e
            m['D'] = -(d & 0x7fff) if d & 0x8000 else d
            m['bits'] = s[19]
            if not need_data:
                return m
        elif sn == 6:
            m['bitmap'] = s[5]
        elif sn == 7:
            if m.get('dtpl') != 0:
                raise ValueError('不是簡單壓縮（template 5.%s）' % m.get('dtpl'))
            if m.get('bitmap', 255) != 255:
                raise ValueError('有 bitmap，這個腳本沒處理')
            raw = np.frombuffer(s, dtype=np.uint8, offset=5)
            b, n = m['bits'], m['npts']
            if b == 0:
                x = np.zeros(n, dtype=np.float64)
            elif b % 8 == 0:
                k = b // 8
                a = raw[:n * k].reshape(n, k).astype(np.uint32)
                x = np.zeros(n, dtype=np.uint32)
                for j in range(k):
                    x = (x << 8) | a[:, j]
                x = x.astype(np.float64)
            else:
                bitsarr = np.unpackbits(raw)[:n * b].reshape(n, b)
                x = bitsarr.dot(1 << np.arange(b - 1, -1, -1, dtype=np.uint64)).astype(np.float64)
            m['values'] = (m['R'] + x * 2.0 ** m['E']) / 10.0 ** m['D']
        p += sl
    return m


def is_wind10(m, num):
    return m.get('disc') == 0 and m.get('cat') == 2 and m.get('num') == num and m.get('stype') == 103 and abs(m.get('sval', -1) - 10) < 0.01


# ---------- 抓資料 ----------
_CONN = {}


def _request(method, url, headers):
    """同一台主機共用一條連線（一個檔要讀六十幾段表頭，每次重新連線太慢）。連線斷了就重連再試。"""
    u = urllib.parse.urlsplit(url)
    key = (u.scheme, u.netloc)
    last = None
    for t in range(5):
        try:
            c = _CONN.get(key)
            if c is None:
                c = _CONN[key] = (http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection)(u.netloc, timeout=120)
            h = {'User-Agent': 'campfire-camp-weather', 'Connection': 'keep-alive'}
            h.update(headers)
            c.request(method, u.path + ('?' + u.query if u.query else ''), headers=h)
            r = c.getresponse()
            data = r.read()
            if r.status not in (200, 206):
                raise IOError('HTTP %d %s' % (r.status, url))
            return r, data
        except Exception as e:   # noqa
            last = e
            try:
                _CONN.pop(key).close()
            except Exception:   # noqa
                pass
            time.sleep(2 * (t + 1))
    raise last


def http_range(url, a, b):
    last = None
    for t in range(3):
        r, data = _request('GET', url, {'Range': 'bytes=%d-%d' % (a, b)})
        if r.status == 206 and len(data) == b - a + 1:
            return data
        last = IOError('要 %d 位元組、拿到 %d（HTTP %d）' % (b - a + 1, len(data), r.status))
        time.sleep(3)
    raise last


def http_size(url):
    r, _ = _request('HEAD', url, {})
    return int(r.getheader('Content-Length'))


def remote_run(hour=0):
    """只讀檔頭，看氣象署現在放的是哪一次起算"""
    return parse_message(http_range(BASE % hour, 0, 599), need_data=False)['ref']


def find_wind(url, hint=None):
    """回傳 (U 訊息位移, 長度, V 訊息位移, 長度)。hint 是上一個檔找到的「U 是第幾段」，先猜、猜錯再整檔掃表頭。"""
    total = http_size(url)
    off, idx, found = 0, 0, {}
    while off < total:
        head = parse_message(http_range(url, off, min(total, off + 600) - 1), need_data=False)
        if is_wind10(head, 2):
            found['u'] = (off, head['len'], idx)
        if is_wind10(head, 3):
            found['v'] = (off, head['len'])
        if 'u' in found and 'v' in found:
            return found['u'][0], found['u'][1], found['v'][0], found['v'][1]
        off += head['len']
        idx += 1
    raise RuntimeError('找不到 10 米風：' + url)


def remote_frames(hours):
    for h in hours:
        url = BASE % h
        uo, ul, vo, vl = find_wind(url)
        mu = parse_message(http_range(url, uo, uo + ul - 1))
        mv = parse_message(http_range(url, vo, vo + vl - 1))
        yield mu, mv


def local_frames(path):
    buf = open(path, 'rb').read()
    off, pend = 0, {}
    while off < len(buf):
        total = struct.unpack('>Q', buf[off + 8:off + 16])[0]
        m = parse_message(buf[off:off + total])
        off += total
        key = (m['ref'], m['fh'])
        if is_wind10(m, 2):
            pend.setdefault(key, {})['u'] = m
        elif is_wind10(m, 3):
            pend.setdefault(key, {})['v'] = m
    for key in sorted(pend, key=lambda k: k[1]):
        if 'u' in pend[key] and 'v' in pend[key]:
            yield pend[key]['u'], pend[key]['v']


# ---------- 投影與取樣 ----------
def lambert_ij(g, lat, lon):
    """經緯度 → 模式格點座標（i 往東、j 往北，可帶小數）"""
    p1, p2 = math.radians(g['latin1']), math.radians(g['latin2'])
    if abs(p1 - p2) < 1e-9:
        n = math.sin(p1)
    else:
        n = math.log(math.cos(p1) / math.cos(p2)) / math.log(math.tan(math.pi / 4 + p2 / 2) / math.tan(math.pi / 4 + p1 / 2))
    F = math.cos(p1) * math.tan(math.pi / 4 + p1 / 2) ** n / n

    def xy(la, lo):
        rho = R_EARTH * F / np.tan(np.pi / 4 + np.radians(la) / 2) ** n
        th = n * np.radians(((lo - g['lov'] + 180) % 360) - 180)
        return rho * np.sin(th), -rho * np.cos(th)
    x0, y0 = xy(g['la1'], g['lo1'])
    x, y = xy(lat, lon)
    return (x - x0) / g['dx'], (y - y0) / g['dy'], n


def bilinear(a, fi, fj):
    ny, nx = a.shape
    fi = np.clip(fi, 0, nx - 1.001)
    fj = np.clip(fj, 0, ny - 1.001)
    i0 = np.floor(fi).astype(np.int64)
    j0 = np.floor(fj).astype(np.int64)
    di, dj = fi - i0, fj - j0
    return (a[j0, i0] * (1 - di) * (1 - dj) + a[j0, i0 + 1] * di * (1 - dj) +
            a[j0 + 1, i0] * (1 - di) * dj + a[j0 + 1, i0 + 1] * di * dj)


def to_latlon_grid(mu, mv):
    g = mu['grid']
    if g['scan'] != 64:
        raise ValueError('掃描方向不是 64（先西到東、再南到北）：%s' % g['scan'])
    u = mu['values'].reshape(g['ny'], g['nx'])
    v = mv['values'].reshape(g['ny'], g['nx'])
    nx = int(round((E - W) / STEP)) + 1
    ny = int(round((N - S) / STEP)) + 1
    lon = W + STEP * np.arange(nx)
    lat = N - STEP * np.arange(ny)          # 第 0 列最北
    LON, LAT = np.meshgrid(lon, lat)
    fi, fj, n = lambert_ij(g, LAT, LON)
    if fi.min() < 0 or fj.min() < 0 or fi.max() > g['nx'] - 1 or fj.max() > g['ny'] - 1:
        raise ValueError('輸出範圍超出模式範圍')
    uu, vv = bilinear(u, fi, fj), bilinear(v, fi, fj)
    if g['flags'] & 0x08:   # U、V 是沿模式格線方向：轉回正東、正北
        ang = n * np.radians(((LON - g['lov'] + 180) % 360) - 180)
        uu, vv = uu * np.cos(ang) + vv * np.sin(ang), -uu * np.sin(ang) + vv * np.cos(ang)
    return uu, vv, nx, ny


def encode_png(uu, vv):
    def rng(a):
        lo, hi = float(np.floor(a.min())), float(np.ceil(a.max()))
        if hi - lo < 1:
            hi = lo + 1
        return lo, hi
    (ul, uh), (vl, vh) = rng(uu), rng(vv)
    rgb = np.zeros(uu.shape + (3,), dtype=np.uint8)
    rgb[..., 0] = np.clip(np.round((uu - ul) / (uh - ul) * 255), 0, 255)
    rgb[..., 1] = np.clip(np.round((vv - vl) / (vh - vl) * 255), 0, 255)
    bio = io.BytesIO()
    Image.fromarray(rgb, 'RGB').save(bio, 'PNG', optimize=True)
    return bio.getvalue(), [ul, uh], [vl, vh]


class NotReady(Exception):
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--local')
    ap.add_argument('--out', default='docs/wind')
    ap.add_argument('--skip-same', action='store_true')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    jp = os.path.join(a.out, 'wind.json')
    if a.skip_same and not a.local and os.path.exists(jp):
        try:
            have = json.load(open(jp, encoding='utf-8')).get('run')
        except Exception:   # noqa
            have = None
        now = remote_run().strftime('%Y-%m-%dT%H:%MZ')
        print('氣象署現在放的是 %s 起算，手上的是 %s' % (now, have))
        if have == now:
            print('同一批，不重抓')
            return
    frames, files, run, nx, ny = [], [], None, 0, 0
    src = local_frames(a.local) if a.local else remote_frames(HOURS)
    try:
        for mu, mv in src:
            if run is None:
                run = mu['ref']
            if mu['ref'] != run or mv['ref'] != run or mu['fh'] != mv['fh']:
                raise NotReady('第 %s 小時那個檔是 %s 起算，前面的是 %s' % (mu['fh'], mu['ref'], run))
            uu, vv, nx, ny = to_latlon_grid(mu, mv)
            png, ur, vr = encode_png(uu, vv)
            name = 'w%03d.png' % mu['fh']
            files.append((name, png))
            sp = np.hypot(uu, vv)
            t = run + timedelta(hours=mu['fh'])
            frames.append({'h': mu['fh'], 't': t.strftime('%Y-%m-%dT%H:%MZ'), 'f': name, 'u': ur, 'v': vr,
                           'max': round(float(sp.max()), 1), 'kb': round(len(png) / 1024, 1)})
            print('%s %s  U %s V %s  最大 %.1f m/s  %.0f KB' % (name, t.astimezone(TW).strftime('%m/%d %H:%M'), ur, vr, sp.max(), len(png) / 1024), flush=True)
    except NotReady as e:
        # 氣象署正在換新一批，新舊檔混在一起：這次不動現有資料，下一班排程再抓
        print('::warning::氣象署的檔案正在換新一批（%s），這次先不更新' % e)
        return
    if len(frames) != len(HOURS) and not a.local:
        raise RuntimeError('只拿到 %d 格（應該 %d 格）' % (len(frames), len(HOURS)))
    if len(frames) < 2:
        raise RuntimeError('只拿到 %d 格' % len(frames))
    # 全部算好才一次寫出去，中途失敗不會留下半新半舊的圖
    for name, png in files:
        open(os.path.join(a.out, name), 'wb').write(png)
    meta = {'gen': datetime.now(TW).strftime('%Y-%m-%d %H:%M'), 'run': run.strftime('%Y-%m-%dT%H:%MZ'),
            'src': '交通部中央氣象署 區域預報模式 WRF 3 公里（M-A0064）10 米風',
            'bbox': [W, S, round(W + STEP * (nx - 1), 4), N], 'nx': nx, 'ny': ny, 'step': STEP, 'frames': frames}
    json.dump(meta, open(jp, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('完成：%d 格，起算 %s（台灣時間 %s）' % (len(frames), meta['run'], run.astimezone(TW).strftime('%m/%d %H:%M')))


if __name__ == '__main__':
    main()
