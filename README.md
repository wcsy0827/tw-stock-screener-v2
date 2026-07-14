# 台股選股系統 MVP（Phase 1 + Phase 2 + Phase 2.5）

移植自 `D:\us-stock-screener`（美股 S&P 500 選股系統）的三層篩選架構，範圍縮到
「台灣50＋中型100」近似範圍（依 30 日均成交金額排序、加名單遲滯，約 150~180 支），
驗證資料管線與 L2 分數分布是否合理。**本階段不接 L3 AI 精選、不做 tracker 追蹤、
不發布報告。**

跨 session 的待辦清單見 [TODO.md](TODO.md)（Phase 3）。

## 執行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py --dry-run
```

輸出候選池 JSON 至 `data/candidates.json`，並在終端機印出分數分布 Top 10。

詳細人工驗證步驟見 [MANUAL_TESTING.md](MANUAL_TESTING.md)。

## 架構

```
universe.py    TWSE OpenAPI 抓上市公司清單 + ISIN頁面產業中文名稱 + 當日成交金額 shortlist，
                 並依 30日均成交金額重排 + 名單遲滯（roster hysteresis）決定最終名單
                 （data/universe_roster.json 持久化，跨執行維持名單穩定）
fetcher.py     yfinance 批次下載日K+info，快取邏輯移植自美股版
                 （時區改 Asia/Taipei 13:30 收盤 + 15分鐘 buffer）
taifex_vix.py  抓取 TAIFEX 臺指選擇權波動率指數（真 VIX 等價物）最新值
market.py      Regime 判定：優先用真 VIX（taifex_vix.py），失敗 fallback 為
                 ^TWII HV20（20日已實現波動率），邊界值已用歷史分布校準
                 （scripts/calibrate_hv.py）
filter.py      L1 流動性硬篩，門檻已用實際分布校準（scripts/calibrate_l1.py）
scorer.py      六維度 L2 評分移植，RS 維度改用同產業 equal-weight 籃子替代 sector ETF
main.py        兩段式串起以上模組，--dry-run 輸出候選池，累積波動率訊號歷史
scripts/       一次性校準腳本（HV20 邊界、L1 門檻），供未來重新校準時重跑
```

## 已校準/已接上項目（Phase 2 + Phase 2.5）

- **真 VIX 接入**（`taifex_vix.py`）：TAIFEX 臺指選擇權波動率指數，選擇權隱含
  波動率，語意與美股 VIX 相同（前瞻指標，非替代品）。下載失敗時乾淨 fallback
  為 `^TWII` HV20（落後指標），`vix_source` 欄位記錄實際使用的訊號來源。
  **已知限制**：TAIFEX 該 endpoint 只保留約 3~4 個月近期資料，不是深度歷史
  archive，這次沒有足夠深度可以獨立校準邊界，暫時沿用 HV20 的校準值（見下方）。
  `data/taifex_vix_history.json` 每次執行累積一筆，供未來（6~12 個月後）重新
  校準用。
- **Regime 五象限邊界**（`market.py`）：用 `^TWII` 近 5 年歷史（2021-07-14~
  2026-07-14，1194 個 HV20 樣本）分位數校準，`VOL_LOW_THRESHOLD=19.44`（P70）、
  `VOL_HIGH_THRESHOLD=27.49`（P90）——對齊「Regime 出現頻率」而非照抄美股 VIX
  絕對值。真 VIX 與 HV20 兩種訊號源目前共用同一組邊界（見上方限制說明）。
- **L1 流動性門檻**（`filter.py`）：`MIN_DAILY_TRADE_VALUE=NT$10億`、
  `MIN_MARKET_CAP=NT$150億`，設在名單自身 P5 分位數之下，作安全網用（universe 本身
  已是流動性前 150~180 名，這兩項門檻在池內近乎冗餘）。`MAX_ATR_PCT=8%` 維持不變
  （P70=7.83% < 8% < P90=9.25%，是有意義的風控上限，實測會刷掉約 2 成）。
- **Universe 排序邏輯**：從單日成交金額排序改為 30 日均成交金額，並加名單遲滯
  （已在名單內的股票除非跌出前 180 名才移除），避免每日換血。
- **產業別顯示名稱**：改用 TWSE ISIN 頁面（`isin.twse.com.tw`）解析出的中文產業
  名稱（如「半導體業」），取代 `t187ap03_L` 回傳的兩位數代碼。

## 未解決的設計問題（下一階段前必須處理，詳見 TODO.md）

1. **VIX 邊界校準仍是暫定值**：真 VIX 已接上（見上方），但因歷史尚淺（約4個月）
   無法獨立校準分位數，暫時沿用 `^TWII` HV20 的校準值。累積 6~12 個月
   `data/taifex_vix_history.json` 後需要重新校準（TODO.md 已記錄）。

2. **漲跌停止損模擬失真**：台股 ±10% 漲跌停鎖死時掛單不會成交，若直接移植美股版
   tracker.py 的「`today_low ≤ stop_loss` 即視為止損成交」邏輯，會系統性低估虧損、
   污染績效資料。**開始做 tracker 追蹤、累積 performance_history 之前，必須先設計
   跌停無量判定機制**（設計草案見 TODO.md Phase 3），否則後續要砍掉重練。

3. **Universe 範圍為近似，非官方指數成分股**：TWSE OpenAPI 沒有「台灣50/中型100
   成分股」endpoint（那是 FTSE 方法論下的 0050/0051 ETF 成分股）。目前用 30 日均
   成交金額排序近似，且僅涵蓋 TWSE 上市（`.TW`），未涵蓋 TPEx 上櫃（`.TWO`）。

## 快取與持久化檔案

| 檔案 | 用途 | 有效期 |
|------|------|--------|
| `.cache/price_YYYYMMDD.pkl` | 日K數據快取 | 當日 |
| `.cache/info_YYYYMMDD.json` | 基本面快取 | 7日內取最新一份 |
| `data/universe_roster.json` | 名單遲滯用的前次名單 | 永久（每次執行覆寫） |
| `data/taifex_vix_history.json` | 波動率訊號歷史（供未來重新校準） | 永久（只增不改，同日重跑覆寫當天那筆） |
| `data/candidates.json` | 候選池輸出 | 每次執行覆寫 |

`--no-cache` 強制重新下載 price/info（不影響 `universe_roster.json`／
`taifex_vix_history.json`）。

## 後續階段（尚未開始，見 TODO.md）

Phase 3：漲跌停止損機制設計（需先抗辯審查）→ tracker 移植 → L3 AI 精選（DeepSeek）
→ 報告發布。本階段刻意不建立 `specs/`/`plans/` 規格治理，待 Phase 3 真正開始做
tracker/ranker 再視需要引入。
