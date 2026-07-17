# 待辦事項（跨 session 追蹤）

本檔案是這個 repo 的持久化待辦清單，不依賴任何工具的 session 內任務追蹤——
新開 session 時，先讀這份檔案就能知道目前進度與下一步。完成的項目直接刪除
該條目並在 commit message 說明，不要留「已完成」的殘留列表。

## Phase 2（已完成，見 commit）

Universe 遲滯排序、HV20/L1 門檻校準、產業中文名稱、人工測試手冊皆已完成，詳見
README.md「已校準項目」章節與 `scripts/calibrate_hv.py`／`scripts/calibrate_l1.py`。

## Phase 2.5（已完成，見 commit）

找到 TAIFEX 真 VIX（臺指選擇權波動率指數）並接上，見 `src/taifex_vix.py`、
`market.py` 的 `fetch_volatility_signal()`。已知限制與後續待辦：

- [ ] **累積 6~12 個月 `data/taifex_vix_history.json` 後，重新用真 VIX 自己的分布
      校準 `market.VOL_LOW_THRESHOLD`/`VOL_HIGH_THRESHOLD`，不要繼續沿用 HV20
      校準值**（TAIFEX 該 endpoint 只保留約 3~4 個月近期資料，這次沒有足夠深度
      可以獨立校準，暫時沿用 HV20 的 19.44/27.49）。
- [ ] 若長期觀察 `vix_source` 經常是 `hv20_fallback`（而非 `taifex`），代表 TAIFEX
      抓取端可能不穩定，需要回頭排查 `taifex_vix.py` 的 URL/解析邏輯是否過期。

## Phase 3（設計已完成並通過抗辯審查，尚未實作）

**開始前提：Phase 2 的 universe/regime/L1 校準要先站穩，因為 tracker 會開始累積
`performance_history`，一旦地基（規則）设计错了，历史绩效数据要砍掉重练。**

- [x] **漲跌停止損模擬機制設計**：完整設計見
      [docs/phase3_limit_lock_design.md](docs/phase3_limit_lock_design.md)（v10 定稿）。
      經過 11 輪 skeptic/red-team/simplifier 三鏡頭抗辯（累積修出 5 個 blocker、
      約 15 個 major，完整修正對照見文件附錄 A），連續兩輪收工確認皆為零
      blocker/major，符合 loop-until-dry 收工條件。核心機制：鎖死判定用
      「收盤=最低+量能枯竭」三條件合取（不依賴精確 ±10% 比值，因 yfinance
      配息缺漏會使比值失真）、pending_exit 旗標（非新 status 值）、鎖死順延
      至解除日以開盤價出場、進場側一字漲停 gate。
- [x] **依 docs/phase3_limit_lock_design.md 移植 tracker.py**（訊號追蹤、績效歸檔）：
      見 `src/tracker.py` + `tests/test_tracker.py`（41 個測試，含 §6 施工檢查表
      要求的 `is_limit_down_locked`/`is_one_price_limit_up` 純函式 fixture、
      `_check_settlement` 的 defer/hold_pending/exit 三態改寫、主迴圈新鮮度守衛、
      §5.1 gate 的 `status=="watch"` 前提、§9 Q1 拆股 fixture）。關鍵測試已用
      fault-injection 驗證真的會抓到對應 bug（§5.1 blocker 重現、Q1 標尺混用
      重現），非僅通過即信。`is_safe_to_run()` 提供 P6 運行前提 guard（僅排除
      週末，未排除法定假日，供未來排程整合時呼叫）。
  - [x] **`LOCK_VOLUME_RATIO` 已校準：0.3 → 0.6**（2026-07，universe 150 支
        × 3 年歷史）。原「雙峰谷底」校準假設證偽後，改用**一字切分定錨**：
        一字跌停 bar（全日僅跌停價成交，n=222）= 確定真鎖死 ground truth，
        其量能比分布定錨門檻。0.3 只涵蓋 69.4% 真鎖死，漏掉的走樂觀結算
        （記當日跌停價而非解鎖日開盤，平均 +5pp 樂觀）違反設計第一原則；
        0.6 涵蓋 89.6%（剔除 2025-04 崩盤集中樣本後 93.9%），且已實證誤鎖
        代價近中性、pending 不會卡死（解鎖天數中位 1 日不受門檻影響）。
        經四輪三鏡頭抗辯定案（含推翻中間版本 0.75=單一崩盤事件過擬合），
        完整推理鏈與監控基線見 docs/phase3_limit_lock_design.md §3.1 補述
        與附錄 B。測試 fixture 已同步（fail-then-pass：2 failed → 全套 125
        passed）。殘餘追蹤項：下游績效分析須用 `exit_deferred` 欄位分群；
        實際運行若 `locked_days` 頻繁 >3 或 thin_fill 佔比異常，重跑校準。
- [x] **處置股/全額交割股排除**：見 `src/disposition.py` + `tests/test_disposition.py`
      + `tests/test_filter.py`（26 個測試）。處置股用 TWSE OpenAPI
      `/v1/announcement/punish`（含處置起迄時間，過期自動解除排除，不需維護
      到期清單）；全額交割股 TWSE OpenAPI 未提供獨立現況清單（查過全部 143 個
      endpoint 確認），改用 `/v1/exchangeReport/TWT85U`「分盤集合競價」欄位作
      代理指標（更嚴重的交易限制，設計 R11 原文即容許「處置公告或代理指標」）。
      已接進 `filter.apply_filters()` 的 `excluded_symbols` 參數並在 main.py
      Step 3.5 呼叫，`python main.py --dry-run` 端到端跑過確認：2303.TW（聯電，
      NT$166 收盤、30日均額 NT$355億，L1 正常會通過）因處於處置期間被正確排除
      ——非僅單元測試通過，已用當下真實 TWSE 處置名單驗證。
- [x] **移植 `ranker.py`（L3 DeepSeek AI 精選）**：見 `src/ranker.py` +
      `tests/test_ranker.py`。相對美股版的調整：RS_vs_Sector 改直接複用
      `scorer.py` 既有的同產業 equal-weight 籃子計算（不重算，台股無對應美股
      sector ETF 體系）；Beta_60D 對照基準由 SPY 改為 ^TWII；**移除財報剩餘
      天數（Earnings_Days_Left）維度**——TWSE 未接入財報日曆資料源，與其移植
      一個永遠常數（99/安全）的死欄位，不如整個維度先不做，待未來有資料源
      再補；幣別 "$"→"NT$"，分析師人設/持有天數單位改為台股語境。
      `market.py` 新增 `fetch_market_context()`（regime/vix/^TWII 走勢/產業籃子
      背景，供 L3 Prompt 與報告儀表板使用）。
- [x] **移植 `publisher.py`（報告發布）**：見 `src/publisher.py` +
      `tests/test_publisher.py`。CSS 逐字沿用（設計系統無關）；新增 Phase 3
      顯示支援——`pending_exit` 部位顯示「⏳ 跌停鎖死排隊中」，「今日結算」
      區塊依 `tracker.py` 新增的 `_exit_note` 欄位優先顯示漲跌停鎖死相關的
      精確出場說明（跌停鎖死無量陰跌/順延解除/停牌強制結算），避免使用者
      誤以為系統照 AI 原始止損價正常成交。GitHub Pages 遠端尚未設定時
      `_check_git_remote()` 既有邏輯優雅略過 push（見下方待辦）。
  - [ ] **待辦**：GitHub 遠端尚未設定（`git remote`為空），`publish()` 目前
        每次執行都會優雅略過 push 並印出設定指引；正式上線前需
        `git remote add origin ...` 並視需要建 GitHub Actions 排程（見下方
        「尚未排入階段的觀察項」）。
- [x] **main.py 完整接線**：L0(universe)→L1(filter)→L2(scorer)→L3(ranker)→
      tracker→publisher 全流程已串通，`python main.py --dry-run` 端到端驗證
      通過（無 DEEPSEEK_API_KEY 時 L3 走 fallback，`is_fallback=True` 的結果
      不納入 tracker 追蹤，符合既有 DD-20 語意）。tracker 呼叫前已加
      `tracker.is_safe_to_run()` guard（P6）：非收盤後/非交易日時，L0~L3
      仍完整輸出，只跳過 tracker 追蹤與報告發布這一段。
      CLI 新增 `--top`/`--min-score`/`--no-ai-cache`/`--yes` 對齊美股版慣例。
- [ ] 視情況引入 `specs/`/`plans/` 規格治理（DD 編號系統）——等真正開始做下一個
      模組（TPEx 上櫃／排程整合）才需要

## 尚未排入階段的觀察項

- [ ] TPEx 上櫃（`.TWO`）納入 universe，目前僅涵蓋 TWSE 上市
- [ ] GitHub 遠端/Actions 排程——本機驗證穩定後再建
