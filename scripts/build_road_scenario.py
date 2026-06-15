from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from pykrige.ok import OrdinaryKriging

import build_road_pmtiles_web as pmtiles_builder


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBSITE_ROOT = PROJECT_ROOT / "Taipei_City_Urban_Resilience_Map_Website"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
SCENARIO_ROOT = WEBSITE_ROOT / "scenarios"
PMTILES_SCENARIO_ROOT = WEBSITE_ROOT / "road_pmtiles_web" / "data" / "scenarios"

TARGET_CRS = "EPSG:3826"
WEB_CRS = "EPSG:4326"

LANDSLIDE_WARNING_RAIN_MM = 500

FLOOD_SCENARIOS = [78.8, 100.0, 130.0]
FLOOD_SCENARIO_METHOD = "ceiling"
FLOOD_DEPTH_METHOD = "mid"
TUNNEL_RAIN_CLOSURE_MMHR = 78.8

TUNNEL_VALUES = {
    "tunnel",
    "underground",
    "underground_road",
    "underpass",
    "yes",
    "true",
    "1",
    "building_passage",
}

NORMAL_SPEED_KPH = {
    "expressway": 80,
    "arterial": 60,
    "bridge": 50,
    "tunnel": 40,
    "residential": 30,
    "service": 20,
}

RAIN_FACTOR_TABLE = {
    "expressway": {"low": 0.96, "mid": 0.95, "high": 0.93},
    "arterial": {"low": 0.97, "mid": 0.96, "high": 0.94},
    "bridge": {"low": 0.97, "mid": 0.96, "high": 0.94},
    "tunnel": {"low": 0.97, "mid": 0.96, "high": 0.94},
    "residential": {"low": 0.97, "mid": 0.96, "high": 0.94},
    "service": {"low": 0.97, "mid": 0.96, "high": 0.94},
}


def scenario_id_from_time(target_time: pd.Timestamp) -> str:
    return target_time.strftime("%Y%m%d_%H%M")


def rain_value_label(value: float) -> str:
    text = f"{float(value):g}"
    return text.replace("-", "m").replace(".", "p")


def scenario_id_from_uniform_rain(
    past1hr_mm: float,
    past24hr_mm: float,
    past2days_mm: float,
    past3days_mm: float,
) -> str:
    return (
        f"custom_p1_{rain_value_label(past1hr_mm)}"
        f"_p24_{rain_value_label(past24hr_mm)}"
        f"_p2d_{rain_value_label(past2days_mm)}"
        f"_p3d_{rain_value_label(past3days_mm)}"
    )


def parse_target_time(value: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError(f"Invalid target time: {value}")
    return parsed


def rain_file_for_time(target_time: pd.Timestamp) -> Path:
    path = DATA_DIR / f"rain_{target_time:%Y%m%d}.csv"
    if path.exists():
        return path

    previous_day_path = DATA_DIR / f"rain_{(target_time - pd.Timedelta(days=1)):%Y%m%d}.csv"
    if previous_day_path.exists():
        return previous_day_path

    raise FileNotFoundError(
        f"Cannot find rain CSV for {target_time:%Y-%m-%d}. "
        f"Expected {path}."
    )


def read_static_inputs():
    road_grid_path = OUTPUT_DIR / "taipei_road_grid_segments.geojson"
    grid_path = OUTPUT_DIR / "Taipei_grid_full_risk.geojson"
    debris_path = DATA_DIR / "debris1753_20260126_twd97" / "debris1753_20260126_twd97.shp"
    flood_paths = {
        78.8: DATA_DIR / "78.8mm_flooding.gpkg",
        100.0: DATA_DIR / "100mm_flooding.gpkg",
        130.0: DATA_DIR / "130mm_flooding.gpkg",
    }

    required_paths = [road_grid_path, grid_path, debris_path, *flood_paths.values()]
    missing_paths = [path for path in required_paths if not path.exists()]
    if missing_paths:
        raise FileNotFoundError(
            "Missing required input files:\n"
            + "\n".join(str(path) for path in missing_paths)
        )

    road_grid_segments = gpd.read_file(road_grid_path).to_crs(TARGET_CRS)
    grid = gpd.read_file(grid_path).to_crs(TARGET_CRS)

    if "grid_id" not in grid.columns:
        raise KeyError("grid data must contain a 'grid_id' column")

    grid = grid[["grid_id", "geometry"]].copy()

    flood_maps = {
        scenario_mmhr: gpd.read_file(path).to_crs(TARGET_CRS)
        for scenario_mmhr, path in flood_paths.items()
    }
    debris_potential = gpd.read_file(debris_path, encoding="cp950").to_crs(TARGET_CRS)

    return road_grid_segments, grid, flood_maps, debris_potential


def read_taipei_rain(rain_path: Path) -> pd.DataFrame:
    rain = pd.read_csv(rain_path, encoding="utf-8-sig")
    rain["CountyName"] = rain["CountyName"].astype(str).str.strip()
    rain["DateTime"] = pd.to_datetime(rain["DateTime"], errors="coerce")

    rain_cols = [
        "Past1hr",
        "Past10Min",
        "Past3hr",
        "Past6hr",
        "Past12hr",
        "Past24hr",
        "NOW",
        "Past2days",
        "Past3days",
    ]

    num_cols = [
        "StationLatitude",
        "StationLongitude",
        "StationAltitude",
        *rain_cols,
    ]

    for col in num_cols:
        if col in rain.columns:
            rain[col] = pd.to_numeric(rain[col], errors="coerce")

    return rain[rain["CountyName"].isin(["臺北市", "台北市"])].copy()


def extract_rain_station_tables(rain_tp: pd.DataFrame, target_time: pd.Timestamp) -> dict:
    rain_tp = rain_tp.copy()
    rain_tp["DateTime"] = pd.to_datetime(rain_tp["DateTime"], errors="coerce")

    required_value_cols = [
        "Past1hr",
        "Past24hr",
        "Past2days",
        "Past3days",
        "StationLongitude",
        "StationLatitude",
    ]

    missing_cols = [col for col in required_value_cols if col not in rain_tp.columns]
    if missing_cols:
        raise KeyError(f"rain data is missing columns: {missing_cols}")

    for col in required_value_cols:
        rain_tp[col] = pd.to_numeric(rain_tp[col], errors="coerce")

    rain_at_time = rain_tp[rain_tp["DateTime"] == target_time].copy()
    if rain_at_time.empty:
        available = (
            rain_tp["DateTime"]
            .dropna()
            .sort_values()
            .dt.strftime("%Y-%m-%d %H:%M:%S")
            .unique()
        )
        sample = ", ".join(list(available[:5]) + ["..."] + list(available[-5:]))
        raise ValueError(
            f"No Taipei rain records at {target_time:%Y-%m-%d %H:%M:%S}. "
            f"Available sample: {sample}"
        )

    station_cols = [
        "StationId",
        "StationName",
        "CountyName",
        "TownName",
        "StationLatitude",
        "StationLongitude",
    ]

    specs = {
        "rain_past1hr": ("Past1hr", "rain_past1hr_mm"),
        "rain_past24hr": ("Past24hr", "rain_past24hr_mm"),
        "rain_past2days": ("Past2days", "rain_past2days_mm"),
        "rain_past3days": ("Past3days", "rain_past3days_mm"),
    }

    tables = {}
    for key, (source_col, output_col) in specs.items():
        table = (
            rain_at_time
            .dropna(subset=[source_col, "StationLatitude", "StationLongitude"])
            [station_cols + [source_col]]
            .rename(columns={source_col: output_col})
            .reset_index(drop=True)
        )

        if len(table) < 3:
            raise ValueError(
                f"{key}: only {len(table)} valid Taipei stations at "
                f"{target_time:%Y-%m-%d %H:%M:%S}; at least 3 are required."
            )

        tables[key] = table

    return tables


def kriging_to_grid(
    station_df: pd.DataFrame,
    value_col: str,
    output_prefix: str,
    target_grid: gpd.GeoDataFrame,
    variogram_model: str = "spherical",
    nugget: float = 0,
    sill: float | None = None,
    range_m: float = 15000,
    n_closest_points: int = 16,
):
    station_gdf = gpd.GeoDataFrame(
        station_df,
        geometry=gpd.points_from_xy(
            station_df["StationLongitude"],
            station_df["StationLatitude"],
        ),
        crs="EPSG:4326",
    ).to_crs(TARGET_CRS)

    x = station_gdf.geometry.x.to_numpy()
    y = station_gdf.geometry.y.to_numpy()
    z = station_gdf[value_col].to_numpy()

    if len(station_gdf) < 3:
        raise ValueError(f"{output_prefix}: fewer than 3 stations; cannot kriging")

    if sill is None:
        sill = np.var(z)

    grid_centroid = target_grid.geometry.centroid
    grid_x = grid_centroid.x.to_numpy()
    grid_y = grid_centroid.y.to_numpy()

    if np.nanmax(z) == np.nanmin(z):
        pred = np.full(len(target_grid), z[0])
        var = np.zeros(len(target_grid))
    else:
        ok = OrdinaryKriging(
            x,
            y,
            z,
            variogram_model=variogram_model,
            variogram_parameters={
                "nugget": nugget,
                "sill": sill,
                "range": range_m,
            },
            coordinates_type="euclidean",
            verbose=False,
            enable_plotting=False,
        )

        pred, var = ok.execute(
            "points",
            grid_x,
            grid_y,
            n_closest_points=n_closest_points,
            backend="loop",
        )

        pred = np.asarray(pred, dtype=float)
        var = np.asarray(var, dtype=float)

    return np.clip(pred, 0, None), np.clip(var, 0, None)


def build_grid_rain(
    grid: gpd.GeoDataFrame,
    rain_tables: dict,
    target_time: pd.Timestamp,
) -> gpd.GeoDataFrame:
    grid_rain = grid.copy()

    inputs = [
        {
            "label": "Past 1hr",
            "station_df": rain_tables["rain_past1hr"],
            "value_col": "rain_past1hr_mm",
            "prefix": "rain_past1hr",
            "range_m": 10000,
            "n_closest_points": 12,
        },
        {
            "label": "Past 24hr",
            "station_df": rain_tables["rain_past24hr"],
            "value_col": "rain_past24hr_mm",
            "prefix": "rain_past24hr",
            "range_m": 15000,
            "n_closest_points": 16,
        },
        {
            "label": "Past 2days",
            "station_df": rain_tables["rain_past2days"],
            "value_col": "rain_past2days_mm",
            "prefix": "rain_past2days",
            "range_m": 15000,
            "n_closest_points": 16,
        },
        {
            "label": "Past 3days",
            "station_df": rain_tables["rain_past3days"],
            "value_col": "rain_past3days_mm",
            "prefix": "rain_past3days",
            "range_m": 15000,
            "n_closest_points": 16,
        },
    ]

    for item in inputs:
        pred, var = kriging_to_grid(
            item["station_df"],
            item["value_col"],
            item["prefix"],
            target_grid=grid,
            variogram_model="spherical",
            nugget=0,
            sill=None,
            range_m=item["range_m"],
            n_closest_points=item["n_closest_points"],
        )

        grid_rain[f"{item['prefix']}_mm"] = pred
        grid_rain[f"{item['prefix']}_var"] = var
        grid_rain[f"{item['prefix']}_std"] = np.sqrt(np.clip(var, 0, None))

        mm_col = f"{item['prefix']}_mm"
        print(
            f"{item['label']} kriging:",
            f"{grid_rain[mm_col].min():.2f}",
            "~",
            f"{grid_rain[mm_col].max():.2f}",
            "mm",
        )

    grid_rain["rain_target_time"] = target_time
    return grid_rain


def build_uniform_rain_grid(
    grid: gpd.GeoDataFrame,
    past1hr_mm: float,
    past24hr_mm: float,
    past2days_mm: float,
    past3days_mm: float,
) -> gpd.GeoDataFrame:
    grid_rain = grid.copy()

    rain_values = {
        "rain_past1hr_mm": past1hr_mm,
        "rain_past24hr_mm": past24hr_mm,
        "rain_past2days_mm": past2days_mm,
        "rain_past3days_mm": past3days_mm,
    }

    for col, value in rain_values.items():
        grid_rain[col] = float(value)

    for prefix in [
        "rain_past1hr",
        "rain_past24hr",
        "rain_past2days",
        "rain_past3days",
    ]:
        grid_rain[f"{prefix}_var"] = 0.0
        grid_rain[f"{prefix}_std"] = 0.0

    grid_rain["rain_target_time"] = (
        "custom_uniform:"
        f"past1hr={float(past1hr_mm):g},"
        f"past24hr={float(past24hr_mm):g},"
        f"past2days={float(past2days_mm):g},"
        f"past3days={float(past3days_mm):g}"
    )
    return grid_rain


def get_rain_bin(past1hr_mm):
    if pd.isna(past1hr_mm):
        return "missing"
    if past1hr_mm < 2.54:
        return "low"
    if past1hr_mm <= 6.35:
        return "mid"
    return "high"


def rainfall_speed_factor(past1hr_mm, road_type):
    rain_bin = get_rain_bin(past1hr_mm)
    if rain_bin == "missing":
        return 1.00
    if road_type not in RAIN_FACTOR_TABLE:
        road_type = "residential"
    return RAIN_FACTOR_TABLE[road_type][rain_bin]


def parse_flood_depth_mm(value, method="mid"):
    if pd.isna(value):
        return np.nan

    text = str(value).strip()
    values = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if not values:
        return np.nan

    is_meter = "m" in text.lower()
    values_mm = [x * 1000 if is_meter else x for x in values]

    if len(values_mm) == 1:
        return values_mm[0]

    low = min(values_mm[0], values_mm[1])
    high = max(values_mm[0], values_mm[1])

    if method == "min":
        return low
    if method == "max":
        return high
    return (low + high) / 2


def flood_speed_cap_kph(depth_mm):
    if pd.isna(depth_mm) or depth_mm <= 0:
        return np.nan
    if depth_mm >= 300:
        return 0.0
    speed = 0.0009 * depth_mm ** 2 - 0.5529 * depth_mm + 86.9448
    return max(0.0, speed)


def choose_flood_scenario(past1hr_mm):
    if pd.isna(past1hr_mm):
        return np.nan

    scenarios = np.array(sorted(FLOOD_SCENARIOS), dtype=float)
    if past1hr_mm < scenarios.min():
        return np.nan

    if FLOOD_SCENARIO_METHOD == "floor":
        candidates = scenarios[scenarios <= past1hr_mm]
        return candidates.max() if len(candidates) else np.nan

    if FLOOD_SCENARIO_METHOD == "nearest":
        return scenarios[np.argmin(np.abs(scenarios - past1hr_mm))]

    candidates = scenarios[scenarios >= past1hr_mm]
    return candidates.min() if len(candidates) else scenarios.max()


def scenario_label(scenario):
    return f"{scenario:g}".replace(".", "p")


def add_flood_depth_to_grid(grid_gdf, flood_maps):
    out = grid_gdf.copy()
    out["_grid_row_id"] = np.arange(len(out))

    for scenario_mmhr, flood_gdf in flood_maps.items():
        flood = flood_gdf.to_crs(out.crs).copy()

        if "depth" in flood.columns:
            flood["_flood_depth_mm"] = flood["depth"].apply(
                lambda x: parse_flood_depth_mm(x, method=FLOOD_DEPTH_METHOD)
            )
        elif "GRIDCODE" in flood.columns:
            flood["_flood_depth_mm"] = flood["GRIDCODE"].map(
                {
                    1: 75,
                    2: 225,
                    3: 400,
                    4: 750,
                }
            )
        else:
            raise KeyError(
                f"Flood map {scenario_mmhr} has neither 'depth' nor 'GRIDCODE'"
            )

        joined = gpd.sjoin(
            out[["_grid_row_id", "geometry"]],
            flood[["_flood_depth_mm", "geometry"]],
            how="left",
            predicate="intersects",
        )

        depth_by_grid = joined.groupby("_grid_row_id")["_flood_depth_mm"].max()
        depth_col = f"flood_depth_{scenario_label(scenario_mmhr)}_mm"
        out[depth_col] = out["_grid_row_id"].map(depth_by_grid).fillna(0)

    return out.drop(columns="_grid_row_id")


def add_debris_potential_to_grid(grid_gdf, debris_gdf):
    out = grid_gdf.copy()
    out["_grid_row_id"] = np.arange(len(out))
    debris = debris_gdf.to_crs(out.crs)

    joined = gpd.sjoin(
        out[["_grid_row_id", "geometry"]],
        debris[["geometry"]],
        how="left",
        predicate="intersects",
    )

    debris_grid_rows = joined.loc[
        joined["index_right"].notna(),
        "_grid_row_id",
    ].unique()

    out["has_debris_potential"] = out["_grid_row_id"].isin(debris_grid_rows)
    return out.drop(columns="_grid_row_id")


def add_landslide_effective_rain_to_grid(grid_gdf):
    out = grid_gdf.copy()

    r0 = pd.to_numeric(out["rain_past24hr_mm"], errors="coerce").fillna(0)
    r1 = (
        pd.to_numeric(out["rain_past2days_mm"], errors="coerce").fillna(0)
        - r0
    ).clip(lower=0)
    r2 = (
        pd.to_numeric(out["rain_past3days_mm"], errors="coerce").fillna(0)
        - pd.to_numeric(out["rain_past2days_mm"], errors="coerce").fillna(0)
    ).clip(lower=0)

    out["landslide_r0_past24hr_mm"] = r0
    out["landslide_r1_prev_day_mm"] = r1
    out["landslide_r2_prev_2day_mm"] = r2
    out["landslide_effective_rain_mm"] = r0 + 0.7 * r1 + (0.7 ** 2) * r2

    return out


def landslide_factor(has_debris_potential, effective_rain_mm):
    if not bool(has_debris_potential):
        return 1.00
    if pd.isna(effective_rain_mm):
        return 1.00
    if effective_rain_mm >= LANDSLIDE_WARNING_RAIN_MM:
        return 0.00
    return 1.00


def add_disaster_factors_to_grid(grid_gdf, flood_maps, debris_gdf):
    required_cols = [
        "grid_id",
        "rain_past1hr_mm",
        "rain_past24hr_mm",
        "rain_past2days_mm",
        "rain_past3days_mm",
        "geometry",
    ]
    missing_cols = [col for col in required_cols if col not in grid_gdf.columns]
    if missing_cols:
        raise KeyError(f"grid is missing columns: {missing_cols}")

    out = grid_gdf.copy()

    out["rain_bin"] = out["rain_past1hr_mm"].apply(get_rain_bin)
    for road_type in NORMAL_SPEED_KPH:
        out[f"rain_factor_{road_type}"] = out["rain_past1hr_mm"].apply(
            lambda x: rainfall_speed_factor(x, road_type)
        )

    out = add_flood_depth_to_grid(out, flood_maps)
    out["flood_scenario_mmhr"] = out["rain_past1hr_mm"].apply(choose_flood_scenario)
    out["standing_water_depth_mm"] = 0.0

    for scenario in FLOOD_SCENARIOS:
        depth_col = f"flood_depth_{scenario_label(scenario)}_mm"
        mask = out["flood_scenario_mmhr"] == scenario
        out.loc[mask, "standing_water_depth_mm"] = out.loc[mask, depth_col].fillna(0)

    out["flood_speed_cap_kph"] = out["standing_water_depth_mm"].apply(
        flood_speed_cap_kph
    )
    out["flood_closure_factor"] = np.where(
        out["standing_water_depth_mm"] >= 300,
        0.00,
        1.00,
    )
    out["is_closed_by_flood_grid"] = out["flood_closure_factor"] == 0

    out = add_debris_potential_to_grid(out, debris_gdf)
    out = add_landslide_effective_rain_to_grid(out)
    out["landslide_factor"] = out.apply(
        lambda row: landslide_factor(
            row["has_debris_potential"],
            row["landslide_effective_rain_mm"],
        ),
        axis=1,
    )
    out["is_closed_by_landslide_grid"] = out["landslide_factor"] == 0
    out["is_closed_grid"] = (
        out["is_closed_by_flood_grid"] | out["is_closed_by_landslide_grid"]
    )

    return out


def normalize_road_type(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if pd.isna(value):
        return None

    value = str(value).strip().lower()
    if value in TUNNEL_VALUES:
        return "tunnel"
    if value in NORMAL_SPEED_KPH:
        return value
    if value in ["motorway", "motorway_link", "trunk", "trunk_link"]:
        return "expressway"
    if value in [
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
    ]:
        return "arterial"
    if value == "service":
        return "service"
    if value in ["residential", "living_street", "unclassified", "road"]:
        return "residential"
    return None


def is_tunnel_value(value) -> bool:
    if isinstance(value, list):
        values = value
    else:
        values = [value]

    for item in values:
        if pd.isna(item):
            continue

        text = str(item).strip().lower()
        if text in TUNNEL_VALUES:
            return True

    return False


def is_tunnel_road(row) -> bool:
    for col in ["road_type", "road_type_id", "highway", "highway_type", "tunnel"]:
        if col in row.index and is_tunnel_value(row[col]):
            return True

    return False


def infer_segment_road_type(row):
    for col in ["road_type", "road_type_id", "highway", "highway_type"]:
        if col in row.index:
            road_type = normalize_road_type(row[col])
            if road_type is not None:
                return road_type
    return "residential"


def travel_time_seconds(length_m, speed_kph):
    speed_mps = speed_kph * 1000 / 3600
    return np.where(speed_mps > 0, length_m / speed_mps, np.nan)


def calculate_road_travel(
    road_grid_segments: gpd.GeoDataFrame,
    grid_disaster: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    road_travel = road_grid_segments.copy()

    if "segment_length_m" in road_travel.columns:
        length_col = "segment_length_m"
    elif "length" in road_travel.columns:
        length_col = "length"
    else:
        raise KeyError("road_grid_segments must contain 'segment_length_m' or 'length'")

    road_travel["segment_length_m"] = pd.to_numeric(
        road_travel[length_col],
        errors="coerce",
    )

    grid_factor_cols = [
        "grid_id",
        "rain_past1hr_mm",
        "rain_bin",
        "rain_factor_expressway",
        "rain_factor_arterial",
        "rain_factor_bridge",
        "rain_factor_tunnel",
        "rain_factor_residential",
        "rain_factor_service",
        "flood_scenario_mmhr",
        "standing_water_depth_mm",
        "flood_speed_cap_kph",
        "flood_closure_factor",
        "is_closed_by_flood_grid",
        "rain_past24hr_mm",
        "rain_past2days_mm",
        "rain_past3days_mm",
        "landslide_effective_rain_mm",
        "has_debris_potential",
        "landslide_factor",
        "is_closed_by_landslide_grid",
        "is_closed_grid",
    ]

    missing_grid_cols = [
        col for col in grid_factor_cols if col not in grid_disaster.columns
    ]
    if missing_grid_cols:
        raise KeyError(f"grid_disaster is missing columns: {missing_grid_cols}")

    if grid_disaster["grid_id"].duplicated().any():
        raise ValueError("grid_disaster has duplicated grid_id")

    road_travel = road_travel.merge(
        grid_disaster[grid_factor_cols],
        on="grid_id",
        how="left",
    )

    road_travel["road_type_model"] = road_travel.apply(
        infer_segment_road_type,
        axis=1,
    )
    road_travel["is_tunnel_road"] = road_travel.apply(
        is_tunnel_road,
        axis=1,
    )
    road_travel["pre_speed_kph"] = road_travel["road_type_model"].map(
        NORMAL_SPEED_KPH
    )

    def select_rain_factor(row):
        factor_col = f"rain_factor_{row['road_type_model']}"
        if factor_col in row.index and pd.notna(row[factor_col]):
            return row[factor_col]
        return 1.00

    road_travel["rain_factor"] = road_travel.apply(select_rain_factor, axis=1)
    road_travel["speed_after_rain_kph"] = (
        road_travel["pre_speed_kph"] * road_travel["rain_factor"]
    )

    road_travel["flood_speed_cap_kph"] = pd.to_numeric(
        road_travel["flood_speed_cap_kph"],
        errors="coerce",
    )
    road_travel["speed_after_rain_flood_kph"] = np.where(
        road_travel["flood_speed_cap_kph"].notna(),
        np.minimum(
            road_travel["speed_after_rain_kph"],
            road_travel["flood_speed_cap_kph"],
        ),
        road_travel["speed_after_rain_kph"],
    )

    road_travel["landslide_factor"] = pd.to_numeric(
        road_travel["landslide_factor"],
        errors="coerce",
    ).fillna(1.00)
    road_travel["post_speed_kph"] = (
        road_travel["speed_after_rain_flood_kph"] * road_travel["landslide_factor"]
    )

    road_travel["rain_past1hr_mm"] = pd.to_numeric(
        road_travel["rain_past1hr_mm"],
        errors="coerce",
    )
    road_travel["is_closed_by_tunnel_rain"] = (
        road_travel["is_tunnel_road"]
        & (road_travel["rain_past1hr_mm"] > TUNNEL_RAIN_CLOSURE_MMHR)
    )

    road_travel["is_closed_by_flood"] = (
        (road_travel["standing_water_depth_mm"].fillna(0) >= 300)
        | road_travel["is_closed_by_tunnel_rain"]
    )
    road_travel["is_closed_by_landslide"] = road_travel["landslide_factor"] == 0
    road_travel["is_closed_post"] = (
        road_travel["is_closed_by_flood"]
        | road_travel["is_closed_by_landslide"]
        | (road_travel["post_speed_kph"] <= 0)
    )
    road_travel.loc[road_travel["is_closed_post"], "post_speed_kph"] = 0.0

    road_travel["pre_travel_time_sec"] = travel_time_seconds(
        road_travel["segment_length_m"],
        road_travel["pre_speed_kph"],
    )
    road_travel["post_travel_time_sec"] = travel_time_seconds(
        road_travel["segment_length_m"],
        road_travel["post_speed_kph"],
    )
    road_travel["pre_travel_time_min"] = road_travel["pre_travel_time_sec"] / 60
    road_travel["post_travel_time_min"] = road_travel["post_travel_time_sec"] / 60
    road_travel["travel_time_increase_sec"] = (
        road_travel["post_travel_time_sec"] - road_travel["pre_travel_time_sec"]
    )
    road_travel["travel_time_ratio"] = (
        road_travel["post_travel_time_sec"] / road_travel["pre_travel_time_sec"]
    )
    road_travel.loc[
        road_travel["is_closed_post"],
        [
            "post_travel_time_sec",
            "post_travel_time_min",
            "travel_time_increase_sec",
            "travel_time_ratio",
        ],
    ] = np.nan

    return road_travel


def update_manifest(record: dict) -> None:
    manifest_paths = [
        PMTILES_SCENARIO_ROOT / "manifest.json",
        SCENARIO_ROOT / "manifest.json",
    ]

    existing_records = {}
    for manifest_path in manifest_paths:
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            for item in data.get("scenarios", []):
                existing_records[item["id"]] = item

    existing_records[record["id"]] = record
    scenarios = sorted(
        existing_records.values(),
        key=lambda item: (item.get("target_time", ""), item.get("id", "")),
    )
    manifest = {"scenarios": scenarios}

    for manifest_path in manifest_paths:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def write_geojson_or_empty(gdf: gpd.GeoDataFrame, path: Path) -> None:
    if gdf.empty:
        path.write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return

    gdf.to_file(path, driver="GeoJSON", encoding="utf-8")


def export_hazard_grid_layers(
    grid_disaster: gpd.GeoDataFrame,
    scenario_web_dir: Path,
) -> dict:
    scenario_web_dir.mkdir(parents=True, exist_ok=True)

    out = grid_disaster.copy()
    out["rain_past1hr_mm"] = pd.to_numeric(
        out.get("rain_past1hr_mm", np.nan),
        errors="coerce",
    )
    out["flood_scenario_mmhr"] = pd.to_numeric(
        out.get("flood_scenario_mmhr", np.nan),
        errors="coerce",
    )
    out["standing_water_depth_mm"] = pd.to_numeric(
        out.get("standing_water_depth_mm", 0),
        errors="coerce",
    ).fillna(0)
    out["landslide_effective_rain_mm"] = pd.to_numeric(
        out.get("landslide_effective_rain_mm", np.nan),
        errors="coerce",
    )

    def optional_bool_series(column: str) -> pd.Series:
        if column not in out.columns:
            return pd.Series(False, index=out.index)

        series = out[column]
        if series.dtype == bool:
            return series.fillna(False)

        return series.astype(str).str.lower().str.strip().isin(["true", "1", "yes"])

    flood_mask = (
        out["flood_scenario_mmhr"].notna()
        | (out["standing_water_depth_mm"] > 0)
        | optional_bool_series("is_closed_by_flood_grid")
    )
    landslide_mask = optional_bool_series("is_closed_by_landslide_grid")

    flood_cols = [
        "grid_id",
        "rain_past1hr_mm",
        "flood_scenario_mmhr",
        "standing_water_depth_mm",
        "flood_speed_cap_kph",
        "is_closed_by_flood_grid",
        "geometry",
    ]
    landslide_cols = [
        "grid_id",
        "rain_past24hr_mm",
        "rain_past2days_mm",
        "rain_past3days_mm",
        "landslide_effective_rain_mm",
        "has_debris_potential",
        "is_closed_by_landslide_grid",
        "geometry",
    ]

    flood_cols = [col for col in flood_cols if col in out.columns]
    landslide_cols = [col for col in landslide_cols if col in out.columns]

    flood_grid = out.loc[flood_mask, flood_cols].to_crs(WEB_CRS)
    landslide_grid = out.loc[landslide_mask, landslide_cols].to_crs(WEB_CRS)

    flood_path = scenario_web_dir / "flood_affected_grids.geojson"
    landslide_path = scenario_web_dir / "landslide_affected_grids.geojson"

    write_geojson_or_empty(flood_grid, flood_path)
    write_geojson_or_empty(landslide_grid, landslide_path)

    return {
        "flood_affected_grid_count": int(len(flood_grid)),
        "landslide_affected_grid_count": int(len(landslide_grid)),
    }


def build_pmtiles_package(
    road_geojson_path: Path,
    scenario_web_dir: Path,
    target_time: pd.Timestamp | str,
    scenario_id: str,
    rain_path: Path | str,
) -> dict:
    original_data_dir = pmtiles_builder.DATA_DIR
    original_roads_path = pmtiles_builder.ROADS_PATH
    target_time_text = (
        target_time.strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(target_time, "strftime")
        else str(target_time)
    )
    rain_file_text = rain_path.name if isinstance(rain_path, Path) else str(rain_path)

    try:
        pmtiles_builder.DATA_DIR = scenario_web_dir
        pmtiles_builder.ROADS_PATH = road_geojson_path
        pmtiles_builder.ensure_dirs()

        print("Preparing PMTiles road data...")
        roads = pmtiles_builder.prepare_roads()

        print("Reading Taipei boundary...")
        boundary = pmtiles_builder.read_taipei_boundary()
        map_info = pmtiles_builder.export_boundary(boundary)

        print("Computing PMTiles stats...")
        stats = pmtiles_builder.compute_stats(roads)
        stats.update(
            {
                "scenario_id": scenario_id,
                "target_time": target_time_text,
                "rain_file": rain_file_text,
                "built_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        pmtiles_builder.write_stats(stats)

        print("Building PMTiles...")
        pmtiles_builder.build_pmtiles(roads, map_info)
        return stats
    finally:
        pmtiles_builder.DATA_DIR = original_data_dir
        pmtiles_builder.ROADS_PATH = original_roads_path


def build_scenario(target_time: pd.Timestamp, rain_path: Path | None = None) -> None:
    scenario_id = scenario_id_from_time(target_time)
    scenario_dir = SCENARIO_ROOT / scenario_id
    scenario_web_dir = PMTILES_SCENARIO_ROOT / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    scenario_web_dir.mkdir(parents=True, exist_ok=True)

    if rain_path is None:
        rain_path = rain_file_for_time(target_time)

    print(f"Scenario: {scenario_id}")
    print(f"Target time: {target_time:%Y-%m-%d %H:%M:%S}")
    print(f"Rain file: {rain_path}")

    road_grid_segments, grid, flood_maps, debris_potential = read_static_inputs()

    rain_tp = read_taipei_rain(rain_path)
    rain_tables = extract_rain_station_tables(rain_tp, target_time)
    for key, table in rain_tables.items():
        value_col = [col for col in table.columns if col.endswith("_mm")][0]
        print(
            key,
            "stations:",
            len(table),
            "range:",
            table[value_col].min(),
            "~",
            table[value_col].max(),
        )

    grid_rain = build_grid_rain(grid, rain_tables, target_time)
    grid_disaster = add_disaster_factors_to_grid(
        grid_gdf=grid_rain,
        flood_maps=flood_maps,
        debris_gdf=debris_potential,
    )
    road_travel = calculate_road_travel(road_grid_segments, grid_disaster)

    grid_rain_path = scenario_dir / "Taipei_grid_with_rain_post.geojson"
    grid_disaster_path = scenario_dir / "Taipei_grid_disaster_factors.geojson"
    road_geojson_path = scenario_dir / "road_grid_travel_time_pre_post.geojson"
    road_csv_path = scenario_dir / "road_grid_travel_time_pre_post.csv"

    print("Saving scenario files...")
    grid_rain.to_file(grid_rain_path, driver="GeoJSON", encoding="utf-8")
    grid_disaster.to_file(grid_disaster_path, driver="GeoJSON", encoding="utf-8")
    road_travel.to_file(road_geojson_path, driver="GeoJSON", encoding="utf-8")
    road_travel.drop(columns="geometry").to_csv(
        road_csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    hazard_grid_stats = export_hazard_grid_layers(
        grid_disaster=grid_disaster,
        scenario_web_dir=scenario_web_dir,
    )

    stats = build_pmtiles_package(
        road_geojson_path=road_geojson_path,
        scenario_web_dir=scenario_web_dir,
        target_time=target_time,
        scenario_id=scenario_id,
        rain_path=rain_path,
    )

    record = {
        "id": scenario_id,
        "label": target_time.strftime("%Y-%m-%d %H:%M"),
        "date": target_time.strftime("%Y-%m-%d"),
        "time": target_time.strftime("%H:%M:%S"),
        "target_time": target_time.strftime("%Y-%m-%d %H:%M:%S"),
        "rain_file": rain_path.name,
        "road_travel_csv": str(road_csv_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "pmtiles_url": f"data/scenarios/{scenario_id}/roads.pmtiles",
        "stats_url": f"data/scenarios/{scenario_id}/stats.json",
        "flood_grid_url": f"data/scenarios/{scenario_id}/flood_affected_grids.geojson",
        "landslide_grid_url": f"data/scenarios/{scenario_id}/landslide_affected_grids.geojson",
        "average_travel_time_increase_rate_pct": stats.get(
            "average_travel_time_increase_rate_pct"
        ),
        "affected_road_ratio_pct_by_length": stats.get(
            "affected_road_ratio_pct_by_length"
        ),
        "affected_gt0_road_ratio_pct_by_length": stats.get(
            "affected_gt0_road_ratio_pct_by_length"
        ),
        "affected_gt5_road_ratio_pct_by_length": stats.get(
            "affected_gt5_road_ratio_pct_by_length"
        ),
        "affected_gt10_road_ratio_pct_by_length": stats.get(
            "affected_gt10_road_ratio_pct_by_length"
        ),
        "flood_affected_road_ratio_pct_by_length": stats.get(
            "flood_affected_road_ratio_pct_by_length"
        ),
        "flood_affected_grid_count": hazard_grid_stats.get(
            "flood_affected_grid_count"
        ),
        "landslide_affected_grid_count": hazard_grid_stats.get(
            "landslide_affected_grid_count"
        ),
        "closed_segments": stats.get("closed_segments"),
    }
    update_manifest(record)

    print("Done.")
    print(f"Scenario folder: {scenario_dir}")
    print(f"PMTiles folder: {scenario_web_dir}")


def build_uniform_rain_scenario(
    past1hr_mm: float,
    past24hr_mm: float,
    past2days_mm: float,
    past3days_mm: float,
) -> dict:
    scenario_id = scenario_id_from_uniform_rain(
        past1hr_mm,
        past24hr_mm,
        past2days_mm,
        past3days_mm,
    )
    scenario_dir = SCENARIO_ROOT / scenario_id
    scenario_web_dir = PMTILES_SCENARIO_ROOT / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    scenario_web_dir.mkdir(parents=True, exist_ok=True)

    target_time_text = (
        "custom_uniform:"
        f"past1hr={float(past1hr_mm):g},"
        f"past24hr={float(past24hr_mm):g},"
        f"past2days={float(past2days_mm):g},"
        f"past3days={float(past3days_mm):g}"
    )

    print(f"Scenario: {scenario_id}")
    print(f"Uniform rain: {target_time_text}")

    road_grid_segments, grid, flood_maps, debris_potential = read_static_inputs()

    grid_rain = build_uniform_rain_grid(
        grid=grid,
        past1hr_mm=past1hr_mm,
        past24hr_mm=past24hr_mm,
        past2days_mm=past2days_mm,
        past3days_mm=past3days_mm,
    )
    grid_disaster = add_disaster_factors_to_grid(
        grid_gdf=grid_rain,
        flood_maps=flood_maps,
        debris_gdf=debris_potential,
    )
    road_travel = calculate_road_travel(road_grid_segments, grid_disaster)
    road_travel["rain_mode"] = "custom_uniform"

    grid_rain_path = scenario_dir / "Taipei_grid_with_rain_post.geojson"
    grid_disaster_path = scenario_dir / "Taipei_grid_disaster_factors.geojson"
    road_geojson_path = scenario_dir / "road_grid_travel_time_pre_post.geojson"
    road_csv_path = scenario_dir / "road_grid_travel_time_pre_post.csv"

    print("Saving custom scenario files...")
    grid_rain.to_file(grid_rain_path, driver="GeoJSON", encoding="utf-8")
    grid_disaster.to_file(grid_disaster_path, driver="GeoJSON", encoding="utf-8")
    road_travel.to_file(road_geojson_path, driver="GeoJSON", encoding="utf-8")
    road_travel.drop(columns="geometry").to_csv(
        road_csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    hazard_grid_stats = export_hazard_grid_layers(
        grid_disaster=grid_disaster,
        scenario_web_dir=scenario_web_dir,
    )

    stats = build_pmtiles_package(
        road_geojson_path=road_geojson_path,
        scenario_web_dir=scenario_web_dir,
        target_time=target_time_text,
        scenario_id=scenario_id,
        rain_path="custom_uniform_rain",
    )

    label = (
        "自選均雨量 "
        f"P1={float(past1hr_mm):g}, "
        f"P24={float(past24hr_mm):g}, "
        f"P2d={float(past2days_mm):g}, "
        f"P3d={float(past3days_mm):g}"
    )
    record = {
        "id": scenario_id,
        "label": label,
        "scenario_type": "custom_uniform",
        "target_time": target_time_text,
        "rain_file": "custom_uniform_rain",
        "custom_rain_mm": {
            "past1hr": float(past1hr_mm),
            "past24hr": float(past24hr_mm),
            "past2days": float(past2days_mm),
            "past3days": float(past3days_mm),
        },
        "road_travel_csv": str(road_csv_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "pmtiles_url": f"data/scenarios/{scenario_id}/roads.pmtiles",
        "stats_url": f"data/scenarios/{scenario_id}/stats.json",
        "flood_grid_url": f"data/scenarios/{scenario_id}/flood_affected_grids.geojson",
        "landslide_grid_url": f"data/scenarios/{scenario_id}/landslide_affected_grids.geojson",
        "average_travel_time_increase_rate_pct": stats.get(
            "average_travel_time_increase_rate_pct"
        ),
        "affected_road_ratio_pct_by_length": stats.get(
            "affected_road_ratio_pct_by_length"
        ),
        "affected_gt0_road_ratio_pct_by_length": stats.get(
            "affected_gt0_road_ratio_pct_by_length"
        ),
        "affected_gt5_road_ratio_pct_by_length": stats.get(
            "affected_gt5_road_ratio_pct_by_length"
        ),
        "affected_gt10_road_ratio_pct_by_length": stats.get(
            "affected_gt10_road_ratio_pct_by_length"
        ),
        "flood_affected_road_ratio_pct_by_length": stats.get(
            "flood_affected_road_ratio_pct_by_length"
        ),
        "flood_affected_grid_count": hazard_grid_stats.get(
            "flood_affected_grid_count"
        ),
        "landslide_affected_grid_count": hazard_grid_stats.get(
            "landslide_affected_grid_count"
        ),
        "closed_segments": stats.get("closed_segments"),
    }
    update_manifest(record)

    print("Done.")
    print(f"Scenario folder: {scenario_dir}")
    print(f"PMTiles folder: {scenario_web_dir}")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build one selectable pre/post road-network rainfall scenario."
    )
    parser.add_argument(
        "--target-time",
        required=True,
        help='Target time, for example "2024-07-10 09:00:00".',
    )
    parser.add_argument(
        "--rain-file",
        type=Path,
        default=None,
        help="Optional rain CSV path. If omitted, data/rain_YYYYMMDD.csv is used.",
    )
    args = parser.parse_args()

    target_time = parse_target_time(args.target_time)
    build_scenario(target_time, args.rain_file)


if __name__ == "__main__":
    main()
