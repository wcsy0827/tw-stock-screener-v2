# 人工測試操作手冊

給人工驗證這個 MVP 管線是否正常運作用。目的是確認資料管線跑得通、候選池品質
合理——不是自動化測試（這個階段還沒有 tracker，沒有可回歸測試的純函式）。

## 前置準備

```powershell
cd D:\tw-stock-screener-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 測試 1：全新環境跑通（首次執行）

```powershell
python main.py --dry-run --no-cache
```

**預期行為**：
- 終端機依序印出 Step 1 ~ Step 5，過程中不拋例外，結束碼為 0。
- `data/universe_roster.json` 原本不存在（或是舊的），Step 1 印出「前次名單 0 支」
  （首次執行的正常狀態）。
- 結尾印出「=== 結果摘要 ===」，附 Regime／廣度／波動率／Universe→L1→L2 數量。

**檢查清單**：
- [ ] `Regime` 是五象限之一：`BULL_TREND`/`CONSOLIDATION`/`CONSOLIDATION_VOLATILE`/
      `PANIC_REVERSAL`/`BEAR_DISTRIBUTION`，不是空字串或例外訊息
- [ ] `breadth_pct` 介於 0~100 之間
- [ ] `vix_source` 是 `"taifex"`（正常情況，用真 VIX）。若持續是
      `"hv20_fallback"` 代表 TAIFEX 下載端可能失效，需要檢查 `src/taifex_vix.py`
      的 URL／解析邏輯是否仍對應網站結構（見 README「未解決的設計問題」第1點）
- [ ] `vix_value` 是正數（真 VIX 通常在 15~40 區間，HV20 fallback 通常在
      10~40% 區間，量級相近）
- [ ] `universe_count` 介於 150~180 之間（名單遲滯的容許區間，見 README）
- [ ] `l1_passed_count` 明顯小於 `universe_count` 但不是 0（正常應該是八九成通過，
      因為 universe 本身已是流動性排序後的池子）
- [ ] `candidate_count` > 0（若為 0，檢查當天 Regime 是否為 `BEAR_DISTRIBUTION`——
      這是唯一設計上允許候選池為空的象限）

## 測試 2：候選池分數分布合理性（開 `data/candidates.json` 人工檢查）

**檢查清單**：
- [ ] `total_score` 有明顯差異（不是全部同分，也不是全部滿分或全部 0 分）
- [ ] 隨機挑 3~5 支候選股，六個子分數（`ma_score`/`rsi_score`/`macd_score`/
      `volume_score`/`momentum_score`/`rs_score`）加總等於 `total_score`
- [ ] `sector` 欄位是可讀的中文產業名稱（如「半導體業」），不是兩位數代碼
      （如果又出現代碼，代表 ISIN 頁面抓取失敗，檢查終端機是否有
      `[universe] ISIN 產業別名稱抓取失敗` 的警告）
- [ ] 隨手挑一支候選股的 `symbol`（如 `2330.TW`），去 Yahoo Finance 或看盤軟體
      核對股價、產業別是否合理（不是查證所有 150 支，抽查即可）

## 測試 3：快取機制

```powershell
# 第一次（會下載）
python main.py --dry-run
# 立刻再跑一次（應該直接讀快取，不重新下載）
python main.py --dry-run
```

**預期行為**：第二次執行的終端機應該印出 `[cache] 讀取 price 快取` 與
`[cache] 讀取 info 快取`，而不是 `[fetcher] 下載 ...` 的訊息，執行時間明顯變短
（省去 yfinance 下載）。

**強制略過快取**：

```powershell
python main.py --dry-run --no-cache
```

應該完全重新下載，忽略 `.cache/` 內任何檔案。

## 測試 4：名單遲滯（roster hysteresis）穩定性

連續兩個交易日各跑一次（或至少間隔一段時間，讓 TWSE 當日成交金額排序自然變動），
比對兩次的 `data/candidates.json` 或終端機印出的名單數量：

**檢查清單**：
- [ ] 兩次 `universe_count` 都落在 150~180 之間，不會暴衝到明顯偏離這個區間
- [ ] 用文字編輯器打開兩次的 `data/universe_roster.json`（若有備份），比對
      `symbols` 陣列，應該大部分股票重疊，只有少數幾支換手——不應該整份名單
      幾乎完全不同（若整份换血，代表 hysteresis 邏輯可能失效，需要回頭檢查
      `universe.apply_roster_hysteresis()`）

## 常見錯誤排除

| 現象 | 可能原因 | 排除方式 |
|------|----------|----------|
| `requests.exceptions.*` 在 Step 1 | TWSE OpenAPI 或 ISIN 頁面暫時無回應 | 稍後重跑；若持續失敗，檢查該 API 網址是否變更 |
| `[universe] ISIN 產業別名稱抓取失敗` | ISIN 頁面編碼/結構變動，或暫時無法連線 | 不中斷流程，`sector` 會 fallback 為 `t187ap03_L` 的數字代碼；若長期出現需檢查 `universe.fetch_industry_names()` 的解析邏輯是否仍對應頁面結構 |
| `[fetcher] 下載失敗` 重試多次後仍失敗 | yfinance/Yahoo 端流量限制或網路問題 | 稍後重跑；若整批持續失敗，考慮減少 `fetcher.BATCH_SIZE` |
| `candidate_count: 0` 但 Regime 不是 `BEAR_DISTRIBUTION` | L2 門檻可能偏高，或當天大盤確實極弱 | 檢查 `data/candidates.json` 的 `l1_passed_count`，若 L1 通過數正常但 L2 掉到 0，屬於當天市況真的低迷，不一定是 bug |
| `vix_source: "hv20_fallback"` | TAIFEX 該月資料檔下載失敗或該月尚無資料 | 檢查網路；偶發可忽略（自動 fallback），若連續多日出現需檢查 `src/taifex_vix.py` 的 URL 格式是否仍對應 TAIFEX 網站結構 |

## 已知限制（測試時心裡有數，不是 bug）

以下是 README「未解決的設計問題」列出的既知限制，人工測試時不需要為此回報：

- 真 VIX（TAIFEX）已接上，但邊界值因歷史尚淺暫時沿用 HV20 校準值，兩者量級可比
  但不完全等價
- Universe 是流動性近似範圍，不是官方 0050/0051 成分股
- 目前只涵蓋 TWSE 上市（`.TW`），不含 TPEx 上櫃（`.TWO`）
- 沒有 L3 AI 精選、沒有 tracker 追蹤、沒有報告發布（這些是 Phase 3 才做）
