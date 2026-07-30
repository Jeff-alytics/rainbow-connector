"""Render a static JPEG of every live Rainbow Connector review-camera location."""

import json
import math
import sys
from pathlib import Path

import geopandas as gpd
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 2400, 1500
MARGIN = (120, 205, 120, 190)
COLORS = {
    "FAA WeatherCam": "#f97316",
    "Maryland CHART": "#e11d48",
    "DelDOT": "#a855f7",
    "Caltrans": "#06b6d4",
    "Iowa DOT": "#22c55e",
    "Ohio OHGO": "#eab308",
    "WSDOT": "#3b82f6",
}


def font(size, bold=False):
    names = ["C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def albers(lon, lat):
    lat1, lat2, lat0, lon0 = map(math.radians, (29.5, 45.5, 23.0, -96.0))
    phi, lam = math.radians(lat), math.radians(lon)
    n = 0.5 * (math.sin(lat1) + math.sin(lat2))
    c = math.cos(lat1) ** 2 + 2 * n * math.sin(lat1)
    rho = math.sqrt(c - 2 * n * math.sin(phi)) / n
    rho0 = math.sqrt(c - 2 * n * math.sin(lat0)) / n
    theta = n * (lam - lon0)
    return rho * math.sin(theta), rho0 - rho * math.cos(theta)


def rings(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry.exterior.coords
    elif geometry.geom_type == "MultiPolygon":
        for polygon in geometry.geoms:
            yield polygon.exterior.coords


def main(data_path, boundary_path, output_path):
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    states = gpd.read_file(boundary_path)
    states = states[~states["STUSPS"].isin(["AK", "HI", "PR", "AS", "GU", "MP", "VI"])]

    projected_rings = []
    for geometry in states.geometry:
        for ring in rings(geometry):
            projected_rings.append([albers(lon, lat) for lon, lat in ring])
    all_xy = [point for ring in projected_rings for point in ring]
    min_x, max_x = min(x for x, _ in all_xy), max(x for x, _ in all_xy)
    min_y, max_y = min(y for _, y in all_xy), max(y for _, y in all_xy)
    left, top, right, bottom = MARGIN
    map_w, map_h = WIDTH - left - right, HEIGHT - top - bottom
    scale = min(map_w / (max_x - min_x), map_h / (max_y - min_y))
    offset_x = left + (map_w - (max_x - min_x) * scale) / 2
    offset_y = top + (map_h - (max_y - min_y) * scale) / 2

    def screen(x, y):
        return (offset_x + (x - min_x) * scale, top + map_h - (offset_y - top) - (y - min_y) * scale)

    image = Image.new("RGB", (WIDTH, HEIGHT), "#08111f")
    draw = ImageDraw.Draw(image, "RGBA")
    for ring in projected_rings:
        points = [screen(x, y) for x, y in ring]
        draw.polygon(points, fill="#16263a", outline="#60758d", width=2)

    # Large catalogs go first so small national/regional sources stay visible.
    for source in sorted(data["sources"], key=lambda item: len(item["locations"]), reverse=True):
        color = COLORS[source["source"]]
        for camera in source["locations"]:
            x, y = screen(*albers(camera["lon"], camera["lat"]))
            radius = 4 if len(source["locations"]) < 600 else 3
            draw.ellipse((x-radius-1, y-radius-1, x+radius+1, y+radius+1), fill="#06101dcc")
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color + "dc")

    draw.text((120, 55), "Rainbow Connector review-camera coverage", font=font(52, True), fill="#f8fafc")
    draw.text((120, 122), f'{data["total"]:,} distinct camera locations in the contiguous U.S. · generated {data["generatedAt"][:10]}',
              font=font(27), fill="#b8c7d9")

    legend_y = HEIGHT - 118
    x = 120
    for source in data["sources"]:
        label = f'{source["source"]}  {len(source["locations"]):,}'
        draw.ellipse((x, legend_y+7, x+20, legend_y+27), fill=COLORS[source["source"]])
        draw.text((x+31, legend_y), label, font=font(23, True), fill="#e8eef6")
        x += 42 + int(draw.textlength(label, font=font(23, True)))
    draw.text((120, HEIGHT-60), "A marker indicates an available review location, not a currently active rainbow candidate.",
              font=font(22), fill="#91a4b9")

    image.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    print(json.dumps({"output": str(Path(output_path).resolve()), "size": image.size, "total": data["total"]}))


if __name__ == "__main__":
    main(*sys.argv[1:4])
