"""Street-level flood forecast.

The model, in four steps:
  1. Terrain. Priority-flood the elevation grid. This fills every depression (the
     places water ponds: underpasses, low colonies, old lake beds) and gives every
     cell a downhill receiver (D8 flow direction).
  2. Runoff. Each 10-minute step, rain above the local storm-drain capacity becomes
     runoff (urban runoff coefficient 0.85).
  3. Routing. Runoff is passed downhill cell by cell. Water entering a depression is
     stored there until it fills to its spill level; then it overflows downstream.
  4. Depth. Ponded depth comes from each depression's stored volume and its shape.
     Moving-water depth on slopes comes from discharge via Manning's equation.

Street depth = the deepest point along each street segment, per 10-minute frame.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter, label

from .rain import RainModel

RUNOFF_COEFF = 0.85
MANNING_N = 0.035
CHANNEL_W = 25.0      # effective flow width on a street, metres
MIN_SLOPE = 0.0015
BASE_DRAIN_MMPH = 18.0
DT_MIN = 10
PUMP_M3S = 0.6        # assumed pumping per subway (a few motor pumps), m3/s
T_START, T_END = -30, 360


def _point_in_polys(lat, lon, polys):
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
    raster: np.ndarray     # (frames, r, c) uint8 centimetres, half resolution
    rain_mean: list        # area-mean intensity per frame, mm/h
    rain_coarse: np.ndarray  # (frames, r, c) uint8 mm/h, 1/8 resolution
    total_mm: float

    def frame_at(self, t_min: float) -> int:
        return int(min(max(round(t_min / DT_MIN), 0), len(self.times) - 1))


class FloodModel:
    def __init__(self, area):
        self.area = area
        dem = area.dem.astype(np.float64).copy()
        # Subways sit several metres below the road around them. Cut them into the terrain
        # so they become the depressions they are (pumps are part of their drain capacity).
        cols0 = dem.shape[1]
        self.underpass_cells = 0
        sub_cells = []
        for sg in area.segments:
            if getattr(sg, "underpass", False) and sg.cells is not None and len(sg.cells):
                r, c = np.divmod(sg.cells, cols0)
                dem[r, c] -= 1.8
                self.underpass_cells += len(sg.cells)
                sub_cells.append(sg.cells)
        self._sub_cells = np.concatenate(sub_cells) if sub_cells else np.zeros(0, dtype=np.int64)
        self.rows, self.cols = dem.shape
        self.cell = area.cell_m
        n = self.rows * self.cols

        # water-body mask (lakes, marsh): they act as outlets and are never 'flooding'
        rr, cc = np.mgrid[0:self.rows, 0:self.cols]
        s, w, nn, e = area.bbox
        lat = nn - (rr + 0.5) / self.rows * (nn - s)
        lon = w + (cc + 0.5) / self.cols * (e - w)
        self.water = _point_in_polys(lat, lon, area.meta.get("water") or [])

        self.loc_grid = area.locality_grid()
        # context (set_context): runoff, drain efficiency, tide and the low-lying mask
        self.base_drain = BASE_DRAIN_MMPH
        self.runoff = RUNOFF_COEFF
        self.drain_eff = 1.0
        self.tide = None
        lo = np.percentile(dem, 20)
        self.lowlying = ((dem <= max(lo, 4.0)) & (dem < 7.0)).ravel()
        self.loc_names = [L["name"] for L in (area.meta.get("localities") or [{"name": "Area"}])]
        self.drain_by_loc = np.full(len(self.loc_names), BASE_DRAIN_MMPH)

        self._prepare_terrain(dem, n)
        # all street sample cells concatenated, for one vectorised max per frame
        segs = area.segments
        self.seg_cells = np.concatenate([sg.cells for sg in segs]) if segs else np.zeros(0, dtype=np.int64)
        self.seg_starts = np.cumsum([0] + [len(sg.cells) for sg in segs[:-1]]) if segs else np.zeros(0, dtype=np.int64)
        self.bridges = np.array([sg.bridge for sg in segs], dtype=bool) if segs else np.zeros(0, dtype=bool)
        # coarse corridor grids: spread moving water over the whole cell, not a 25 m street
        self.channel_w = max(CHANNEL_W, self.cell * 0.8)

    # ------------------------------------------------------------------ terrain
    def _prepare_terrain(self, dem, n):
        rows, cols = self.rows, self.cols
        flat = dem.ravel()
        filled = flat.copy()
        recv = np.full(n, -1, dtype=np.int64)
        closed = np.zeros(n, dtype=bool)
        heap = []
        water = self.water.ravel()
        for i in range(n):
            r, c = divmod(i, cols)
            if r == 0 or c == 0 or r == rows - 1 or c == cols - 1 or water[i]:
                closed[i] = True
                heap.append((flat[i], i))
        heapq.heapify(heap)
        order = []
        nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
        eps = 1e-5
        while heap:
            z, i = heapq.heappop(heap)
            order.append(i)
            r, c = divmod(i, cols)
            for dr, dc in nbrs:
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    j = rr * cols + cc
                    if not closed[j]:
                        closed[j] = True
                        recv[j] = i
                        filled[j] = max(flat[j], z + eps)
                        heapq.heappush(heap, (filled[j], j))
        self.dem_flat = flat
        self.filled = filled
        self.recv = recv
        self.up_order = order[::-1]  # upstream cells first

        # slope towards receiver
        slope = np.full(n, MIN_SLOPE)
        has = recv >= 0
        idx = np.nonzero(has)[0]
        r1, c1 = np.divmod(idx, cols)
        r2, c2 = np.divmod(recv[idx], cols)
        dist = np.where((r1 != r2) & (c1 != c2), self.cell * math.sqrt(2), self.cell)
        slope[idx] = np.maximum((filled[idx] - filled[recv[idx]]) / dist, MIN_SLOPE)
        self.slope = slope

        # depressions
        cap = (filled - flat).reshape(rows, cols)
        lab, ncomp = label(cap > 0.03, structure=np.ones((3, 3)))
        self.comp = lab.ravel().astype(np.int64) - 1  # -1 = not in a depression
        self.ncomp = ncomp
        A = self.cell ** 2
        self.comp_cells, self.comp_z, self.comp_cum, self.comp_cap, self.comp_area = [], [], [], [], []
        self.comp_loc = np.zeros(ncomp, dtype=np.int64)
        for k in range(ncomp):
            cells = np.nonzero(self.comp == k)[0]
            z = np.sort(flat[cells])
            spill = filled[cells].max()
            cum = np.cumsum(z)
            self.comp_cells.append(cells)
            self.comp_z.append(z)
            self.comp_cum.append(cum)
            self.comp_cap.append(A * float(np.sum(np.maximum(spill - z, 0))))
            self.comp_area.append(A * len(cells))
            self.comp_loc[k] = np.bincount(self.loc_grid.ravel()[cells]).argmax()
        self.comp_cap = np.array(self.comp_cap)
        self.comp_area = np.array(self.comp_area)
        self.comp_spill = np.array([filled[c].max() for c in self.comp_cells]) if ncomp else np.array([])
        # Every Chennai subway has motor pumps. Depressions holding a subway get pump capacity.
        self.comp_pump = np.zeros(ncomp)
        sub = getattr(self, "_sub_cells", np.zeros(0, dtype=np.int64))
        if ncomp and len(sub):
            for k in set(self.comp[sub].tolist()) - {-1}:
                self.comp_pump[k] = PUMP_M3S

    def _pond_level(self, k, vol):
        z, cum = self.comp_z[k], self.comp_cum[k]
        if vol <= 0:
            return z[0]
        v = vol / self.cell ** 2
        lo, hi = 1, len(z)
        while lo < hi:  # find number of submerged cells m
            mid = (lo + hi + 1) // 2
            if mid * z[mid - 1] - cum[mid - 1] <= v:
                lo = mid
            else:
                hi = mid - 1
        m = lo
        level = (v + cum[m - 1]) / m
        return min(level, self.comp_spill[k])

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
            self.base_drain = base_drain
        if runoff:
            self.runoff = runoff
        if drain_eff:
            self.drain_eff = drain_eff
        if tide != "keep":
            self.tide = tide

    # --------------------------------------------------------------- simulate
    def simulate(self, rain, run_id: str, start: int = T_START) -> SimResult:
        rows, cols = self.rows, self.cols
        n = rows * cols
        A = self.cell ** 2
        dt_h, dt_s = DT_MIN / 60, DT_MIN * 60
        from .chennai import tide_factor
        drain_cell0 = self.drain_by_loc[self.loc_grid.ravel()] * self.drain_eff
        comp_drain = (self.drain_by_loc[self.comp_loc] * self.drain_eff / 1000 * dt_h * self.comp_area + self.comp_pump * dt_s) if self.ncomp else np.array([])
        store = np.zeros(self.ncomp)
        recv = self.recv.tolist()
        comp = self.comp.tolist()
        order = self.up_order
        sqrt_s = np.sqrt(self.slope)
        segs = self.area.segments

        times, seg_frames, rasters, rain_means, rain_c = [], [], [], [], []
        total_mm = 0.0
        for t in range(start, T_END + 1, DT_MIN):
            I = rain.field(t + DT_MIN / 2).ravel()
            tf = tide_factor(self.tide, t + DT_MIN / 2)
            drain_cell = np.where(self.lowlying, drain_cell0 * tf, drain_cell0) if tf < 0.999 else drain_cell0
            if t >= 0:
                total_mm += float(I.mean()) * dt_h
            net = np.maximum(I * self.runoff - drain_cell, 0) / 1000 * dt_h * A
            Q = net.tolist()
            full = (store >= self.comp_cap - 1e-6).tolist() if self.ncomp else []
            add = [0.0] * self.ncomp
            for i in order:
                q = Q[i]
                if q <= 0:
                    continue
                k = comp[i]
                if k >= 0 and not full[k]:
                    add[k] += q
                    Q[i] = 0.0
                    continue
                r = recv[i]
                if r >= 0:
                    Q[r] += q
            if self.ncomp:
                store = np.clip(store + np.array(add) - comp_drain, 0, self.comp_cap)
            Qa = np.array(Q) / dt_s
            h = np.power(np.maximum(Qa, 0) / self.channel_w * MANNING_N / sqrt_s, 0.6)
            depth = np.minimum(h, 1.2)
            for k in np.nonzero(store > 0)[0]:
                L = self._pond_level(k, store[k])
                cells = self.comp_cells[k]
                depth[cells] = np.maximum(depth[cells], np.maximum(L - self.dem_flat[cells], 0))
            depth[self.water.ravel()] = 0
            if t < 0:
                continue
            g = gaussian_filter(depth.reshape(rows, cols), 0.8)
            gf = g.ravel()
            cm = np.maximum.reduceat(gf[self.seg_cells], self.seg_starts) * 100 if segs else np.zeros(0)
            if segs:
                cm[self.bridges] = 0
            seg_frames.append(np.clip(cm, 0, 250).astype(np.uint8))
            r2, c2 = rows // 2, cols // 2
            pooled = g[:r2 * 2, :c2 * 2].reshape(r2, 2, c2, 2).max(axis=(1, 3))
            rasters.append(np.clip(pooled * 100, 0, 250).astype(np.uint8))
            Ig = I.reshape(rows, cols)
            r8, c8 = rows // 8, cols // 8
            rain_c.append(np.clip(Ig[:r8 * 8, :c8 * 8].reshape(r8, 8, c8, 8).mean(axis=(1, 3)), 0, 250).astype(np.uint8))
            rain_means.append(round(float(I.mean()), 1))
            times.append(t)
        return SimResult(run_id=run_id, version=rain.version, label=rain.label, times=times,
                         seg_depth=np.stack(seg_frames), raster=np.stack(rasters),
                         rain_mean=rain_means, rain_coarse=np.stack(rain_c), total_mm=round(total_mm, 1))
