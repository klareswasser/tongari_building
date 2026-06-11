"""
NYC City Hall 中心から半径30km圏内の三角形建物を検出する。

南北ストリップに分割してバッチ処理&インクリメンタル保存。
途中で中断しても再開可能（完了ストリップはスキップ）。
最後に30km圏外の建物をフィルタ除外する。
"""

import gc
import json
import math
import time
from pathlib import Path

import numpy as np
from overturemaps.core import geodataframe as om_geodataframe
from shapely.geometry import mapping, Point

# ── 設定 ──────────────────────────────────────────
# NYC City Hall 座標
CENTER_LAT = 40.7128
CENTER_LNG = -74.0060
RADIUS_KM  = 30

# バウンディングボックス (30km 円の外接矩形 + 少し余裕)
_dlat = RADIUS_KM / 111.32
_dlng = RADIUS_KM / (111.32 * math.cos(math.radians(CENTER_LAT)))
AREA_SOUTH = round(CENTER_LAT - _dlat, 3)
AREA_NORTH = round(CENTER_LAT + _dlat, 3)
AREA_WEST  = round(CENTER_LNG - _dlng, 3)
AREA_EAST  = round(CENTER_LNG + _dlng, 3)

N_STRIPS = 30   # 南北分割数 (各≈2km)

# UTM zone 18N (ニューヨーク周辺)
TARGET_CRS = "EPSG:32618"

OUT_DIR = Path(__file__).parent / "output"
OUT_DIR.mkdir(exist_ok=True)
DB_FILE       = OUT_DIR / "circle30_nyc_triangle_buildings.geojson"
HTML_FILE     = OUT_DIR / "circle30_nyc_triangles_map.html"
PROGRESS_FILE = OUT_DIR / "circle30_nyc_progress.json"

# Web ディレクトリ (Firebase 用)
WEB_DIR  = Path(__file__).parent / "web"
WEB_DATA = WEB_DIR / "data"


# ── 距離計算 ──────────────────────────────────────
def haversine_km(lat1, lng1, lat2, lng2):
    """2 点間の距離 (km)"""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── 三角形判定 ────────────────────────────────────
def _mbr_aspect_ratio(geom):
    mbr = geom.minimum_rotated_rectangle
    coords = list(mbr.exterior.coords)
    d1 = np.hypot(coords[1][0] - coords[0][0], coords[1][1] - coords[0][1])
    d2 = np.hypot(coords[2][0] - coords[1][0], coords[2][1] - coords[1][1])
    long_side, short_side = max(d1, d2), min(d1, d2)
    return long_side / short_side if short_side > 0 else 999


def is_roughly_triangular(geom):
    """ざっくり三角形: 頂点 ≤4, 凸性 >0.92, アスペクト <3, 矩形度 <0.70"""
    if geom is None or geom.is_empty:
        return False
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    if geom.area < 1e-6:
        return False
    n_verts = len(geom.exterior.coords) - 1
    if n_verts > 4:
        return False
    convexity = geom.area / geom.convex_hull.area
    if convexity <= 0.92:
        return False
    rectangularity = geom.area / geom.minimum_rotated_rectangle.area
    if rectangularity >= 0.70:
        return False
    if _mbr_aspect_ratio(geom) >= 3.0:
        return False
    return True


# ── 進捗管理 ──────────────────────────────────────
def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {"completed_strips": [], "total_buildings": 0, "features": []}


def save_progress(progress):
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
    tmp.rename(PROGRESS_FILE)


# ── ストリップ処理 ────────────────────────────────
def process_strips():
    strip_height = (AREA_NORTH - AREA_SOUTH) / N_STRIPS
    print(f"NYC City Hall 中心 半径{RADIUS_KM}km圏を南北 {N_STRIPS} ストリップに分割 "
          f"(各 ≈{strip_height*111_000:.0f}m)")
    print(f"対象範囲: lng [{AREA_WEST:.3f}, {AREA_EAST:.3f}], "
          f"lat [{AREA_SOUTH:.3f}, {AREA_NORTH:.3f}]")

    progress = load_progress()
    done = set(progress["completed_strips"])
    all_triangles = progress["features"]
    total_buildings = progress["total_buildings"]

    if done:
        print(f"前回の続き: {len(done)} ストリップ完了済み, "
              f"三角 {len(all_triangles)} 棟 / {total_buildings:,} 棟")

    t0 = time.time()

    for i in range(N_STRIPS):
        if i in done:
            continue

        s_lat = AREA_SOUTH + strip_height * i
        n_lat = AREA_SOUTH + strip_height * (i + 1)
        bbox = (AREA_WEST, s_lat, AREA_EAST, n_lat)
        tag = f"[{i+1:2d}/{N_STRIPS}]"

        print(f"{tag} ダウンロード中 lat [{s_lat:.4f}, {n_lat:.4f}] ...")
        try:
            gdf = om_geodataframe("building", bbox=bbox,
                                  connect_timeout=60, request_timeout=900)
        except Exception as e:
            print(f"{tag} エラー: {e}")
            continue

        if gdf is None or len(gdf) == 0:
            print(f"{tag} 建物 0 棟 → スキップ")
            progress["completed_strips"].append(i)
            save_progress(progress)
            continue

        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")

        n_bldg = len(gdf)
        print(f"{tag} {n_bldg:,} 棟取得 → 三角形判定中...")

        gdf_proj = gdf.to_crs(TARGET_CRS)
        gdf_proj["area_m2"] = gdf_proj.geometry.area
        mask = gdf_proj.geometry.apply(is_roughly_triangular)
        n_tri = mask.sum()
        total_buildings += n_bldg

        if n_tri > 0:
            tri_proj = gdf_proj[mask].copy()
            tri_wgs = tri_proj.to_crs("EPSG:4326")
            for _, row in tri_wgs.iterrows():
                feat = {
                    "type": "Feature",
                    "geometry": mapping(row.geometry),
                    "properties": {"area_m2": round(row["area_m2"], 1)},
                }
                if "id" in row.index and row["id"] is not None:
                    feat["properties"]["id"] = str(row["id"])
                all_triangles.append(feat)
            del tri_proj, tri_wgs

        elapsed = time.time() - t0
        print(f"{tag} 三角 {n_tri} 棟 | "
              f"累計 {len(all_triangles)} / {total_buildings:,} 棟 ({elapsed:.0f}s)")

        progress["completed_strips"].append(i)
        progress["total_buildings"] = total_buildings
        progress["features"] = all_triangles
        save_progress(progress)

        del gdf, gdf_proj
        gc.collect()

    return all_triangles, total_buildings


# ── 30km 圏内フィルタ ─────────────────────────────
def filter_within_circle(features):
    """各建物の重心が City Hall 中心から 30km 以内のものだけ残す"""
    kept = []
    for f in features:
        geom = f["geometry"]
        if geom["type"] == "Polygon":
            coords = geom["coordinates"][0]
        elif geom["type"] == "MultiPolygon":
            coords = geom["coordinates"][0][0]
        else:
            continue
        clng = sum(c[0] for c in coords) / len(coords)
        clat = sum(c[1] for c in coords) / len(coords)
        if haversine_km(CENTER_LAT, CENTER_LNG, clat, clng) <= RADIUS_KM:
            kept.append(f)
    print(f"30km 圏内フィルタ: {len(features)} → {len(kept)} 棟")
    return kept


# ── GeoJSON 保存 ───────────────────────────────────
def save_geojson(features, path):
    geojson = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    size_kb = path.stat().st_size / 1024
    print(f"GeoJSON 保存: {path}  ({size_kb:.1f} KB, {len(features)} 棟)")


# ── Web ディレクトリに配置 ────────────────────────
def deploy_to_web(features, total_buildings):
    """web/data にデータをコピーし、nyc.html を生成"""
    import shutil
    WEB_DATA.mkdir(parents=True, exist_ok=True)

    # GeoJSON をコピー
    shutil.copy2(DB_FILE, WEB_DATA / "circle30_nyc_triangle_buildings.geojson")

    n_tri = len(features)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NYC City Hall - Triangular Buildings within 30km</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ margin: 0; padding: 0; }}
  #map {{ width: 100vw; height: 100vh; }}
  .info-box {{
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: rgba(255,255,255,0.94); padding: 14px 18px;
    border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.18);
    font-family: -apple-system, "Helvetica Neue", sans-serif; font-size: 14px;
    max-width: 280px;
  }}
  .info-box h3 {{ margin: 0 0 8px; font-size: 16px; }}
  .legend-row {{ display: flex; align-items: center; gap: 6px; margin: 4px 0; }}
  .legend-swatch {{ width: 16px; height: 16px; border: 1px solid #333; border-radius: 2px; }}
  #loading {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
    z-index: 2000; background: rgba(255,255,255,0.95); padding: 20px 30px;
    border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.2);
    font-family: -apple-system, sans-serif; font-size: 16px; }}
  .scale-circle-label {{
    background: none !important; border: none !important; box-shadow: none !important;
    color: #888; font-size: 11px; font-family: -apple-system, "Helvetica Neue", sans-serif;
  }}
</style>
</head>
<body>
<div id="map"></div>
<div id="loading">Loading...</div>
<div class="info-box">
  <h3>NYC City Hall — 30km Triangular Buildings</h3>
  <div class="legend-row">
    <div class="legend-swatch" style="background:#E53935;"></div>
    <span>Triangular buildings: <b>{n_tri:,}</b></span>
  </div>
  <p style="margin:8px 0 0;font-size:12px;color:#666;">
    Detected from ~{total_buildings:,} buildings<br>
    Overture Maps 2026-03-18.0
  </p>
</div>
<script>
const map = L.map('map').setView([{CENTER_LAT}, {CENTER_LNG}], 11);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}@2x.png', {{
  attribution: '&copy; <a href="https://carto.com/">CARTO</a> '
             + '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
  maxZoom: 19,
  subdomains: 'abcd',
}}).addTo(map);

// City Hall から半径30kmの円
const cityHall = [{CENTER_LAT}, {CENTER_LNG}];
L.circle(cityHall, {{
  radius: 30000,
  color: '#999', weight: 1.2, dashArray: '5 4',
  fillColor: '#999', fillOpacity: 0.03, interactive: false,
}}).addTo(map);
L.marker(cityHall, {{
  icon: L.divIcon({{ className: 'scale-circle-label',
    html: 'City Hall — 30 km radius', iconSize: [140, 16], iconAnchor: [70, 8] }}),
  interactive: false,
}}).addTo(map);

fetch('data/circle30_nyc_triangle_buildings.geojson')
  .then(r => r.json())
  .then(data => {{
    L.geoJSON(data, {{
      style: {{ color: '#B71C1C', weight: 2, fillColor: '#E53935', fillOpacity: 0.55 }},
      onEachFeature: function(f, layer) {{
        const p = f.properties;
        let h = '<b>Triangular Building</b><br>Area: ' + p.area_m2 + ' m²';
        if (p.id) h += '<br><small>ID: ' + p.id + '</small>';
        layer.bindPopup(h);
      }},
    }}).addTo(map);
    document.getElementById('loading').style.display = 'none';
  }})
  .catch(e => {{
    document.getElementById('loading').textContent = 'GeoJSON load error: ' + e;
  }});
</script>
</body>
</html>"""

    (WEB_DIR / "nyc.html").write_text(html, encoding="utf-8")
    print(f"Web 更新: {WEB_DIR / 'nyc.html'}")


# ── メイン ─────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print(f"NYC City Hall 中心 半径{RADIUS_KM}km 三角形建物 検出")
    print("=" * 60)

    features, total = process_strips()

    # 30km 圏内フィルタ
    features = filter_within_circle(features)

    print(f"\n{'=' * 60}")
    print(f"結果: 三角形建物 {len(features)} 棟 / 全 {total:,} 棟")
    print(f"{'=' * 60}")

    save_geojson(features, DB_FILE)
    deploy_to_web(features, total)

    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
