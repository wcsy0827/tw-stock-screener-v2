# 台股選股系統 MVP（Phase 1）

移植自 `D:\us-stock-screener`（美股 S&P 500 選股系統）的三層篩選架構，範圍縮到
「台灣50＋中型100」近似範圍（依成交金額排序前 150 支），驗證資料管線與 L2 分數
分布是否合理。**本階段不接 L3 AI 精選、不做 tracker 追蹤、不發布報告。**

## 執行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py --dry-run
```

輸出候選池 JSON 至 `data/candidates.json`，並在終端機印出分數分布 Top 10。

## 架構

```
universe.py   TWSE OpenAPI 抓上市公司清單 + 產業別 + 當日成交金額，取前150支近似範圍
fetcher.py    yfinance 批次下載日K+info，快取邏輯移植自美股版
                （時區改 Asia/Taipei 13:30 收盤 + 15分鐘 buffer）
market.py     簡化版 Regime：VIX → ^TWII HV20（20日已實現波動率）替代
filter.py     L1 流動性硬篩，門檻改新台幣量級
scorer.py     六維度 L2 評分移植，RS 維度改用同產業 equal-weight 籃子替代 sector ETF
main.py       串起以上模組，--dry-run 輸出候選池
```

## 未解決的設計問題（下一階段前必須處理）

1. **VIX 替代語意落差**：`market.py` 用 `^TWII` HV20（已實現波動率，落後指標）替代
   VIX（隱含波動率，前瞻指標）。五象限邊界值（`HV_LOW_THRESHOLD=15`、
   `HV_HIGH_THRESHOLD=25`）是暫定值，尚未用 `^TWII` 歷史 HV20 分布校準分位數。

2. **漲跌停止損模擬失真**：台股 ±10% 漲跌停鎖死時掛單不會成交，若直接移植美股版
   tracker.py 的「`today_low ≤ stop_loss` 即視為止損成交」邏輯，會系統性低估虧損、
   污染績效資料。**開始做 tracker 追蹤、累積 performance_history 之前，必須先設計
   跌停無量判定機制**，否則後續要砍掉重練。

3. **Universe 範圍為市值/成交金額近似，非官方指數成分股**：TWSE OpenAPI 沒有
   「台灣50/中型100成分股」endpoint（那是 FTSE 方法論下的 0050/0051 ETF 成分股）。
   目前用當日成交金額排序前 150 支上市股（`universe.py`）近似，且僅涵蓋 TWSE 上市
   （`.TW`），未涵蓋 TPEx 上櫃（`.TWO`）。

4. **L1 門檻（`filter.py` 的 `MIN_PRICE`/`MIN_DAILY_TRADE_VALUE`/`MIN_MARKET_CAP`）
   為暫定值**，尚未用台股實際分布校準，先觀察通過數量分布再收斂。

5. **產業別為代碼非名稱**：TWSE OpenAPI `t187ap03_L` 的「產業別」欄位實測回傳兩位數
   代碼（如 `"01"`）而非中文名稱（先前用 WebFetch 分析文件時被誤判為含名稱文字）。
   不影響 RS 維度的同產業分組邏輯（仍能正確分組），只是候選池輸出的 `sector` 欄位
   對人類不易讀，之後可補一份代碼→名稱對照表。

## 快取

同美股版：`.cache/price_YYYYMMDD.pkl`（當日）、`.cache/info_YYYYMMDD.json`（7日）。
`--no-cache` 強制重新下載。

## 後續階段（尚未開始）

L3 AI 精選（DeepSeek）、tracker 訊號追蹤與績效歸檔（需先解決上述問題 2）、報告
發布。本階段刻意不建立 `specs/`/`plans/` 規格治理，待後續階段真正開始做
tracker/ranker 再視需要引入。
