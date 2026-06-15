from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import geopandas as gpd
import mercantile
import networkx as nx
import numpy as np
import pandas as pd
from mapbox_vector_tile import encode
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write
from shapely.geometry import box, mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBSITE_ROOT = PROJECT_ROOT / "Taipei_City_Urban_Resilience_Map_Website"
OUTPUT_DIR = PROJECT_ROOT / "output"
WEB_DIR = WEBSITE_ROOT / "road_pmtiles_web"
DATA_DIR = WEB_DIR / "data"

TARGET_CRS = "EPSG:3826"
WEB_CRS = "EPSG:4326"
MERCATOR_CRS = "EPSG:3857"

ROADS_PATH = OUTPUT_DIR / "road_grid_travel_time_pre_post.geojson"
TOWN_PATH = PROJECT_ROOT / "data" / "TOWN_MOI_1140318" / "TOWN_MOI_1140318.shp"

MIN_ZOOM = 10
MAX_ZOOM = 15
TILE_EXTENT = 4096
LAYER_NAME = "roads"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def read_taipei_boundary() -> gpd.GeoDataFrame:
    town = gpd.read_file(TOWN_PATH)
    town.columns = [str(c).strip().strip("'").strip('"') for c in town.columns]

    if "COUNTYCODE" in town.columns:
        boundary = town[
            town["COUNTYCODE"].astype(str).str.contains("63000", na=False)
        ].copy()
    elif "COUNTYNAME" in town.columns:
        boundary = town[
            town["COUNTYNAME"].astype(str).str.contains(
                "臺北市|台北市", na=False, regex=True
            )
        ].copy()
    else:
        boundary = town.copy()

    boundary = boundary.to_crs(TARGET_CRS).dissolve()
    return boundary


def bool_series(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series.fillna(False)

    text = series.astype(str).str.lower().str.strip()
    return text.isin(["true", "1", "yes", "y"])


def prepare_roads() -> gpd.GeoDataFrame:
    roads = gpd.read_file(ROADS_PATH).to_crs(TARGET_CRS)

    required = [
        "road_grid_id",
        "grid_id",
        "road_type_model",
        "segment_length_m",
        "pre_travel_time_min",
        "post_travel_time_min",
        "pre_speed_kph",
        "post_speed_kph",
        "is_closed_post",
        "geometry",
    ]
    missing = [col for col in required if col not in roads.columns]
    if missing:
        raise KeyError(f"Missing required road columns: {missing}")

    roads["is_closed_post"] = bool_series(roads["is_closed_post"])
    if "is_closed_by_flood" in roads.columns:
        roads["is_closed_by_flood"] = bool_series(roads["is_closed_by_flood"])
    elif "is_closed_by_flood_grid" in roads.columns:
        roads["is_closed_by_flood"] = bool_series(roads["is_closed_by_flood_grid"])
    else:
        roads["is_closed_by_flood"] = False

    if "is_tunnel_road" in roads.columns:
        roads["is_tunnel_road"] = bool_series(roads["is_tunnel_road"])
    elif "is_underground_road" in roads.columns:
        roads["is_tunnel_road"] = bool_series(roads["is_underground_road"])
    else:
        roads["is_tunnel_road"] = (
            roads["road_type_model"].astype(str).str.lower().str.strip() == "tunnel"
        )

    if "is_closed_by_tunnel_rain" in roads.columns:
        roads["is_closed_by_tunnel_rain"] = bool_series(
            roads["is_closed_by_tunnel_rain"]
        )
    elif "is_closed_by_underground_rain" in roads.columns:
        roads["is_closed_by_tunnel_rain"] = bool_series(
            roads["is_closed_by_underground_rain"]
        )
    else:
        roads["is_closed_by_tunnel_rain"] = False

    roads["segment_length_m"] = pd.to_numeric(
        roads["segment_length_m"], errors="coerce"
    ).fillna(0)
    roads["pre_travel_time_min"] = pd.to_numeric(
        roads["pre_travel_time_min"], errors="coerce"
    )
    roads["post_travel_time_min"] = pd.to_numeric(
        roads["post_travel_time_min"], errors="coerce"
    )
    roads["pre_speed_kph"] = pd.to_numeric(roads["pre_speed_kph"], errors="coerce")
    roads["post_speed_kph"] = pd.to_numeric(roads["post_speed_kph"], errors="coerce")
    roads["standing_water_depth_mm"] = pd.to_numeric(
        roads.get("standing_water_depth_mm", 0), errors="coerce"
    ).fillna(0)
    roads["flood_scenario_mmhr"] = pd.to_numeric(
        roads.get("flood_scenario_mmhr", np.nan),
        errors="coerce",
    )
    roads["flood_affected"] = (
        (roads["standing_water_depth_mm"] > 0)
        | roads["flood_scenario_mmhr"].notna()
        | roads["is_closed_by_flood"]
    )

    roads["travel_time_increase_min"] = (
        roads["post_travel_time_min"] - roads["pre_travel_time_min"]
    )

    roads["travel_time_increase_rate_pct"] = np.where(
        (roads["pre_travel_time_min"] > 0) & roads["post_travel_time_min"].notna(),
        (
            roads["post_travel_time_min"] / roads["pre_travel_time_min"] - 1
        )
        * 100,
        np.nan,
    )

    roads["affected"] = (
        roads["is_closed_post"]
        | (roads["travel_time_increase_min"].fillna(0) > 1e-6)
    )

    return roads


def weighted_mean(values: pd.Series, weights: pd.Series) -> float | None:
    valid = values.notna() & weights.notna() & (weights > 0)
    if valid.sum() == 0:
        return None
    return float(np.average(values[valid], weights=weights[valid]))


def length_share_pct(mask: pd.Series, lengths: pd.Series, total_length_m: float) -> float | None:
    if total_length_m <= 0:
        return None
    return float(lengths[mask].sum()) / total_length_m * 100


def largest_component_node_count(edge_df: pd.DataFrame) -> int:
    graph = nx.Graph()

    for row in edge_df.itertuples(index=False):
        graph.add_edge(str(row.u), str(row.v))

    if graph.number_of_nodes() == 0:
        return 0

    return len(max(nx.connected_components(graph), key=len))


def compute_stats(roads: gpd.GeoDataFrame) -> dict:
    total_segments = int(len(roads))
    total_length_m = float(roads["segment_length_m"].sum())

    open_roads = roads[
        (~roads["is_closed_post"])
        & roads["pre_travel_time_min"].notna()
        & roads["post_travel_time_min"].notna()
    ].copy()

    affected_roads = roads[roads["affected"]].copy()
    closed_roads = roads[roads["is_closed_post"]].copy()
    flood_affected_roads = roads[roads["flood_affected"]].copy()

    increase_rate = pd.to_numeric(
        roads["travel_time_increase_rate_pct"],
        errors="coerce",
    )
    affected_gt0_mask = roads["is_closed_post"] | (increase_rate > 0)
    affected_gt5_mask = roads["is_closed_post"] | (increase_rate > 5)
    affected_gt10_mask = roads["is_closed_post"] | (increase_rate > 10)

    pre_time_sum = float(open_roads["pre_travel_time_min"].sum())
    post_time_sum = float(open_roads["post_travel_time_min"].sum())

    avg_increase_rate_pct = None
    if pre_time_sum > 0:
        avg_increase_rate_pct = (post_time_sum / pre_time_sum - 1) * 100

    length_weighted_increase_rate_pct = weighted_mean(
        open_roads["travel_time_increase_rate_pct"],
        open_roads["segment_length_m"],
    )

    affected_length_m = float(affected_roads["segment_length_m"].sum())
    closed_length_m = float(closed_roads["segment_length_m"].sum())
    flood_affected_length_m = float(flood_affected_roads["segment_length_m"].sum())

    affected_road_ratio_pct = (
        affected_length_m / total_length_m * 100 if total_length_m > 0 else None
    )
    affected_gt0_ratio_pct = length_share_pct(
        affected_gt0_mask,
        roads["segment_length_m"],
        total_length_m,
    )
    affected_gt5_ratio_pct = length_share_pct(
        affected_gt5_mask,
        roads["segment_length_m"],
        total_length_m,
    )
    affected_gt10_ratio_pct = length_share_pct(
        affected_gt10_mask,
        roads["segment_length_m"],
        total_length_m,
    )
    closed_road_ratio_pct = (
        closed_length_m / total_length_m * 100 if total_length_m > 0 else None
    )
    flood_affected_road_ratio_pct = (
        flood_affected_length_m / total_length_m * 100
        if total_length_m > 0
        else None
    )

    accessibility_decline_pct = closed_road_ratio_pct
    accessibility_method = "closed road length share"

    if {"u", "v"}.issubset(roads.columns):
        edge_cols = ["u", "v"]
        if "key" in roads.columns:
            edge_cols.append("key")

        edge_status = (
            roads.groupby(edge_cols, as_index=False)
            .agg(is_closed_post=("is_closed_post", "max"))
            .copy()
        )

        pre_lcc_nodes = largest_component_node_count(edge_status[["u", "v"]])
        post_lcc_nodes = largest_component_node_count(
            edge_status.loc[~edge_status["is_closed_post"], ["u", "v"]]
        )

        if pre_lcc_nodes > 0:
            accessibility_decline_pct = (
                (pre_lcc_nodes - post_lcc_nodes) / pre_lcc_nodes * 100
            )
            accessibility_method = "largest connected component node loss"
    else:
        pre_lcc_nodes = None
        post_lcc_nodes = None

    max_pre_min = float(roads["pre_travel_time_min"].quantile(0.98))
    max_post_min = float(open_roads["post_travel_time_min"].quantile(0.98))

    return {
        "total_segments": total_segments,
        "total_length_km": total_length_m / 1000,
        "open_segments": int(len(open_roads)),
        "closed_segments": int(len(closed_roads)),
        "flood_affected_segments": int(len(flood_affected_roads)),
        "affected_segments": int(len(affected_roads)),
        "affected_gt0_segments": int(affected_gt0_mask.sum()),
        "affected_gt5_segments": int(affected_gt5_mask.sum()),
        "affected_gt10_segments": int(affected_gt10_mask.sum()),
        "average_travel_time_increase_rate_pct": avg_increase_rate_pct,
        "length_weighted_increase_rate_pct": length_weighted_increase_rate_pct,
        "affected_road_ratio_pct_by_length": affected_road_ratio_pct,
        "affected_gt0_road_ratio_pct_by_length": affected_gt0_ratio_pct,
        "affected_gt5_road_ratio_pct_by_length": affected_gt5_ratio_pct,
        "affected_gt10_road_ratio_pct_by_length": affected_gt10_ratio_pct,
        "closed_road_ratio_pct_by_length": closed_road_ratio_pct,
        "flood_affected_road_ratio_pct_by_length": flood_affected_road_ratio_pct,
        "flood_affected_length_km": flood_affected_length_m / 1000,
        "accessibility_decline_pct": accessibility_decline_pct,
        "accessibility_method": accessibility_method,
        "pre_largest_component_nodes": pre_lcc_nodes,
        "post_largest_component_nodes": post_lcc_nodes,
        "pre_time_p98_min": max_pre_min,
        "post_time_p98_min": max_post_min,
    }


def export_boundary(boundary: gpd.GeoDataFrame) -> dict:
    boundary_wgs84 = boundary.to_crs(WEB_CRS)
    path = DATA_DIR / "taipei_boundary.geojson"
    boundary_wgs84.to_file(path, driver="GeoJSON", encoding="utf-8")

    bounds = boundary_wgs84.total_bounds
    centroid = boundary.geometry.centroid.to_crs(WEB_CRS).iloc[0]

    return {
        "bounds": [float(x) for x in bounds],
        "center": [float(centroid.x), float(centroid.y)],
    }


def round_or_none(value: object, digits: int = 3) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def feature_properties(row: pd.Series) -> dict:
    return {
        "id": str(row["road_grid_id"]),
        "grid": int(row["grid_id"]) if pd.notna(row["grid_id"]) else None,
        "rt": str(row.get("road_type_model", "")),
        "len_m": round_or_none(row.get("segment_length_m"), 1),
        "pre_m": round_or_none(row.get("pre_travel_time_min"), 4),
        "post_m": round_or_none(row.get("post_travel_time_min"), 4),
        "inc_m": round_or_none(row.get("travel_time_increase_min"), 4),
        "inc_pct": round_or_none(row.get("travel_time_increase_rate_pct"), 2),
        "pre_kph": round_or_none(row.get("pre_speed_kph"), 1),
        "post_kph": round_or_none(row.get("post_speed_kph"), 1),
        "flood_mm": round_or_none(row.get("standing_water_depth_mm"), 1),
        "tunnel": int(bool(row.get("is_tunnel_road", False))),
        "tunnel_closed": int(bool(row.get("is_closed_by_tunnel_rain", False))),
        "closed": int(bool(row.get("is_closed_post", False))),
        "affected": int(bool(row.get("affected", False))),
    }


def simplify_tolerance(z: int) -> float:
    tolerances = {
        10: 35,
        11: 25,
        12: 14,
        13: 8,
        14: 4,
        15: 2,
    }
    return tolerances.get(z, 2)


def build_tile_features(
    roads_3857: gpd.GeoDataFrame,
    tile: mercantile.Tile,
) -> tuple[list[dict], tuple[float, float, float, float]]:
    bounds = mercantile.xy_bounds(tile)
    tile_bounds = (bounds.left, bounds.bottom, bounds.right, bounds.top)
    tile_box = box(*tile_bounds)

    idx = list(roads_3857.sindex.query(tile_box, predicate="intersects"))
    if not idx:
        return [], tile_bounds

    subset = roads_3857.iloc[idx].copy()
    subset = subset[subset.geometry.intersects(tile_box)].copy()
    if subset.empty:
        return [], tile_bounds

    tolerance = simplify_tolerance(tile.z)
    features = []

    for _, row in subset.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        clipped = geom.intersection(tile_box)
        if clipped.is_empty:
            continue

        if tolerance > 0:
            clipped = clipped.simplify(tolerance, preserve_topology=True)
            if clipped.is_empty:
                continue

        features.append(
            {
                "geometry": mapping(clipped),
                "properties": feature_properties(row),
            }
        )

    return features, tile_bounds


def build_pmtiles(roads: gpd.GeoDataFrame, map_info: dict) -> None:
    roads_3857 = roads.to_crs(MERCATOR_CRS)
    bounds_wgs84 = roads.to_crs(WEB_CRS).total_bounds

    pmtiles_path = DATA_DIR / "roads.pmtiles"
    tile_count = 0
    feature_count = 0

    header = {
        "tile_compression": Compression.GZIP,
        "tile_type": TileType.MVT,
        "min_lon_e7": math.floor(bounds_wgs84[0] * 10_000_000),
        "min_lat_e7": math.floor(bounds_wgs84[1] * 10_000_000),
        "max_lon_e7": math.ceil(bounds_wgs84[2] * 10_000_000),
        "max_lat_e7": math.ceil(bounds_wgs84[3] * 10_000_000),
        "center_zoom": 12,
        "center_lon_e7": round(map_info["center"][0] * 10_000_000),
        "center_lat_e7": round(map_info["center"][1] * 10_000_000),
    }

    metadata = {
        "name": "Taipei road travel time pre/post disaster",
        "description": "Road segment pre-disaster and post-disaster travel time.",
        "version": "1.0.0",
        "format": "pbf",
        "minzoom": MIN_ZOOM,
        "maxzoom": MAX_ZOOM,
        "bounds": ",".join(str(float(x)) for x in bounds_wgs84),
        "center": f"{map_info['center'][0]},{map_info['center'][1]},12",
        "vector_layers": [
            {
                "id": LAYER_NAME,
                "description": "Road-grid segment travel time.",
                "minzoom": MIN_ZOOM,
                "maxzoom": MAX_ZOOM,
                "fields": {
                    "id": "String",
                    "grid": "Number",
                    "rt": "String",
                    "len_m": "Number",
                    "pre_m": "Number",
                    "post_m": "Number",
                    "inc_m": "Number",
                    "inc_pct": "Number",
                    "pre_kph": "Number",
                    "post_kph": "Number",
                    "flood_mm": "Number",
                    "tunnel": "Number",
                    "tunnel_closed": "Number",
                    "closed": "Number",
                    "affected": "Number",
                },
            }
        ],
    }

    with write(str(pmtiles_path)) as writer:
        for z in range(MIN_ZOOM, MAX_ZOOM + 1):
            tiles = list(mercantile.tiles(*bounds_wgs84, zooms=[z]))
            print(f"zoom {z}: {len(tiles)} candidate tiles")

            for tile in tiles:
                features, tile_bounds = build_tile_features(roads_3857, tile)
                if not features:
                    continue

                mvt = encode(
                    {"name": LAYER_NAME, "features": features},
                    default_options={
                        "quantize_bounds": tile_bounds,
                        "extents": TILE_EXTENT,
                    },
                )
                writer.write_tile(zxy_to_tileid(tile.z, tile.x, tile.y), gzip.compress(mvt))
                tile_count += 1
                feature_count += len(features)

        writer.finalize(header, metadata)

    print(f"wrote {pmtiles_path}")
    print(f"tiles: {tile_count}, encoded feature instances: {feature_count}")


def write_stats(stats: dict) -> None:
    path = DATA_DIR / "stats.json"
    path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_index_html(map_info: dict) -> None:
    center = map_info["center"]
    bounds = map_info["bounds"]
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Taipei Road Travel Time Map</title>
  <link href="https://unpkg.com/maplibre-gl@5.0.0/dist/maplibre-gl.css" rel="stylesheet" />
  <style>
    html, body, #map {{
      height: 100%;
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .panel {{
      position: absolute;
      top: 12px;
      left: 12px;
      width: 340px;
      max-width: calc(100vw - 24px);
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d6d6d6;
      border-radius: 8px;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.16);
      padding: 14px;
      z-index: 1;
    }}
    h1 {{
      font-size: 18px;
      margin: 0 0 10px;
    }}
    .subtitle {{
      color: #555;
      font-size: 13px;
      margin-bottom: 12px;
      line-height: 1.4;
    }}
    .stats {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 12px;
    }}
    .stat {{
      background: #f5f7fa;
      border: 1px solid #e2e6ea;
      border-radius: 6px;
      padding: 8px;
    }}
    .stat .label {{
      color: #666;
      font-size: 12px;
      margin-bottom: 3px;
    }}
    .stat .value {{
      color: #111;
      font-size: 18px;
      font-weight: 700;
    }}
    .layers {{
      display: grid;
      gap: 7px;
      font-size: 14px;
    }}
    label {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .swatch {{
      width: 28px;
      height: 4px;
      border-radius: 999px;
      display: inline-block;
    }}
    .pre {{ background: #3182bd; }}
    .post {{ background: #e34a33; }}
    .closed {{ background: #111; height: 6px; }}
    .legend {{
      position: absolute;
      right: 12px;
      bottom: 28px;
      width: 260px;
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d6d6d6;
      border-radius: 8px;
      padding: 10px 12px;
      z-index: 1;
      font-size: 12px;
    }}
    .bar {{
      height: 10px;
      border-radius: 999px;
      margin: 7px 0;
      background: linear-gradient(to right, #ffffb2, #fecc5c, #fd8d3c, #f03b20, #bd0026);
    }}
    .popup {{
      font: 12px/1.45 system-ui, sans-serif;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <aside class="panel">
    <h1>臺北市道路災後旅行時間</h1>
    <div class="subtitle">比較降雨事件前的正常道路路網，以及降雨事件後的道路旅行時間與封閉路段。</div>
    <div class="stats">
      <div class="stat">
        <div class="label">平均旅行時間增加率</div>
        <div class="value" id="avgIncrease">-</div>
      </div>
      <div class="stat">
        <div class="label">受影響道路比例</div>
        <div class="value" id="affectedRatio">-</div>
      </div>
      <div class="stat">
        <div class="label">可達性下降幅度</div>
        <div class="value" id="accessDrop">-</div>
      </div>
      <div class="stat">
        <div class="label">封閉路段數</div>
        <div class="value" id="closedCount">-</div>
      </div>
    </div>
    <div class="layers">
      <label><input type="checkbox" id="togglePre" checked><span class="swatch pre"></span> 災前正常路網旅行時間</label>
      <label><input type="checkbox" id="togglePost" checked><span class="swatch post"></span> 災後道路旅行時間</label>
      <label><input type="checkbox" id="toggleClosed" checked><span class="swatch closed"></span> 封閉路段</label>
    </div>
  </aside>
  <aside class="legend">
    <strong>旅行時間顏色</strong>
    <div class="bar"></div>
    <div style="display:flex;justify-content:space-between;">
      <span>短</span><span>長</span>
    </div>
  </aside>

  <script src="https://unpkg.com/maplibre-gl@5.0.0/dist/maplibre-gl.js"></script>
  <script src="https://unpkg.com/pmtiles@4.3.0/dist/pmtiles.js"></script>
  <script>
    const center = [{center[0]:.8f}, {center[1]:.8f}];
    const bounds = [[{bounds[0]:.8f}, {bounds[1]:.8f}], [{bounds[2]:.8f}, {bounds[3]:.8f}]];

    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);

    const map = new maplibregl.Map({{
      container: "map",
      center,
      zoom: 11.4,
      maxZoom: 18,
      style: {{
        version: 8,
        sources: {{
          osm: {{
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors"
          }},
          roads: {{
            type: "vector",
            url: "pmtiles://data/roads.pmtiles"
          }},
          boundary: {{
            type: "geojson",
            data: "data/taipei_boundary.geojson"
          }}
        }},
        layers: [
          {{
            id: "osm",
            type: "raster",
            source: "osm",
            paint: {{ "raster-opacity": 0.72 }}
          }},
          {{
            id: "taipei-boundary-fill",
            type: "fill",
            source: "boundary",
            paint: {{ "fill-color": "#ffffff", "fill-opacity": 0.04 }}
          }},
          {{
            id: "taipei-boundary-line",
            type: "line",
            source: "boundary",
            paint: {{ "line-color": "#111111", "line-width": 2 }}
          }},
          {{
            id: "pre-roads",
            type: "line",
            source: "roads",
            "source-layer": "roads",
            filter: [">=", ["get", "closed"], 0],
            paint: {{
              "line-color": [
                "interpolate", ["linear"], ["coalesce", ["get", "pre_m"], 0],
                0, "#eff3ff",
                0.10, "#bdd7e7",
                0.25, "#6baed6",
                0.50, "#3182bd",
                1.00, "#08519c"
              ],
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.6, 14, 1.4, 17, 3],
              "line-opacity": 0.72
            }}
          }},
          {{
            id: "post-roads",
            type: "line",
            source: "roads",
            "source-layer": "roads",
            filter: ["==", ["get", "closed"], 0],
            paint: {{
              "line-color": [
                "interpolate", ["linear"], ["coalesce", ["get", "post_m"], 0],
                0, "#ffffb2",
                0.10, "#fecc5c",
                0.25, "#fd8d3c",
                0.50, "#f03b20",
                1.00, "#bd0026"
              ],
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.8, 14, 1.8, 17, 3.4],
              "line-opacity": 0.82
            }}
          }},
          {{
            id: "closed-roads",
            type: "line",
            source: "roads",
            "source-layer": "roads",
            filter: ["==", ["get", "closed"], 1],
            paint: {{
              "line-color": "#111111",
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.2, 14, 2.8, 17, 5],
              "line-opacity": 0.95
            }}
          }}
        ]
      }}
    }});

    map.addControl(new maplibregl.NavigationControl({{ visualizePitch: true }}));
    map.fitBounds(bounds, {{ padding: 40, duration: 0 }});

    function setLayerVisible(id, visible) {{
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    }}

    document.getElementById("togglePre").addEventListener("change", (event) => {{
      setLayerVisible("pre-roads", event.target.checked);
    }});
    document.getElementById("togglePost").addEventListener("change", (event) => {{
      setLayerVisible("post-roads", event.target.checked);
    }});
    document.getElementById("toggleClosed").addEventListener("change", (event) => {{
      setLayerVisible("closed-roads", event.target.checked);
    }});

    function pct(value) {{
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      return `${{value.toFixed(1)}}%`;
    }}

    fetch("data/stats.json")
      .then((response) => response.json())
      .then((stats) => {{
        document.getElementById("avgIncrease").textContent = pct(stats.average_travel_time_increase_rate_pct);
        document.getElementById("affectedRatio").textContent = pct(stats.affected_road_ratio_pct_by_length);
        document.getElementById("accessDrop").textContent = pct(stats.accessibility_decline_pct);
        document.getElementById("closedCount").textContent = stats.closed_segments.toLocaleString();
      }});

    const popup = new maplibregl.Popup({{ closeButton: false, closeOnClick: false }});

    map.on("mousemove", (event) => {{
      const features = map.queryRenderedFeatures(event.point, {{
        layers: ["closed-roads", "post-roads", "pre-roads"]
      }});

      if (!features.length) {{
        popup.remove();
        map.getCanvas().style.cursor = "";
        return;
      }}

      const p = features[0].properties;
      map.getCanvas().style.cursor = "pointer";
      popup
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="popup">
            <strong>${{p.id}}</strong><br>
            Road type: ${{p.rt}}<br>
            Pre: ${{Number(p.pre_m ?? 0).toFixed(3)}} min<br>
            Post: ${{p.closed ? "Closed" : Number(p.post_m ?? 0).toFixed(3) + " min"}}<br>
            Increase: ${{p.inc_pct == null ? "N/A" : Number(p.inc_pct).toFixed(1) + "%"}}<br>
            Flood depth: ${{Number(p.flood_mm ?? 0).toFixed(0)}} mm<br>
            Tunnel: ${{Number(p.tunnel ?? 0) ? "Yes" : "No"}}<br>
            Tunnel rainfall closure: ${{Number(p.tunnel_closed ?? 0) ? "Yes" : "No"}}
          </div>
        `)
        .addTo(map);
    }});
  </script>
</body>
</html>
"""

    (WEB_DIR / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    ensure_dirs()

    print("reading road travel data...")
    roads = prepare_roads()

    print("reading Taipei boundary...")
    boundary = read_taipei_boundary()
    map_info = export_boundary(boundary)

    print("computing statistics...")
    stats = compute_stats(roads)
    write_stats(stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    print("building PMTiles...")
    build_pmtiles(roads, map_info)

    print("writing web page...")
    write_index_html(map_info)
    print(f"done: {WEB_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
