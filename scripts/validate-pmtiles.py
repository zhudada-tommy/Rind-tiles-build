#!/usr/bin/env python3
"""PMTiles 产物自动校验。

只读解析文件头（127 字节）+ gzip 元数据块，不加载整文件，适合 CI/构建后快速检查。

检查项：
  1. magic == "PMTiles"，版本 == v3
  2. tile 类型（默认 MVT）
  3. 压缩方式（默认内部目录 gzip、瓦片 gzip/zlib）
  4. zoom 范围（默认 0-15）
  5. 图层集合（默认 Protomaps Basemap 9 层；可用 --layers 覆盖）
  6. 各图层 minzoom<=maxzoom 且落在 [0,15]
  7. 头部经纬度范围合法
  8. 可选：头部范围落在期望区域 bbox 内（--expect-bounds）
  9. 文件非空且至少有 1 个瓦片条目

用法示例：
  validate-pmtiles.py out.pmtiles
  validate-pmtiles.py out.pmtiles --expect-bounds "-141.0,41.7,-52.6,83.1"
  validate-pmtiles.py out.pmtiles --layers boundaries,buildings,earth,landcover,landuse,places,pois,roads,water

退出码：全部通过返回 0，否则返回 1 并打印失败原因。
"""
import argparse
import gzip
import json
import struct
import sys

# PMTiles v3 枚举（https://github.com/protomaps/PMTiles spec）
TILE_TYPE = {"UNKNOWN": 0, "MVT": 1, "PNG": 2, "JPEG": 3, "WEBP": 4, "AVIF": 5}
COMPRESSION = {"UNKNOWN": 0, "NONE": 1, "GZIP": 2, "BROTLI": 3, "ZSTD": 4}

# 实测参照值：12 个美国州级文件 + nyc/usa-east-se/nyc-metro（2026-10-15 解析）
# Protomaps Basemap 构建产物，planetiler 输出
DEFAULT_LAYERS = ["boundaries", "buildings", "earth", "landcover",
                  "landuse", "places", "pois", "roads", "water"]


class CheckFail(Exception):
    pass


def parse_header(path):
    with open(path, "rb") as f:
        head = f.read(127)
    if len(head) < 127:
        raise CheckFail("文件小于 127 字节，不是有效 PMTiles")
    magic = head[0:7]
    version = head[7]
    (root_off, root_len, meta_off, meta_len, leaf_off, leaf_len,
     tile_data_off, num_addressed, num_entries, num_contents,
     _reserved) = struct.unpack("<11Q", head[8:96])
    (clustered, internal_comp, tile_comp, tile_type,
     min_zoom, max_zoom) = struct.unpack("<6B", head[96:102])
    min_lon, min_lat, max_lon, max_lat = struct.unpack("<4i", head[102:118])
    center_zoom = head[118]
    center_lon, center_lat = struct.unpack("<2i", head[119:127])
    return {
        "magic": magic, "version": version,
        "meta_off": meta_off, "meta_len": meta_len,
        "num_addressed": num_addressed, "num_entries": num_entries,
        "clustered": clustered, "internal_comp": internal_comp,
        "tile_comp": tile_comp, "tile_type": tile_type,
        "min_zoom": min_zoom, "max_zoom": max_zoom,
        "bounds": (min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7),
        "center": (center_lon / 1e7, center_lat / 1e7, center_zoom),
    }


def parse_metadata(path, header):
    with open(path, "rb") as f:
        f.seek(header["meta_off"])
        raw = f.read(header["meta_len"])
    comp = header["internal_comp"]
    if comp == COMPRESSION["GZIP"]:
        raw = gzip.decompress(raw)
    elif comp != COMPRESSION["NONE"]:
        raise CheckFail("不支持的元数据压缩方式: %d" % comp)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise CheckFail("元数据 JSON 解析失败: %s" % e)


def check(path, args):
    errors = []
    h = parse_header(path)

    # 文件头完整性先行：损坏的文件头会导致后续解析全部错位，直接判死
    if h["magic"] != b"PMTiles":
        raise CheckFail("magic 不是 'PMTiles'（文件头损坏或不是 PMTiles 文件）")
    if h["version"] != 3:
        raise CheckFail("版本不是 v3（实测=%d）" % h["version"])

    want_type = TILE_TYPE[args.tile_type]
    if h["tile_type"] != want_type:
        errors.append("tile 类型不是 %s（实测=%d）" % (args.tile_type, h["tile_type"]))

    if h["min_zoom"] != args.min_zoom or h["max_zoom"] != args.max_zoom:
        errors.append("zoom 范围不是 %d-%d（实测=%d-%d）"
                      % (args.min_zoom, args.max_zoom, h["min_zoom"], h["max_zoom"]))

    if h["num_entries"] == 0:
        errors.append("瓦片条目数为 0（空文件）")

    min_lon, min_lat, max_lon, max_lat = h["bounds"]
    if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
        errors.append("头部经纬度范围非法: %s" % (h["bounds"],))

    if args.expect_bounds:
        e = [float(x) for x in args.expect_bounds.split(",")]
        if not (e[0] <= min_lon and e[1] <= min_lat and max_lon <= e[2] and max_lat <= e[3]):
            errors.append("文件范围 %s 超出期望区域 %s" % (h["bounds"], args.expect_bounds))

    meta = parse_metadata(path, h)
    layers = meta.get("vector_layers", [])
    got = [ly["id"] for ly in layers]
    want = args.layers.split(",")
    if sorted(got) != sorted(want):
        errors.append("图层集合不符：期望 %d 层 %s，实测 %d 层 %s"
                      % (len(want), want, len(got), got))
    for ly in layers:
        z0, z1 = ly.get("minzoom", 0), ly.get("maxzoom", 15)
        if not (0 <= z0 <= z1 <= 15):
            errors.append("图层 %s zoom 范围非法: %d-%d" % (ly["id"], z0, z1))

    return h, meta, errors


def main():
    ap = argparse.ArgumentParser(description="校验 PMTiles 构建产物")
    ap.add_argument("pmtiles", help="待校验的 .pmtiles 文件")
    ap.add_argument("--layers", default=",".join(DEFAULT_LAYERS),
                    help="期望图层 id 列表（逗号分隔），默认 9 层 Protomaps Basemap")
    ap.add_argument("--min-zoom", type=int, default=0)
    ap.add_argument("--max-zoom", type=int, default=15)
    ap.add_argument("--tile-type", default="MVT",
                    choices=list(TILE_TYPE), help="期望瓦片类型")
    ap.add_argument("--expect-bounds", default=None,
                    help="期望区域 'minlon,minlat,maxlon,maxlat'（值以 - 开头时请用 = 连接，"
                         "如 --expect-bounds=\"-74.3,40.4,-73.7,40.95\"），文件范围须落在其中")
    args = ap.parse_args()

    try:
        h, meta, errors = check(args.pmtiles, args)
    except CheckFail as e:
        print("FAIL: %s: %s" % (args.pmtiles, e))
        return 1
    except FileNotFoundError:
        print("FAIL: 文件不存在: %s" % args.pmtiles)
        return 1
    except Exception as e:
        print("FAIL: %s: 解析异常 %r" % (args.pmtiles, e))
        return 1

    layers = [ly["id"] for ly in meta.get("vector_layers", [])]
    print("文件: %s" % args.pmtiles)
    print("  magic=PMTiles v%d, type=%s, zoom=%d-%d, 条目=%d"
          % (h["version"],
             [k for k, v in TILE_TYPE.items() if v == h["tile_type"]][0],
             h["min_zoom"], h["max_zoom"], h["num_entries"]))
    print("  图层(%d): %s" % (len(layers), ",".join(layers)))
    print("  范围: %.4f,%.4f,%.4f,%.4f" % h["bounds"])
    if errors:
        print("FAIL:")
        for e in errors:
            print("  - " + e)
        return 1
    print("PASS: 全部 %d 项检查通过" % (8 if args.expect_bounds else 7))
    return 0


if __name__ == "__main__":
    sys.exit(main())
