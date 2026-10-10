"""Street-level flood forecast for one map tile.

The model, in four steps:
  1. Terrain. Priority-flood the elevation grid. This fills every depression (the
     places water ponds: underpasses, low colonies, old lake beds) and gives every
     cell a downhill receiver (D8 flow direction).
  2. Runoff. Each 10-minute step, rain above the local storm-drain capacity becomes
     runoff (urban runoff coefficient, raised when the ground is already soaked).
  3. Routing. Runoff is passed downhill cell by cell. Water entering a depression is
     stored there until it fills to its spill level; then it overflows downstream.
  4. Depth. Ponded depth comes from each depression's stored volume and its shape.
     Moving-water depth on slopes comes from discharge via Manning's equation.

Street depth = the deepest point along each street segment, per 10-minute frame.

Performance. The grid covers the tile plus a halo (so water flowing in from
neighbouring tiles is counted) and is ~300 x 300 cells. Flow accumulation is
vectorised by topological level: every cell at level k has all its donors at levels
< k, so one level is one numpy scatter-add. Ponding is a vectorised bisection over
all depressions at once. A 6-hour forecast for a tile runs in well under a second.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import gaussian_filter, label

RUNOFF_COEFF = 0.85
MANNING_N = 0.035
CHANNEL_W = 25.0      # effective flow width on a street, metres
MIN_SLOPE = 0.0015
BASE_DRAIN_MMPH = 18.0
DT_MIN = 10
PUMP_M3S = 0.6        # assumed pumping per subway (a few motor pumps), m3/s
T_START, T_END = -30, 360


def raster_polys(bbox, rows, cols, polys):
    """Rasterise lat/lon polygons onto the grid (True inside). Uses PIL; fast for big lakes."""
    from PIL import Image, ImageDraw
    if not polys:
        return np.zeros((rows, cols), dtype=bool)
    s, w, n, e = bbox
    img = Image.new("1", (cols, rows), 0)
    d = ImageDraw.Draw(img)
    for poly in polys:
        if len(poly) < 3:
            continue
        pts = [((lo - w) / (e - w) * cols, (n - la) / (n - s) * rows) for la, lo in poly]
        d.polygon(pts, fill=1)
    return np.array(img, dtype=bool)


def _point_in_polys(lat, lon, polys):  # kept for scripts that pass arbitrary coordinate grids
    inside = np.zeros(lat.shape, dtype=bool)
    for poly in polys:
        p = np.asarray(poly, dtype=float)
        if len(p) < 3:
            continue
        y, x = p[:, 0], p[:, 1]
        y2, x2 = np.roll(y, -1), np.roll(x, -1)
        cur = np.zeros(lat.shape, dtype=bool)
        for i in range(len(p)):
            cond = (y[i] > lat) != (y2[i] > lat)
            with np.errstate(divide="ignore", invalid="ignore"):
                xint = (x2[i] - x[i]) * (lat - y[i]) / (y2[i] - y[i] + 1e-12) + x[i]
            cur ^= cond & (lon < xint)
        inside |= cur
    return inside


@dataclass
class SimResult:
    run_id: str
    version: int
    label: str
    times: list            # minutes after now, one per frame
    seg_depth: np.ndarray  # (frames, segments) uint8, centimetres
    raster: np.ndarray     # (frames, r, c) uint8 centimetres, half resolution, tile window only
    rain_mean: list        # tile-mean intensity per frame, mm/h
    rain_coarse: np.ndarray  # (frames, r, c) uint8 mm/h, 1/8 resolution, tile window only
    total_mm: float
    raster_bbox: list = field(default_factory=list)  # [s, w, n, e] of the raster window
    observations: int = 0

    def frame_at(self, t_min: float) -> int:
        return int(min(max(round(t_min / DT_MIN), 0), len(self.times) - 1))


class FloodModel:
    def __init__(self, area):
        self.area = area
        dem = area.dem.astype(np.float64).copy()
        self.rows, self.cols = dem.shape
        cols0 = self.cols
        # Subways sit several metres below the road around them. Cut them into the terrain
        # so they become the depressions they are (pumps are part of their drain capacity).
        sub_cells = []
        for sg in area.segments:
            if getattr(sg, "underpass", False) and sg.cells is not None and len(sg.cells):
                r, c = np.divmod(sg.cells, cols0)
                dem[r, c] -= 1.8
                sub_cells.append(sg.cells)
        self.underpass_cells = int(sum(len(c) for c in sub_cells))
        self._sub_cells = np.concatenate(sub_cells) if sub_cells else np.zeros(0, dtype=np.int64)
        self.cell = area.cell_m
        n = self.rows * self.cols

        # lakes, tanks and marsh act as outlets and are never 'flooding'
        self.water = raster_polys(area.bbox, self.rows, self.cols, area.meta.get("water") or [])

        # the tile's own window inside the padded grid (the halo is only for hydrology)
        self.inner = self._inner_window()

        self.loc_grid = area.locality_grid()
        self.base_drain = BASE_DRAIN_MMPH
        self.runoff = RUNOFF_COEFF
        self.drain_eff = 1.0
        self.tide = None
        lo = np.percentile(dem, 20)
        self.lowlying = ((dem <= max(lo, 4.0)) & (dem < 7.0)).ravel()
        self.loc_names = [L["name"] for L in (area.meta.get("localities") or [{"name": "Area"}])] or ["Area"]
        self.drain_by_loc = np.full(len(self.loc_names), BASE_DRAIN_MMPH)

        self._prepare_terrain(dem, n)
        segs = area.segments
        self.seg_cells = np.concatenate([sg.cells for sg in segs]) if segs else np.zeros(0, dtype=np.int64)
        self.seg_starts = np.cumsum([0] + [len(sg.cells) for sg in segs[:-1]]).astype(np.int64) if segs else np.zeros(0, dtype=np.int64)
        self.bridges = np.array([sg.bridge for sg in segs], dtype=bool) if segs else np.zeros(0, dtype=bool)
        # coarse grids: spread moving water over the whole cell, not a 25 m street
        self.channel_w = max(CHANNEL_W, self.cell * 0.8)

    def _inner_window(self):
        tb = self.area.meta.get("tile_bbox")
        if not tb:
            return (0, self.rows, 0, self.cols)
        s, w, n, e = self.area.bbox
        ts, tw, tn, te = tb
        r0 = int(math.floor((n - tn) / (n - s) * self.rows))
        r1 = int(math.ceil((n - ts) / (n - s) * self.rows))
        c0 = int(math.floor((tw - w) / (e - w) * self.cols))
        c1 = int(math.ceil((te - w) / (e - w) * self.cols))
        r0, c0 = max(0, r0), max(0, c0)
        r1, c1 = min(self.rows, max(r1, r0 + 8)), min(self.cols, max(c1, c0 + 8))
        # even sizes so the 2x2 pooled raster lines up: grow (never shrink) so tiles overlap, not gap
        if (r1 - r0) % 2:
            r1 = r1 + 1 if r1 < self.rows else r1 - 1
        if (c1 - c0) % 2:
            c1 = c1 + 1 if c1 < self.cols else c1 - 1
        return (r0, r1, c0, c1)

    def window_bbox(self, r0, r1, c0, c1):
        s, w, n, e = self.area.bbox
        return [n - r1 / self.rows * (n - s), w + c0 / self.cols * (e - w),
                n - r0 / self.rows * (n - s), w + c1 / self.cols * (e - w)]

    # ------------------------------------------------------------------ terrain
    def _prepare_terrain(self, dem, n):
        rows, cols = self.rows, self.cols
        flat = dem.ravel()
        filled = flat.copy()
        recv = np.full(n, -1, dtype=np.int64)
        closed = np.zeros(n, dtype=bool)
        water = self.water.ravel()
        edge = np.zeros((rows, cols), dtype=bool)
        edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
        seeds = np.nonzero(edge.ravel() | water)[0]
        closed[seeds] = True
        heap = list(zip(flat[seeds].tolist(), seeds.tolist()))
        heapq.heapify(heap)
        order = []
        push, pop = heapq.heappush, heapq.heappop
        fl, fi, cl, rv = flat.tolist(), filled.tolist(), closed.tolist(), recv.tolist()
        offs = (-cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1)
        dcs = (-1, 0, 1, -1, 1, -1, 0, 1)
        eps = 1e-5
        while heap:
            z, i = pop(heap)
            order.append(i)
            c = i % cols
            for o, dc in zip(offs, dcs):
                j = i + o
                if j < 0 or j >= n:
                    continue
                cc = c + dc
                if cc < 0 or cc >= cols or cl[j]:
                    continue
                cl[j] = True
                rv[j] = i
                v = fl[j]
                nz = z + eps
                if v < nz:
                    v = nz
                fi[j] = v
                push(heap, (v, j))
        filled = np.array(fi)
        recv = np.array(rv, dtype=np.int64)
        self.dem_flat = flat
        self.filled = filled
        self.recv = recv
        up = np.array(order[::-1], dtype=np.int64)  # upstream cells first

        # slope towards receiver
        slope = np.full(n, MIN_SLOPE)
        idx = np.nonzero(recv >= 0)[0]
        r1, c1 = np.divmod(idx, cols)
        r2, c2 = np.divmod(recv[idx], cols)
        dist = np.where((r1 != r2) & (c1 != c2), self.cell * math.sqrt(2), self.cell)
        slope[idx] = np.maximum((filled[idx] - filled[recv[idx]]) / dist, MIN_SLOPE)
        self.sqrt_s = np.sqrt(slope)

        # topological levels: a cell's level is above all of its donors'
        lvl = [0] * n
        rl = recv.tolist()
        for i in up.tolist():
            r = rl[i]
            if r >= 0 and lvl[r] <= lvl[i]:
                lvl[r] = lvl[i] + 1
        lvl = np.array(lvl, dtype=np.int64)
        srt = np.argsort(lvl, kind="stable")
        bounds = np.searchsorted(lvl[srt], np.arange(lvl.max() + 2))
        self.n_levels = int(lvl.max() + 1)

        # depressions
        cap = (filled - flat).reshape(rows, cols)
        lab, ncomp = label(cap > 0.03, structure=np.ones((3, 3)))
        comp = lab.ravel().astype(np.int64) - 1  # -1 = not in a depression
        self.comp = comp
        self.ncomp = ncomp
        A = self.cell ** 2
        dep = np.nonzero(comp >= 0)[0]
        self.dep_cells = dep
        self.dep_comp = comp[dep]
        if ncomp:
            o = np.lexsort((flat[dep], comp[dep]))
            zs = flat[dep][o]
            cz = comp[dep][o]
            counts = np.bincount(cz, minlength=ncomp)
            offs_ = np.concatenate([[0], np.cumsum(counts)[:-1]])
            cum = np.cumsum(zs)
            base = np.concatenate([[0.0], cum])[offs_]
            self.comp_z, self.comp_cumz, self.comp_off, self.comp_cnt = zs, cum - np.repeat(base, counts), offs_, counts
            spill = np.zeros(ncomp)
            np.maximum.at(spill, comp[dep], filled[dep])
            self.comp_spill = spill
            depth_at_spill = np.maximum(spill[cz] - zs, 0)
            self.comp_cap = A * np.bincount(cz, weights=depth_at_spill, minlength=ncomp)
            self.comp_area = A * counts.astype(float)
            # most common locality in each depression
            lg = self.loc_grid.ravel()[dep]
            nl = int(lg.max()) + 1 if len(lg) else 1
            tab = np.zeros((ncomp, nl), dtype=np.int64)
            np.add.at(tab, (comp[dep], lg), 1)
            self.comp_loc = tab.argmax(axis=1)
        else:
            self.comp_cap = self.comp_area = self.comp_spill = np.zeros(0)
            self.comp_loc = np.zeros(0, dtype=np.int64)
        # every Chennai subway has motor pumps: depressions holding a subway get pump capacity
        self.comp_pump = np.zeros(ncomp)
        sub = self._sub_cells
        if ncomp and len(sub):
            ks = np.unique(comp[sub])
            self.comp_pump[ks[ks >= 0]] = PUMP_M3S

        # per-level scatter targets. Index n is the outlet sink; n+1+k is depression k.
        sink = n
        tgt = np.where(recv >= 0, recv, sink)
        self.levels = []
        for k in range(self.n_levels):
            ids = srt[bounds[k]:bounds[k + 1]]
            if not len(ids):
                continue
            cmp = comp[ids]
            has = cmp >= 0
            self.levels.append((ids, tgt[ids], (cmp, has, sink + 1 + np.where(has, cmp, 0)) if has.any() else None))

    # ------------------------------------------------------------- calibration
    def calibrate(self, reports):
        """More past flood reports in a locality -> weaker effective drainage there."""
        counts = np.zeros(len(self.loc_names))
        for r in reports:
            if r.get("locality") in self.loc_names:
                counts[self.loc_names.index(r["locality"])] += 1
        norm = counts / counts.max() if counts.max() > 0 else counts
        self.drain_by_loc = self.base_drain * (1 - 0.35 * norm)
        return dict(zip(self.loc_names, np.round(self.drain_by_loc, 1).tolist()))

    def set_context(self, base_drain=None, runoff=None, drain_eff=None, tide="keep"):
        if base_drain:
            ratio = base_drain / self.base_drain
            self.base_drain = base_drain
            self.drain_by_loc = self.drain_by_loc * ratio
        if runoff:
            self.runoff = runoff
        if drain_eff:
            self.drain_eff = drain_eff
        if tide != "keep":
            self.tide = tide

    # ---------------------------------------------------------------- ponding
    def _pond_levels(self, vol, active):
        """Water level in each active depression for a stored volume (vectorised bisection)."""
        v = vol[active] / self.cell ** 2
        off, cnt = self.comp_off[active], self.comp_cnt[active]
        z, cz = self.comp_z, self.comp_cumz
        lo, hi = np.ones(len(v), dtype=np.int64), cnt.copy()
        for _ in range(int(np.ceil(np.log2(max(int(cnt.max()), 2)))) + 1):
            mid = (lo + hi + 1) // 2
            j = off + mid - 1
            ok = mid * z[j] - cz[j] <= v
            lo = np.where(ok, mid, lo)
            hi = np.where(ok, hi, mid - 1)
        j = off + lo - 1
        level = (v + cz[j]) / lo
        return np.minimum(level, self.comp_spill[active])

    # --------------------------------------------------------------- simulate
    def simulate(self, rain, run_id: str, start: int = T_START) -> SimResult:
        rows, cols = self.rows, self.cols
        n = rows * cols
        A = self.cell ** 2
        dt_h, dt_s = DT_MIN / 60, DT_MIN * 60
        from .chennai import tide_factor
        drain_cell0 = self.drain_by_loc[self.loc_grid.ravel()] * self.drain_eff
        nc = self.ncomp
        comp_drain = (self.drain_by_loc[self.comp_loc] * self.drain_eff / 1000 * dt_h * self.comp_area + self.comp_pump * dt_s) if nc else np.zeros(0)
        store = np.zeros(nc)
        segs = self.area.segments
        water = self.water.ravel()
        r0, r1, c0, c1 = self.inner
        dep_cells, dep_comp = self.dep_cells, self.dep_comp
        dem_dep = self.dem_flat[dep_cells] if nc else None

        times, seg_frames, rasters, rain_means, rain_c = [], [], [], [], []
        total_mm = 0.0
        Q = np.zeros(n + 1 + nc)
        for t in range(start, T_END + 1, DT_MIN):
            I = rain.field(t + DT_MIN / 2).ravel()
            tf = tide_factor(self.tide, t + DT_MIN / 2)
            drain_cell = np.where(self.lowlying, drain_cell0 * tf, drain_cell0) if tf < 0.999 else drain_cell0
            Ig = I.reshape(rows, cols)
            inner_mean = float(Ig[r0:r1, c0:c1].mean())
            if t >= 0:
                total_mm += inner_mean * dt_h
            Q[:] = 0.0
            Q[:n] = np.maximum(I * self.runoff - drain_cell, 0) / 1000 * dt_h * A
            absorbing = store < self.comp_cap - 1e-6 if nc else None
            for ids, tg, cinfo in self.levels:
                if cinfo is not None:
                    cmp, has, vnode = cinfo
                    ab = has & absorbing[np.where(has, cmp, 0)]
                    if ab.any():
                        tg = np.where(ab, vnode, tg)
                np.add.at(Q, tg, Q[ids])
            if nc:
                add = Q[n + 1:]
                store = np.clip(store + add - comp_drain, 0, self.comp_cap)
                inflow_cells = absorbing[dep_comp]
            Qa = Q[:n] / dt_s
            if nc:
                Qa[dep_cells[inflow_cells]] = 0.0  # absorbed into a pond: no moving water there
            h = np.power(np.maximum(Qa, 0) / self.channel_w * MANNING_N / self.sqrt_s, 0.6)
            depth = np.minimum(h, 1.2)
            if nc:
                active = store > 0
                if active.any():
                    lv = np.full(nc, -np.inf)
                    lv[active] = self._pond_levels(store, active)
                    d = np.maximum(lv[dep_comp] - dem_dep, 0)
                    depth[dep_cells] = np.maximum(depth[dep_cells], d)
            depth[water] = 0
            if t < 0:
                continue
            g = gaussian_filter(depth.reshape(rows, cols), 0.8)
            gf = g.ravel()
            cm = np.maximum.reduceat(gf[self.seg_cells], self.seg_starts) * 100 if segs else np.zeros(0)
            if segs:
                cm[self.bridges] = 0
            seg_frames.append(np.clip(cm, 0, 250).astype(np.uint8))
            gw = g[r0:r1, c0:c1]
            h2, w2 = gw.shape[0] // 2, gw.shape[1] // 2
            pooled = gw[:h2 * 2, :w2 * 2].reshape(h2, 2, w2, 2).max(axis=(1, 3))
            rasters.append(np.clip(pooled * 100, 0, 250).astype(np.uint8))
            Iw = Ig[r0:r1, c0:c1]
            h8, w8 = max(1, Iw.shape[0] // 8), max(1, Iw.shape[1] // 8)
            rain_c.append(np.clip(Iw[:h8 * 8, :w8 * 8].reshape(h8, 8, w8, 8).mean(axis=(1, 3)), 0, 250).astype(np.uint8))
            rain_means.append(round(inner_mean, 1))
            times.append(t)
        empty = np.zeros((len(times), 0), dtype=np.uint8)
        return SimResult(run_id=run_id, version=getattr(rain, "version", 0), label=getattr(rain, "label", ""), times=times,
                         seg_depth=np.stack(seg_frames) if segs else empty, raster=np.stack(rasters),
                         rain_mean=rain_means, rain_coarse=np.stack(rain_c), total_mm=round(total_mm, 1),
                         raster_bbox=self.window_bbox(r0, r0 + rasters[0].shape[0] * 2, c0, c0 + rasters[0].shape[1] * 2))
