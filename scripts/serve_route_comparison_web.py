from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from pyproj import Transformer
from shapely import wkt
from shapely.geometry import LineString, mapping
from shapely.ops import linemerge

import build_road_scenario


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBSITE_ROOT = PROJECT_ROOT / "Taipei_City_Urban_Resilience_Map_Website"
OUTPUT_DIR = PROJECT_ROOT / "output"
WEB_DIR = WEBSITE_ROOT / "route_comparison_web"
PMTILES_WEB_DIR = WEBSITE_ROOT / "road_pmtiles_web"
DATA_DIR = PMTILES_WEB_DIR / "data"
SCENARIO_OUTPUT_DIR = WEBSITE_ROOT / "scenarios"
SCENARIO_DATA_DIR = DATA_DIR / "scenarios"
RAIN_SCENARIO_DATES = ["20240418", "20240710", "20240724", "20240725"]

TARGET_CRS = "EPSG:3826"


def clean_edge_id(value) -> str | None:
    if pd.isna(value):
        return None

    try:
        return str(int(float(value)))
    except Exception:
        return str(value)


def bool_series(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series.fillna(False)

    text = series.astype(str).str.lower().str.strip()
    return text.isin(["true", "1", "yes", "y"])


def parse_edge_geometry(graph, u, v, data):
    geom = data.get("geometry")

    if isinstance(geom, str):
        try:
            geom = wkt.loads(geom)
        except Exception:
            geom = None

    if geom is not None:
        return geom

    ux = graph.nodes[u]["x"]
    uy = graph.nodes[u]["y"]
    vx = graph.nodes[v]["x"]
    vy = graph.nodes[v]["y"]

    return LineString([(ux, uy), (vx, vy)])


def best_edge_data(graph, u, v, weight_col):
    edge_data = graph.get_edge_data(u, v)

    if edge_data is None:
        return None

    best_key = min(
        edge_data,
        key=lambda k: edge_data[k].get(weight_col, np.inf),
    )

    return edge_data[best_key]


class RouteEngine:
    def __init__(self, graph_path: Path, road_travel_csv_path: Path, shelter_path: Path):
        self.graph_path = graph_path
        self.road_travel_csv_path = road_travel_csv_path
        self.shelter_path = shelter_path
        self.transformer_to_target = Transformer.from_crs(
            "EPSG:4326",
            TARGET_CRS,
            always_xy=True,
        )

        print("Loading road graph...")
        self.G = ox.load_graphml(graph_path)
        self.G = ox.project_graph(self.G, to_crs=TARGET_CRS)

        print("Loading road travel table...")
        self.road_travel = pd.read_csv(road_travel_csv_path)

        print("Building pre/post routing graphs...")
        self.G_pre, self.G_post = self.build_graphs()

        print("Preparing graph GeoDataFrames...")
        self.pre_nodes_gdf = ox.graph_to_gdfs(self.G_pre, nodes=True, edges=False)
        self.pre_edges_gdf = ox.graph_to_gdfs(self.G_pre, nodes=False, edges=True)
        self.post_nodes_gdf = ox.graph_to_gdfs(self.G_post, nodes=True, edges=False)
        self.post_edges_gdf = ox.graph_to_gdfs(self.G_post, nodes=False, edges=True)

        print("Loading shelters...")
        self.shelters = self.prepare_shelters()

        print("Route engine ready.")
        print(f"Pre edges: {len(self.G_pre.edges)}")
        print(f"Post edges: {len(self.G_post.edges)}")

    def prepare_shelters(self) -> gpd.GeoDataFrame:
        shelters = gpd.read_file(self.shelter_path).to_crs(TARGET_CRS)

        name_col = "避難收容處所名稱"
        address_col = "避難收容處所地址"
        capacity_col = "預計收容人數"
        town_col = "TOWNNAME"

        def first_existing(candidates, fallback=None):
            for col in candidates:
                if col in shelters.columns:
                    return col
            return fallback

        name_col = first_existing(
            [name_col, "shelter_name", "name"],
            shelters.columns[0],
        )
        address_col = first_existing(
            [address_col, "address", "addr"],
            None,
        )
        capacity_col = first_existing(
            [capacity_col, "capacity"],
            None,
        )
        town_col = first_existing(
            [town_col, "town", "TownName"],
            None,
        )

        out = shelters.copy()
        out["_shelter_name"] = out[name_col].astype(str)
        out["_address"] = out[address_col].astype(str) if address_col else ""
        out["_capacity"] = out[capacity_col] if capacity_col else None
        out["_town"] = out[town_col].astype(str) if town_col else ""

        xs = out.geometry.x.to_numpy()
        ys = out.geometry.y.to_numpy()
        out["_pre_node_static"] = ox.nearest_nodes(self.G_pre, X=xs, Y=ys)
        out["_post_node_static"] = ox.nearest_nodes(self.G_post, X=xs, Y=ys)
        return out

    def build_graphs(self):
        roads = self.road_travel.copy()

        required_cols = [
            "u",
            "v",
            "key",
            "road_grid_id",
            "segment_length_m",
            "pre_travel_time_sec",
            "post_travel_time_sec",
            "is_closed_post",
        ]
        missing = [col for col in required_cols if col not in roads.columns]
        if missing:
            raise KeyError(f"Missing road travel columns: {missing}")

        roads["u_key"] = roads["u"].apply(clean_edge_id)
        roads["v_key"] = roads["v"].apply(clean_edge_id)
        roads["key_key"] = roads["key"].apply(clean_edge_id)

        roads["segment_length_m"] = pd.to_numeric(
            roads["segment_length_m"],
            errors="coerce",
        )
        roads["pre_travel_time_sec"] = pd.to_numeric(
            roads["pre_travel_time_sec"],
            errors="coerce",
        )
        roads["post_travel_time_sec"] = pd.to_numeric(
            roads["post_travel_time_sec"],
            errors="coerce",
        )
        roads["is_closed_post"] = bool_series(roads["is_closed_post"])

        edge_cost = (
            roads.groupby(["u_key", "v_key", "key_key"], as_index=False)
            .agg(
                pre_time_sec=("pre_travel_time_sec", "sum"),
                post_time_sec=("post_travel_time_sec", "sum"),
                length_m=("segment_length_m", "sum"),
                closed_count=("is_closed_post", "sum"),
                segment_count=("road_grid_id", "count"),
            )
        )

        edge_cost["post_is_closed"] = (
            (edge_cost["closed_count"] > 0)
            | (~np.isfinite(edge_cost["post_time_sec"]))
        )

        edge_lookup = {
            (row.u_key, row.v_key, row.key_key): row
            for row in edge_cost.itertuples(index=False)
        }

        G_pre = self.G.copy()
        G_post = self.G.copy()

        for u, v, key, data in G_pre.edges(keys=True, data=True):
            lookup_key = (clean_edge_id(u), clean_edge_id(v), clean_edge_id(key))
            row = edge_lookup.get(lookup_key)

            if row is None or not np.isfinite(row.pre_time_sec):
                data["pre_route_time_sec"] = np.inf
            else:
                data["pre_route_time_sec"] = float(row.pre_time_sec)
                data["route_length_m"] = float(row.length_m)

        for u, v, key, data in G_post.edges(keys=True, data=True):
            lookup_key = (clean_edge_id(u), clean_edge_id(v), clean_edge_id(key))
            row = edge_lookup.get(lookup_key)

            if (
                row is None
                or bool(row.post_is_closed)
                or not np.isfinite(row.post_time_sec)
            ):
                data["post_route_time_sec"] = np.inf
                data["post_status"] = "closed_or_missing"
            else:
                data["post_route_time_sec"] = float(row.post_time_sec)
                data["route_length_m"] = float(row.length_m)
                data["post_status"] = "open"

        pre_remove_edges = [
            (u, v, key)
            for u, v, key, data in G_pre.edges(keys=True, data=True)
            if not np.isfinite(data.get("pre_route_time_sec", np.inf))
        ]

        post_remove_edges = [
            (u, v, key)
            for u, v, key, data in G_post.edges(keys=True, data=True)
            if not np.isfinite(data.get("post_route_time_sec", np.inf))
        ]

        G_pre.remove_edges_from(pre_remove_edges)
        G_post.remove_edges_from(post_remove_edges)

        return G_pre, G_post

    def lonlat_to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self.transformer_to_target.transform(lon, lat)

    def route_to_geometry_and_stats(self, graph, route, weight_col):
        parts = []
        total_time_sec = 0.0
        total_length_m = 0.0

        for u, v in zip(route[:-1], route[1:]):
            data = best_edge_data(graph, u, v, weight_col)

            if data is None:
                continue

            travel_time_sec = data.get(weight_col, np.inf)
            if not np.isfinite(travel_time_sec):
                continue

            geom = parse_edge_geometry(graph, u, v, data)
            length_m = data.get("route_length_m", data.get("length", geom.length))

            parts.append(geom)
            total_time_sec += float(travel_time_sec)
            total_length_m += float(length_m)

        if not parts:
            return None, np.inf, np.inf

        return linemerge(parts), total_time_sec, total_length_m

    def calculate_one(self, graph, origin_xy, dest_xy, weight_col):
        origin_node = ox.nearest_nodes(graph, X=origin_xy[0], Y=origin_xy[1])
        dest_node = ox.nearest_nodes(graph, X=dest_xy[0], Y=dest_xy[1])

        route = nx.shortest_path(
            graph,
            source=origin_node,
            target=dest_node,
            weight=weight_col,
        )

        geom, total_time_sec, total_length_m = self.route_to_geometry_and_stats(
            graph,
            route,
            weight_col,
        )

        return {
            "nodes": route,
            "geometry": geom,
            "time_sec": total_time_sec,
            "length_m": total_length_m,
            "origin_node": origin_node,
            "dest_node": dest_node,
        }

    def route_geojson(self, route_result):
        geom = route_result["geometry"]
        if geom is None:
            return None

        gdf = gpd.GeoDataFrame(
            [{"geometry": geom}],
            geometry="geometry",
            crs=TARGET_CRS,
        ).to_crs(epsg=4326)

        return mapping(gdf.geometry.iloc[0])

    def calculate(self, origin_lon, origin_lat, dest_lon, dest_lat):
        origin_xy = self.lonlat_to_xy(origin_lon, origin_lat)
        dest_xy = self.lonlat_to_xy(dest_lon, dest_lat)

        try:
            pre = self.calculate_one(
                self.G_pre,
                origin_xy,
                dest_xy,
                "pre_route_time_sec",
            )
            pre_error = None
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            pre = None
            pre_error = str(exc)

        try:
            post = self.calculate_one(
                self.G_post,
                origin_xy,
                dest_xy,
                "post_route_time_sec",
            )
            post_error = None
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            post = None
            post_error = str(exc)

        pre_time_min = pre["time_sec"] / 60 if pre else None
        post_time_min = post["time_sec"] / 60 if post else None

        if pre_time_min is not None and post_time_min is not None:
            time_change_min = post_time_min - pre_time_min
            time_change_pct = (
                time_change_min / pre_time_min * 100 if pre_time_min > 0 else None
            )
        else:
            time_change_min = None
            time_change_pct = None

        route_changed = None
        if pre is not None and post is not None:
            route_changed = pre["nodes"] != post["nodes"]

        return {
            "origin": {"lon": origin_lon, "lat": origin_lat},
            "destination": {"lon": dest_lon, "lat": dest_lat},
            "summary": {
                "pre_reachable": pre is not None,
                "post_reachable": post is not None,
                "pre_travel_time_min": pre_time_min,
                "post_travel_time_min": post_time_min,
                "travel_time_change_min": time_change_min,
                "travel_time_change_pct": time_change_pct,
                "pre_route_length_m": pre["length_m"] if pre else None,
                "post_route_length_m": post["length_m"] if post else None,
                "route_changed": route_changed,
                "pre_error": pre_error,
                "post_error": post_error,
            },
            "routes": {
                "pre": self.route_geojson(pre) if pre else None,
                "post": self.route_geojson(post) if post else None,
            },
        }

    def make_isochrone_geojson(
        self,
        nodes_gdf: gpd.GeoDataFrame,
        edges_gdf: gpd.GeoDataFrame,
        reachable_lengths: dict,
        node_buffer_m: float = 120,
        edge_buffer_m: float = 50,
    ):
        reachable_nodes = set(reachable_lengths.keys())
        if not reachable_nodes:
            return None, 0.0

        geoms = []

        node_geoms = nodes_gdf.loc[
            nodes_gdf.index.isin(reachable_nodes),
            "geometry",
        ]
        if len(node_geoms) > 0:
            geoms.extend(list(node_geoms.buffer(node_buffer_m)))

        if len(edges_gdf) > 0:
            edge_u = edges_gdf.index.get_level_values(0)
            edge_v = edges_gdf.index.get_level_values(1)
            edge_geoms = edges_gdf.loc[
                edge_u.isin(reachable_nodes) & edge_v.isin(reachable_nodes),
                "geometry",
            ]
            if len(edge_geoms) > 0:
                geoms.extend(list(edge_geoms.buffer(edge_buffer_m)))

        if not geoms:
            return None, 0.0

        geom = gpd.GeoSeries(geoms, crs=TARGET_CRS).union_all()
        area_km2 = geom.area / 1_000_000

        geom_wgs84 = gpd.GeoSeries([geom], crs=TARGET_CRS).to_crs(epsg=4326).iloc[0]
        return mapping(geom_wgs84), float(area_km2)

    def calculate_accessibility(self, origin_lon, origin_lat, cutoff_min=5):
        origin_xy = self.lonlat_to_xy(origin_lon, origin_lat)
        cutoff_sec = float(cutoff_min) * 60

        origin_node_pre = ox.nearest_nodes(
            self.G_pre,
            X=origin_xy[0],
            Y=origin_xy[1],
        )
        origin_node_post = ox.nearest_nodes(
            self.G_post,
            X=origin_xy[0],
            Y=origin_xy[1],
        )

        pre_lengths = nx.single_source_dijkstra_path_length(
            self.G_pre,
            origin_node_pre,
            cutoff=cutoff_sec,
            weight="pre_route_time_sec",
        )
        post_lengths = nx.single_source_dijkstra_path_length(
            self.G_post,
            origin_node_post,
            cutoff=cutoff_sec,
            weight="post_route_time_sec",
        )

        pre_iso, pre_area_km2 = self.make_isochrone_geojson(
            self.pre_nodes_gdf,
            self.pre_edges_gdf,
            pre_lengths,
        )
        post_iso, post_area_km2 = self.make_isochrone_geojson(
            self.post_nodes_gdf,
            self.post_edges_gdf,
            post_lengths,
        )

        shelter_features = []
        shelters = self.shelters.copy()

        shelters["_pre_node"] = shelters["_pre_node_static"]
        shelters["_post_node"] = shelters["_post_node_static"]
        shelters["_pre_time_min"] = shelters["_pre_node"].map(pre_lengths) / 60
        shelters["_post_time_min"] = shelters["_post_node"].map(post_lengths) / 60
        shelters["_pre_5min"] = shelters["_pre_node"].isin(pre_lengths.keys())
        shelters["_post_5min"] = shelters["_post_node"].isin(post_lengths.keys())

        shelters_wgs84 = shelters.to_crs(epsg=4326)

        for idx, row in shelters_wgs84.iterrows():
            shelter_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "id": int(idx) if isinstance(idx, (int, np.integer)) else str(idx),
                        "name": row["_shelter_name"],
                        "address": row["_address"],
                        "town": row["_town"],
                        "capacity": (
                            None
                            if pd.isna(row["_capacity"])
                            else float(row["_capacity"])
                        ),
                        "pre_time_min": (
                            None
                            if pd.isna(row["_pre_time_min"])
                            else float(row["_pre_time_min"])
                        ),
                        "post_time_min": (
                            None
                            if pd.isna(row["_post_time_min"])
                            else float(row["_post_time_min"])
                        ),
                        "pre_5min": bool(row["_pre_5min"]),
                        "post_5min": bool(row["_post_5min"]),
                    },
                    "geometry": mapping(row.geometry),
                }
            )

        nearest_post = (
            shelters[
                shelters["_post_time_min"].notna()
            ]
            .sort_values("_post_time_min")
            .head(5)
        )

        nearest_5 = [
            {
                "name": row["_shelter_name"],
                "town": row["_town"],
                "post_time_min": float(row["_post_time_min"]),
                "pre_time_min": (
                    None
                    if pd.isna(row["_pre_time_min"])
                    else float(row["_pre_time_min"])
                ),
            }
            for _, row in nearest_post.iterrows()
        ]

        area_decline_pct = None
        if pre_area_km2 > 0:
            area_decline_pct = (pre_area_km2 - post_area_km2) / pre_area_km2 * 100

        return {
            "origin": {"lon": origin_lon, "lat": origin_lat},
            "cutoff_min": cutoff_min,
            "summary": {
                "pre_area_km2": pre_area_km2,
                "post_area_km2": post_area_km2,
                "area_decline_pct": area_decline_pct,
                "pre_shelter_count": int(shelters["_pre_5min"].sum()),
                "post_shelter_count": int(shelters["_post_5min"].sum()),
                "nearest_5_post_shelters": nearest_5,
            },
            "isochrones": {
                "pre": pre_iso,
                "post": post_iso,
            },
            "shelters": {
                "type": "FeatureCollection",
                "features": shelter_features,
            },
        }


class ScenarioManager:
    def __init__(
        self,
        graph_path: Path,
        default_road_travel_csv_path: Path,
        shelter_path: Path,
    ):
        self.graph_path = graph_path
        self.default_road_travel_csv_path = default_road_travel_csv_path
        self.shelter_path = shelter_path
        self.scenarios = self.load_scenarios()
        self.engine_cache: dict[str, RouteEngine] = {}
        self.build_lock = threading.Lock()
        self._rain_times_cache: dict | None = None

    def load_scenarios(self) -> list[dict]:
        manifest_paths = [
            SCENARIO_DATA_DIR / "manifest.json",
            SCENARIO_OUTPUT_DIR / "manifest.json",
        ]

        scenarios = []
        for manifest_path in manifest_paths:
            if not manifest_path.exists():
                continue

            data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            scenarios = data.get("scenarios", [])
            if scenarios:
                break

        if not scenarios:
            scenarios = [
                {
                    "id": "current",
                    "label": "目前輸出資料",
                    "target_time": None,
                    "road_travel_csv": str(
                        self.default_road_travel_csv_path.relative_to(PROJECT_ROOT)
                    ).replace("\\", "/"),
                    "pmtiles_url": "data/roads.pmtiles",
                    "stats_url": "data/stats.json",
                }
            ]

        return scenarios

    def list_for_api(self) -> dict:
        return {
            "default_scenario_id": self.scenarios[0]["id"] if self.scenarios else None,
            "scenarios": self.scenarios,
        }

    def refresh(self) -> None:
        self.scenarios = self.load_scenarios()
        self._rain_times_cache = None

    @staticmethod
    def scenario_id_from_target_time(target_time: pd.Timestamp) -> str:
        return target_time.strftime("%Y%m%d_%H%M")

    def rain_times_for_api(self) -> dict:
        if self._rain_times_cache is not None:
            return self._rain_times_cache

        built_ids = {scenario.get("id") for scenario in self.scenarios}
        date_items = []

        for date_key in RAIN_SCENARIO_DATES:
            rain_path = PROJECT_ROOT / "data" / f"rain_{date_key}.csv"
            if not rain_path.exists():
                date_items.append(
                    {
                        "date_key": date_key,
                        "date_label": date_key,
                        "rain_file": rain_path.name,
                        "times": [],
                        "error": f"Missing rain file: {rain_path}",
                    }
                )
                continue

            df = pd.read_csv(rain_path, encoding="utf-8-sig", usecols=["DateTime"])
            datetimes = (
                pd.to_datetime(df["DateTime"], errors="coerce")
                .dropna()
                .drop_duplicates()
                .sort_values()
            )
            datetimes = datetimes[
                (datetimes.dt.minute == 0)
                & (datetimes.dt.second == 0)
            ]

            if datetimes.empty:
                date_items.append(
                    {
                        "date_key": date_key,
                        "date_label": date_key,
                        "rain_file": rain_path.name,
                        "times": [],
                        "error": "No valid DateTime records",
                    }
                )
                continue

            start_date = pd.Timestamp(f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}")
            times = []
            for dt in datetimes:
                target_time = pd.Timestamp(dt)
                scenario_id = self.scenario_id_from_target_time(target_time)
                hour_offset = int((target_time - start_date).total_seconds() // 3600)
                minute = target_time.minute
                if target_time.date() == start_date.date():
                    label = target_time.strftime("%H:%M")
                else:
                    label = f"{hour_offset:02d}:{minute:02d}"

                times.append(
                    {
                        "target_time": target_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "scenario_id": scenario_id,
                        "label": label,
                        "built": scenario_id in built_ids,
                    }
                )

            date_items.append(
                {
                    "date_key": date_key,
                    "date_label": start_date.strftime("%Y-%m-%d"),
                    "rain_file": rain_path.name,
                    "times": times,
                }
            )

        self._rain_times_cache = {
            "dates": date_items,
            "built_scenario_ids": sorted(x for x in built_ids if x),
        }
        return self._rain_times_cache

    def scenario_record(self, scenario_id: str | None) -> dict:
        if not self.scenarios:
            raise ValueError("No scenarios are available")

        if scenario_id is None or scenario_id == "":
            return self.scenarios[0]

        for scenario in self.scenarios:
            if scenario.get("id") == scenario_id:
                return scenario

        raise KeyError(f"Unknown scenario_id: {scenario_id}")

    def road_travel_path(self, scenario: dict) -> Path:
        csv_text = scenario.get("road_travel_csv")
        if not csv_text:
            return self.default_road_travel_csv_path

        csv_path = Path(csv_text)
        if csv_path.is_absolute():
            return csv_path

        candidates = [
            PROJECT_ROOT / csv_path,
            WEBSITE_ROOT / csv_path,
        ]

        if csv_text.startswith("scenarios/") or csv_text.startswith("scenarios\\"):
            candidates.append(SCENARIO_OUTPUT_DIR / Path(csv_text).relative_to("scenarios"))

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return candidates[-1]

    def scenario_assets_exist(self, scenario: dict) -> bool:
        csv_path = self.road_travel_path(scenario)
        pmtiles_url = scenario.get("pmtiles_url", "")
        pmtiles_path = DATA_DIR / pmtiles_url.removeprefix("data/")
        return csv_path.exists() and pmtiles_path.exists()

    def engine_for(self, scenario_id: str | None) -> RouteEngine:
        scenario = self.scenario_record(scenario_id)
        resolved_id = scenario["id"]

        if resolved_id not in self.engine_cache:
            road_travel_csv = self.road_travel_path(scenario)
            if not road_travel_csv.exists():
                raise FileNotFoundError(
                    f"Road travel CSV for scenario {resolved_id} not found: "
                    f"{road_travel_csv}"
                )

            print(f"Loading route engine for scenario {resolved_id}: {road_travel_csv}")
            self.engine_cache[resolved_id] = RouteEngine(
                self.graph_path,
                road_travel_csv,
                self.shelter_path,
            )

        return self.engine_cache[resolved_id]

    def ensure_scenario(self, target_time_text: str) -> dict:
        target_time = pd.Timestamp(target_time_text)
        if pd.isna(target_time):
            raise ValueError(f"Invalid target_time: {target_time_text}")

        scenario_id = self.scenario_id_from_target_time(target_time)

        for scenario in self.scenarios:
            if scenario.get("id") == scenario_id:
                if self.scenario_assets_exist(scenario):
                    return scenario

        with self.build_lock:
            self.refresh()

            for scenario in self.scenarios:
                if scenario.get("id") == scenario_id:
                    if self.scenario_assets_exist(scenario):
                        return scenario

        self.engine_cache.pop(scenario_id, None)
        raise FileNotFoundError(
            f"Historical scenario {scenario_id} is not built yet. "
            "Build it with scripts/build_road_scenario.py, then refresh scenarios."
        )

    def ensure_uniform_rain_scenario(
        self,
        past1hr_mm: float,
        past24hr_mm: float,
        past2days_mm: float,
        past3days_mm: float,
    ) -> dict:
        scenario_id = build_road_scenario.scenario_id_from_uniform_rain(
            past1hr_mm,
            past24hr_mm,
            past2days_mm,
            past3days_mm,
        )

        for scenario in self.scenarios:
            if scenario.get("id") == scenario_id and self.scenario_assets_exist(scenario):
                return scenario

        with self.build_lock:
            self.refresh()

            for scenario in self.scenarios:
                if scenario.get("id") == scenario_id and self.scenario_assets_exist(scenario):
                    return scenario

            build_road_scenario.build_uniform_rain_scenario(
                past1hr_mm=past1hr_mm,
                past24hr_mm=past24hr_mm,
                past2days_mm=past2days_mm,
                past3days_mm=past3days_mm,
            )
            self.engine_cache.pop(scenario_id, None)
            self.refresh()

        for scenario in self.scenarios:
            if scenario.get("id") == scenario_id:
                return scenario

        raise RuntimeError(f"Custom scenario build finished but {scenario_id} is not in manifest")


def write_index_html() -> None:
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    html = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Taipei Pre/Post Route Comparison</title>
  <link href="https://unpkg.com/maplibre-gl@5.0.0/dist/maplibre-gl.css" rel="stylesheet" />
  <style>
    html, body, #map {
      height: 100%;
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .panel {
      position: absolute;
      top: 12px;
      left: 12px;
      width: 370px;
      max-width: calc(100vw - 24px);
      background: rgba(255, 255, 255, 0.95);
      border: 1px solid #d7d7d7;
      border-radius: 8px;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.16);
      padding: 14px;
      z-index: 1;
    }
    h1 {
      font-size: 18px;
      margin: 0 0 8px;
    }
    .hint {
      color: #555;
      font-size: 13px;
      line-height: 1.4;
      margin-bottom: 10px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 10px;
    }
    .hidden {
      display: none !important;
    }
    .full-row {
      grid-column: 1 / -1;
    }
    label {
      color: #555;
      font-size: 12px;
      display: grid;
      gap: 3px;
    }
    input,
    select {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #cfcfcf;
      border-radius: 6px;
      padding: 7px 8px;
      font-size: 13px;
    }
    .scenario-note {
      color: #555;
      font-size: 12px;
      line-height: 1.4;
      margin: -2px 0 10px;
    }
    .buttons {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 8px;
    }
    button {
      border: 1px solid #b9c2cc;
      background: #f7f9fb;
      border-radius: 6px;
      padding: 8px 10px;
      cursor: pointer;
      font-weight: 600;
    }
    button.primary {
      background: #1463ff;
      border-color: #1463ff;
      color: white;
    }
    button.active {
      background: #fff3cd;
      border-color: #d39e00;
    }
    .summary {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 10px;
    }
    .stat {
      background: #f4f6f8;
      border: 1px solid #e2e6ea;
      border-radius: 6px;
      padding: 8px;
    }
    .stat .label {
      color: #666;
      font-size: 12px;
    }
    .stat .value {
      font-size: 18px;
      font-weight: 800;
      margin-top: 2px;
    }
    .layers-panel {
      margin-top: 10px;
      display: grid;
      gap: 8px;
      font-size: 13px;
      border-top: 1px solid #e3e6ea;
      padding-top: 10px;
    }
    .layer-group-title {
      color: #555;
      font-size: 12px;
      font-weight: 800;
      margin-top: 2px;
    }
    .layer-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px 10px;
    }
    .layer-item {
      display: flex;
      align-items: center;
      gap: 7px;
      color: #333;
      font-size: 13px;
      line-height: 1.25;
      min-width: 0;
    }
    .layer-item input {
      width: auto;
      flex: 0 0 auto;
      margin: 0;
    }
    .layer-item span:last-child {
      min-width: 0;
    }
    .swatch {
      display: inline-block;
      width: 30px;
      height: 5px;
      border-radius: 999px;
      vertical-align: middle;
      flex: 0 0 30px;
    }
    .pre { background: #2166ac; }
    .post { background: #d73027; }
    .closed { background: #111; }
    .iso-pre { background: rgba(33, 102, 172, 0.45); border: 1px solid #2166ac; }
    .iso-post { background: rgba(215, 48, 39, 0.45); border: 1px solid #d73027; }
    .shelter { background: #1a9850; height: 10px; width: 10px; border-radius: 50%; }
    .flood-grid { background: rgba(43, 140, 190, 0.55); border: 1px solid #08589e; height: 12px; }
    .landslide-grid { background: rgba(123, 50, 148, 0.55); border: 1px solid #542788; height: 12px; }
    .status {
      min-height: 18px;
      color: #555;
      font-size: 13px;
      margin-top: 7px;
    }
    .section-title {
      font-size: 14px;
      font-weight: 800;
      margin-top: 12px;
      margin-bottom: 6px;
    }
    .nearest-list {
      margin: 8px 0 0;
      padding-left: 18px;
      color: #333;
      font-size: 12px;
      line-height: 1.45;
      max-height: 116px;
      overflow: auto;
    }
    .popup {
      font: 12px/1.45 system-ui, sans-serif;
    }
    .map-legend {
      position: absolute;
      right: 14px;
      bottom: 28px;
      width: 260px;
      max-width: calc(100vw - 28px);
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d7d7d7;
      border-radius: 8px;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.16);
      padding: 12px;
      z-index: 1;
      font-size: 12px;
      color: #333;
    }
    .legend-title {
      font-weight: 800;
      margin-bottom: 7px;
    }
    .speed-bar {
      height: 10px;
      border-radius: 999px;
      background: linear-gradient(to right, #a50026, #f46d43, #fee08b, #66bd63, #1a9850);
      margin: 7px 0 4px;
    }
    .speed-labels {
      display: flex;
      justify-content: space-between;
      color: #555;
      font-size: 11px;
    }
    .legend-note {
      color: #666;
      font-size: 11px;
      line-height: 1.35;
      margin-top: 5px;
    }
    .legend-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 7px;
    }
    .legend-box {
      width: 18px;
      height: 12px;
      border-radius: 3px;
      flex: 0 0 auto;
    }
  </style>
</head>
<body>
  <div id="map"></div>

  <aside class="map-legend">
    <div class="legend-title">道路旅行速度 (km/hr)</div>
    <div class="speed-bar"></div>
    <div class="speed-labels">
      <span>0</span><span>20</span><span>40</span><span>60</span><span>80+</span>
    </div>
    <div class="legend-note">紅色較慢，綠色較快；封閉道路以黑色表示。</div>
    <div class="legend-row"><span class="legend-box" style="background: rgba(43, 140, 190, 0.55); border: 1px solid #08589e;"></span><span>淹水影響 grid</span></div>
    <div class="legend-row"><span class="legend-box" style="background: rgba(123, 50, 148, 0.55); border: 1px solid #542788;"></span><span>土石流門檻超標 grid</span></div>
  </aside>

  <aside class="panel">
    <h1>災前 / 災後最佳路徑比較</h1>
    <div class="hint">
      可直接輸入經緯度，或按「設定起點 / 終點」後在地圖上點選。
    </div>

    <div class="grid">
      <label class="full-row">情境模式
        <select id="scenarioMode">
          <option value="historical">歷史降雨事件</option>
          <option value="custom">自選均雨量</option>
        </select>
      </label>
    </div>
    <div id="historicalScenarioControls" class="grid">
      <label>雨量日期<select id="scenarioDate"></select></label>
      <label>雨量時間<select id="scenarioTime"></select></label>
    </div>
    <div id="customScenarioControls" class="grid hidden">
      <label>Past 1hr mm<input id="customPast1hr" type="number" min="0" step="0.1" value="20"></label>
      <label>Past 24hr mm<input id="customPast24hr" type="number" min="0" step="0.1" value="100"></label>
      <label>Past 2day mm<input id="customPast2days" type="number" min="0" step="0.1" value="150"></label>
      <label>Past 3day mm<input id="customPast3days" type="number" min="0" step="0.1" value="200"></label>
    </div>
    <div class="buttons">
      <button id="refreshScenarios">重新整理情境</button>
      <button id="loadScenario" class="primary">建立 / 載入情境</button>
    </div>
    <div id="scenarioNote" class="scenario-note">正在讀取可用雨量時間...</div>

    <div class="section-title">全市路網統計</div>
    <div class="summary">
      <div class="stat"><div class="label">平均旅行時間增加率</div><div id="avgIncrease" class="value">-</div></div>
      <div class="stat"><div class="label">旅行時間增加 >0%</div><div id="affectedGt0Ratio" class="value">-</div></div>
      <div class="stat"><div class="label">旅行時間增加 >5%</div><div id="affectedGt5Ratio" class="value">-</div></div>
      <div class="stat"><div class="label">旅行時間增加 >10%</div><div id="affectedGt10Ratio" class="value">-</div></div>
      <div class="stat"><div class="label">封閉道路數量</div><div id="closedCount" class="value">-</div></div>
      <div class="stat"><div class="label">受淹水影響道路比例</div><div id="floodAffectedRatio" class="value">-</div></div>
    </div>

    <div class="grid">
      <label>起點 Lon<input id="originLon" type="number" step="0.000001" value="121.574264"></label>
      <label>起點 Lat<input id="originLat" type="number" step="0.000001" value="25.166960"></label>
      <label>終點 Lon<input id="destLon" type="number" step="0.000001" value="121.565000"></label>
      <label>終點 Lat<input id="destLat" type="number" step="0.000001" value="25.120000"></label>
    </div>

    <div class="buttons">
      <button id="pickOrigin">設定起點</button>
      <button id="pickDest">設定終點</button>
      <button id="clearRoutes">清除路線</button>
      <button id="runRoute" class="primary">計算路線</button>
    </div>

    <div class="grid">
      <label>可達時間 cutoff 分鐘<input id="cutoffMin" type="number" step="1" min="1" value="5"></label>
      <label>&nbsp;<button id="runAccess" class="primary" style="height:34px;">計算可達性</button></label>
    </div>

    <div class="layers-panel">
      <div class="layer-group-title">全市路網</div>
      <div class="layer-grid">
        <label class="layer-item"><input type="checkbox" id="toggleOverallPre" checked><span class="swatch pre"></span><span>災前路網</span></label>
        <label class="layer-item"><input type="checkbox" id="toggleOverallPost" checked><span class="swatch post"></span><span>災後路網</span></label>
        <label class="layer-item"><input type="checkbox" id="toggleClosed" checked><span class="swatch closed"></span><span>封閉路段</span></label>
        <label class="layer-item"><input type="checkbox" id="toggleFloodGrid" checked><span class="swatch flood-grid"></span><span>淹水影響 grid</span></label>
        <label class="layer-item"><input type="checkbox" id="toggleLandslideGrid" checked><span class="swatch landslide-grid"></span><span>土石流影響 grid</span></label>
      </div>

      <div class="layer-group-title">最佳路徑</div>
      <div class="layer-grid">
        <label class="layer-item"><input type="checkbox" id="togglePreRoute" checked><span class="swatch pre"></span><span>災前路徑</span></label>
        <label class="layer-item"><input type="checkbox" id="togglePostRoute" checked><span class="swatch post"></span><span>災後路徑</span></label>
      </div>

      <div class="layer-group-title">可達範圍 / 避難所</div>
      <div class="layer-grid">
        <label class="layer-item"><input type="checkbox" id="togglePreIso" checked><span class="swatch iso-pre"></span><span>災前可達</span></label>
        <label class="layer-item"><input type="checkbox" id="togglePostIso" checked><span class="swatch iso-post"></span><span>災後可達</span></label>
        <label class="layer-item"><input type="checkbox" id="toggleShelters" checked><span class="swatch shelter"></span><span>避難所</span></label>
      </div>
    </div>

    <div class="summary">
      <div class="stat"><div class="label">災前總旅行時間</div><div id="preTime" class="value">-</div></div>
      <div class="stat"><div class="label">災後總旅行時間</div><div id="postTime" class="value">-</div></div>
      <div class="stat"><div class="label">增加時間</div><div id="changeTime" class="value">-</div></div>
      <div class="stat"><div class="label">增加率</div><div id="changePct" class="value">-</div></div>
      <div class="stat"><div class="label">災前路線長度</div><div id="preLen" class="value">-</div></div>
      <div class="stat"><div class="label">災後路線長度</div><div id="postLen" class="value">-</div></div>
      <div class="stat"><div class="label">路線是否改變</div><div id="changed" class="value">-</div></div>
      <div class="stat"><div class="label">災後可達</div><div id="reachable" class="value">-</div></div>
    </div>

    <div class="section-title">5 分鐘可達性 / 避難所</div>
    <div class="summary">
      <div class="stat"><div class="label">災前可達面積</div><div id="preArea" class="value">-</div></div>
      <div class="stat"><div class="label">災後可達面積</div><div id="postArea" class="value">-</div></div>
      <div class="stat"><div class="label">面積下降幅度</div><div id="areaDrop" class="value">-</div></div>
      <div class="stat"><div class="label">災後可達避難所</div><div id="postShelters" class="value">-</div></div>
    </div>
    <ol id="nearestShelters" class="nearest-list"></ol>

    <div id="status" class="status"></div>
  </aside>

  <script src="https://unpkg.com/maplibre-gl@5.0.0/dist/maplibre-gl.js"></script>
  <script src="https://unpkg.com/pmtiles@4.3.0/dist/pmtiles.js"></script>
  <script>
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);

    const map = new maplibregl.Map({
      container: "map",
      center: [121.55331424, 25.08292052],
      zoom: 12,
      maxZoom: 18,
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors"
          },
          roads: {
            type: "vector",
            url: "pmtiles://data/roads.pmtiles"
          },
          boundary: {
            type: "geojson",
            data: "data/taipei_boundary.geojson"
          },
          floodGrid: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          landslideGrid: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          preRoute: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          postRoute: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          routePoints: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          preIso: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          postIso: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          },
          shelters: {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] }
          }
        },
        layers: [
          {
            id: "osm",
            type: "raster",
            source: "osm",
            paint: { "raster-opacity": 0.68 }
          },
          {
            id: "taipei-boundary-line",
            type: "line",
            source: "boundary",
            paint: { "line-color": "#111111", "line-width": 2 }
          },
          {
            id: "flood-grid-fill",
            type: "fill",
            source: "floodGrid",
            paint: {
              "fill-color": "#2b8cbe",
              "fill-opacity": 0.24
            }
          },
          {
            id: "flood-grid-line",
            type: "line",
            source: "floodGrid",
            paint: {
              "line-color": "#08589e",
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.4, 14, 1.0, 17, 1.8],
              "line-opacity": 0.85
            }
          },
          {
            id: "landslide-grid-fill",
            type: "fill",
            source: "landslideGrid",
            paint: {
              "fill-color": "#7b3294",
              "fill-opacity": 0.26
            }
          },
          {
            id: "landslide-grid-line",
            type: "line",
            source: "landslideGrid",
            paint: {
              "line-color": "#542788",
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 14, 1.1, 17, 2.0],
              "line-opacity": 0.9
            }
          },
          {
            id: "overall-pre-roads",
            type: "line",
            source: "roads",
            "source-layer": "roads",
            paint: {
              "line-color": [
                "interpolate", ["linear"], ["coalesce", ["get", "pre_kph"], 0],
                0, "#a50026",
                20, "#f46d43",
                40, "#fee08b",
                60, "#66bd63",
                80, "#1a9850"
              ],
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.35, 14, 1.0, 17, 2.4],
              "line-opacity": 0.42
            }
          },
          {
            id: "overall-post-roads",
            type: "line",
            source: "roads",
            "source-layer": "roads",
            filter: ["==", ["get", "closed"], 0],
            paint: {
              "line-color": [
                "interpolate", ["linear"], ["coalesce", ["get", "post_kph"], 0],
                0, "#a50026",
                20, "#f46d43",
                40, "#fee08b",
                60, "#66bd63",
                80, "#1a9850"
              ],
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.45, 14, 1.2, 17, 2.8],
              "line-opacity": 0.48
            }
          },
          {
            id: "context-closed-roads",
            type: "line",
            source: "roads",
            "source-layer": "roads",
            filter: ["==", ["get", "closed"], 1],
            paint: {
              "line-color": "#111111",
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.2, 14, 2.4, 17, 4.5],
              "line-opacity": 0.9
            }
          },
          {
            id: "pre-iso-fill",
            type: "fill",
            source: "preIso",
            paint: {
              "fill-color": "#2166ac",
              "fill-opacity": 0.18
            }
          },
          {
            id: "pre-iso-line",
            type: "line",
            source: "preIso",
            paint: {
              "line-color": "#2166ac",
              "line-width": 2,
              "line-dasharray": [2, 1]
            }
          },
          {
            id: "post-iso-fill",
            type: "fill",
            source: "postIso",
            paint: {
              "fill-color": "#d73027",
              "fill-opacity": 0.18
            }
          },
          {
            id: "post-iso-line",
            type: "line",
            source: "postIso",
            paint: {
              "line-color": "#d73027",
              "line-width": 2
            }
          },
          {
            id: "shelters-circle",
            type: "circle",
            source: "shelters",
            paint: {
              "circle-radius": ["case", ["get", "post_5min"], 6, ["get", "pre_5min"], 5, 4],
              "circle-color": [
                "case",
                ["get", "post_5min"], "#1a9850",
                ["get", "pre_5min"], "#fdae61",
                "#8c8c8c"
              ],
              "circle-stroke-color": "#111111",
              "circle-stroke-width": 1,
              "circle-opacity": 0.9
            }
          },
          {
            id: "pre-route-line",
            type: "line",
            source: "preRoute",
            paint: {
              "line-color": "#2166ac",
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4, 15, 7],
              "line-opacity": 0.9,
              "line-dasharray": [1.5, 1]
            }
          },
          {
            id: "post-route-line",
            type: "line",
            source: "postRoute",
            paint: {
              "line-color": "#d73027",
              "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4, 15, 7],
              "line-opacity": 0.95
            }
          },
          {
            id: "route-points",
            type: "circle",
            source: "routePoints",
            paint: {
              "circle-radius": 7,
              "circle-color": ["case", ["==", ["get", "kind"], "origin"], "#1a9850", "#d73027"],
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 2
            }
          }
        ]
      }
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));

    let pickMode = null;
    let lastResult = null;
    let scenarioCatalog = { scenarios: [] };
    let rainTimeCatalog = { dates: [] };
    let activeScenarioId = null;

    const fields = {
      originLon: document.getElementById("originLon"),
      originLat: document.getElementById("originLat"),
      destLon: document.getElementById("destLon"),
      destLat: document.getElementById("destLat"),
      scenarioMode: document.getElementById("scenarioMode"),
      historicalScenarioControls: document.getElementById("historicalScenarioControls"),
      customScenarioControls: document.getElementById("customScenarioControls"),
      scenarioDate: document.getElementById("scenarioDate"),
      scenarioTime: document.getElementById("scenarioTime"),
      customPast1hr: document.getElementById("customPast1hr"),
      customPast24hr: document.getElementById("customPast24hr"),
      customPast2days: document.getElementById("customPast2days"),
      customPast3days: document.getElementById("customPast3days"),
      scenarioNote: document.getElementById("scenarioNote"),
      avgIncrease: document.getElementById("avgIncrease"),
      affectedGt0Ratio: document.getElementById("affectedGt0Ratio"),
      affectedGt5Ratio: document.getElementById("affectedGt5Ratio"),
      affectedGt10Ratio: document.getElementById("affectedGt10Ratio"),
      closedCount: document.getElementById("closedCount"),
      floodAffectedRatio: document.getElementById("floodAffectedRatio"),
      preTime: document.getElementById("preTime"),
      postTime: document.getElementById("postTime"),
      changeTime: document.getElementById("changeTime"),
      changePct: document.getElementById("changePct"),
      preLen: document.getElementById("preLen"),
      postLen: document.getElementById("postLen"),
      changed: document.getElementById("changed"),
      reachable: document.getElementById("reachable"),
      status: document.getElementById("status")
    };
    fields.cutoffMin = document.getElementById("cutoffMin");
    fields.preArea = document.getElementById("preArea");
    fields.postArea = document.getElementById("postArea");
    fields.areaDrop = document.getElementById("areaDrop");
    fields.postShelters = document.getElementById("postShelters");
    fields.nearestShelters = document.getElementById("nearestShelters");

    function fmtMin(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      return `${Number(value).toFixed(2)} min`;
    }

    function fmtPct(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      return `${Number(value).toFixed(1)}%`;
    }

    function fmtMeter(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      value = Number(value);
      return value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${value.toFixed(0)} m`;
    }

    function fmtArea(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
      return `${Number(value).toFixed(2)} km²`;
    }

    function fmtNumber(value, digits = 1) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
      return Number(value).toFixed(digits);
    }

    function routeFeature(geometry, scenario) {
      if (!geometry) return { type: "FeatureCollection", features: [] };
      return {
        type: "FeatureCollection",
        features: [{ type: "Feature", properties: { scenario }, geometry }]
      };
    }

    function isCustomMode() {
      return fields.scenarioMode.value === "custom";
    }

    function customRainValues() {
      const values = {
        past1hr_mm: Number(fields.customPast1hr.value),
        past24hr_mm: Number(fields.customPast24hr.value),
        past2days_mm: Number(fields.customPast2days.value),
        past3days_mm: Number(fields.customPast3days.value)
      };

      for (const [key, value] of Object.entries(values)) {
        if (!Number.isFinite(value) || value < 0) {
          throw new Error(`${key} must be a non-negative number`);
        }
      }

      return values;
    }

    function updateScenarioModeUI() {
      const custom = isCustomMode();
      fields.historicalScenarioControls.classList.toggle("hidden", custom);
      fields.customScenarioControls.classList.toggle("hidden", !custom);
      refreshScenarioNote();
    }

    function selectedTimeRecord() {
      if (isCustomMode()) return null;

      const option = fields.scenarioTime.selectedOptions[0];
      if (!option) return null;
      return {
        targetTime: option.value,
        scenarioId: option.dataset.scenarioId,
        built: option.dataset.built === "true"
      };
    }

    function findScenario(scenarioId) {
      return scenarioCatalog.scenarios.find((scenario) => scenario.id === scenarioId) || null;
    }

    function selectedScenarioRecord() {
      const selected = selectedTimeRecord();
      return selected ? findScenario(selected.scenarioId) : null;
    }

    function refreshScenarioNote() {
      if (isCustomMode()) {
        try {
          const values = customRainValues();
          fields.scenarioNote.textContent =
            `自選均雨量：Past1hr ${values.past1hr_mm} mm、` +
            `Past24hr ${values.past24hr_mm} mm、` +
            `Past2day ${values.past2days_mm} mm、` +
            `Past3day ${values.past3days_mm} mm。按「建立 / 載入情境」套用。`;
        } catch (error) {
          fields.scenarioNote.textContent = error.message;
        }
        return;
      }

      const selected = selectedTimeRecord();
      if (!selected) {
        fields.scenarioNote.textContent = "沒有可用的雨量時間。";
        return;
      }

      if (selected.scenarioId === activeScenarioId) {
        fields.scenarioNote.textContent = `目前地圖情境：${selected.targetTime}`;
      } else if (selected.built) {
        fields.scenarioNote.textContent = `此時間已建立，可直接載入：${selected.targetTime}`;
      } else {
        fields.scenarioNote.textContent =
          `此歷史時間尚未建立：${selected.targetTime}。` +
          "請先用腳本建立情境，再按「重新整理情境」。";
      }
    }

    function populateDateSelect() {
      fields.scenarioDate.innerHTML = "";
      for (const item of rainTimeCatalog.dates || []) {
        const option = document.createElement("option");
        option.value = item.date_key;
        option.textContent = item.date_label;
        fields.scenarioDate.appendChild(option);
      }
    }

    function populateTimeSelect(preferredTargetTime = null) {
      const dateItem = (rainTimeCatalog.dates || []).find(
        (item) => item.date_key === fields.scenarioDate.value
      );

      fields.scenarioTime.innerHTML = "";
      if (!dateItem) {
        refreshScenarioNote();
        return;
      }

      const preferred =
        preferredTargetTime ||
        (dateItem.times.find((item) => item.label === "09:00") || dateItem.times[0] || {}).target_time;

      for (const item of dateItem.times) {
        const option = document.createElement("option");
        option.value = item.target_time;
        option.dataset.scenarioId = item.scenario_id;
        option.dataset.built = item.built ? "true" : "false";
        option.textContent = `${item.label}${item.built ? " 已建立" : " 未建立"}`;
        if (item.target_time === preferred) option.selected = true;
        fields.scenarioTime.appendChild(option);
      }

      refreshScenarioNote();
    }

    async function refreshScenarioData(preferredTargetTime = null) {
      const [timesResponse, scenariosResponse] = await Promise.all([
        fetch("/api/rain-times"),
        fetch("/api/scenarios")
      ]);

      if (!timesResponse.ok) throw new Error(await timesResponse.text());
      if (!scenariosResponse.ok) throw new Error(await scenariosResponse.text());

      rainTimeCatalog = await timesResponse.json();
      scenarioCatalog = await scenariosResponse.json();

      const previousDate = fields.scenarioDate.value;
      populateDateSelect();

      if (previousDate && [...fields.scenarioDate.options].some((option) => option.value === previousDate)) {
        fields.scenarioDate.value = previousDate;
      }

      populateTimeSelect(preferredTargetTime);
    }

    function clearScenarioDependentLayers() {
      map.getSource("preRoute").setData({ type: "FeatureCollection", features: [] });
      map.getSource("postRoute").setData({ type: "FeatureCollection", features: [] });
      map.getSource("preIso").setData({ type: "FeatureCollection", features: [] });
      map.getSource("postIso").setData({ type: "FeatureCollection", features: [] });
      map.getSource("shelters").setData({ type: "FeatureCollection", features: [] });
    }

    function emptyFeatureCollection() {
      return { type: "FeatureCollection", features: [] };
    }

    function scenarioAssetUrl(scenario, key, fallbackFileName) {
      if (scenario[key]) return scenario[key];
      const base = scenario.pmtiles_url.replace(/roads\.pmtiles$/, "");
      return `${base}${fallbackFileName}`;
    }

    async function loadGeoJsonSource(sourceId, url) {
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const data = await response.json();
        map.getSource(sourceId).setData(data);
      } catch (error) {
        console.warn(`Failed to load ${sourceId}`, error);
        map.getSource(sourceId).setData(emptyFeatureCollection());
      }
    }

    async function loadHazardGridLayers(scenario) {
      await Promise.all([
        loadGeoJsonSource(
          "floodGrid",
          scenarioAssetUrl(scenario, "flood_grid_url", "flood_affected_grids.geojson")
        ),
        loadGeoJsonSource(
          "landslideGrid",
          scenarioAssetUrl(scenario, "landslide_grid_url", "landslide_affected_grids.geojson")
        )
      ]);
    }

    function switchRoadSource(pmtilesUrl) {
      const url = `pmtiles://${pmtilesUrl}`;
      const source = map.getSource("roads");

      if (source && typeof source.setUrl === "function") {
        source.setUrl(url);
        return;
      }

      const roadLayers = map.getStyle().layers.filter((layer) => layer.source === "roads");
      for (const layer of roadLayers.slice().reverse()) {
        if (map.getLayer(layer.id)) map.removeLayer(layer.id);
      }
      if (map.getSource("roads")) map.removeSource("roads");

      map.addSource("roads", {
        type: "vector",
        url
      });

      for (const layer of roadLayers) {
        map.addLayer(layer, "pre-iso-fill");
      }

      setLayerVisible("overall-pre-roads", document.getElementById("toggleOverallPre").checked);
      setLayerVisible("overall-post-roads", document.getElementById("toggleOverallPost").checked);
      setLayerVisible("context-closed-roads", document.getElementById("toggleClosed").checked);
    }

    async function switchScenario(scenario) {
      if (!scenario) return false;

      activeScenarioId = scenario.id;
      switchRoadSource(scenario.pmtiles_url);
      clearScenarioDependentLayers();
      await loadHazardGridLayers(scenario);
      await loadOverallStats(scenario.stats_url);
      refreshScenarioNote();
      fields.status.textContent = `已載入情境：${scenario.label || scenario.id}`;
      return true;
    }

    async function ensureSelectedScenario() {
      if (isCustomMode()) {
        const values = customRainValues();
        fields.status.textContent = "正在建立 / 載入自選均雨量情境...";
        fields.scenarioNote.textContent =
          `正在套用自選均雨量：Past1hr ${values.past1hr_mm} mm、` +
          `Past24hr ${values.past24hr_mm} mm、` +
          `Past2day ${values.past2days_mm} mm、` +
          `Past3day ${values.past3days_mm} mm`;

        const params = new URLSearchParams(values);
        const response = await fetch(`/api/ensure-custom-scenario?${params.toString()}`);
        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || "Custom scenario build failed");
        }

        const result = await response.json();
        scenarioCatalog = result.scenarios;
        rainTimeCatalog = result.rain_times;
        await switchScenario(result.scenario);
        return result.scenario;
      }

      const selected = selectedTimeRecord();
      if (!selected) throw new Error("沒有選取雨量時間");

      const existing = findScenario(selected.scenarioId);
      if (existing) {
        await switchScenario(existing);
        return existing;
      }

      fields.status.textContent =
        "此歷史情境尚未建立。請先用腳本建立，完成後按「重新整理情境」。";
      fields.scenarioNote.textContent =
        `歷史情境尚未建立：${selected.targetTime}`;
      throw new Error("Historical scenario is not built yet.");
    }

    function updatePointSource() {
      const origin = [Number(fields.originLon.value), Number(fields.originLat.value)];
      const dest = [Number(fields.destLon.value), Number(fields.destLat.value)];
      map.getSource("routePoints").setData({
        type: "FeatureCollection",
        features: [
          { type: "Feature", properties: { kind: "origin" }, geometry: { type: "Point", coordinates: origin } },
          { type: "Feature", properties: { kind: "destination" }, geometry: { type: "Point", coordinates: dest } }
        ]
      });
    }

    function setPickMode(mode) {
      pickMode = mode;
      document.getElementById("pickOrigin").classList.toggle("active", mode === "origin");
      document.getElementById("pickDest").classList.toggle("active", mode === "dest");
      fields.status.textContent = mode ? `請在地圖上點選${mode === "origin" ? "起點" : "終點"}` : "";
    }

    document.getElementById("pickOrigin").addEventListener("click", () => setPickMode("origin"));
    document.getElementById("pickDest").addEventListener("click", () => setPickMode("dest"));

    map.on("click", (event) => {
      if (!pickMode) return;
      const lon = event.lngLat.lng.toFixed(6);
      const lat = event.lngLat.lat.toFixed(6);
      if (pickMode === "origin") {
        fields.originLon.value = lon;
        fields.originLat.value = lat;
      } else {
        fields.destLon.value = lon;
        fields.destLat.value = lat;
      }
      updatePointSource();
      setPickMode(null);
    });

    document.getElementById("clearRoutes").addEventListener("click", () => {
      map.getSource("preRoute").setData({ type: "FeatureCollection", features: [] });
      map.getSource("postRoute").setData({ type: "FeatureCollection", features: [] });
      map.getSource("preIso").setData({ type: "FeatureCollection", features: [] });
      map.getSource("postIso").setData({ type: "FeatureCollection", features: [] });
      map.getSource("shelters").setData({ type: "FeatureCollection", features: [] });
      fields.status.textContent = "已清除路線";
    });

    function setLayerVisible(layerId, visible) {
      map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
    }

    function setLayersVisible(layerIds, visible) {
      for (const layerId of layerIds) {
        setLayerVisible(layerId, visible);
      }
    }

    document.getElementById("toggleOverallPre").addEventListener("change", (event) => {
      setLayerVisible("overall-pre-roads", event.target.checked);
    });

    document.getElementById("toggleOverallPost").addEventListener("change", (event) => {
      setLayerVisible("overall-post-roads", event.target.checked);
    });

    document.getElementById("toggleClosed").addEventListener("change", (event) => {
      setLayerVisible("context-closed-roads", event.target.checked);
    });

    document.getElementById("toggleFloodGrid").addEventListener("change", (event) => {
      setLayersVisible(["flood-grid-fill", "flood-grid-line"], event.target.checked);
    });

    document.getElementById("toggleLandslideGrid").addEventListener("change", (event) => {
      setLayersVisible(["landslide-grid-fill", "landslide-grid-line"], event.target.checked);
    });

    document.getElementById("togglePreRoute").addEventListener("change", (event) => {
      setLayerVisible("pre-route-line", event.target.checked);
    });

    document.getElementById("togglePostRoute").addEventListener("change", (event) => {
      setLayerVisible("post-route-line", event.target.checked);
    });

    document.getElementById("togglePreIso").addEventListener("change", (event) => {
      setLayersVisible(["pre-iso-fill", "pre-iso-line"], event.target.checked);
    });

    document.getElementById("togglePostIso").addEventListener("change", (event) => {
      setLayersVisible(["post-iso-fill", "post-iso-line"], event.target.checked);
    });

    document.getElementById("toggleShelters").addEventListener("change", (event) => {
      setLayerVisible("shelters-circle", event.target.checked);
    });

    fields.scenarioMode.addEventListener("change", () => {
      updateScenarioModeUI();
      if (!isCustomMode()) {
        const scenario = selectedScenarioRecord();
        if (scenario) {
          switchScenario(scenario).catch((error) => {
            console.error(error);
            fields.status.textContent = `錯誤：${error.message}`;
          });
        }
      }
    });

    for (const input of [
      fields.customPast1hr,
      fields.customPast24hr,
      fields.customPast2days,
      fields.customPast3days
    ]) {
      input.addEventListener("input", refreshScenarioNote);
    }

    fields.scenarioDate.addEventListener("change", () => {
      populateTimeSelect();
      const scenario = selectedScenarioRecord();
      if (scenario) {
        switchScenario(scenario).catch((error) => {
          console.error(error);
          fields.status.textContent = `錯誤：${error.message}`;
        });
      }
    });

    fields.scenarioTime.addEventListener("change", () => {
      const scenario = selectedScenarioRecord();
      if (scenario) {
        switchScenario(scenario).catch((error) => {
          console.error(error);
          fields.status.textContent = `錯誤：${error.message}`;
        });
      } else {
        refreshScenarioNote();
      }
    });

    document.getElementById("refreshScenarios").addEventListener("click", () => {
      const selected = selectedTimeRecord();
      refreshScenarioData(selected ? selected.targetTime : null)
        .then(() => {
          fields.status.textContent = "情境清單已更新";
          const scenario = selectedScenarioRecord();
          if (scenario) return switchScenario(scenario);
          refreshScenarioNote();
        })
        .catch((error) => {
          console.error(error);
          fields.status.textContent = `錯誤：${error.message}`;
        });
    });

    document.getElementById("loadScenario").addEventListener("click", () => {
      ensureSelectedScenario().catch((error) => {
        console.error(error);
        fields.status.textContent = `錯誤：${error.message}`;
      });
    });

    async function loadOverallStats(statsUrl = "data/stats.json") {
      try {
        const response = await fetch(statsUrl);
        const stats = await response.json();
        fields.avgIncrease.textContent = fmtPct(stats.average_travel_time_increase_rate_pct);
        fields.affectedGt0Ratio.textContent = fmtPct(
          stats.affected_gt0_road_ratio_pct_by_length ?? stats.affected_road_ratio_pct_by_length
        );
        fields.affectedGt5Ratio.textContent = fmtPct(stats.affected_gt5_road_ratio_pct_by_length);
        fields.affectedGt10Ratio.textContent = fmtPct(stats.affected_gt10_road_ratio_pct_by_length);
        fields.closedCount.textContent = (
          stats.closed_segments === null || stats.closed_segments === undefined
        ) ? "N/A" : Number(stats.closed_segments).toLocaleString();
        fields.floodAffectedRatio.textContent = fmtPct(stats.flood_affected_road_ratio_pct_by_length);
      } catch (error) {
        console.warn("Failed to load overall stats", error);
        fields.avgIncrease.textContent = "N/A";
        fields.affectedGt0Ratio.textContent = "N/A";
        fields.affectedGt5Ratio.textContent = "N/A";
        fields.affectedGt10Ratio.textContent = "N/A";
        fields.closedCount.textContent = "N/A";
        fields.floodAffectedRatio.textContent = "N/A";
      }
    }

    async function calculateRoute() {
      await ensureSelectedScenario();
      updatePointSource();
      fields.status.textContent = "計算中...";

      const params = new URLSearchParams({
        origin_lon: fields.originLon.value,
        origin_lat: fields.originLat.value,
        dest_lon: fields.destLon.value,
        dest_lat: fields.destLat.value,
        scenario_id: activeScenarioId
      });

      const response = await fetch(`/api/route?${params.toString()}`);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || "Route API failed");
      }

      const result = await response.json();
      lastResult = result;

      map.getSource("preRoute").setData(routeFeature(result.routes.pre, "pre"));
      map.getSource("postRoute").setData(routeFeature(result.routes.post, "post"));

      const s = result.summary;
      fields.preTime.textContent = fmtMin(s.pre_travel_time_min);
      fields.postTime.textContent = fmtMin(s.post_travel_time_min);
      fields.changeTime.textContent = fmtMin(s.travel_time_change_min);
      fields.changePct.textContent = fmtPct(s.travel_time_change_pct);
      fields.preLen.textContent = fmtMeter(s.pre_route_length_m);
      fields.postLen.textContent = fmtMeter(s.post_route_length_m);
      fields.changed.textContent = s.route_changed === null ? "N/A" : (s.route_changed ? "Yes" : "No");
      fields.reachable.textContent = s.post_reachable ? "Yes" : "No";

      const geoms = [result.routes.pre, result.routes.post].filter(Boolean);
      if (geoms.length) {
        const coords = [];
        for (const geom of geoms) {
          const lines = geom.type === "MultiLineString" ? geom.coordinates : [geom.coordinates];
          for (const line of lines) {
            for (const coord of line) coords.push(coord);
          }
        }
        const xs = coords.map(c => c[0]);
        const ys = coords.map(c => c[1]);
        map.fitBounds(
          [[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]],
          { padding: 80, maxZoom: 15 }
        );
      }

      fields.status.textContent = "完成";
    }

    const roadHoverPopup = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false
    });
    let roadHoverInstalled = false;

    function roadPopupHtml(properties) {
      const isTunnel = Number(properties.tunnel ?? 0) === 1;
      const isTunnelClosed = Number(properties.tunnel_closed ?? 0) === 1;
      const postText = Number(properties.closed ?? 0) === 1
        ? "封閉"
        : `${Number(properties.post_m ?? 0).toFixed(3)} 分鐘`;

      return `
        <div class="popup">
          <strong>${properties.id}</strong><br>
          道路型別：${properties.rt || "N/A"}${isTunnel ? "（tunnel）" : ""}<br>
          災前旅行時間：${Number(properties.pre_m ?? 0).toFixed(3)} 分鐘<br>
          災後旅行時間：${postText}<br>
          增加率：${properties.inc_pct == null ? "N/A" : Number(properties.inc_pct).toFixed(1) + "%"}<br>
          災前速度：${Number(properties.pre_kph ?? 0).toFixed(1)} km/hr<br>
          災後速度：${Number(properties.post_kph ?? 0).toFixed(1)} km/hr<br>
          淹水深度：${Number(properties.flood_mm ?? 0).toFixed(0)} mm<br>
          Tunnel 降雨封閉：${isTunnelClosed ? "Yes" : "No"}
        </div>
      `;
    }

    function installRoadHoverPopup() {
      if (roadHoverInstalled) return;
      roadHoverInstalled = true;

      for (const layerId of ["context-closed-roads", "overall-post-roads", "overall-pre-roads"]) {
        map.on("mousemove", layerId, (event) => {
          if (!event.features.length) return;
          map.getCanvas().style.cursor = "pointer";
          roadHoverPopup
            .setLngLat(event.lngLat)
            .setHTML(roadPopupHtml(event.features[0].properties))
            .addTo(map);
        });

        map.on("mouseleave", layerId, () => {
          map.getCanvas().style.cursor = "";
          roadHoverPopup.remove();
        });
      }
    }

    document.getElementById("runRoute").addEventListener("click", () => {
      calculateRoute().catch((error) => {
        console.error(error);
        fields.status.textContent = `錯誤：${error.message}`;
      });
    });

    function featureCollectionFromGeometry(geometry) {
      if (!geometry) return { type: "FeatureCollection", features: [] };
      return {
        type: "FeatureCollection",
        features: [{ type: "Feature", properties: {}, geometry }]
      };
    }

    async function calculateAccessibility() {
      await ensureSelectedScenario();
      updatePointSource();
      fields.status.textContent = "計算可達性中...";

      const params = new URLSearchParams({
        origin_lon: fields.originLon.value,
        origin_lat: fields.originLat.value,
        cutoff_min: fields.cutoffMin.value,
        scenario_id: activeScenarioId
      });

      const response = await fetch(`/api/accessibility?${params.toString()}`);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || "Accessibility API failed");
      }

      const result = await response.json();
      map.getSource("preIso").setData(featureCollectionFromGeometry(result.isochrones.pre));
      map.getSource("postIso").setData(featureCollectionFromGeometry(result.isochrones.post));
      map.getSource("shelters").setData(result.shelters);

      const s = result.summary;
      fields.preArea.textContent = fmtArea(s.pre_area_km2);
      fields.postArea.textContent = fmtArea(s.post_area_km2);
      fields.areaDrop.textContent = fmtPct(s.area_decline_pct);
      fields.postShelters.textContent = String(s.post_shelter_count);

      fields.nearestShelters.innerHTML = "";
      for (const shelter of s.nearest_5_post_shelters) {
        const li = document.createElement("li");
        li.textContent = `${shelter.name}：${fmtMin(shelter.post_time_min)}`;
        fields.nearestShelters.appendChild(li);
      }

      fields.status.textContent = "可達性完成";
    }

    document.getElementById("runAccess").addEventListener("click", () => {
      calculateAccessibility().catch((error) => {
        console.error(error);
        fields.status.textContent = `錯誤：${error.message}`;
      });
    });

    map.on("click", "shelters-circle", (event) => {
      const p = event.features[0].properties;
      new maplibregl.Popup()
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="popup">
            <strong>${p.name}</strong><br>
            行政區：${p.town || "N/A"}<br>
            災前時間：${fmtMin(p.pre_time_min)}<br>
            災後時間：${fmtMin(p.post_time_min)}<br>
            災後 5 分鐘可達：${p.post_5min ? "Yes" : "No"}
          </div>
        `)
        .addTo(map);
    });

    map.on("click", "flood-grid-fill", (event) => {
      if (pickMode) return;
      const p = event.features[0].properties;
      new maplibregl.Popup()
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="popup">
            <strong>淹水影響 grid ${p.grid_id ?? ""}</strong><br>
            Past1hr: ${fmtNumber(p.rain_past1hr_mm)} mm/hr<br>
            Flood scenario: ${fmtNumber(p.flood_scenario_mmhr)} mm/hr<br>
            Standing water depth: ${fmtNumber(p.standing_water_depth_mm)} mm
          </div>
        `)
        .addTo(map);
    });

    map.on("click", "landslide-grid-fill", (event) => {
      if (pickMode) return;
      const p = event.features[0].properties;
      new maplibregl.Popup()
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="popup">
            <strong>土石流影響 grid ${p.grid_id ?? ""}</strong><br>
            Effective rain: ${fmtNumber(p.landslide_effective_rain_mm)} mm<br>
            Past24hr: ${fmtNumber(p.rain_past24hr_mm)} mm<br>
            Past2days: ${fmtNumber(p.rain_past2days_mm)} mm<br>
            Past3days: ${fmtNumber(p.rain_past3days_mm)} mm
          </div>
        `)
        .addTo(map);
    });

    map.on("load", () => {
      installRoadHoverPopup();
      updatePointSource();
      updateScenarioModeUI();
      refreshScenarioData()
        .then(() => {
          const scenario = selectedScenarioRecord();
          if (scenario) {
            return switchScenario(scenario);
          }
          refreshScenarioNote();
          return loadOverallStats();
        })
        .catch((error) => {
          console.error(error);
          fields.status.textContent = `錯誤：${error.message}`;
          loadOverallStats();
        });
    });
  </script>
</body>
</html>
"""

    (WEB_DIR / "index.html").write_text(html, encoding="utf-8")


class RouteRequestHandler(SimpleHTTPRequestHandler):
    scenarios: ScenarioManager | None = None

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def guess_type(self, path: str) -> str:
        if path.endswith(".pmtiles"):
            return "application/octet-stream"
        return super().guess_type(path)

    def translate_path(self, path: str) -> str:
        parsed_path = urlparse(path).path
        if parsed_path.startswith("/data/"):
            rel = parsed_path.removeprefix("/data/")
            return str((DATA_DIR / rel).resolve())
        return super().translate_path(path)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/scenarios":
            self.handle_scenarios_api()
            return
        if parsed.path == "/api/rain-times":
            self.handle_rain_times_api()
            return
        if parsed.path == "/api/ensure-scenario":
            self.handle_ensure_scenario_api(parsed)
            return
        if parsed.path == "/api/ensure-custom-scenario":
            self.handle_ensure_custom_scenario_api(parsed)
            return
        if parsed.path == "/api/route":
            self.handle_route_api(parsed)
            return
        if parsed.path == "/api/accessibility":
            self.handle_accessibility_api(parsed)
            return

        super().do_GET()

    def handle_scenarios_api(self) -> None:
        try:
            body = json.dumps(
                self.scenarios.list_for_api(),
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def handle_rain_times_api(self) -> None:
        try:
            body = json.dumps(
                self.scenarios.rain_times_for_api(),
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def handle_ensure_scenario_api(self, parsed) -> None:
        try:
            params = parse_qs(parsed.query)
            target_time = params["target_time"][0]
            scenario = self.scenarios.ensure_scenario(target_time)

            body = json.dumps(
                {
                    "scenario": scenario,
                    "scenarios": self.scenarios.list_for_api(),
                    "rain_times": self.scenarios.rain_times_for_api(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def handle_ensure_custom_scenario_api(self, parsed) -> None:
        try:
            params = parse_qs(parsed.query)
            scenario = self.scenarios.ensure_uniform_rain_scenario(
                past1hr_mm=float(params["past1hr_mm"][0]),
                past24hr_mm=float(params["past24hr_mm"][0]),
                past2days_mm=float(params["past2days_mm"][0]),
                past3days_mm=float(params["past3days_mm"][0]),
            )

            body = json.dumps(
                {
                    "scenario": scenario,
                    "scenarios": self.scenarios.list_for_api(),
                    "rain_times": self.scenarios.rain_times_for_api(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def handle_route_api(self, parsed) -> None:
        try:
            params = parse_qs(parsed.query)
            origin_lon = float(params["origin_lon"][0])
            origin_lat = float(params["origin_lat"][0])
            dest_lon = float(params["dest_lon"][0])
            dest_lat = float(params["dest_lat"][0])
            scenario_id = params.get("scenario_id", [None])[0]

            engine = self.scenarios.engine_for(scenario_id)
            result = engine.calculate(
                origin_lon,
                origin_lat,
                dest_lon,
                dest_lat,
            )

            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def handle_accessibility_api(self, parsed) -> None:
        try:
            params = parse_qs(parsed.query)
            origin_lon = float(params["origin_lon"][0])
            origin_lat = float(params["origin_lat"][0])
            cutoff_min = float(params.get("cutoff_min", ["5"])[0])
            scenario_id = params.get("scenario_id", [None])[0]

            engine = self.scenarios.engine_for(scenario_id)
            result = engine.calculate_accessibility(
                origin_lon,
                origin_lat,
                cutoff_min=cutoff_min,
            )

            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def send_head(self):
        path = self.translate_path(self.path)
        file_path = Path(path)

        if file_path.is_dir():
            return super().send_head()

        if not file_path.exists():
            self.send_error(404, "File not found")
            return None

        range_header = self.headers.get("Range")
        if not range_header:
            return super().send_head()

        try:
            units, range_spec = range_header.split("=", 1)
            if units.strip() != "bytes":
                raise ValueError("Only bytes range is supported")

            start_text, end_text = range_spec.split("-", 1)
            file_size = file_path.stat().st_size
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else file_size - 1
            end = min(end, file_size - 1)

            if start < 0 or end < start or start >= file_size:
                self.send_error(416, "Requested Range Not Satisfiable")
                return None
        except Exception:
            self.send_error(400, "Bad Range header")
            return None

        f = file_path.open("rb")
        f.seek(start)
        self.range = (start, end)

        self.send_response(206)
        self.send_header("Content-type", self.guess_type(str(file_path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(file_path.stat().st_mtime))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile) -> None:
        if hasattr(self, "range"):
            start, end = self.range
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
            del self.range
            return

        super().copyfile(source, outputfile)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--graph",
        type=Path,
        default=OUTPUT_DIR / "taipei_drive.graphml",
    )
    parser.add_argument(
        "--road-travel",
        type=Path,
        default=OUTPUT_DIR / "road_grid_travel_time_pre_post.csv",
    )
    parser.add_argument(
        "--shelters",
        type=Path,
        default=OUTPUT_DIR / "taipei_shelters_join_500m.geojson",
    )
    args = parser.parse_args()

    WEB_DIR.mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("application/octet-stream", ".pmtiles")
    write_index_html()

    RouteRequestHandler.scenarios = ScenarioManager(
        args.graph,
        args.road_travel,
        args.shelters,
    )

    server = ThreadingHTTPServer(("127.0.0.1", args.port), RouteRequestHandler)
    print(f"Serving route comparison at http://127.0.0.1:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
