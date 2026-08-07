# 台股選股系統

移植自 `D:\us-stock-screener`（美股 S&P 500 選股系統）的三層篩選架構＋訊號追蹤＋
報告發布全流程，範圍縮到「台灣50＋中型100」近似範圍（依 30 日均成交金額排序、
加名單遲滯，約 150~180 支）。

流程：Universe → L1 流動性篩選（含處置股/分盤集合競價排除）→ L2 技術評分 →
L3 DeepSeek AI 精選 → tracker 訊號追蹤與漲跌停止損模擬 → HTML 報告發布。
未設定 `DEEPSEEK_API_KEY` 時 L3 自動 fallback 為 L2 分數排序（`is_fallback=True`，
不納入 tracker 追蹤）。

跨 session 的待辦清單見 [TODO.md](TODO.md)。

## 執行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env   # 填入 DEEPSEEK_API_KEY 才會啟用 L3 AI 精選
python main.py --dry-run
```

`--dry-run` 會完整跑完 L0~L3、tracker 追蹤（僅收盤後/交易日）、HTML 報告生成，
但略過 git push。輸出候選池 JSON 至 `data/candidates.json`，報告見
`docs/reports/<market_date>.html`（可直接用瀏覽器開啟）。

詳細人工驗證步驟見 [MANUAL_TESTING.md](MANUAL_TESTING.md)。

## 架構

```
universe.py    TWSE OpenAPI 抓上市公司清單 + ISIN頁面產業中文名稱 + 當日成交金額 shortlist，
                 並依 30日均成交金額重排 + 名單遲滯（roster hysteresis）決定最終名單
                 （data/universe_roster.json 持久化，跨執行維持名單穩定）
fetcher.py     yfinance 批次下載日K+info，快取邏輯移植自美股版
                 （時區改 Asia/Taipei 13:30 收盤 + 15分鐘 buffer）
taifex_vix.py  抓取 TAIFEX 臺指選擇權波動率指數（真 VIX 等價物）最新值
market.py      Regime 判定 + fetch_market_context()（供 L3 Prompt 與報告儀表板的
                 大盤/產業籃子背景）：優先用真 VIX（taifex_vix.py），失敗 fallback
                 為 ^TWII HV20（20日已實現波動率），邊界值已用歷史分布校準
                 （scripts/calibrate_hv.py）
disposition.py 排除目前處於 TWSE 處置公告期間或分盤集合競價的股票（補進 L1）
filter.py      L1 流動性硬篩，門檻已用實際分布校準（scripts/calibrate_l1.py）
scorer.py      六維度 L2 評分移植，RS 維度改用同產業 equal-weight 籃子替代 sector ETF
ranker.py      L3 DeepSeek AI 精選：RS_vs_Sector/Beta_60D 複用同產業籃子與 ^TWII，
                 未設定 DEEPSEEK_API_KEY 時 fallback 為 L2 分數排序
tracker.py     訊號追蹤與績效歸檔，含台股漲跌停止損模擬機制（見
                 docs/phase3_limit_lock_design.md）：跌停鎖死時止損單順延至解除日
                 開盤價出場，避免把連續跌停誤記為一般停損
publisher.py   生成每日 HTML 報告 + 首頁索引，dry-run 略過 git push
main.py        串起以上全部模組，--dry-run 輸出候選池 JSON + 本機 HTML 報告
scripts/       一次性校準腳本（HV20 邊界、L1 門檻、漲跌停鎖死量能比），供未來
                 重新校準時重跑
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

## 未解決的設計問題（詳見 TODO.md）

1. **VIX 邊界校準仍是暫定值**：真 VIX 已接上（見上方），但因歷史尚淺（約4個月）
   無法獨立校準分位數，暫時沿用 `^TWII` HV20 的校準值。累積 6~12 個月
   `data/taifex_vix_history.json` 後需要重新校準（TODO.md 已記錄）。

2. **`LOCK_VOLUME_RATIO`（漲跌停鎖死量能枯竭門檻）已校準為 0.6**（2026-07，
   一字跌停真鎖死群定錨法；原「雙峰谷底」假設實測證偽後修正方法論）。已知
   殘餘限制：定錨樣本 85% 集中於 2025-04 崩盤事件、存活者偏差（鎖死至下市股
   不在樣本）、漲停側 U3 沿用跌停側定錨值僅取保守方向；下游績效分析須用
   `exit_deferred` 欄位分群校正順延出場偏差。詳見
   [docs/phase3_limit_lock_design.md](docs/phase3_limit_lock_design.md) §3.1 與附錄 B。

3. **Earnings_Days_Left 維度未移植**：L3 AI Prompt 移植時刻意省略財報剩餘天數
   維度——TWSE 未接入財報日曆資料源，維持恆定值的欄位對 AI 判斷無資訊量，
   待未來有資料源再補上。

4. **Universe 範圍為近似，非官方指數成分股**：TWSE OpenAPI 沒有「台灣50/中型100
   成分股」endpoint（那是 FTSE 方法論下的 0050/0051 ETF 成分股）。目前用 30 日均
   成交金額排序近似，且僅涵蓋 TWSE 上市（`.TW`），未涵蓋 TPEx 上櫃（`.TWO`）。

5. **GitHub Actions 排程已建立**：GitHub 遠端已設定（公開 repo
   [wcsy0827/tw-stock-screener-v2](https://github.com/wcsy0827/tw-stock-screener-v2)，
   已於 2026-08-07 由私有轉公開以免費使用 GitHub Pages），GitHub Pages 已啟用
   （`master` 分支 `/docs` 目錄），報告可於
   https://wcsy0827.github.io/tw-stock-screener-v2/ 瀏覽。
   `.github/workflows/daily-screener.yml` 已設定週一至週五台灣時間 17:00
   自動執行並 push 報告，仍需使用者自行設定 `DEEPSEEK_API_KEY` repo secret
   才能啟用 L3 AI 精選（未設定時 L3 fallback 為 L2 排序）。
   **注意**：repo 為公開，`data/` 下的歷史實跑資料（追蹤名單、績效歸檔、
   VIX 歷史）與選股邏輯原始碼皆可被任何人存取。

## 快取與持久化檔案

| 檔案 | 用途 | 有效期 |
|------|------|--------|
| `.cache/price_YYYYMMDD.pkl` | 日K數據快取 | 當日 |
| `.cache/info_YYYYMMDD.json` | 基本面快取 | 7日內取最新一份 |
| `.cache/ranked_YYYYMMDD.json` | L3 AI 排序結果快取（同日重跑不重打 API） | 當日 |
| `data/universe_roster.json` | 名單遲滯用的前次名單 | 永久（每次執行覆寫） |
| `data/taifex_vix_history.json` | 波動率訊號歷史（供未來重新校準） | 永久（只增不改，同日重跑覆寫當天那筆） |
| `data/watchlist.json` | tracker 訊號追蹤現況（含 pending_exit 漲跌停排隊狀態） | 永久（每次執行覆寫） |
| `data/performance_history.json` | 已結算訊號績效歸檔 | 永久（只增不改，原子寫入） |
| `data/candidates.json`（gitignored） | L2 候選池除錯輸出 | 每次執行覆寫 |
| `docs/reports/<date>.html`、`docs/index.html`、`docs/data/*.json` | 每日報告與首頁索引 | 永久累積 |

`--no-cache` 強制重新下載 price/info（不影響 `universe_roster.json`／
`taifex_vix_history.json`）；`--no-ai-cache` 只略過 L3 AI 快取。

## 後續階段（見 TODO.md）

處置股/全額交割股排除、tracker（含漲跌停止損模擬）、L3 AI 精選（DeepSeek）、
報告發布皆已完成並串通。`LOCK_VOLUME_RATIO` 已校準為 0.6（詳見上方「未解決
的設計問題」第 2 項）。GitHub 遠端已設定。剩餘：GitHub Actions 排程、
TPEx 上櫃納入 universe。
