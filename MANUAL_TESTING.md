# 人工測試操作手冊

給人工驗證這個系統的完整管線（Universe → L1 → L2 → L3 AI 精選 → tracker 追蹤 →
報告發布）是否正常運作用。自動化測試見 `tests/`（pytest，涵蓋各模組純函式與
fault-injection 驗證），本文件聚焦「跑一次真實流程，人工檢查輸出是否合理」。

## 前置準備

```powershell
cd D:\tw-stock-screener-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

編輯 `.env` 填入 `DEEPSEEK_API_KEY`（選填）：
- **有填**：L3 會真的呼叫 DeepSeek AI 精選，`ranked` 會有 AI 給的 buy_zone/target/
  stop_loss/reason 等欄位，且會納入 tracker 追蹤。
- **不填**：L3 自動 fallback 為 L2 分數排序（`is_fallback: true`），**不會**納入
  tracker 追蹤（這是既有設計 DD-20：fallback 不是真實 AI 判斷，不該污染
  `performance_history.json`）。適合只想測資料管線、不想燒 API 額度的情境。

## 測試 1：全新環境跑通（首次執行）

```powershell
python main.py --dry-run --no-cache
```

**預期行為**：
- 終端機依序印出 Step 1 ~ Step 6，過程中不拋例外，結束碼為 0。
- `data/universe_roster.json` 原本不存在（或是舊的），Step 1 印出「前次名單 0 支」
  （首次執行的正常狀態）。
- Step 3.5 印出處置股/分盤集合競價排除統計（見測試 5）。
- Step 6 印出 L3 精選結果（有 API 金鑰會呼叫 DeepSeek；沒有則印出「未設定
  DEEPSEEK_API_KEY，跳過 AI 排序」）。
- 若目前是收盤後（13:45 台北時間後）且為交易日，接著會印出 tracker 追蹤與
  `[publisher] 報告已生成：...` 訊息；若非安全時段，會印出「跳過 tracker 追蹤
  與報告發布」的警告並提早結束（這是 P6 設計保護，不是 bug，見下方測試 6）。
- 結尾印出「=== 結果摘要 ===」，附 Regime／廣度／波動率／Universe→L1→L2→L3 數量。

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
- [ ] `candidate_count`（L2）> 0（若為 0，檢查當天 Regime 是否為
      `BEAR_DISTRIBUTION`——這是唯一設計上允許候選池為空的象限）
- [ ] L3 精選數量 <= `--top`（預設 3）且 <= L2 候選數量

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

## 測試 5：處置股/分盤集合競價排除

**檢查清單**：
- [ ] Step 3.5 印出「處置股公告：N 筆，目前仍在處置期間內 M 支」與「變更交易
      清單：N 支，分盤集合競價中 M 支」，兩個 M 都合理（通常個位數到二十幾支，
      TWSE 每日發布，是浮動數字）
- [ ] 抽查 `data/universe_roster.json` 裡的一支處置股代號（可到
      [TWSE 處置公告頁](https://www.twse.com.tw/zh/announcement/punish.html)
      對照當天名單），確認它**沒有**出現在 `data/candidates.json` 的
      `candidates` 陣列裡（即使它的成交量/市值遠超過 L1 門檻）

## 測試 6：L3 AI 精選（需 DEEPSEEK_API_KEY）

```powershell
python main.py --dry-run
```

**檢查清單**：
- [ ] 若有設定 API 金鑰，Step 6 印出「送出 N 支候選股給 DeepSeek AI...」與
      「AI 排序完成，回傳 Top N」
- [ ] `.cache/ranked_YYYYMMDD.json` 有產生，內容是 JSON 陣列，每筆含
      `ticker`/`reason`/`confidence`/`buy_zone`/`target`/`stop_loss`/`strategy`
- [ ] 立刻重跑一次 `python main.py --dry-run`：Step 6 應印出「複用今日 AI 快取」
      而不是重新呼叫 API（省 token）；用 `--no-ai-cache` 可強制重打
- [ ] `strategy` 欄位只會是「動能策略」「突破策略」「反轉策略」三者之一
- [ ] `buy_zone`/`target`/`stop_loss` 格式為 `NT$xxx` 或 `NT$xxx～NT$xxx`（幣別是
      新台幣，不是美股版的 `$`）
- [ ] 若 `Regime` 剛好是 `BEAR_DISTRIBUTION`，Step 6 應印出「系統全面防禦，不
      輸出買入標的」，`ranked` 為空陣列（不會呼叫 API，設計上的防禦機制）

## 測試 7：tracker 追蹤與報告發布

只在收盤後（13:45 台北時間後）且為交易日執行才會跑到這段（見 `tracker.is_safe_to_run()`）。

```powershell
python main.py --dry-run
```

**檢查清單**：
- [ ] `data/watchlist.json` 有更新，若 L3 有真實 AI 精選（非 fallback），新增的
      股票會以 `status: "watch"` 出現
- [ ] `docs/reports/<market_date>.html` 有產生，用瀏覽器開啟：
  - [ ] 頁首「台股資料截止日」與終端機印出的 `market_date` 一致
  - [ ] 大盤儀表板顯示的 Regime／市場廣度／波動率與終端機摘要一致
  - [ ] 若無任何追蹤中股票，顯示「今日無資料」而非空白或報錯
- [ ] `docs/index.html` 有同步更新（開啟後應看到「📖 系統說明」與歷史報告列表，
      即使目前只有一筆）
- [ ] `docs/data/last_run.json` 的 `run_at_utc` 是剛剛執行的時間
- [ ] Dry-run 模式下終端機印出「Dry-run 模式，略過 git push」，`git status`
      應該看得到 `docs/` 底下的變更是**未 commit** 的（因為還沒真的 push，只是
      本機生成；若要正式發布需另外 `git add`/`commit`/`push`，或未來接上
      GitHub Actions 排程後由 CI 自動處理）
- [ ] 連續重跑同一天兩次：第二次應觸發「今日已執行過追蹤，再次執行不會增加
      追蹤天數」的確認提示（`--yes` 可跳過），且 `docs/data/reports-index.json`
      裡當天的紀錄是**更新**而非重複新增一筆

**若持有部位遇到跌停鎖死（不容易人工湊出真實案例，可讀 `tests/test_tracker.py`
的 fixture 理解機制）**：
- [ ] 報告的「有效追蹤清單」會顯示「⏳ 跌停鎖死排隊中（第 N 天）」而非一般持倉
- [ ] 結算後的「今日結算」區塊會顯示「🔻 跌停鎖死無量陰跌」或「⏳ 跌停鎖死解除」
      等精確出場說明，而非籠統的「🛑 觸發止損，停損出場」

## 常見錯誤排除

| 現象 | 可能原因 | 排除方式 |
|------|----------|----------|
| `requests.exceptions.*` 在 Step 1 | TWSE OpenAPI 或 ISIN 頁面暫時無回應 | 稍後重跑；若持續失敗，檢查該 API 網址是否變更 |
| `[universe] ISIN 產業別名稱抓取失敗` | ISIN 頁面編碼/結構變動，或暫時無法連線 | 不中斷流程，`sector` 會 fallback 為 `t187ap03_L` 的數字代碼；若長期出現需檢查 `universe.fetch_industry_names()` 的解析邏輯是否仍對應頁面結構 |
| `[fetcher] 下載失敗` 重試多次後仍失敗 | yfinance/Yahoo 端流量限制或網路問題 | 稍後重跑；若整批持續失敗，考慮減少 `fetcher.BATCH_SIZE` |
| `candidate_count: 0` 但 Regime 不是 `BEAR_DISTRIBUTION` | L2 門檻可能偏高，或當天大盤確實極弱 | 檢查 `data/candidates.json` 的 `l1_passed_count`，若 L1 通過數正常但 L2 掉到 0，屬於當天市況真的低迷，不一定是 bug |
| `vix_source: "hv20_fallback"` | TAIFEX 該月資料檔下載失敗或該月尚無資料 | 檢查網路；偶發可忽略（自動 fallback），若連續多日出現需檢查 `src/taifex_vix.py` 的 URL 格式是否仍對應 TAIFEX 網站結構 |
| `[disposition] 處置股清單抓取失敗` | TWSE OpenAPI `/v1/announcement/punish` 暫時無回應 | 不中斷流程，本次不排除任何處置股（優雅降級）；偶發可忽略，持續失敗需檢查 endpoint 是否變更 |
| Step 6 印出「未設定 DEEPSEEK_API_KEY，跳過 AI 排序」 | `.env` 沒填 `DEEPSEEK_API_KEY` | 這是設計上的 fallback，不是錯誤；若想測 L3 真實流程需要填金鑰 |
| 執行到一半印出「目前非收盤後/非交易日，跳過 tracker 追蹤與報告發布」就結束 | 現在是盤中或非交易日（週末） | 這是 P6 設計保護（`tracker.is_safe_to_run()`），不是 bug；L0~L3 結果仍已正確輸出並印在終端機，只是不會寫 `watchlist.json`/生報告。若真的需要在非安全時段測試 tracker/publisher，可在 Python 互動環境裡直接呼叫 `tracker.run_tracker(...)`/`publisher.publish(...)`繞過此 guard（僅供測試，正式使用不應這樣做） |

## 已知限制（測試時心裡有數，不是 bug）

以下是 README「未解決的設計問題」列出的既知限制，人工測試時不需要為此回報：

- 真 VIX（TAIFEX）已接上，但邊界值因歷史尚淺暫時沿用 HV20 校準值，兩者量級可比
  但不完全等價
- `LOCK_VOLUME_RATIO`（漲跌停鎖死量能枯竭門檻）已校準為 0.6（一字跌停真鎖死群
  定錨），殘餘限制（樣本集中單一崩盤事件、存活者偏差等）見
  docs/phase3_limit_lock_design.md §3.1 補述與附錄 B
- L3 AI Prompt 沒有財報剩餘天數（Earnings_Days_Left）維度，TWSE 未接入財報日曆資料源
- Universe 是流動性近似範圍，不是官方 0050/0051 成分股
- 目前只涵蓋 TWSE 上市（`.TW`），不含 TPEx 上櫃（`.TWO`）
- GitHub 遠端尚未設定，`--dry-run` 與正式執行皆不會真的 push（`git remote` 為空時
  優雅略過並印出設定指引）
