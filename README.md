# Final_01_pre.ipynb 目錄

## 1. 套件載入與環境設定

載入本階段需要的套件，並建立 `output` 資料夾，作為後續輸出檔案的儲存位置。

主要資料：

| 變數 | 說明 |
|---|---|
| `output/` | 儲存所有前處理輸出檔案的資料夾 |

---

## 2. 讀取 / 下載臺北市道路路網

使用 OSMnx 讀取或下載臺北市道路路網，並將道路 graph 投影至 `EPSG:3826`，作為後續道路分析與空間運算的基礎。

主要資料：

| 變數 | 說明 |
|---|---|
| `G` | 臺北市道路 graph |
| `graph_path` | 道路 graph 輸出路徑 |

---

## 3. 道路型態分類

依據 OSM 道路屬性，將每條道路分類為不同 `road_type`。本階段只建立道路型態，不定義車速與通行時間。

道路分類包含：

| road_type | 說明 |
|---|---|
| `expressway` | 快速道路或高容量道路 |
| `arterial` | 主要幹道 |
| `bridge` | 橋梁或高架道路 |
| `underground` | 地下道、隧道或地下道路 |
| `residential` | 一般住宅道路 |
| `service` | 服務道路或低優先道路 |

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/taipei_drive.graphml` | 含道路型態分類的臺北市道路 graph |

---

## 4. 避難所篩選與最近道路配對

讀取全臺避難所資料，清理欄位與文字格式後，篩選臺北市範圍內的室內避難所。接著將避難所轉為空間點資料，並配對 500 公尺內最近道路。

主要資料：

| 變數 | 說明 |
|---|---|
| `shelters` | 原始避難所資料 |
| `shelters_tp` | 臺北市室內避難所 |
| `matched_unique` | 每個避難所與最近道路的配對結果 |

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/taipei_shelters_join_500m.geojson` | 臺北市室內避難所與最近道路配對結果 |

---

## 5. 建立臺北市 Grid

讀取臺北市行政區邊界，並以 `250 m × 250 m` 建立臺北市 grid。每個 grid 會被賦予唯一的 `grid_id`。

主要資料：

| 變數 | 說明 |
|---|---|
| `tp_boundary` | 臺北市行政區合併後邊界 |
| `grid_tp` | 臺北市範圍內的 250 m grid |

---

## 6. 地形與坡度分析

讀取臺北市 DEM，計算每個 grid 的平均高程與平均坡度，作為後續地形淹水風險與邊坡風險分析的基礎。

主要輸出欄位：

| 欄位 | 說明 |
|---|---|
| `mean_elevation` | grid 平均高程 |
| `slope` | grid 平均坡度 |

---

## 7. 河川疊圖與距離分析

讀取河川圖層，計算每個 grid 內的河川面積、河川占比，以及到最近河川的距離。

主要輸出欄位：

| 欄位 | 說明 |
|---|---|
| `river_area_m2` | grid 內河川面積 |
| `grid_area_m2` | grid 面積 |
| `river_ratio` | 河川面積占比 |
| `has_river` | 是否包含河川 |
| `dist_to_river_m` | 到最近河川的距離 |

---

## 8. 歷史淹水資料疊圖

讀取歷史淹水資料，將淹水 polygon 與 grid 進行空間疊合，計算每個 grid 曾經發生過幾個不同日期的淹水事件。

主要輸出欄位：

| 欄位 | 說明 |
|---|---|
| `flood_event_count` | grid 曾經相交的不同淹水日期數 |

---

## 9. 風險分數計算

根據地形、河川、歷史淹水與坡度資訊，建立四項風險指標。前三項組成總淹水風險，邊坡風險則獨立作為坡地災害指標。

風險指標：

| 欄位 | 說明 |
|---|---|
| `terrain_flood_risk` | 地形淹水風險 |
| `river_flood_risk` | 河川淹水風險 |
| `history_flood_risk` | 歷史淹水風險 |
| `total_flood_risk` | 總淹水風險 |
| `slope_risk` | 邊坡風險 |

---

## 10. 輸出完整風險 Grid

將 grid 與所有風險指標整合後輸出，作為 post-disaster analysis 的基礎資料。

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/Taipei_grid_full_risk.geojson` | 含 geometry 的完整風險 grid |
| `output/Taipei_grid_full_risk.csv` | 不含 geometry 的完整風險 grid 屬性表 |

---

## 11. Taipei Pre Map 視覺化

建立互動式 HTML 地圖，用於檢查前處理成果，包含風險 grid、道路分類、避難所與臺北市行政邊界。

主要圖層：

| 圖層 | 說明 |
|---|---|
| `Total Flood Risk` | 總淹水風險 |
| `Slope Risk` | 邊坡風險 |
| `Road` | 不同類型道路 |
| `Shelters` | 臺北市室內避難所 |
| `Taipei Boundary` | 臺北市行政邊界 |

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/Taipei_pre_map.html` | 前處理互動式地圖 |

---

## 12. 道路切分至 Grid

將道路依照 `road_type` 進行編號，並與 grid 進行空間疊合。若同一條道路穿過多個 grid，會被切分成多個 road-grid segment，並產生唯一的 `road_grid_id`。

主要輸出欄位：

| 欄位 | 說明 |
|---|---|
| `road_grid_id` | 道路與 grid 交集後的唯一分段 ID |
| `road_base_id` | 道路本身的 ID |
| `road_type` | 道路型態 |
| `grid_id` | 道路分段所在 grid |
| `segment_length_m` | 道路分段長度 |

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/taipei_road_grid_segments.geojson` | 道路切分至 grid 後的空間資料 |

---
---

# final_02_post.ipynb 目錄

## 1. Input Data Loading

讀取第一階段輸出的基礎資料，包含臺北市風險網格、道路-grid 分段資料、避難所、道路路網 graph，以及臺北市行政邊界。

主要資料：

| 變數 | 說明 |
|---|---|
| `g` | 臺北市 grid 與原始風險指標 |
| `road_grid_segments` | 被 grid 切分後的道路段 |
| `shelters` | 臺北市避難所資料 |
| `G` | 臺北市道路 graph |
| `nodes_gdf`, `edges_gdf` | graph 轉成的節點與邊 |
| `boundary` | 臺北市邊界 |

---

## 2. 讀取雨量資料

讀取 `data/rain_20240418.csv`，清理時間、座標與雨量欄位，並只保留臺北市測站資料。

輸出：

| 變數 | 說明 |
|---|---|
| `rain` | 原始雨量資料 |
| `rain_tp` | 臺北市雨量測站資料 |

---

## 3. 擷取指定時間雨量資料

設定分析時間 `target_time`，並擷取該時間點的三種累積雨量。

| 雨量欄位 | 輸出資料 | 用途 |
|---|---|---|
| `Past3hr` | `rain_past3hr_station` | 即時雨勢造成的行車降速 |
| `Past6hr` | `rain_past6hr_station` | 淹水影響判斷 |
| `Past24hr` | `rain_past24hr_station` | 坡地或土石流危害判斷 |

---

## 4. Kriging 雨量內插至 Grid

使用 Ordinary Kriging 將測站雨量內插到每個 grid 中心點，產生每個網格的雨量估計值與不確定度。

| 雨量類型 | 雨量欄位 | 不確定度欄位 |
|---|---|---|
| Past 3hr | `rain_past3hr_mm` | `rain_past3hr_std` |
| Past 6hr | `rain_past6hr_mm` | `rain_past6hr_std` |
| Past 24hr | `rain_past24hr_mm` | `rain_past24hr_std` |

---

## 5. 雨量與不確定度視覺化

繪製 Past 3hr、Past 6hr、Past 24hr 的雨量分布圖與 Kriging 不確定度圖，用來檢查內插結果是否合理。

---

## 6. 輸出含雨量資訊的 Grid 資料

將三種 kriging 雨量結果寫回 grid，形成後續災害道路分析用的 grid 資料。

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/Taipei_grid_with_rain_post.geojson` | 含 geometry 的雨量 grid |
| `output/Taipei_grid_with_rain_post.csv` | 不含 geometry 的雨量 grid 屬性表 |

---

## 7. Post Map HTML 視覺化

產生互動式 HTML 地圖，整合雨量、原始風險指標、道路、避難所與臺北市邊界。

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/Taipei_post_map.html` | 災後雨量與風險互動地圖 |

---

## 8. 道路車速函式定義

定義道路在災害情境下的速度調整規則。

道路災後速度公式：

**post_speed_kph = normal_speed_kph × rainfall_speed_factor × flood_factor × landslide_factor**

包含三種災害影響：

| 因子 | 使用資料 | 說明 |
|---|---|---|
| `rainfall_speed_factor` | `rain_past3hr_mm` | 即時雨勢造成的車速下降 |
| `flood_factor` | `rain_past6hr_mm` + `total_flood_risk` | 淹水影響 |
| `landslide_factor` | `rain_past24hr_mm` + `slope_risk` | 坡地或土石流危害 |

---

## 9. Road-Grid Disaster Travel Time Calculation

將道路-grid 分段資料與 grid 雨量/風險資料結合，計算每段道路在災害情境下的速度、通行時間與道路狀態。

主要輸出欄位：

| 欄位 | 說明 |
|---|---|
| `normal_speed_kph` | 正常狀態車速 |
| `rainfall_speed_factor` | 雨勢降速因子 |
| `flood_factor` | 淹水影響因子 |
| `landslide_factor` | 坡地危害因子 |
| `post_speed_kph` | 災後調整車速 |
| `road_status` | 道路狀態 |
| `post_travel_time` | 災後通行時間 |

輸出檔案：

| 檔案 | 說明 |
|---|---|
| `output/road_grid_traveling.csv` | 每段 road-grid 的災後通行屬性表 |

---

## 10. 下一階段

本 notebook 已完成雨量內插、災害影響規則、道路災後速度與 travel time 計算。後續可另開 notebook 進行：

| Notebook | 內容 |
|---|---|
| `final_03_recovery.ipynb` | 復原優先順序、受影響道路與避難所分析 |
| `final_04_decision.ipynb` | 決策者輸出、路徑建議、救災與避難支援分析 |