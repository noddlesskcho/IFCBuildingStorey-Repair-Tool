"""Plan footprints and block boundaries from real element geometry.

Every element held (directly or through decomposition) by a building is
tessellated in world coordinates. Each element is reduced to the convex hull of
its plan (XY) vertices. A building's *boundary* is the union of its element
hulls rasterised on a plan grid, closed (small gaps bridged) and hole-filled,
then grown by a margin. That gives the real outline of a block of any size or
shape - L, cross, twin tower, rotated - not a bounding box.

Placement points are deliberately not used: Revit writes many element
placements at the link origin, far away from the element itself.
"""

from __future__ import annotations

import multiprocessing
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ---------------------------------------------------------------- convex hull


def convex_hull(points: np.ndarray) -> np.ndarray:
    """Monotone chain convex hull, counter-clockwise, of (n, 2) points."""
    pts = np.unique(np.round(points, 6), axis=0)
    if len(pts) <= 2:
        return pts
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(tuple(p))
    for p in pts[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(tuple(p))
    return np.array(lower[:-1] + upper[:-1], dtype=float)


def _inside_convex(hull: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Vectorised point-in-convex-polygon (CCW hull, boundary counts as inside)."""
    if len(hull) < 3:
        return np.zeros(x.shape, dtype=bool)
    inside = np.ones(x.shape, dtype=bool)
    for i in range(len(hull)):
        (x0, y0), (x1, y1) = hull[i], hull[(i + 1) % len(hull)]
        inside &= (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0) >= -1e-9
    return inside


def points_in_polygon(points: np.ndarray, polygon) -> np.ndarray:
    """Even-odd rule point-in-polygon for any simple polygon (concave allowed)."""
    poly = np.asarray(polygon, dtype=float)
    x, y = points[:, 0], points[:, 1]
    inside = np.zeros(len(points), dtype=bool)
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i], poly[i - 1]
        crosses = (y0 > y) != (y1 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
        inside ^= crosses & (x < x_cross)
    return inside


# ---------------------------------------------------------------- per-element plan geometry


@dataclass
class PlanData:
    """Plan hulls of tessellated products (product step id -> CCW hull, project units)."""

    element_hulls: dict[int, np.ndarray] = field(default_factory=dict)
    failed: int = 0
    engine_error: str = ""


_CACHE: dict[tuple, PlanData] = {}


def _cache_key(path: str):
    try:
        st = os.stat(path)
        return (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def cached_plan_data(path: str) -> Optional[PlanData]:
    key = _cache_key(path) if path else None
    return _CACHE.get(key) if key is not None else None


def collect_plan_data(model, product_ids, unit_scale: float, path: str = "",
                      known: Optional[PlanData] = None) -> PlanData:
    """Tessellate products in world coordinates and keep each one's plan hull.

    Results are cached per file (path + modification time), so analysing and then
    exporting the same file tessellates it once. ``known`` supplies hulls already
    computed for the same step ids (e.g. the original file when checking a repair).
    """
    key = _cache_key(path) if path else None
    if key is not None and key in _CACHE:
        return _CACHE[key]

    data = PlanData()
    wanted = set(product_ids)
    if known is not None:
        data.element_hulls.update({i: h for i, h in known.element_hulls.items() if i in wanted})
        wanted -= set(data.element_hulls)
    if wanted:
        _tessellate(model, wanted, unit_scale, data)
    if key is not None and not data.engine_error:
        _CACHE.clear()  # keep one file in memory
        _CACHE[key] = data
    return data


def _tessellate(model, ids: set, unit_scale: float, data: PlanData) -> None:
    try:
        import ifcopenshell.geom

        settings = ifcopenshell.geom.settings()
        settings.set("use-world-coords", True)
        settings.set("disable-opening-subtractions", True)  # openings do not change the plan outline
        threads = max(1, multiprocessing.cpu_count())
        iterator = ifcopenshell.geom.iterator(settings, model, threads, include=[model.by_id(i) for i in ids])
        ok = iterator.initialize()
    except Exception as exc:  # geometry engine unavailable or nothing to process
        data.engine_error = str(exc)
        return
    while ok:
        shape = iterator.get()
        try:
            verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
            if len(verts):
                data.element_hulls[shape.id] = convex_hull(verts[:, :2] / unit_scale)  # engine works in metres
        except Exception:
            data.failed += 1
        ok = iterator.next()


# ---------------------------------------------------------------- boundary raster


class PlanGrid:
    def __init__(self, extent: tuple[float, float, float, float], cell: float):
        self.cell = cell
        self.x0, self.y0 = extent[0] - 4 * cell, extent[1] - 4 * cell
        self.nx = int(np.ceil((extent[2] - self.x0) / cell)) + 5
        self.ny = int(np.ceil((extent[3] - self.y0) / cell)) + 5

    def index(self, pts: np.ndarray):
        ix = np.floor((pts[:, 0] - self.x0) / self.cell).astype(int)
        iy = np.floor((pts[:, 1] - self.y0) / self.cell).astype(int)
        valid = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
        return ix, iy, valid

    def rasterise(self, hulls: list[np.ndarray]) -> np.ndarray:
        mask = np.zeros((self.nx, self.ny), dtype=bool)
        for hull in hulls:
            lo = np.floor((hull.min(axis=0) - (self.x0, self.y0)) / self.cell).astype(int)
            hi = np.floor((hull.max(axis=0) - (self.x0, self.y0)) / self.cell).astype(int)
            lo = np.clip(lo, 0, (self.nx - 1, self.ny - 1))
            hi = np.clip(hi, 0, (self.nx - 1, self.ny - 1))
            gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + 1), np.arange(lo[1], hi[1] + 1), indexing="ij")
            cx = self.x0 + (gx + 0.5) * self.cell
            cy = self.y0 + (gy + 0.5) * self.cell
            cover = _inside_convex(hull, cx, cy)
            # thin or tiny elements (beams, walls seen edge-on) may miss every cell centre
            mask[gx[cover], gy[cover]] = True
            centre = hull.mean(axis=0)
            ix, iy, valid = self.index(np.vstack([hull, centre[None, :]]))
            mask[ix[valid], iy[valid]] = True
        return mask


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask
    out = mask.copy()
    # separable square dilation
    for axis in (0, 1):
        src = out.copy()
        for s in range(1, r + 1):
            out |= np.roll(src, s, axis=axis)
            out |= np.roll(src, -s, axis=axis)
    return out


def _erode(mask: np.ndarray, r: int) -> np.ndarray:
    return ~_dilate(~mask, r)


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Cells not reachable from the grid border through empty cells are holes."""
    outside = np.zeros_like(mask)
    outside[0, :] = ~mask[0, :]
    outside[-1, :] = ~mask[-1, :]
    outside[:, 0] |= ~mask[:, 0]
    outside[:, -1] |= ~mask[:, -1]
    free = ~mask
    while True:
        grown = outside.copy()
        grown[1:, :] |= outside[:-1, :]
        grown[:-1, :] |= outside[1:, :]
        grown[:, 1:] |= outside[:, :-1]
        grown[:, :-1] |= outside[:, 1:]
        grown &= free
        if np.array_equal(grown, outside):
            return mask | ~outside
        outside = grown


@dataclass
class Boundary:
    grid: PlanGrid
    mask: np.ndarray

    @property
    def area(self) -> float:
        return float(self.mask.sum()) * self.grid.cell ** 2

    def contains(self, centres: np.ndarray) -> float:
        """Fraction of the given plan points inside the boundary."""
        if not len(centres):
            return 0.0
        ix, iy, valid = self.grid.index(centres)
        inside = np.zeros(len(centres), dtype=bool)
        inside[valid] = self.mask[ix[valid], iy[valid]]
        return float(inside.mean())


def build_boundary(hulls: list[np.ndarray], cell: float, close: float, margin: float) -> Optional[Boundary]:
    """Filled plan outline of a set of element hulls (all lengths in project units)."""
    if not hulls:
        return None
    pts = np.vstack(hulls)
    pad = close + margin
    extent = (pts[:, 0].min() - pad, pts[:, 1].min() - pad, pts[:, 0].max() + pad, pts[:, 1].max() + pad)
    # keep the raster small for very large sites
    cell = max(cell, (extent[2] - extent[0]) / 1500.0, (extent[3] - extent[1]) / 1500.0)
    grid = PlanGrid(extent, cell)
    mask = grid.rasterise(hulls)
    close_cells = int(round(close / cell))
    mask = _erode(_dilate(mask, close_cells), close_cells)  # bridge small gaps between elements
    mask = _fill_holes(mask)
    mask = _dilate(mask, int(round(margin / cell)))
    return Boundary(grid, mask)
