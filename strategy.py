"""Motor de señales: puntúa cada moneda por probabilidad de explosión intradía (arriba o abajo).

Score 0-100 = compresión (30) + despertar de volumen (12) + ADX (8) + sesgo direccional (50).
  - Compresión : ancho de Bollinger en percentil bajo, ATR corto/largo, barras en squeeze (BB dentro de Keltner).
  - Sesgo      : Smart Trail (Pine OMNIX), EMAs 9/21, tendencia 1h, posición en la caja, mínimos/máximos
                 que se aprietan, cercanía al máximo/mínimo del día anterior, 'Precise Entries' fuertes, EP.
  - PARA SHORT : agrietamiento de una corrida parabólica diaria (Pine OMNIX) como setup independiente.
Disparo: ruptura de la caja de consolidación (máx/mín de BOX_BARS velas) + volumen (lo confirma main/scanner).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

import config
from indicators import atr, dmi, ema, resample, rolling_std_mean, slope, smart_trail, true_range


@dataclass
class Setup:
    symbol: str
    side: str            # LONG | SHORT
    kind: str            # SQUEEZE | EP+SQ | PARA
    score: float
    trigger: float
    sl: float
    tp: float
    atr: float
    price: float
    state: str           # WATCH (bajo el gatillo) | BREAKOUT (ya rompiendo)
    box_hi: float
    box_lo: float
    feats: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def risk(self) -> float:
        return abs(self.trigger - self.sl)


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def closed_only(k, now_ms, base_ms):
    """Descarta la vela en formación."""
    if len(k["t"]) and k["t"][-1] + base_ms > now_ms:
        return {x: a[:-1] for x, a in k.items()}
    return k


# ------------------------------------------------------------------ contexto diario (EP / Parabolic / ADR)
def daily_context(k, daily, now_ms):
    """Velas diarias cerradas + vela parcial de hoy construida con las velas de 15m."""
    ctx = {}
    if daily is None or len(daily.get("t", [])) < 30 or len(k["t"]) == 0:
        return ctx
    day0 = (now_ms // 86_400_000) * 86_400_000
    m = daily["t"] < day0
    O, H, L, C = (daily[x][m] for x in "ohlc")
    if len(C) < 30:
        return ctx
    ctx["adr"] = float(np.mean((H[-20:] - L[-20:]) / np.maximum(C[-20:], 1e-12) * 100.0))
    ctx["pdh"], ctx["pdl"] = float(H[-1]), float(L[-1])
    tm = k["t"] >= day0
    if not tm.any():
        return ctx
    to, th, tl, tc = k["o"][tm][0], k["h"][tm].max(), k["l"][tm].min(), k["c"][tm][-1]
    ctx["day_hi"], ctx["day_lo"] = float(th), float(tl)
    O2, H2, L2, C2 = (np.append(a, v) for a, v in ((O, to), (H, th), (L, tl), (C, tc)))
    ctx["dchg"] = float((C2[-1] - C2[-2]) / C2[-2] * 100.0)
    e21, e50 = ema(C2, config.EMA_EP_FAST)[-1], ema(C2, config.EMA_EP_SLOW)[-1]
    adr_ok = ctx["adr"] >= config.MIN_ADR_EP
    if config.ENABLE_EP and adr_ok and not (np.isnan(e21) or np.isnan(e50)):
        ctx["ep_up"] = bool(ctx["dchg"] >= config.MIN_CHANGE_EP and C2[-1] > e21 and C2[-1] > e50)
        ctx["ep_dn"] = bool(ctx["dchg"] <= -config.MIN_CHANGE_EP and C2[-1] < e21 and C2[-1] < e50)
    # Parabolic (Qullamaggie): corrida %, días verdes consecutivos, extensión sobre EMA20
    lb = config.PARA_LOOKBACK
    if config.ENABLE_PARA and len(C2) > lb + 3:
        e20 = ema(C2, config.EMA_PARA_SLOW)
        up_days = np.zeros(len(C2), dtype=int)
        for i in range(1, len(C2)):
            up_days[i] = up_days[i - 1] + 1 if C2[i] > C2[i - 1] else 0
        run = np.full(len(C2), np.nan)
        run[lb:] = (C2[lb:] - C2[:-lb]) / C2[:-lb] * 100.0
        ext = (C2 - e20) / e20 * 100.0
        is_para = (run >= config.MIN_RUN_PCT) & (up_days >= config.MIN_UP_DAYS) & (ext >= config.EXT_THRESHOLD)
        ctx["para_run"] = bool(is_para[-1])
        ctx["para_crack"] = bool(
            is_para[-2] and (C2[-1] < L2[-2] or (C2[-1] < O2[-1] and C2[-1] < C2[-2]))
        )
        ctx["para_run_pct"] = float(run[-2]) if not np.isnan(run[-2]) else 0.0
        ctx["para_ext"] = float(ext[-2]) if not np.isnan(ext[-2]) else 0.0
    return ctx


# ------------------------------------------------------------------ análisis principal
def analyze(symbol, k_all, daily, now_ms=None):
    """Devuelve el mejor Setup de la moneda (o None)."""
    now_ms = now_ms or int(time.time() * 1000)
    base_ms = config.TF_SECONDS * 1000
    k = closed_only(k_all, now_ms, base_ms)
    n = len(k["c"])
    if n < 150:
        return None
    o, h, l, c, v = (k[x] for x in "ohlcv")
    px = float(c[-1])
    if px <= 0 or (c[-120:] <= 0).any():
        return None
    a14 = float(atr(h, l, c, 14)[-1])
    if not a14 > 0:
        return None
    ctx = daily_context(k, daily, now_ms)
    adr = ctx.get("adr")
    if adr is not None and adr < config.MIN_ADR_PCT:
        return None

    # --- compresión
    tr_ = true_range(h, l, c)
    atr_ratio = float(tr_[-8:].mean() / max(tr_[-96:].mean(), 1e-12))
    mid, sd = rolling_std_mean(c, 20)
    bbw = 4.0 * sd / np.where(mid == 0, 1.0, mid)
    bbw_pct = float((bbw[-config.SQ_LOOKBACK:] < bbw[-1]).mean())
    m = 40
    e20, a20 = ema(c, 20), atr(h, l, c, 20)
    sq = ((mid + 2 * sd)[-m:] < (e20 + 1.5 * a20)[-m:]) & ((mid - 2 * sd)[-m:] > (e20 - 1.5 * a20)[-m:])
    sq_bars = 0
    for flag in sq[::-1]:
        if not flag:
            break
        sq_bars += 1
    comp = (14 * (1 - _clip(bbw_pct / 0.5, 0, 1)) + 8 * _clip((0.9 - atr_ratio) / 0.5, 0, 1)
            + 8 * _clip(sq_bars / 8.0, 0, 1))

    # --- caja y volumen
    B = config.BOX_BARS
    H, L = float(h[-B:].max()), float(l[-B:].min())
    loc = (px - L) / max(H - L, 1e-12)
    rvol3 = float(v[-3:].mean() / max(v[-43:-3].mean(), 1e-12))
    volp = 12 * _clip((rvol3 - 0.9) / 0.9, 0, 1)

    # --- ADX / tendencia (Pine)
    pdi, mdi, adx = dmi(h, l, c, config.ADX_LEN, config.ADX_LEN)
    adx_now, adx_slope = float(adx[-1]), float(adx[-1] - adx[-4])
    adxp = 0.0
    if 12 <= adx_now <= 40:
        adxp = 4 + 4 * _clip(adx_slope / 3.0, 0, 1) if adx_slope > 0 else 2.0
    ef, es = float(ema(c, config.EMA_FAST)[-1]), float(ema(c, config.EMA_SLOW)[-1])
    _, direction, _, _ = smart_trail(h, l, c, config.ATR_LEN, config.ATR_MULT)
    tdir = int(direction[-1])
    flip_up = any(direction[i] == 1 and direction[i - 1] == -1 for i in range(n - 8, n))
    flip_dn = any(direction[i] == -1 and direction[i - 1] == 1 for i in range(n - 8, n))
    strong_long = flip_up and ef > es and px > ef and adx_now > config.ADX_THRESHOLD and rvol3 >= config.VOL_MULT
    strong_short = flip_dn and ef < es and px < ef and adx_now > config.ADX_THRESHOLD and rvol3 >= config.VOL_MULT

    k1 = resample(k, 3_600_000, base_ms)
    htf = 0
    if len(k1["c"]) >= 55:
        c1 = k1["c"]
        e21h, e50h = ema(c1, 21)[-1], ema(c1, 50)[-1]
        if c1[-1] > e21h > e50h:
            htf = 2
        elif c1[-1] < e21h < e50h:
            htf = -2
        elif c1[-1] > e21h:
            htf = 1
        elif c1[-1] < e21h:
            htf = -1

    slope_lo, slope_hi = slope(l[-12:]) / a14, slope(h[-12:]) / a14
    pdh, pdl = ctx.get("pdh"), ctx.get("pdl")

    def key_pts(s):
        if pdh is None:
            return 0.0
        d = ((pdh - px) if s == 1 else (px - pdl)) / a14
        return 6 * (1 - d / 1.2) if 0 <= d <= 1.2 else 0.0

    def side_pts(s):
        ep = ctx.get("ep_up") if s == 1 else ctx.get("ep_dn")
        p = 8.0 if tdir == s else 0.0
        p += 6.0 if (s * (ef - es) > 0 and s * (px - ef) > 0) else (3.0 if s * (px - es) > 0 else 0.0)
        p += 6.0 if htf == 2 * s else (3.0 if htf == s else 0.0)
        p += 10 * _clip(((loc if s == 1 else 1 - loc) - 0.5) / 0.35, 0, 1)
        p += 4 * _clip((slope_lo if s == 1 else -slope_hi) / 0.06, 0, 1)
        p += key_pts(s)
        p += 5.0 if (strong_long if s == 1 else strong_short) else (2.0 if (flip_up if s == 1 else flip_dn) else 0.0)
        p += 5.0 if ep else 0.0
        return p

    base = comp + volp + adxp
    pl, ps = side_pts(1), side_pts(-1)
    side_n, pts, other = (1, pl, ps) if pl >= ps else (-1, ps, pl)
    side = "LONG" if side_n == 1 else "SHORT"
    penalty = 0.0
    if (h[-1] - l[-1]) > 2.2 * a14 or abs(px - es) / a14 > 3.0:
        penalty = 20.0          # ya se movió: no es "a punto de", es "en curso"
    score = _clip(base + pts - penalty, 0, 100)

    best = None
    clear_bias = (pts - other) >= 4
    ep_flag = bool(ctx.get("ep_up") if side_n == 1 else ctx.get("ep_dn"))
    if clear_bias and (comp >= 12 or (ep_flag and bbw_pct <= 0.6)):
        buf = config.TRIGGER_BUFFER_ATR * a14
        if side_n == 1:
            trigger = H + buf
            struct = trigger - (L - 0.1 * a14)
        else:
            trigger = L - buf
            struct = (H + 0.1 * a14) - trigger
        dist = min(max(struct, 1.0 * a14), config.SL_ATR_MULT * a14)
        dist = min(max(dist, config.MIN_SL_PCT / 100 * trigger), config.MAX_SL_PCT / 100 * trigger)
        beyond = (px - trigger) if side_n == 1 else (trigger - px)
        if beyond <= config.MAX_CHASE_ATR * a14:
            sl = trigger - side_n * dist
            tp = trigger + side_n * dist * config.TP_R
            best = Setup(symbol, side, "EP+SQ" if ep_flag else "SQUEEZE", round(score, 1), trigger, sl, tp,
                         a14, px, "BREAKOUT" if beyond >= 0 else "WATCH", H, L)

    para = _para_setup(symbol, ctx, px, a14, tdir, rvol3, H, L)
    if para is not None and (best is None or para.score > best.score):
        best = para
    if best is None:
        return None
    best.feats = {
        "bbw_pct": bbw_pct, "atr_ratio": atr_ratio, "sq_bars": sq_bars, "rvol3": rvol3, "adx": adx_now,
        "adx_slope": adx_slope, "loc": loc, "tdir": tdir, "htf": htf, "adr": adr, "dchg": ctx.get("dchg"),
        "ep": ep_flag, "para": bool(ctx.get("para_crack")),
    }
    best.notes = _notes(best.feats, best)
    return best


def _para_setup(symbol, ctx, px, a14, tdir, rvol3, H, L):
    if not (config.ENABLE_PARA and ctx.get("para_crack")):
        return None
    day_hi = ctx.get("day_hi", px)
    pdl = ctx.get("pdl", px)
    sl = day_hi + 0.25 * a14
    dist = sl - px
    if dist <= 0 or dist > 2 * config.MAX_SL_PCT / 100 * px:
        return None
    trigger = pdl if px > pdl else px
    dist = max(sl - trigger, config.MIN_SL_PCT / 100 * trigger)
    sl = trigger + dist
    score = (55 + _clip((ctx.get("para_ext", 0) - config.EXT_THRESHOLD) / 2.0, 0, 15)
             + (10 if tdir == -1 else 0) + 8 * _clip(rvol3 - 1.0, 0, 1)
             + 10 * _clip((ctx.get("para_run_pct", 0) - config.MIN_RUN_PCT) / 100.0, 0, 1))
    beyond = trigger - px
    return Setup(symbol, "SHORT", "PARA", round(_clip(score, 0, 100), 1), trigger, sl,
                 trigger - dist * config.TP_R, a14, px, "BREAKOUT" if beyond >= 0 else "WATCH", H, L)


def _notes(f, s):
    out = [f"BB {f['bbw_pct'] * 100:.0f}%", f"rvol {f['rvol3']:.1f}x", f"ADX {f['adx']:.0f}{'↑' if f['adx_slope'] > 0 else '↓'}"]
    if f["sq_bars"] >= 3:
        out.insert(1, f"squeeze {f['sq_bars']}v")
    out.append("Trail " + ("↑" if f["tdir"] == 1 else "↓"))
    if f["htf"]:
        out.append("1h " + ("↑" if f["htf"] > 0 else "↓"))
    if f.get("ep") and f.get("dchg") is not None:
        out.append(f"EP {f['dchg']:+.1f}% día")
    if f.get("para"):
        out.append("PARABÓLICO agrietado")
    return out


# ------------------------------------------------------------------ confirmación en vivo del disparo
def confirm_breakout(setup, k_all, now_ms=None):
    """Con velas recientes (la última en formación): volumen proyectado y cuerpo vs ATR."""
    now_ms = now_ms or int(time.time() * 1000)
    base_ms = config.TF_SECONDS * 1000
    if len(k_all["t"]) < 25:
        return False, "pocas velas"
    forming = k_all["t"][-1] + base_ms > now_ms
    closed = {x: a[:-1] for x, a in k_all.items()} if forming else k_all
    base_vol = float(closed["v"][-20:].mean())
    if base_vol <= 0:
        return False, "sin volumen base"
    if forming:
        frac = _clip((now_ms - int(k_all["t"][-1])) / base_ms, 0.2, 1.0)
        proj = float(k_all["v"][-1]) / frac
        rvol = max(proj / base_vol, float(closed["v"][-1]) / base_vol)
    else:
        rvol = float(closed["v"][-1]) / base_vol
    if rvol < config.TRIGGER_RVOL:
        return False, f"volumen insuficiente ({rvol:.1f}x < {config.TRIGGER_RVOL}x)"
    return True, f"volumen {rvol:.1f}x"
