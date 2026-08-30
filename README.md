# campfire-camp-weather

營火部落營地天氣資料。每天兩次抓中央氣象署鄉鎮天氣預報，
用營地與鄉鎮預報點的海拔差修正氣溫，輸出 `docs/weather.json` 給
campfiretw.com 的營地天氣頁使用。

## 設定步驟

1. Settings → Secrets and variables → Actions → New repository secret
   名稱 `CWA_API_KEY`，值填氣象資料開放平臺的授權碼（CWA- 開頭）
2. Settings → Pages → Source 選 `main` 分支 `/docs` 資料夾
3. Actions 分頁 →「產生營地天氣資料」→ Run workflow 手動跑一次

跑完之後 JSON 的網址是

    https://campfire20180312-lgtm.github.io/campfire-camp-weather/weather.json

## 排程

台北時間每天 05:40 與 17:40 各跑一次（workflow 檔裡用 UTC 寫）。

## 資料來源與內容

- 預報：中央氣象署開放資料 F-D0047-091 鄉鎮天氣預報－臺灣未來 1 週天氣預報
- 營地清單：campfiretw.com/camp-database/ 頁面內嵌資料（397 筆，含 DEM 海拔）
- 預報點海拔：opentopodata SRTM 30m，結果快取在 `scripts/town_elev.json`

輸出只含頁面會用到的欄位（營地名、縣市鄉鎮、海拔、對應預報點、逐日預報），
座標、價格、分類等留在資料庫頁，不在這裡再複製一份。

## 換算

    營地溫度 = 預報溫度 − (營地海拔 − 預報點海拔) ÷ 100 × 0.6

體感另用風寒公式（氣溫 10°C 以下、風速 1.3 m/s 以上才適用）。
