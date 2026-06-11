"""
GeoJSON → TopoJSON 最適化スクリプト
1. 座標の桁数を6桁に丸める
2. 不要プロパティ (id) を削除
3. TopoJSON に変換
4. gzip 圧縮版も生成してサイズ比較
"""

import json
import gzip
import os
import topojson as tp
import shapely.geometry

DATA_DIR = os.path.join(os.path.dirname(__file__), "web", "data")

# 実際にHTMLから参照されているファイルのみ処理
FILES = [
    "circle30_triangle_buildings.geojson",       # tokyo.html
    "osaka15_triangle_buildings.geojson",         # osaka.html
    "circle30_nyc_triangle_buildings.geojson",    # nyc.html
]


def round_coords(coords, precision=6):
    """座標を再帰的に丸める"""
    if isinstance(coords[0], (list, tuple)):
        return [round_coords(c, precision) for c in coords]
    return [round(x, precision) for x in coords]


def optimize_geojson(geojson):
    """座標丸め + id プロパティ削除"""
    for feature in geojson["features"]:
        # 座標を6桁に丸める
        geom = feature["geometry"]
        geom["coordinates"] = round_coords(geom["coordinates"])
        # id プロパティを削除 (area_m2 は NYC で使用するので残す)
        feature["properties"].pop("id", None)
    return geojson


def convert_to_topojson(geojson):
    """GeoJSON → TopoJSON 変換 (prequantize で更に圧縮)"""
    topology = tp.Topology(geojson, toposimplify=False, prequantize=1e6)
    return topology.to_dict()


def main():
    print(f"{'File':<50} {'Original':>10} {'Optimized':>10} {'TopoJSON':>10} {'gzip':>10} {'Ratio':>8}")
    print("-" * 100)

    for fname in FILES:
        src = os.path.join(DATA_DIR, fname)
        if not os.path.exists(src):
            print(f"  SKIP: {fname} not found")
            continue

        original_size = os.path.getsize(src)

        # 読み込み
        with open(src) as f:
            geojson = json.load(f)

        n_features = len(geojson["features"])

        # 最適化
        optimized = optimize_geojson(geojson)

        # 最適化済み GeoJSON サイズ (参考用)
        opt_json = json.dumps(optimized, ensure_ascii=False, separators=(",", ":"))
        opt_size = len(opt_json.encode("utf-8"))

        # TopoJSON 変換
        topo = convert_to_topojson(optimized)
        topo_json = json.dumps(topo, ensure_ascii=False, separators=(",", ":"))
        topo_size = len(topo_json.encode("utf-8"))

        # TopoJSON を保存
        out_name = fname.replace(".geojson", ".topojson")
        out_path = os.path.join(DATA_DIR, out_name)
        with open(out_path, "w") as f:
            f.write(topo_json)

        # gzip サイズ (転送時の実際のサイズ目安)
        gz_data = gzip.compress(topo_json.encode("utf-8"), compresslevel=9)
        gz_size = len(gz_data)

        ratio = gz_size / original_size * 100

        print(
            f"  {fname:<48} {original_size:>8,} B {opt_size:>8,} B {topo_size:>8,} B {gz_size:>8,} B {ratio:>6.1f}%"
        )
        print(f"    → {out_name} ({n_features} features)")

    # 未使用ファイルの確認
    unused = "circle15_osaka_triangle_buildings.geojson"
    unused_path = os.path.join(DATA_DIR, unused)
    if os.path.exists(unused_path):
        size = os.path.getsize(unused_path)
        print(f"\n  NOTE: {unused} ({size:,} B) はどのHTMLからも参照されていません。削除可能です。")


if __name__ == "__main__":
    main()
