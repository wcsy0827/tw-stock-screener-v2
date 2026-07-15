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
- [ ] **依 docs/phase3_limit_lock_design.md 移植 tracker.py**（訊號追蹤、績效歸檔）：
  - §6 實作範圍清單為施工檢查表，含 `is_limit_down_locked`/`is_one_price_limit_up`
    純函式、`_check_settlement` 改寫、主迴圈新鮮度守衛、§5.1 gate 的 `status==
    "watch"` 前提、§9 Q1 的拆股 fixture 強制驗證
  - `scripts/calibrate_lock.py` 校準 `LOCK_VOLUME_RATIO`（跌停/漲停側分別）
- [ ] 處置股/全額交割股排除（TWSE 處置公告或代理指標，補進 L1）——設計文件
      R11 已記錄「排除機制落地前」的殘餘風險，建議與 tracker.py 移植順序一併確認
- [ ] 移植 `ranker.py`（L3 DeepSeek AI 精選）
- [ ] 移植 `publisher.py`（報告發布）
- [ ] 視情況引入 `specs/`/`plans/` 規格治理（DD 編號系統）——等真正開始做 tracker 才需要

## 尚未排入階段的觀察項

- [ ] TPEx 上櫃（`.TWO`）納入 universe，目前僅涵蓋 TWSE 上市
- [ ] GitHub 遠端/Actions 排程——本機驗證穩定後再建
