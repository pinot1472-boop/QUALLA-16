"""Indicadores en numpy puro. Réplica de las funciones ta.* de Pine v5 usadas en OMNIX Fusion."""
from __future__ import annotations

import numpy as np

NAN = np.nan


def sma(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), NAN)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _recursive(x, n, alpha):
    """Media recursiva con semilla SMA(n) (como ta.ema / ta.rma de Pine)."""
    x = np.asarray(x, float)
    out = np.full(len(x), NAN)
    valid = np.where(~np.isnan(x))[0]
    if len(valid) < n:
        return out
    s = valid[0]
    i0 = s + n - 1
    out[i0] = x[s:i0 + 1].mean()
    for i in range(i0 + 1, len(x)):
        xi = x[i]
        out[i] = out[i - 1] if np.isnan(xi) else alpha * xi + (1.0 - alpha) * out[i - 1]
    return out


def ema(x, n):
    return _recursive(x, n, 2.0 / (n + 1.0))


def rma(x, n):
    return _recursive(x, n, 1.0 / n)


def true_range(h, l, c):
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    tr = np.empty(len(c))
    if len(c) == 0:
        return tr
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    return tr


def atr(h, l, c, n):
    return rma(true_range(h, l, c), n)


def dmi(h, l, c, di_len=14, adx_len=14):
    """ta.dmi -> (DI+, DI-, ADX)."""
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    n = len(c)
    up = np.full(n, NAN)
    dn = np.full(n, NAN)
    up[1:] = h[1:] - h[:-1]
    dn[1:] = -(l[1:] - l[:-1])
    plus_dm = np.where(np.isnan(up), NAN, np.where((up > dn) & (up > 0), up, 0.0))
    minus_dm = np.where(np.isnan(dn), NAN, np.where((dn > up) & (dn > 0), dn, 0.0))
    trur = rma(true_range(h, l, c), di_len)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = 100.0 * rma(plus_dm, di_len) / trur
        minus = 100.0 * rma(minus_dm, di_len) / trur
    plus = _fixnan(plus)
    minus = _fixnan(minus)
    s = plus + minus
    s = np.where(s == 0, 1.0, s)
    adx = 100.0 * rma(np.abs(plus - minus) / s, adx_len)
    return plus, minus, adx


def _fixnan(x):
    x = np.array(x, float)
    last = NAN
    for i in range(len(x)):
        if np.isnan(x[i]):
            x[i] = last
        else:
            last = x[i]
    return x


def smart_trail(h, l, c, atr_len=10, mult=3.0):
    """Port literal del bloque 'Smart Trail' del Pine. Devuelve (trail, direction, up, dn)."""
    h, l, c = (np.asarray(a, float) for a in (h, l, c))
    n = len(c)
    a = atr(h, l, c, atr_len)
    hl2 = (h + l) / 2.0
    up = np.full(n, NAN)
    dn = np.full(n, NAN)
    direction = np.ones(n, dtype=int)
    trail = np.full(n, NAN)
    for i in range(n):
        prev_dir = direction[i - 1] if i > 0 else 1
        if np.isnan(a[i]):
            direction[i] = prev_dir
            continue
        up_raw = hl2[i] - mult * a[i]
        dn_raw = hl2[i] + mult * a[i]
        prev_up = up[i - 1] if i > 0 and not np.isnan(up[i - 1]) else up_raw
        prev_dn = dn[i - 1] if i > 0 and not np.isnan(dn[i - 1]) else dn_raw
        c1 = c[i - 1] if i > 0 else NAN
        up[i] = max(up_raw, prev_up) if (c1 > prev_up) else up_raw
        dn[i] = min(dn_raw, prev_dn) if (c1 < prev_dn) else dn_raw
        if c[i] > prev_dn:
            direction[i] = 1
        elif c[i] < prev_up:
            direction[i] = -1
        else:
            direction[i] = prev_dir
        trail[i] = up[i] if direction[i] == 1 else dn[i]
    return trail, direction, up, dn


def resample(k, period_ms, base_ms):
    """Agrupa velas base en velas de `period_ms` alineadas a UTC; solo buckets completos."""
    t = k["t"]
    if len(t) == 0:
        return {x: np.array([]) for x in "tohlcv"}
    per = int(period_ms // base_ms)
    bucket = t // period_ms
    out = {x: [] for x in "tohlcv"}
    for b in np.unique(bucket):
        idx = np.where(bucket == b)[0]
        if len(idx) != per:
            continue
        out["t"].append(int(b * period_ms))
        out["o"].append(k["o"][idx[0]])
        out["h"].append(k["h"][idx].max())
        out["l"].append(k["l"][idx].min())
        out["c"].append(k["c"][idx[-1]])
        out["v"].append(k["v"][idx].sum())
    return {x: np.array(v, dtype=np.int64 if x == "t" else float) for x, v in out.items()}


def rolling_std_mean(c, w):
    """(media, desviación poblacional) de ventana w, alineadas al final de la serie."""
    c = np.asarray(c, float)
    if len(c) < w:
        return np.array([]), np.array([])
    win = np.lib.stride_tricks.sliding_window_view(c, w)
    return win.mean(axis=1), win.std(axis=1)


def slope(y):
    """Pendiente de regresión lineal de y sobre 0..n-1."""
    y = np.asarray(y, float)
    n = len(y)
    if n < 3:
        return 0.0
    x = np.arange(n, dtype=float)
    x -= x.mean()
    return float((x * (y - y.mean())).sum() / (x * x).sum())
