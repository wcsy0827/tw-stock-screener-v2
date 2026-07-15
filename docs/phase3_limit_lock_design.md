# Phase 3 設計提案：台股漲跌停止損模擬機制（v10，抗辯審查完成）

狀態：**定稿 v10**。v1~v9 各經一輪三鏡頭抗辯擋回或收工確認，連續第十、十一輪
（v9 版本本身未變更）皆為零 blocker、零 major，僅剩純文件補述層級建議
（已併入本版 R11/R12），符合 loop-until-dry 收工條件，可進入實作。完整
抗辯歷程見附錄 A。

## 1. 背景與問題陳述

美股版 `tracker.py`（`D:\us-stock-screener\src\tracker.py`）的結算邏輯
`_check_settlement()` 假設「`today_low ≤ stop_loss` ⇒ 止損單以 stop_loss 價成交」。
台股有 ±10% 漲跌停制度：跌停鎖死（賣單大量堆積、無人接手）時，市價/停損賣單
**根本不會成交**，隔日往往繼續跳空跌停。直接移植會系統性把「連續跌停 -25% 的
真實虧損」記成「-8% 停損出場」，污染 `performance_history.json`，且此資料一旦
累積便不可回溯修復（當時的盤中資料已不可重建），只能砍掉重練。

本設計的唯一目標：**讓模擬出場價貼近「掛單真的能不能成交、成交在哪」的現實**，
寧可保守（多記虧損）不可樂觀（少記虧損）。

## 2. 依據的市場規則（設計前提）

- P1 台股一般股票單日漲跌幅限制 ±10%，漲停價 = 前收 ×1.1 向下取 tick、
  跌停價 = 前收 ×0.9 向上取 tick。⇒ 跌停價偏離「前收 ×0.9」最多約一個
  tick，比值上界實算 ≈ 0.9040。
- P2 跌停鎖死 = 價格釘在跌停價、委賣遠大於委買、成交量枯竭。掛出的市價
  賣單/停損單**排隊但不成交**；盤中打開時排隊單在跌停價附近優先成交。
- P3 漲停鎖死時「賣單」極易成交 ⇒ 停利不受影響。「買單」則被制度夾死：
  委買價不得高於漲停價，掛單被迫以漲停價排在巨量買單隊伍之後，實質不成交。
  **關鍵推論（進場側判定的基礎）**：連續競價中，只要當日曾有成交發生在
  價位 X **以下**（`today_low < X` 且 X 在漲跌停帶內），掛在 X 的限價買單
  必然已成交（賣方要打到更低價位必先吃掉 X 的委買）。買單唯一不成交的
  日型是「全日只在單一鎖死價位成交」的一字漲停。
- P4 資料來源為 yfinance 日線（auto_adjust=True）。yfinance 台股配息**有
  缺漏風險**——因此本設計的判定式**不得把「比值精確落在 ±10% 帶」當成
  硬條件**，只能當寬鬆的 sanity 檢查，主判定依賴 bar 形狀與量能。
- P5 新上市前 5 個交易日無漲跌幅限制、處置股分盤交易等特例：處置股排除
  是 TODO 獨立條目；新上市股是否可能短期入選 universe 未驗證，列 R5，
  不特判。
- P6 **運行前提**：tracker 僅在收盤後（13:45 後，同 fetcher 的
  `trim_incomplete_session` 慣例）執行，且僅在**台股交易日**執行（沿用
  現有排程之交易日曆判斷，不在非交易日跑）。盤中執行或非交易日誤跑會拿
  殘缺/重複 bar 做鎖死判定並不可逆歸檔，實作需沿用同一 guard。

## 3. 鎖死判定（核心 primitive）

### 3.1 出場側：`is_limit_down_locked`

對單一日 K bar（與前收同一條 auto_adjust 序列）：

```
is_limit_down_locked(bar, prev_close, vol_ma20) =
    (D1) 0.5 ≤ close/prev_close ≤ 0.906             # 寬鬆 sanity
AND (D2) (close − low) ≤ prev_close × 0.002          # 收盤=最低（0.4~2 tick，視價位帶）
AND (D3) volume ≤ LOCK_VOLUME_RATIO × vol_ma20       # 量能枯竭
```

- **D1 上界 0.906**：覆蓋 tick 取整最壞比值 0.9040 + 浮點餘裕，把「正常
  下跌日」擋在鎖死判定之外。
- **D1 下界 0.5**：唯一任務是擋資料垃圾（負值、歸零、少一個數量級）。
  鎖死實質判定交給 D2+D3+H1（§4.2）三者合取，不依賴精確比值（yfinance
  股利缺漏會使比值失真，見 P4）。代價：「跌 >10% 且無量收最低且
  high<stop」的無漲跌幅特例日會被誤判鎖死，方向保守，情境限 R5。
- **D3 的 `LOCK_VOLUME_RATIO` 暫定 0.3**，上線前必跑
  `scripts/calibrate_lock.py`：掃 universe 近 2~3 年日線中滿足 D1+D2
  的跌停形 bar **與** 滿足 U1+U2（§3.2）的漲停形 bar，分別檢視量能比
  分布（預期鎖死 vs 盤中打開雙峰），兩側可各自定值，不假設同分布。
- **vol_ma20 定義**：**不含當日**的前 20 個交易日均量；不足 10 根或結果
  NaN/0 ⇒ D3 視為成立（寧可誤判鎖死，方向保守）；volume NaN 視為 0。
  校準腳本必須用同一定義。
- **pending 期間凍結量能基準**：轉入 pending 當日把 vol_ma20 存為
  `pending_vol_baseline`（**與轉入當日相同的 split_factor 標尺**，見
  §4.1 標尺註記），之後解除判定一律用此凍結值，避免連續跌停多日後滾動
  均量被枯竭日拉低造成假性解鎖。
- **bar 新鮮度前提**：鎖死/解除判定只作用於「bar 日期 == 本次評估日」的
  新 bar；最新 bar 是舊日期（停牌、資料延遲）時，當日不做狀態轉移
  （§4.5 日曆日兜底不受此限，但兜底本身以「有無新 bar」與交易日曆
  雙重確認，見 §4.5）。

**已知邊界（刻意行為）**：盤中殺至跌停→打開→再鎖死、量不低的日子，
D3 不成立 ⇒ 判「未鎖死」⇒ 照常結算——盤中曾有量成交，停損單成交得了。

### 3.2 進場側：`is_one_price_limit_up`

```
is_one_price_limit_up(bar, prev_close, vol_ma20) =
    (U1) (high − low) ≤ prev_close × 0.002       # 全日單一價位（一字）
AND (U2) close > prev_close                       # 方向向上（排除一字跌停）
AND (U3) volume ≤ LOCK_VOLUME_RATIO × vol_ma20    # 買單大排長龍、成交枯竭
```

正當性（由 P3 關鍵推論導出）：買單唯一不成交的日型是一字漲停；只要
`low < high`，掛單必然先於更低價位的成交被吃掉 ⇒ 有成交。因此進場 gate
只需攔一字型。U2 用最弱的方向條件而非比值帶，對股利缺漏免疫；殘餘失效面
（R3）：殖利率 ≥9.1% 的除息日一字漲停使 U2 失效 ⇒ 幽靈進場，屬四重複合
情境，發生率極低。誤攔面（R9）：流動性前 150 universe 中「一字整理日」
近乎不存在，且方向保守（少進場）。

**U3 反向風險（R10，新列）**：主力在漲停價大量倒貨的爆量一字漲停會使
U3 不成立 ⇒ gate 不生效 ⇒ 放行進場；此時排隊隊列位置是否真的成交無法
由日線判斷。方向不定，記錄為殘餘風險，不特別處理（日線資料的天花板）。

## 4. 出場側規則（止損順延）

### 4.1 表示法：旗標而非新 status 值

`pending_exit` **不是**新的 status 枚舉值。部位 `status` 維持 `"active"`，
另加欄位（**全部只讀寫 original entry，下述「寫入鐵律」**）：

```
entry["pending_exit"]         = True/False   # 排隊中的賣單
entry["pending_vol_baseline"] = float        # 轉入時凍結的 vol_ma20（原生標尺，見下）
entry["locked_days"]          = N            # 有效鎖死 bar 數（僅供歸檔分析，接 §4.2 歸檔清單）
entry["pending_stale_runs"]   = N            # 連續「本輪拿不到本 symbol 新鮮 bar」的次數（§4.5 兜底依據）
```

（v5→v6 移除 `pending_last_bar`：與母本既有的同日重跑去重機制
`already_tracked_today`（`today in entry["tracked_dates"]`，tracker.py:615）
功能重複，第五輪 simplifier 指出後直接重用既有機制，見下方「同日重跑」段落。
v6→v7 移除 `pending_since`：第六輪 simplifier 指出此欄位無任何讀取端
——不是判定依據（§4.4/§4.5 皆不讀）、未接歸檔清單（§4.2）、未接
publisher 顯示，是純粹的死欄位。轉入時間點的分析需求已由 `locked_days`
〔接歸檔〕與 performance_history.json 既有的 `signal_date`/`exit_date`
覆蓋，不需要額外欄位。）

**標尺註記（v4 新增，修第三輪 major）**：`pending_vol_baseline` 是唯一帶
量綱（成交量）的 pending 欄位。若轉入當日走 adj 複本路徑（split_factor
偏離 >1%），volume 本身不受 auto_adjust 的價格拆分影響（yfinance 對
volume 的拆股調整與對 price 的調整是同一因子但方向相反——`_calc_split_factor`
只算價格因子）；為避免混淆，`pending_vol_baseline` 一律存**原生
（未經任何 factor 縮放的）volume 值**，且解除判定時的新 bar volume 同樣
用原生值比較（volume 本身不需要標尺轉換，這條純粹是「不要誤用價格
factor 去乘 volume」的實作提醒，非數學修正）。

**寫入目標鐵律**：pending 相關欄位**只讀寫 original `entry`，永不寫入
`adj` 複本**。母本在 split_factor 偏離 >1% 時建 `adj = dict(entry)`
給結算比價用（tracker.py:638），auto_adjust 下任何 >1% 的配息都走此
路徑（台股除息季高頻）——旗標若寫在 adj 上，`save_watchlist` 後蒸發。
實作規範：`_check_settlement` 維持**純決策函式**（母本原本就是純函式，
見附錄 A 第三輪 simplifier 佐證），**只在主迴圈已確認本輪為新鮮 bar
後才會被呼叫**（新鮮度判定與 stale 計數完全在主迴圈處理，見 §4.5，
不屬於 `_check_settlement` 的職責）。因此 `_check_settlement` 只回傳
三種轉移指令（v7 修正第七輪 red-team blocker：舊版曾把 `hold_pending_stale`
/`force_exit_stale` 也列為本函式回傳型別，與 §4.4/§4.5/§6 的實際結構
矛盾，已統一為主迴圈層級處理，不再出現於本函式的回傳型別中）：

```
("defer", {"pending_vol_baseline": <float>})                       # 轉入 pending
("hold_pending", None)                                              # 維持 pending，locked_days += 1
("exit", exit_reason, exit_price, exit_note)                        # 結算（含一般觸停損/停利/到期，以及 §4.2「鎖死∧¬H1」「未鎖死」「pending 解除」三種出場價分支）
None                                                                 # 無事發生
```

`defer` 指令**攜帶** `pending_vol_baseline` 的值（決策函式讀 adj/latest
算出，指令帶著走，主迴圈不重新計算，避免兩處定義不一致）；`pending_exit=True`
時的優先序 1（§4.4）**一律讀 `entry["pending_vol_baseline"]` 既有值**，
不重算——這是唯一一次寫入，之後全程沿用凍結值直到解除或強制結算。
主迴圈收到指令後統一寫入 original entry，寫入函式與 §4.5 強制結算
（`pending_stale_runs` 達門檻時的 `exit`，主迴圈層級組裝、不經
`_check_settlement`）共用同一段「套用指令」程式碼。

母本六處 `status == "active"` 硬編碼分支在旗標方案下全部天然正確：
`_eval_status`（L246）active 短路不會被翻 invalid；`_is_expired`（L544）
active 永不到期不被 G 步驟刪除；DD-9 再入選（L804）active 免疫重置；
`compute_order_plan`（L513）pending 計入 active_count 正確佔槽；F 段
分類歸 active；`_check_settlement` 入口（L309）可進入。log 文案與
publisher 顯示需依 `pending_exit` 改為「跌停鎖死排隊中」。

需顯式判旗標/套用指令的觸點：`_check_settlement` 決策開頭（§4.4）、
`FORCE_EXPIRED` 跳過、`_apply_risk_controls` 跳過、**§4.5 強制結算與
本節指令套用共用同一段「執行 exit 指令」程式碼**（見 §4.5）。

### 4.2 狀態轉移與成交價

**本表僅適用於「本輪已確認為新鮮 bar」的情況**（`_check_settlement`
的呼叫前提，見 §4.1）。「本輪拿不到新鮮 bar」的 pending 部位完全不會
呼叫本函式，其計數與強制結算規則獨立於本表，見 §4.5。

```
active(觸停損, 鎖死 ∧ H1)      ─▶ defer，pending_exit=True，本日不結算
active(觸停損, 鎖死 ∧ ¬H1)     ─▶ exit CLOSED_LOSS，price = 當日 close
active(觸停損, 未鎖死)          ─▶ exit CLOSED_LOSS，price = min(stop, open)
pending(新鮮 bar 仍鎖死)        ─▶ hold_pending，locked_days += 1（已 already_tracked_today 去重）
pending(新鮮 bar 未鎖死)        ─▶ exit CLOSED_LOSS，price = 該日 open
```

- **H1 條件**：`today_high < effective_stop`。high ≥ stop 代表盤中曾在
  停損價上方成交，停損單觸發後成交得了，但成交在哪是關鍵：
- **鎖死 ∧ ¬H1 分支 `exit = 當日 close`**：情境是「開盤高於停損、無量
  陰跌、尾盤鎖死跌停」。真實成交價落在 [close, stop] 區間，日線無從得知
  滑價幅度，依保守公理取區間下緣 close（= 跌停價）。只會多記虧損。
- **未鎖死日觸停損 `exit = min(effective_stop, open)`**：開盤 ≥ 停損、
  盤中跌破 ⇒ 停損價成交；跳空開低但正常運作（有量）⇒ 記 open。
- **pending 解除日 `exit = 該日 open`**：排隊賣單在打開瞬間優先成交
  （P2）。（TODO 草案原寫「收盤價」，與排隊單成交機制不符，改為 open。）
- **open 缺值 fallback**：open NaN/缺欄 → 同 bar close；適用本節所有
  出場價與 §5 進場價。
- 歸檔新增：`exit_deferred`、`locked_days`、`exit_note`
  （`"limit_down_deferred"` / `"limit_down_thin_fill"`〔鎖死∧¬H1〕/
  `"force_settled_after_stale_limit"`〔§4.5 連續無新鮮資料達門檻〕）；
  exit_reason 一律 `CLOSED_LOSS`（勝率統計口徑不變）。

### 4.3 pending 期間的凍結語意

- 不做停利/移動停利/保本/到期判定。實作即 §4.1 列出的顯式判旗標觸點。
- 解除日跳空大漲仍以 open 出場——機制忠實而非樂觀偏差。
- **同日重跑（DD-18）去重（v6 改用既有機制）**：`pending_stale_runs`、
  `locked_days` 的遞增與 pending 轉入，一律比照母本既有的
  `already_tracked_today`（`today in entry["tracked_dates"]`，於主迴圈
  每個 entry 最上層、下載資料**之前**算出，tracker.py:611-617）去重——
  `already_tracked_today=True` 時完全跳過本節所有計數與狀態轉移（沿用
  上一輪已寫入的值），與母本對 `watch_days`/`active_days` 的既有去重
  模式（tracker.py:726-731）一致，不新發明平行機制（v5 的
  `pending_last_bar` 欄位因此刪除，第五輪 simplifier 指出後移除）。

### 4.4 §4.5 新鮮度分流之後的 `_check_settlement` 優先序（純決策函式）

（本節只在 §4.5 判定「本輪已取得新鮮 bar」之後才會被呼叫；`pending_exit`
部位的資料新鮮度分流在呼叫本函式**之前**已於主迴圈完成，見 §4.5。）

1. `pending_exit=True`：判新 bar 鎖死與否（§4.2），不看停利/到期。
2. 觸停損（`today_low ≤ effective_stop`）：依 §4.2 三分支。
3. 停利（`today_high ≥ target`）：不受漲停鎖死影響（P3），照常成交。
   黑天鵝日（同觸停損停利）維持「保守判停損」，先過第 2 步鎖死檢查。
4. 移動停利/到期：邏輯不變；到期需 `pending_exit=False`。

### 4.5 停牌/下市兜底（v6：新鮮度分流移至主迴圈，解決 v5 的兩個 blocker）

**v5 的兩個缺陷**（第五輪 skeptic/red-team 各發現一個 blocker）：
(a) 決策表把「`sym not in latest`」與「`sym in latest` 但 `bar_date !=
today`」都歸類為「拿不到新鮮 bar」，但同節「執行位置」文字卻寫
「`sym in latest` 的一般情況仍照常往下走既有流程」——兩者對「有資料但
不新鮮」的情況互相矛盾，且母本 `_check_settlement(settlement_entry,
price, today_high, today_low)`（tracker.py:738）簽名不含 `bar_date`，
決策表若要保持純函式無法自行判斷新鮮度；(b) `pending_stale_runs` 的
遞增沒有比照母本既有的同日重跑去重，同一天內手動重跑兩次會被計兩次，
在遠少於 15 個真實交易日內提前觸發強制結算。

**v6 修正：新鮮度判定與去重整段移到主迴圈、`_check_settlement` 呼叫之前
（單一守衛，涵蓋 §4.5 blocker (a)）：**

```
# 主迴圈 per-entry，已算出 already_tracked_today（tracker.py:615）之後、
# 呼叫 _check_settlement 之前：
is_fresh = (sym in latest) and (latest[sym]["bar_date"] == today)

if entry.get("pending_exit") and not is_fresh:
    if not already_tracked_today:                      # 同日重跑去重，涵蓋 blocker (b)
        entry["pending_stale_runs"] = entry.get("pending_stale_runs", 0) + 1
        # active_days 照常遞增（v7 修正第六輪 blocker，見下方說明）：
        # pending 部位的 status 仍是 "active"，stale 輪次只是「跳過結算
        # 判定」，不是「跳過持有計時」——沿用母本既有計數規則
        # （tracker.py:730-731，new_status=="active" 即遞增）。
        entry["active_days"] = entry.get("active_days", 0) + 1
    if entry["pending_stale_runs"] >= 15:
        套用「exit」指令（exit_reason=CLOSED_LOSS，
                          exit_price=entry["current_price"] 或 fallback effective_stop，
                          exit_note="force_settled_after_stale_limit"）
        # 完整跑 §4.5 下游接線（見下），然後 continue
    else:
        # hold_pending_stale：不呼叫 _check_settlement，
        # entry["current_price"] 維持上一輪的值不覆寫，
        # 但 active_days 已如上遞增
        continue

if sym not in latest:
    entry.setdefault("current_price", None)
    continue

# 走到這裡：is_fresh=True（sym in latest 且 bar_date==today），
# 或 entry 非 pending_exit 部位（維持母本既有行為，不受本設計影響）。
# 正常往下走既有流程：price = latest[sym]["price"]、更新 current_price、
# 呼叫 _check_settlement（依 §4.4 決策表，pending_stale_runs 於此歸零）。
```

- **單一守衛解決「有資料但不新鮮」的歸屬問題**：`is_fresh` 在主迴圈算好
  後，pending 部位只有兩條路——新鮮 bar 才往下走既有流程（含正常呼叫
  `_check_settlement`，此時 `pending_stale_runs` 歸零）；不新鮮（不論
  `sym not in latest` 或 `bar_date != today`）一律在守衛內處理完畢並
  `continue`，不會混用舊 bar 的 high/low 冒充當輪真實資料（v5 blocker
  (a) 的具體失效路徑，v6 結構上不可能發生：不新鮮的路徑永遠不會呼叫
  `_check_settlement`）。
- **`already_tracked_today` 去重解決同日重跑冪等性**（v5 blocker (b)）：
  與母本對 `watch_days`/`active_days`（tracker.py:726-731）的既有去重
  模式完全對稱，`pending_stale_runs` 與 `active_days` 在同一天內無論
  重跑幾次都只計一次。
- **`active_days` 不受 stale continue 影響（v7 修正第六輪 blocker）**：
  v6 的 `continue` 只跳過「結算判定」（`_check_settlement` 呼叫與其下游），
  不跳過「持有天數計時」——pending 部位的 `status` 全程維持 `"active"`
  （§4.1），若 stale 輪次不遞增 `active_days`，會讓
  `_archive_to_performance_history`（tracker.py:361）算出的
  `holding_days` 系統性少算停牌/無資料的天數，且此欄位一旦歸檔即
  `performance_history.json` 的一部分，不可回溯修復。v7 在 stale 分支
  內顯式遞增 `active_days`（去重邏輯與 `pending_stale_runs` 共用同一個
  `already_tracked_today` 判斷），確保無論走新鮮路徑或 stale 路徑，
  `active_days` 對「已經過的交易日」都完整計數。
- **單一 tracker 行程前提（v7 minor 補述）**：`already_tracked_today` 去重
  依賴「同一天內的多次執行是循序的（前一次寫回 watchlist.json 後，下一次
  才讀取）」，這是母本既有架構就有的前提（`tracked_dates`/`is_rerun`
  機制本身無跨行程鎖），非本設計新增或加劇的風險面；P6（僅收盤後、僅
  交易日執行）隱含「排程只觸發單一行程」，本設計沿用此既有前提，不
  另外處理跨行程併發。
- **由於 P6 保證 tracker 只在交易日執行**，`pending_stale_runs` 的每一次
  有效遞增（已去重）天然對應一個「市場開盤但這檔股票拿不到新鮮資料」的
  交易日，不需要日曆日換算、不需要看其他部位、不會被連假拉長或縮短
  ——連假期間 tracker 根本不執行（P6），不產生任何遞增。維護停機導致
  tracker 某交易日未執行，只會讓門檻對應到的實際天數變多（延後觸發），
  方向與「寧可保守」的原則不衝突，不需特別處理。
- 門檻沿用 15，可於 §3.1 校準腳本一併檢視歷史停牌天數分布調整。
- **接線**：強制結算與 §4.4 一般 `exit` 指令共用同一段「套用指令」
  程式碼，完整跑完母本既有的結算下游：
  1. `_archive_to_performance_history(entry, exit_reason, exit_price, today)`
  2. `entry["_settled"] = True`
  3. `entry["_exit_reason"]`、`entry["_exit_price"]` 寫入
  4. `settled_entries.append(entry)`
  5. **`continue`**（跳過本次迭代其餘步驟）。
  不重寫第二套歸檔邏輯。
- 結算價：`entry["current_price"]`（上一輪有新鮮資料時寫入的最後值，
  stale 輪次不覆寫）；理論上不可能為 None（轉入 pending 前提是曾經
  `today_low ≤ effective_stop`，此時 `current_price` 必已寫入），仍保留
  `effective_stop` 作為程式防禦性 fallback，不預期觸發。
- `exit_note`: `"force_settled_after_stale_limit"`。
- 非 `pending_exit` 部位（active/watch/invalid）的資料新鮮度判定**不受
  本節影響**，維持母本既有行為（`_fetch_latest` 回傳什麼就用什麼）——
  這是既有系統的既有風險面，不在本設計範圍內展開。

## 5. 進場側規則

### 5.1 一字漲停 gate（v4 修正第三輪 major：覆寫模式取代跳過檢查）

**v3 文字「DD-19 觸價檢查前先過 gate」在實作上有歧義**（呼叫端可能誤解
為「跳過 today_low 傳遞」，但 `_eval_status` 的收盤價路徑在
`price ∈ [lower, upper*1.01]` 時仍會回傳 active，繞過 gate——第三輪
skeptic 反例：buy_zone 95~100、一字漲停收 96.8，即使跳過 DD-19，收盤價
落在買區內仍判 active，以 buy_zone_upper=100 幽靈進場）。

**v4 明定為覆寫模式**：`is_one_price_limit_up` 成立時，**不呼叫**
`_eval_status` 的正常路徑，直接**強制回傳 `("watch", None)`**，watch
天數照常消耗（同母本 tracker.py:670-680 的「名單制閘門」先例，同一種
「先跑正常判定，特殊條件下用結果覆寫」模式）。此覆寫套用於 adj 與
非 adj 兩個呼叫點（tracker.py:657 與 :660），且優先於 DD-19 觸價檢查、
也優先於收盤價狀態機的所有分支（追高失效、watch/active 判定皆不執行）。

**套用前提（v9 修正第九輪 blocker）：僅在 `entry["status"] == "watch"`
時才套用此覆寫**。tracker.py:657/660 這兩個呼叫點對**每一個** entry
（不分 watch/active/invalid）無條件執行，`_eval_status` 本身對
active/invalid 的安全性完全依賴函式**內部**最前面的短路
（tracker.py:244-247）；v8 之前的文字「不呼叫 `_eval_status`」等同也
跳過了這個內部短路，若一個**已持有的 active 部位**當日 bar 恰好滿足
U1/U2/U3（利多鎖死的持股完全可能發生，非邊緣案例），gate 會把它強制
降級為 `"watch"`：
- 非拆股路徑：`_check_settlement` 因 `entry["status"] != "active"`
  直接回傳 `None`，該部位當日被靜默剔出 active、`watch_days` 錯誤
  遞增，日後可能被 `_is_expired()` 用 watch 天數上限無聲移除、不經
  `_archive_to_performance_history` 歸檔——即母本 `_eval_status`
  docstring 自身警告的 DD-17 故障模式（tracker.py:239-242），觸發
  路徑從「翻 invalid」換成「翻 watch」，後果相同。
- 拆股路徑：`adj = dict(entry)`（tracker.py:638）早於
  `entry["status"]` 被覆寫（tracker.py:682），`adj["status"]` 停留在
  覆寫前的 `"active"` 快照，`_check_settlement(adj, ...)` 仍會通過並
  產生 `defer`/`exit` 指令，寫回後出現 `entry["status"]=="watch"` 但
  同時 `pending_exit=True` 的矛盾髒狀態——擊穿 §4.1 所有建立在「status
  正確為 active」之上的下游推論（DD-9 免疫、`_is_expired` 永不到期、
  佔槽計算等六處）。

**修正**：呼叫端在套用 gate 覆寫前，先檢查 `entry["status"] == "watch"`；
非 watch 態（active/invalid）一律跳過 gate、直接呼叫 `_eval_status` 的
正常路徑（其內部短路已經正確處理 active/invalid，不需要、也不可以讓
gate 介入）。這與母本既有的「名單制閘門」先例（tracker.py:670-680）
一致——該先例本身也只作用於 `new_status=="active" and prev_status==
"watch"` 的轉移，未曾對已是 active 的部位重新分類。

**輸入標尺澄清（v4 minor 補述）**：gate 的三個輸入（`bar`/`prev_close`/
`vol_ma20`）一律取自 `latest[sym]` 的**原始市場資料**，與 `split_factor`
無關——`split_factor` 只縮放母本 `adj` 字典裡的部位衍生欄位（buy_zone/
stop_loss/target 等），不縮放原始 OHLCV。adj 與非 adj 兩個呼叫點傳入
gate 的 `bar`/`prev_close`/`vol_ma20` 應是同一份未經縮放的值，不需要、
也不應該乘上 `split_factor`。

### 5.2 跌停穿越買入區

`today_low ≤ buy_zone_upper` 時，成交價由母本的 `buy_zone_upper` 改為
`min(buy_zone_upper, open)`：正常回落 ⇒ 仍以 upper 成交（不變）；跳空
開低/一字跌停 ⇒ 以開盤市價成交，如實入帳接刀成本；後續連續跌停由出場側
pending 機制記錄。「準確性優化」（記 upper 反而更保守），成本一行，
採納但明示定位。

## 6. 實作範圍

- `_fetch_latest()` 額外回傳 `open`、`prev_close`、`volume`、`vol_ma20`、
  `bar_date`。進場側（watch 態）symbol 亦需這些欄位（供 §5.1 gate）。
- 新增 `is_limit_down_locked` / `is_one_price_limit_up` 純函式 + 單元測試
  （鎖死/盤中打開/跳空/除權息缺股利/資料異常/樣本不足/一字漲停/一字跌停
  fixture）。
- `_check_settlement()` 依 §4.4 改寫為純決策函式（只在 §4.5 新鮮度守衛
  判定「本輪新鮮」後才被呼叫），回傳型別擴充為
  `("defer"|"hold_pending"|"exit", …)`。
- 主迴圈於 `already_tracked_today` 算出之後、`sym not in latest` continue
  **之前**插入 §4.5 新鮮度守衛（單一守衛，見 §4.5 完整偽代碼）：
  `pending_exit=True` 且本輪無新鮮 bar（`sym not in latest` 或
  `bar_date != today`）⇒ 依 `already_tracked_today` 去重遞增
  `pending_stale_runs`，達門檻走「套用指令」函式強制結算並 `continue`，
  未達門檻直接 `continue`（不呼叫 `_check_settlement`）；本輪為新鮮 bar
  ⇒ `pending_stale_runs` 歸零，放行進入既有流程並呼叫 `_check_settlement`。
  「套用指令」函式供此處與 §4.4 正常 `exit` 指令共用同一段程式碼。
- `FORCE_EXPIRED` 與 `_apply_risk_controls` 加 `pending_exit` 旗標跳過。
- `_eval_status` 呼叫端（adj 與非 adj 兩處）加 §5.1 覆寫模式 gate；
  §5.2 進場價調整。
- DD-9 再入選 log 與 publisher 顯示依 `pending_exit` 改文案。
- `save_watchlist()` 改原子寫入（tmp + replace，同 `_archive` 做法）。
- tracker 入口加收盤後 + 交易日 guard（P6）。
- `scripts/calibrate_lock.py` 校準 `LOCK_VOLUME_RATIO`（跌停/漲停側各自
  校準，§3.1）。
- 單元測試補「pending 期間發生真實拆股」fixture，驗證
  `pending_vol_baseline` 標尺假設（§9 Q1，v9 起為強制前置項，非開放
  問題）；若驗證失敗，套用 §9 Q1 保守 fallback。
- `_eval_status` 呼叫端的 §5.1 gate 覆寫，套用前先檢查
  `entry["status"] == "watch"`（v9 修正第九輪 blocker，見 §5.1）。

## 7. 明確不做（範圍外）

- 處置股/全額交割股排除（TODO 獨立條目；TODO.md 路線圖已將此排在
  tracker.py 移植之前，落地順序上會先做排除——但這個順序保證只存在於
  TODO.md，本設計文件本身不依賴它，見 R11 對「排除尚未落地前」情境的
  獨立分析）。
- 盤中 tick 級模擬（只有日線資料）。
- 漲停「追價買入」模擬（本系統只掛買入區間限價單）。
- 排隊優先權/部分成交模擬（假設排隊單於解除日 open 全數成交，R6）。
- 拆股跨持倉期時歸檔 return_pct 的標尺不一致（母本既有 bug，非本設計
  引入；記 R8，tracker 移植時一併修）。

## 8. 已知風險與取捨

- R1 日線無法分辨鎖死盤中細節；錯判方向設計為保守端（誤判鎖死 ⇒ 順延；
  鎖死∧¬H1 ⇒ 取 close 下緣）。**例外**：漏股利造成的「誤判鎖死」（非
  真鎖死但 D1+D2+D3+H1 全中）方向不保證保守，是期望值近中性的雜訊，見
  R3 延伸；已知但接受，因發生率低且已被 D2/D3/H1 三重限縮。
- R2 `LOCK_VOLUME_RATIO=0.3` 校準前是暫定值，上線前必跑 `calibrate_lock.py`。
- R3 yfinance 配息缺漏：出場側由「D1 降為 sanity、主判定不依賴比值」
  消化，殘餘見 R1 例外；進場側殘餘「殖利率 ≥9.1% × 除息日 × 一字漲停
  × 恰在買區」四重複合幽靈進場，發生率極低，接受並記錄。
- R4 停牌/下市 ⇒ §4.5 兜底（v6：新鮮度守衛移至主迴圈單一守衛，
  `pending_stale_runs` 依 `already_tracked_today` 去重，與正常流程
  結構上互斥），必然收斂、不重複歸檔、不會蓋掉真實資料、同日重跑冪等。
- R5 新上市無漲跌幅股入選 universe 未驗證；D1 放寬後誤判方向保守，接受。
- R6 排隊優先權假設（§7）。
- R7 pending 佔槽至解除日，槽位吞吐下降是現實映射，不是 bug。
- R8 拆股歸檔標尺（§7 末條，母本既有）。
- R9 「一字整理日誤攔進場」：流動性前 150 universe 近乎不存在，方向
  保守（少進場），接受。
- R10 爆量一字漲停使 U3 失效、gate 不生效（§3.2），方向不定，日線資料
  的天花板，接受。
- R11（v9 新增，補第九輪 skeptic major 的 §7/§8 覆蓋缺口）處置股/全額
  交割股在排除機制（TODO 獨立條目）落地前，理論上仍可能進入 universe。
  處置股常見分盤集合競價與可能不同於一般股的漲跌幅限制帶，與 P1~P3
  假設的「一般連續競價 ±10%」不完全相符。方向分析：若處置股實際漲跌幅
  帶較窄，真實鎖死日的 `close/prev_close` 比值可能落在 D1 的 sanity
  窗口 `[0.5, 0.906]` 之外（比一般股的鎖死比值更接近 1），此時 D1 會
  拒絕、判定「非鎖死」——與 R5（新上市股，同類未特判邊緣情境）不同，
  此處**方向不保證保守**：若確實漏判，會落回未鎖死路徑以 `min(stop,
  open)` 結算，可能比真實（受限流動性、可能無法出清）出場價更樂觀。
  緩解：TODO.md 路線圖把處置股排除排在 tracker.py 移植之前（§7 已註記
  此依賴，但設計本身不假設它一定生效），且處置股占 universe（流動性
  前 150~180 名）比例極低，發生率評估為低但非零，**未做特判**，開始
  實作 tracker.py 前建議與處置股排除條目的落地順序一併確認。
  （v10 補述，第十一輪 skeptic 延伸）處置股常見的分盤集合競價，不只
  影響 D1 sanity 窗口，也可能使 P2「排隊單於跌停價附近優先成交」與 P3
  「連續競價中 today_low < X ⇒ 掛在 X 的限價買單必然已成交」這兩條
  奠定 §3.2 gate 與 §4.2 pending 解除價（`= 該日 open`）正當性的機制
  假設失效（單一撮合價可能伴隨按比例分配，不保證「價內必然全額成交」）。
  此為 R11 既有風險母體的延伸，非獨立新風險類別，方向不明，與上述
  「未做特判、發生率低」的結論一併適用。
- R12（v10 新增，第十一輪 red-team）母本既有的批次寫入時序缺口：
  `_archive_to_performance_history()` 逐筆原子寫入
  `performance_history.json`，但 `save_watchlist()` 只在整個主迴圈跑完
  後一次性寫回。若行程在兩者之間崩潰，已歸檔的部位在 watchlist.json
  中仍顯示未結算，下次執行若觸發條件仍成立會產生重複歸檔紀錄。此為
  母本既有架構缺口，適用於所有結算路徑（不限漲跌停鎖死），本設計未
  新增或加劇；與 R7/單一行程前提同屬「既有系統既有風險，範圍外」，
  不在本設計範圍內修正，僅記錄供未來獨立評估。

## 9. 待實作者確認的開放問題

- Q1（v9 調整嚴重度：原標「非阻斷」與其實際風險不對稱，改比照 R2 的
  強制前置驗證處理）`pending_vol_baseline` 若在 pending 期間遇到真實
  拆股（非除息）：凍結值與新 bar volume 的 factor 是否需要換算？§4.1
  標尺註記論證 volume 本身不受價格 factor 影響，但若 yfinance 對
  volume 也做了拆股股數調整，`pending_vol_baseline` 與新 bar volume
  的比值會被拆股因子系統性干擾，可能在真實仍鎖死的日子誤判「解鎖」，
  記錄樂觀的 open 出場價——與 R2（`LOCK_VOLUME_RATIO` 校準）性質相同，
  都是「未驗證、若答案不利會導致樂觀誤判」的上線前置項。**v9 明定為
  §6 實作範圍的強制項**：`scripts/calibrate_lock.py` 或獨立的單元測試
  必須包含至少一組「pending 期間發生真實拆股」的 fixture，驗證
  `pending_vol_baseline` 與拆股後新 bar volume 的可比性；若驗證結果
  顯示 yfinance 確實對 volume 做拆股調整，**保守 fallback**：偵測到
  pending 期間 split_factor 變動時，直接視為 D3 樣本不足（§3.1，
  volume 樣本不足時 D3 視為成立），不信任量能比較，退回保守判定。
- Q2 `_eval_status` 呼叫端加 §5.1 覆寫後，「watch 天數照常消耗」與既有
  `_max_watch_days` 計數器的互動（是否需要例外延長 watch 上限，避免
  多日一字漲停耗盡 watch 天數導致訊號到期移除）：本設計選擇不做特判
  （耗盡即到期，同一般 watch 邏輯），如需調整為實作階段的獨立決策。
  維持非阻斷：Q2 只影響**未進場訊號**的存活，不寫入
  `performance_history.json`（該檔案只記錄已進場部位的結算），與 Q1
  性質不同。

## 附錄 A：抗辯修正對照（v1 → v10，共十一輪三鏡頭抗辯）

### 第一輪（v1 → v2）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | REFUTED | [blocker] 漲停價可低於 buy_zone_upper，「零變更」為假，幽靈進場 | §5 加進場 gate |
| skeptic | | [major] 缺 high 側檢查，無量陰跌尾盤殺跌停誤判不可成交 | §4.2 H1 條件 |
| skeptic | | [major] 漏股利 ⇒ D1 漏判鎖死（樂觀） | D1 下界逐輪放寬至 0.5 |
| skeptic | | [minor] D2「兩 tick」註解不精確 | §3.1 改「0.4~2 tick」 |
| skeptic | | [minor] 新上市斷言未驗證 | 列 R5 |
| red-team | REFUTED | [blocker×3] 新 status 值被 _is_expired/_eval_status/DD-9 摧毀 | §4.1 旗標表示法 |
| red-team | | [major] 槽位不計 pending | 旗標方案天然計入 |
| red-team | | [major] locked_days 停牌計數矛盾 | §4.5 日曆日 + bar 去重 |
| red-team | | [major] vol_ma20 樣本不足未定義 | §3.1 規格 |
| red-team | | [major] 缺 bar D1 失真、open 無 fallback | 新鮮度前提 + fallback 鏈 |
| red-team | | [minor] save_watchlist 非原子；強制結算無資料 fallback | §6 / §4.5 |
| simplifier | SURVIVED | [major] is_limit_up_locked 死碼；pending 應為旗標 | gate 有呼叫點；旗標已採納 |

### 第二輪（v2 → v3）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | REFUTED | [blocker] 漲停側比值下界 1.094 被漏股利擊穿 | §3.2 重寫為一字型判定 |
| skeptic | | [blocker] §4.5 兜底在 continue 後不可達 | 明定執行位置在 continue 之前 |
| skeptic | | [major] 鎖死∧¬H1 記 stop 是樂觀上界 | §4.2 改記當日 close |
| skeptic | | [major] D1 下界 0.85 不覆蓋高殖利率 | 降為 0.5 純 sanity |
| skeptic | | [major] 旗標寫入位置撞 adj 複本 | 寫入目標鐵律 + 純決策函式 |
| skeptic | | [minor] vol_ma20 視窗未定義、連續跌停假性解鎖 | §3.1 定義 + pending_vol_baseline |
| red-team | REFUTED | [blocker] pending 旗標寫進 adj 蒸發 | 同上鐵律 |
| red-team | | [major] 兜底位置不可達 | 同上 |
| red-team | | [minor] DD-9 log 文案誤導 | 改文案 |
| red-team | | [minor] 拆股歸檔標尺（母本既有） | 列 R8，範圍外 |
| red-team | | [minor] 盤中執行危害面 | P6 運行前提 |
| simplifier | SURVIVED | [minor×3] 欄位冗餘、fallback 鏈過深 | 布林保留、fallback 鏈壓為兩層 |

### 第三輪（v3 → v4）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | SURVIVED | [major] 進場 gate「跳過檢查」讀法會被收盤價路徑復活幽靈進場 | §5.1 改覆寫模式，明定兩呼叫點 |
| skeptic | | [major] pending_vol_baseline 量綱與 split_factor 縮放斷言矛盾 | §4.1 標尺註記澄清（volume 不受價格 factor 影響）+ Q1 留待實測驗證 |
| skeptic | | [minor] R1「一律保守」對漏股利誤判鎖死不成立（期望值近中性） | §8 R1 加例外說明 |
| skeptic | | [minor] U3 對爆量一字漲停不設防 | 列 R10 |
| skeptic | | [minor] LOCK_VOLUME_RATIO 校準腳本未涵蓋漲停側 | §3.1 校準改雙側 |
| red-team | REFUTED | [blocker] §4.5 強制結算未接 _settled/settled_entries/G 段，且同迭代可能雙重歸檔 | §4.5 重寫：與正常結算路徑共用套用函式 + 強制 continue |
| red-team | | [major] 15 日曆日兜底遇長假可能提前結算，截斷後續虧損 | §4.5 加「其他部位是否有新 bar」交易日確認 |
| red-team | | [minor] defer 指令 payload 未定義 baseline 來源 | §4.1 defer 指令攜帶 baseline 值 |
| red-team | | [minor] gate 觸發後 _eval_status 呼叫路徑未定義 | 併入 §5.1 覆寫模式修正 |
| red-team | | [minor] pending 期間拆股時 baseline 標尺 | 併入 §4.1 標尺註記 + Q1 |
| simplifier | SURVIVED | [minor×2] 正文夾帶版本考古、對照表應移出本體 | 已收攏至附錄 A（本節） |

### 第四輪（v4 → v5）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | REFUTED | [blocker] §4.5 觸發條件未排除「本 symbol 自己今日有新 bar、只是連續真跌停」情境，會搶在正常流程前用過期資料蓋掉真實資料 | §4.5 v5 重寫：與正常流程互斥，只在真正無資料時計數 |
| skeptic | | [minor] watchlist 全部同時 pending 時「其他部位有新 bar」判斷失效 | 隨 v5 移除跨 symbol 依賴，問題不復存在 |
| skeptic | | [minor] defer 指令「不重算 baseline」未在 §4.4 顯式陳述 | §4.1 補一句顯式陳述 |
| red-team | REFUTED | [blocker] §4.5 兜底無條件搶在 `latest[sym]` 今日真實資料之前，用過期一天的 current_price 結算 | 同上，v5 重寫解決（兩分支互斥） |
| red-team | | [minor] §5.1 gate 輸入是否需隨 split_factor 縮放未明文 | §5.1 補「輸入標尺澄清」段落 |
| simplifier | REFUTED | [major] 「其他部位是否有新 bar」代替交易日曆是脆弱間接推論，P6 已保證交易日執行，應直接用「連續無資料次數」計數 | §4.5 v5 採納：pending_stale_runs 取代日曆日+跨部位判斷 |
| simplifier | | [minor] 「existing_symbols 只有這一檔」退化分支是補丁的補丁 | 隨 v5 重寫，整個跨部位機制連同退化分支一併移除 |

### 第五輪（v5 → v6）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | REFUTED | [blocker] pending_stale_runs 同日重跑（DD-18）沒有去重，手動重跑會多計 | §4.3/§4.5 v6：改用母本既有 already_tracked_today 去重，刪除 pending_last_bar |
| skeptic | | [minor] tracker 維護停機導致某交易日未執行，門檻對應天數變多 | §4.5 補述：方向保守，不需處理 |
| red-team | REFUTED | [blocker] 「sym in latest 但 bar_date != today」的歸屬在決策表與執行位置文字互相矛盾，母本 _check_settlement 簽名無 bar_date 無法判斷 | §4.5 v6：新鮮度判定整段移到主迴圈單一守衛，_check_settlement 只在確認新鮮後才被呼叫 |
| red-team | | [blocker] pending_stale_runs 遞增缺乏同日重跑冪等保護（與 skeptic 同一發現） | 同上，already_tracked_today 去重解決 |
| simplifier | REFUTED | [major] pending_last_bar 與母本既有 already_tracked_today 功能重複，應直接重用 | §4.1/§4.3 v6 採納：移除 pending_last_bar，全面改用 already_tracked_today |
| simplifier | | [minor] §4.2/§4.5 兩張分開的狀態表增加對照負擔 | 未採納：v6 §4.5 已用單一守衛偽代碼取代原本分開的表述，實質已合併 |

### 第六輪（v6 → v7）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | REFUTED | [blocker] stale 分支的 continue 跳過母本 active_days 遞增，holding_days 被系統性低估且不可回溯 | §4.5 v7：stale 分支內顯式遞增 active_days（去重同 pending_stale_runs） |
| red-team | SURVIVED | [minor] pending_stale_runs 同日去重僅在單一 tracker 行程下成立，無跨行程鎖 | §4.5 補述：明文承認單一行程前提（P6 隱含），非本設計新增風險面 |
| simplifier | SURVIVED | [minor] pending_since 欄位無任何讀取端（非判定依據/未接歸檔/未接顯示），為死欄位 | §4.1 v7：移除 pending_since |

### 第七輪（v7 → v8）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | SURVIVED | （無新發現，active_days 遞增邏輯逐項驗證通過） | — |
| red-team | REFUTED | [blocker] §4.1 指令表、§4.2 決策表殘留 v6 重構前的舊描述（hold_pending_stale/force_exit_stale 列為 _check_settlement 回傳型別、「不計數」字樣），與 §4.4/§4.5/§6 的實際三型別結構矛盾，可能誘導實作者做回第六輪已修的 active_days 漏算 | §4.1/§4.2 v8：統一指令集合為 defer/hold_pending/exit 三種，明文 stale 邏輯完全在主迴圈層級處理、不屬於 _check_settlement 職責，刪除 §4.2 殘留行 |
| red-team | | [minor] defer 觸發輪的 active_days 遞增走母本既有機制、非 §4.5 新增邏輯，全文未明文交代 | 已於本輪 red-team 分析內驗證正確，v8 未額外行文（屬選讀性補充，非阻斷） |
| simplifier | SURVIVED | （無新發現；明確建議可停止迭代進入實作，另給兩點定稿期整理建議：附錄A 抽成獨立檔案、正文清理殘留版本考古字樣） | — |

### 第八輪（v8，文字清理確認輪）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | SURVIVED | （無新發現，v8 文字清理未引入新不一致，核心判定邏輯逐項確認正確） | — |
| red-team | SURVIVED | （無新發現，五處 `_check_settlement` 契約描述完全一致，全文無 pending_last_bar/pending_since 殘留） | — |
| simplifier | SURVIVED | （無新發現，v8 純文字修正乾淨，維持「可進入實作」判斷） | — |

（第八輪為全數 SURVIVED 的第一輪，但協議要求連續兩輪無新發現才收工，故續跑第九輪。）

### 第九輪（v8 → v9，收工確認輪）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | SURVIVED | [major] §7/§8 對「處置股在排除機制落地前」缺方向性風險分析，與 R5（新上市股）處理不對稱 | §7/§8 v9：新增 R11 分析方向與緩解，§7 註記 TODO.md 排序依賴 |
| skeptic | | [major] §9 Q1 標「非阻斷」但風險性質等同 R2（未驗證、若不利則樂觀誤判），前置閘門強度不對稱 | §9/§6 v9：Q1 改為強制前置驗證項，補保守 fallback |
| red-team | REFUTED | [blocker] §5.1 gate 覆寫未限定 `status=="watch"`，無條件套用於 adj/非 adj 兩呼叫點，會把已持有的 active 部位誤降級為 watch（非拆股路徑：DD-17 式無聲移除；拆股路徑：status 與 pending_exit 旗標矛盾的髒狀態） | §5.1 v9：明定套用前提為 `entry["status"]=="watch"`，非 watch 態一律跳過 gate、走 `_eval_status` 正常路徑（其內部短路已正確處理 active/invalid） |
| simplifier | SURVIVED | （形式確認，無新發現） | — |

### 第十輪（v9，收工確認輪 1/2）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | SURVIVED | （無新發現，§5.1 v9 修正逐項驗證通過，含 adj 時序、拆股路徑反例） | — |
| red-team | SURVIVED | （無新發現，判斷點確認為呼叫端 if/else 分流、未修改 `_eval_status` 內部，與母本既有短路無交互風險） | — |
| simplifier | SURVIVED | （無新發現，v9 修正皆為最小必要修正，維持「可進入實作」判斷） | — |

### 第十一輪（v9，收工確認輪 2/2）

| 鏡頭 | verdict | 發現 | 處置 |
|---|---|---|---|
| skeptic | SURVIVED | [minor，非阻斷建議] R11 可延伸涵蓋分盤集合競價對 P2/P3 機制假設的影響（既有風險母體的深化，非獨立新風險） | §8 v10：併入 R11 補述 |
| red-team | SURVIVED | [minor，非阻斷建議] 母本既有的 archive/watchlist 批次寫入 crash-window 未被文件記錄（既有架構缺口，非本設計新增或加劇） | §8 v10：新增 R12 記錄，範圍外 |
| simplifier | SURVIVED | （無新發現，確認可收工） | — |

**收工結論**：第十、十一輪連續两輪皆為零 blocker、零 major，僅第十一輪
兩則鏡頭各給出一條「非阻斷、建議補述」的 minor 意見，已併入 v10 的
R11/R12（純文件記錄，不改變任何判定邏輯）。符合 loop-until-dry 收工
條件（連續 2 輪無新的 blocker/major 發現），本設計於 v10 定稿。
