# 待辦事項（跨 session 追蹤）

本檔案是這個 repo 的持久化待辦清單，不依賴任何工具的 session 內任務追蹤——
新開 session 時，先讀這份檔案就能知道目前進度與下一步。完成的項目直接刪除
該條目並在 commit message 說明，不要留「已完成」的殘留列表。

## Phase 2（已完成，見 commit）

Universe 遲滯排序、HV20/L1 門檻校準、產業中文名稱、人工測試手冊皆已完成，詳見
README.md「已校準項目」章節與 `scripts/calibrate_hv.py`／`scripts/calibrate_l1.py`。

## Phase 2.5（未開始，時間盒限定）

- [ ] 探測 TAIFEX 台指選擇權波動率指數（真 VIX 等價物）是否有穩定可用的每日資料來源
      （先前試的 `taifex.com.tw/cht/3/vixQuote` 404，頁面結構可能已變動）。
  - 找得到 → 寫一個帶 fallback 的抓取器（抓不到自動退回 `market.py` 現有的 HV20 邏輯）
  - 找不到 → 記錄結論在 README，只用 HV20，不要讓這條路擋住主線開發

## Phase 3（未開始，需要先抗辯審查再動手）

**開始前提：Phase 2 的 universe/regime/L1 校準要先站穩，因為 tracker 會開始累積
`performance_history`，一旦地基（規則）设计错了，历史绩效数据要砍掉重练。**

- [ ] **漲跌停止損模擬機制設計**（最高優先度，見上次對話的建議方案）：
  - 鎖死判定：`today_low == today_close == 跌停價（前收×0.9，依tick取整）` 且量能顯著低於均量
  - 結算規則：鎖死當日不結算，部位標記 `pending_exit`，順延到第一個非鎖死交易日以該日收盤價出場
  - 進場側：訊號後遇漲停鎖死，限價單視為未成交，watch 天數照常消耗
  - **動手前必須跑一次 adversarial-review（skeptic/red-team/simplifier）**，因為這個設計直接決定
    `performance_history.json` 的可信度
- [ ] 處置股/全額交割股排除（TWSE 處置公告或代理指標，補進 L1）
- [ ] 移植 `tracker.py`（訊號追蹤、績效歸檔）——依賴上面的漲跌停機制先落地
- [ ] 移植 `ranker.py`（L3 DeepSeek AI 精選）
- [ ] 移植 `publisher.py`（報告發布）
- [ ] 視情況引入 `specs/`/`plans/` 規格治理（DD 編號系統）——等真正開始做 tracker 才需要

## 尚未排入階段的觀察項

- [ ] TPEx 上櫃（`.TWO`）納入 universe，目前僅涵蓋 TWSE 上市
- [ ] GitHub 遠端/Actions 排程——本機驗證穩定後再建
