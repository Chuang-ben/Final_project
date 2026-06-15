# 臺北市道路路網災害韌性分析

本專案分析降雨相關災害對臺北市道路路網的影響。流程包含災前路網建立、雨量內插、淹水與土石流影響判斷、災後道路旅行時間估計、路徑比較、可達性分析，以及互動式網站展示。

核心問題是：在不同降雨情境下，臺北市道路的旅行時間會如何改變，哪些地區或道路型態較脆弱，以及災前與災後的最佳路徑是否產生差異。

## 專案架構

| 路徑 | 說明 |
|---|---|
| `Final_01_pre.ipynb` | 建立災前基礎資料，包括臺北市道路路網、道路切分至 grid、避難所、地形與風險 grid、災前地圖。 |
| `Final_02_post.ipynb` | 建立災後分析資料，包括雨量資料、kriging 內插、均雨量模擬、災害折減因子、道路災前與災後旅行時間。 |
| `Final_03_user.ipynb` | 進行使用者端與 grid 層級分析，包括旅行時間增加率地圖、路網密度與地形分組比較。 |
| `Final_04_validation.ipynb` | 進行淹水風險結果驗證。 |
| `scripts/build_road_scenario.py` | 建立網站可選擇的歷史降雨情境與自選均雨量情境。 |
| `scripts/build_road_pmtiles_web.py` | 將道路旅行時間資料轉成 PMTiles 網站圖層。 |
| `scripts/serve_route_comparison_web.py` | 啟動災前與災後路徑比較網站。 |
| `scripts/serve_pmtiles_web.py` | 單獨啟動 PMTiles 靜態地圖服務。 |
| `Taipei_City_Urban_Resilience_Map_Website/` | 網站相關資料夾，包含 HTML、PMTiles、情境資料。 |
| `output/` | notebook 主要輸出資料，可由 notebook 重新產生。 |
| `data/` | 原始輸入資料。 |

## 分析流程

### 1. 災前道路路網建立

對應 notebook：`Final_01_pre.ipynb`

主要工作：

- 讀取或下載臺北市 OpenStreetMap 道路路網。
- 將道路 graph 投影至 `EPSG:3826`。
- 將道路依照模型需求分類。
- 將 `tunnel`、`underground`、`underpass` 統一視為 `tunnel`。
- 建立臺北市 grid，並計算地形、河川、坡度與淹水風險資訊。
- 將道路切分到 grid，形成 road-grid segment。
- 建立災前道路與避難所地圖。

道路自由流速設定：

| 道路型態 | 自由流速 |
|---|---:|
| `expressway` | 80 km/hr |
| `arterial` | 60 km/hr |
| `bridge` | 50 km/hr |
| `tunnel` | 40 km/hr |
| `residential` | 30 km/hr |
| `service` | 20 km/hr |

主要輸出：

| 輸出檔案 | 說明 |
|---|---|
| `output/taipei_drive.graphml` | 臺北市道路 graph。 |
| `output/taipei_road_grid_segments.geojson` | 道路切分至 grid 後的 road-grid segment。 |
| `output/taipei_shelters_join_500m.geojson` | 臺北市避難所與附近道路配對資料。 |
| `output/Taipei_grid_full_risk.geojson` | 臺北市 grid 與地形、河川、坡度、淹水風險資料。 |
| `output/Taipei_grid_full_risk.csv` | 不含 geometry 的 grid 風險屬性表。 |
| `output/Taipei_pre_map.html` | 災前互動式地圖。 |

### 2. 災後雨量與道路旅行時間分析

對應 notebook：`Final_02_post.ipynb`

主要工作：

- 讀取 road-grid segment、臺北市 grid、淹水模擬圖、土石流潛勢影響範圍。
- 讀取歷史雨量資料。
- 擷取指定時間的四種雨量欄位：
  - `Past1hr`
  - `Past24hr`
  - `Past2days`
  - `Past3days`
- 使用 Ordinary Kriging 將測站雨量內插至每個 grid。
- 建立自訂均雨量模擬 grid。
- 透過 `RAIN_MODE` 選擇使用真實雨量或均雨量模擬。
- 計算降雨、淹水、土石流對道路速度與封閉狀態的影響。
- 輸出每段 road-grid 的災前與災後旅行時間。

雨量用途：

| 雨量欄位 | 用途 |
|---|---|
| `Past1hr` | 即時降雨造成道路車速折減，也用於淹水與 tunnel 封閉判斷。 |
| `Past24hr` | 土石流或坡地災害的有效雨量判斷。 |
| `Past2days` | 土石流或坡地災害的前期累積雨量判斷。 |
| `Past3days` | 土石流或坡地災害的前期累積雨量判斷。 |

災害判斷規則：

| 規則 | 門檻或說明 |
|---|---|
| 降雨車速折減 | 依道路型態與雨量級距設定不同速度折減因子。 |
| 淹水影響 | 使用 `Past1hr` 對應 `78.8`、`100`、`130 mm/hr` 淹水模擬圖。 |
| tunnel 封閉 | 當 `Past1hr > 78.8 mm/hr`，`tunnel` 道路視為封閉。 |
| 土石流或坡地災害 | 使用 `Past24hr`、`Past2days`、`Past3days` 計算有效雨量。 |
| 土石流警戒有效雨量 | `500 mm`。 |

主要輸出：

| 輸出檔案 | 說明 |
|---|---|
| `output/Taipei_grid_with_rain_post.geojson` | 含雨量估計值與 kriging 不確定度的 grid。 |
| `output/Taipei_grid_with_rain_post.csv` | 不含 geometry 的雨量 grid 屬性表。 |
| `output/road_grid_travel_time_pre_post.geojson` | 每段 road-grid 的災前與災後速度、旅行時間與封閉狀態。 |
| `output/road_grid_travel_time_pre_post.csv` | 不含 geometry 的道路旅行時間屬性表。 |

### 3. Grid 層級旅行時間增加率分析

對應 notebook：`Final_03_user.ipynb`

主要工作：

- 讀取 `output/road_grid_travel_time_pre_post.geojson`。
- 將道路 segment 的旅行時間增加率彙整到 grid。
- 建立可點擊 grid 的互動式地圖。
- 點擊 grid 後可查看：
  - 災前平均旅行時間
  - 災後平均旅行時間
  - 平均增加時間
  - 平均增加率
  - road segment 數量
  - 封閉道路比例
- 比較不同地形與路網密度下的道路旅行時間變化。

地形與路網密度分類：

| 分類面向 | 說明 |
|---|---|
| 地形分類 | 區分為山區或高坡、平地或低坡。 |
| 路網密度分類 | 依 grid 內道路總長度除以 grid 面積計算道路密度，再分為高路網密度與低路網密度。 |

主要輸出：

| 輸出檔案 | 說明 |
|---|---|
| `output/Taipei_grid_travel_time_increase_rate.geojson` | Grid 層級道路旅行時間增加資料。 |
| `output/Taipei_grid_travel_time_increase_rate.csv` | Grid 層級道路旅行時間增加屬性表。 |
| `output/Taipei_grid_travel_time_increase_rate_map.html` | 可點擊 grid 的旅行時間增加率地圖。 |
| `output/Taipei_grid_density_terrain_travel_time_comparison.geojson` | 地形與路網密度比較資料。 |
| `output/Taipei_grid_density_terrain_travel_time_comparison.csv` | 地形與路網密度比較屬性表。 |
| `output/Taipei_density_terrain_comparison_summary.csv` | 依地形與路網密度分組的統計摘要。 |

### 4. 淹水風險驗證

對應 notebook：`Final_04_validation.ipynb`

主要工作：

- 使用參考淹水資料驗證模型產生的淹水風險。
- 建立驗證地圖。
- 輸出 confusion matrix 與 threshold performance 圖表。

主要輸出：

| 輸出檔案 | 說明 |
|---|---|
| `output/flood_risk_validation_confusion_map.html` | 淹水風險驗證地圖。 |
| `output/confusion_matrix_validation.png` | Confusion matrix 圖。 |
| `output/threshold_performance_validation.png` | 不同門檻下的模型表現圖。 |

## 互動式網站

網站資料放在：

```text
Taipei_City_Urban_Resilience_Map_Website/
```

此資料夾包含：

| 子資料夾 | 說明 |
|---|---|
| `route_comparison_web/` | 路徑比較網站的 HTML。 |
| `road_pmtiles_web/` | 道路 PMTiles、統計資料、災害 grid 圖層。 |
| `scenarios/` | 各降雨情境的道路旅行時間 CSV、GeoJSON、grid 災害資料。 |

網站功能：

- 顯示災前道路路網。
- 顯示災後道路路網。
- 顯示封閉道路。
- 顯示淹水影響 grid。
- 顯示土石流或坡地災害影響 grid。
- 選擇兩個點並比較災前與災後最佳路徑。
- 計算指定時間 cutoff 內的可達範圍。
- 顯示附近避難所。
- 顯示全市路網統計。
- 支援歷史降雨事件與自選均雨量情境。

## 如何開啟網站

1. 開啟 PowerShell。

2. 進入專案資料夾：

```powershell
cd "C:\Users\Ben\Documents\NTU\RSGI\final_project"
```

3. 啟動網站：

```powershell
conda run -n RSGI python scripts\serve_route_comparison_web.py --port 8785
```

4. 在瀏覽器開啟：

```text
http://127.0.0.1:8785
```

5. 如果網站畫面沒有更新，請按：

```text
Ctrl + F5
```

啟動網站的 PowerShell 視窗需要保持開啟。若要停止網站，在該 PowerShell 視窗按：

```text
Ctrl + C
```

如果 `8785` port 被占用，可以改用其他 port，例如：

```powershell
conda run -n RSGI python scripts\serve_route_comparison_web.py --port 8786
```

然後開啟：

```text
http://127.0.0.1:8786
```

## 網站降雨情境

網站支援兩種降雨情境模式。

| 模式 | 說明 |
|---|---|
| 歷史降雨事件 | 選擇已下載的日期與小時。若該時間情境已建立，網站會直接載入。 |
| 自選均雨量 | 使用者自行輸入 `Past1hr`、`Past24hr`、`Past2day`、`Past3day`，所有 grid 使用同一組雨量。 |

目前支援的歷史降雨日期：

| 日期 |
|---|
| `2024-04-18` |
| `2024-07-10` |
| `2024-07-24` |
| `2024-07-25` |

如果歷史降雨事件尚未建立情境，可使用以下指令建立：

```powershell
conda run -n RSGI python scripts\build_road_scenario.py --target-time "2024-07-10 09:00:00"
```

建立後重新整理網站即可看到新情境。

情境輸出位置：

| 路徑 | 說明 |
|---|---|
| `Taipei_City_Urban_Resilience_Map_Website/scenarios/<scenario_id>/` | 該情境的道路旅行時間 CSV、GeoJSON 與 grid 災害資料。 |
| `Taipei_City_Urban_Resilience_Map_Website/road_pmtiles_web/data/scenarios/<scenario_id>/` | 該情境的 PMTiles、統計資料、淹水與土石流 grid 圖層。 |

## 重要輸出與用途

| 輸出檔案 | 用途 |
|---|---|
| `output/taipei_drive.graphml` | 路徑分析與道路 graph 建立。 |
| `output/taipei_road_grid_segments.geojson` | 將道路與 grid 連結，用於災害影響套疊。 |
| `output/Taipei_grid_full_risk.geojson` | 地形、坡度、河川、淹水風險等 grid 層級資料。 |
| `output/Taipei_grid_with_rain_post.geojson` | 雨量內插後的 grid。 |
| `output/road_grid_travel_time_pre_post.geojson` | 災前與災後道路旅行時間主資料。 |
| `Taipei_City_Urban_Resilience_Map_Website/road_pmtiles_web/data/scenarios/` | 網站讀取的道路 PMTiles 情境圖層。 |

## 資料來源

| 資料 | 來源 |
|---|---|
| 道路路網 | OpenStreetMap contributors. OpenStreetMap. https://www.openstreetmap.org/copyright |
| 底圖 | CARTO basemaps. https://carto.com/attributions |
| 雨量觀測資料 | 交通部中央氣象署，政府資料開放平臺。https://data.gov.tw/dataset/9177 |
| 鄉鎮市區界線 | 內政部國土測繪中心，政府資料開放平臺。https://data.gov.tw/dataset/32157 |
| 避難收容處所 | 政府資料開放平臺。https://data.nat.gov.tw/dataset/77940 |
| 臺北市降雨積水模擬圖 | 臺北市政府，政府資料開放平臺。https://data.gov.tw/dataset/121550 |
| 臺北市水利處易積水地區 | 臺北市政府工務局水利工程處，臺北市資料大平臺。https://data.taipei/dataset/detail?id=08101966-7aca-48e0-a028-6da02ba1192e |
| 土石流潛勢溪流影響範圍 | 農業部農村發展及水土保持署，政府資料開放平臺。https://data.gov.tw/dataset/176526 |
| 土石流與大規模崩塌開放資料 | 農業部農村發展及水土保持署。https://246.ardswc.gov.tw/Services/OpenData |
| 數值高程模型 DEM / DTM | 政府資料開放平臺。https://data.gov.tw/dataset/176927 |
| DEM 參考資料 | 內政部國土測繪中心。https://www.nlsc.gov.tw/cp.aspx?n=1853 |
| 河川面資料 | 經濟部水利署，政府資料開放平臺。https://data.gov.tw/dataset/25781 |

本專案使用的本地資料：

| 本地檔案 | 說明 |
|---|---|
| `data/rain_20240418.csv` | 歷史雨量資料。 |
| `data/rain_20240710.csv` | 歷史雨量資料。 |
| `data/rain_20240724.csv` | 歷史雨量資料。 |
| `data/rain_20240725.csv` | 歷史雨量資料。 |
| `data/78.8mm_flooding.gpkg` | `78.8 mm/hr` 淹水深度模擬圖。 |
| `data/100mm_flooding.gpkg` | `100 mm/hr` 淹水深度模擬圖。 |
| `data/130mm_flooding.gpkg` | `130 mm/hr` 淹水深度模擬圖。 |
| `data/debris1753_20260126_twd97/debris1753_20260126_twd97.shp` | 土石流潛勢影響範圍。 |
| `data/Taipei_dem.tif` | 臺北市 DEM / DTM。 |
| `data/riverpoly/riverpoly.shp` | 河川面資料。 |
| `data/shelter_marked.csv` | 避難所資料。 |

## 期末報告修改內容

此區塊整理期末報告修改過程中，研究問題、資料使用與方法設定的主要調整。

### 1. 修正車速影響因子的判斷方式

原先版本曾使用較主觀的風險分數來判斷道路車速折減，容易造成模型解釋不清楚，也較難說明折減因子與實際災害條件之間的關係。因此，期末版本改為以外部資料與明確門檻作為災害影響依據。

主要修改內容：

- 降雨造成的道路車速折減，改為參考 FWHAM 降雨與車速影響關係資料。
- 淹水影響改為使用政府公開資料中的 `78.8`、`100`、`130 mm/hr` 降雨積水模擬圖。
- 淹水不再只用風險分數判斷，而是使用淹水深度對應道路車速上限與道路封閉規則。
- 土石流或坡地災害影響改為使用政府公開資料中的土石流潛勢區，並搭配 `Past24hr`、`Past2days`、`Past3days` 累積雨量判斷。
- `tunnel` 類道路獨立處理，當 `Past1hr > 78.8 mm/hr` 時視為封閉。

修改後，本研究的災後道路旅行時間不再只依賴主觀風險分數，而是由降雨強度、淹水模擬深度、道路型態與土石流潛勢區共同決定，使模型更容易被解釋，也更能對應實際災害資料。

### 2. 聚焦於指揮官角度的都市韌性研究

原先研究方向較容易發散，可能同時牽涉避難行為、災害即時應變、道路修復、個人路徑選擇等多個問題。期末版本將研究主題重新聚焦在「指揮官角度的都市韌性分析」，也就是不只關心單一路徑，而是關心整體城市道路系統在降雨災害下的表現。

主要修改內容：

- 將研究問題從單一使用者避難路徑，調整為臺北市整體道路路網在降雨災害下的旅行時間變化。
- 保留起點與終點的災前 / 災後最佳路徑比較，作為案例分析。
- 新增全市 road segment 的災前與災後旅行時間比較。
- 新增 grid 層級旅行時間增加率，用來觀察哪些地區在災後交通效率下降較明顯。
- 新增全市統計指標，例如平均旅行時間增加率、受影響道路比例、封閉道路數量、淹水影響道路比例。

修改後，本研究可以同時呈現「個案路徑變化」與「整體都市韌性分布」。前者適合說明災害如何影響特定起終點之間的通行，後者則能從指揮官或都市治理角度判斷哪些區域較需要優先關注。

### 3. 將避難路徑問題調整為城市道路韌性比較

期末討論後，考量實際避難時不一定會以行車作為主要避難方式，因此本研究不再將「開車避難」作為唯一核心，而是改用行車道路網來衡量整體城市韌性。道路路網仍然重要，因為救災、物資運輸、消防救護、災後復原與跨區移動都高度依賴道路可通行性。

主要修改內容：

- 將道路旅行時間視為城市道路系統受災後效率下降的指標。
- 使用災前與災後 road segment 旅行時間差異，衡量道路系統受影響程度。
- 將道路影響結果彙整到 grid，建立臺北市道路旅行時間增加率地圖。
- 比較不同地形條件下的韌性差異，例如平地與山坡地。
- 比較不同路網密度下的韌性差異，例如高路網密度與低路網密度地區。
- 探討在相同均雨量條件下，路網密度與地形是否會影響道路旅行時間增加程度。

修改後，本研究的重點從「個人是否能開車避難」轉為「城市道路系統在災害下是否仍具備通行效率」。因此，行車路網被用來分析都市韌性，而不是單純模擬居民避難行為。

### 修改摘要

| 修改面向 | 原先問題 | 期末版本修改 |
|---|---|---|
| 車速影響因子 | 使用主觀風險分數判斷，解釋性不足。 | 改用降雨與車速關係、淹水深度、土石流潛勢區與明確封閉規則。 |
| 研究主題 | 同時牽涉避難、路徑、災害風險，方向較發散。 | 聚焦於指揮官角度的都市道路韌性分析。 |
| 分析尺度 | 偏重單一起終點路徑。 | 同時納入案例路徑、全市道路統計、grid 層級比較。 |
| 避難假設 | 行車避難可能不符合實際避難情境。 | 將行車路網作為救災、運輸與城市通行效率的韌性指標。 |
| 空間比較 | 較少比較不同都市環境條件。 | 新增平地 / 山坡地、高 / 低路網密度的韌性比較。 |

## 注意事項

- 本專案空間分析主要使用 `EPSG:3826`。
- 網站圖層以 PMTiles 與 GeoJSON 提供給瀏覽器讀取。
- `output/` 可以由 notebook 重新產生。
- `Taipei_City_Urban_Resilience_Map_Website/` 是網站運行需要的主要資料夾。
- 如果移動網站資料夾，需要同步修改 `scripts` 中的網站路徑設定。
- 如果網站畫面沒有更新，請重啟 server 並使用 `Ctrl + F5` 強制重新整理。
