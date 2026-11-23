"""Ejecución en BingX: tamaño por riesgo, entrada con SL/TP, protección verificada, breakeven y trailing."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

import config
from bingx import BingXError
from indicators import atr, smart_trail
from tg import esc

log = logging.getLogger("trader")


def fp(x):
    """Precio legible con ~5 cifras significativas (sin notación científica)."""
    x = float(x)
    if x == 0:
        return "0"
    dec = max(0, min(10, 4 - math.floor(math.log10(abs(x)))))
    return f"{x:.{dec}f}"


def _fl(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


class State:
    """Estado persistente (JSON). En Railway sin volumen se pierde al redeploy; el SL/TP ya está en el exchange."""

    def __init__(self, path):
        self.path = path
        self.d = {"positions": {}, "cooldown": {}, "day": "", "day_equity": 0.0, "trades_today": 0,
                  "halted": False, "paused": False}
        try:
            with open(path, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("state ilegible (%s): empiezo limpio", e)

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            log.warning("no pude guardar state: %s", e)


class Trader:
    def __init__(self, api, tg, scanner, state):
        self.api, self.tg, self.scanner, self.state = api, tg, scanner, state
        self.hedge = None
        self.equity = 0.0
        self.available = 0.0
        self._bal_ts = 0.0
        self._configured = set()
        self.lock = threading.RLock()

    # ------------------------------------------------------------- utilidades
    def info(self, sym):
        return self.scanner.contracts.get(sym) or {}

    def rnd_price(self, sym, p):
        tick = self.info(sym).get("tick") or 1e-8
        return round(round(p / tick) * tick, 12)

    def rnd_qty(self, sym, q):
        step = self.info(sym).get("step") or 1e-4
        return round(math.floor(q / step + 1e-9) * step, 12)

    def pos_side(self, side):
        return side if self.hedge else "BOTH"

    def ensure_mode(self):
        if self.hedge is not None:
            return
        if config.POSITION_MODE == "hedge":
            self.hedge = True
        elif config.POSITION_MODE == "oneway":
            self.hedge = False
        else:
            self.hedge = self.api.position_mode()
        log.info("modo de posición: %s", "HEDGE" if self.hedge else "ONE-WAY")

    def refresh_balance(self, force=False):
        if not force and time.time() - self._bal_ts < 30:
            return
        b = self.api.balance()
        self.equity, self.available, self._bal_ts = b["equity"], b["available"], time.time()

    def startup_check(self):
        """Valida claves, modo de posición y saldo. Devuelve texto de estado."""
        self.api.sync_time()
        self.ensure_mode()
        self.refresh_balance(force=True)
        self._roll_day()
        return f"equity {self.equity:.2f} USDT · disponible {self.available:.2f} · modo {'hedge' if self.hedge else 'one-way'}"

    def _roll_day(self):
        d = self.state.d
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if d["day"] != today:
            d.update(day=today, trades_today=0, halted=False, day_equity=self.equity)
            self.state.save()
        elif d["day_equity"] <= 0 < self.equity:
            d["day_equity"] = self.equity
            self.state.save()

    def check_daily_limit(self):
        d = self.state.d
        if d["halted"] or d["day_equity"] <= 0 or self.equity <= 0:
            return
        dd = (d["day_equity"] - self.equity) / d["day_equity"] * 100.0
        if dd >= config.DAILY_LOSS_LIMIT_PCT:
            d["halted"] = True
            self.state.save()
            self.tg.send(f"⛔ <b>Límite de pérdida diaria</b> alcanzado (−{dd:.1f}%). "
                         f"Sin nuevas entradas hasta las 00:00 UTC. Las posiciones abiertas siguen con su SL/TP.")

    # ------------------------------------------------------------- entrada
    def can_enter(self, s):
        d = self.state.d
        if not config.AUTO_TRADE:
            return False, "AUTO_TRADE desactivado"
        if d["paused"]:
            return False, "pausado con /pause"
        if d["halted"]:
            return False, "límite de pérdida diaria"
        if s.score < config.TRADE_SCORE:
            return False, f"score {s.score:.0f} < TRADE_SCORE {config.TRADE_SCORE:.0f}"
        if d["trades_today"] >= config.MAX_TRADES_PER_DAY:
            return False, "máximo de operaciones diarias"
        if time.time() < d["cooldown"].get(s.symbol, 0):
            return False, "cooldown del símbolo"
        if s.symbol in d["positions"]:
            return False, "ya hay posición del bot"
        return True, ""

    def _configure(self, sym, side, lev):
        key = (sym, side, lev)
        if key in self._configured:
            return
        try:
            self.api.set_margin_mode(sym, config.MARGIN_MODE)
        except BingXError as e:
            log.debug("margin mode %s: %s", sym, e)
        try:
            self.api.set_leverage(sym, side if self.hedge else "BOTH", lev)
        except BingXError as e:
            log.warning("leverage %s: %s", sym, e)
        self._configured.add(key)

    def enter(self, s, price):
        """Abre la operación a mercado con SL/TP adjuntos. Devuelve (ok, mensaje)."""
        with self.lock:
            ok, why = self.can_enter(s)
            if not ok:
                return False, why
            sym, side = s.symbol, s.side
            sgn = 1 if side == "LONG" else -1
            try:
                self.ensure_mode()
                self.refresh_balance(force=True)
                self._roll_day()
                self.check_daily_limit()
                if self.state.d["halted"]:
                    return False, "límite de pérdida diaria"
                held = self.api.positions()
            except BingXError as e:
                return False, f"cuenta no accesible: {e.msg or e.code}"
            if len(held) >= config.MAX_OPEN_POSITIONS:
                return False, f"máximo de posiciones abiertas ({len(held)})"
            if any(p["symbol"] == sym for p in held):
                return False, "ya hay posición en el exchange"
            info = self.info(sym)
            sl = self.rnd_price(sym, s.sl)
            if sgn * (price - sl) <= 0:
                return False, "SL inválido respecto al precio"
            min_d = config.MIN_SL_PCT / 100.0 * price
            if abs(price - sl) < min_d:
                sl = self.rnd_price(sym, price - sgn * min_d)
            dist = abs(price - sl)
            if dist > config.MAX_SL_PCT / 100.0 * price * (2 if s.kind == "PARA" else 1):
                return False, "SL demasiado lejos"
            tp = self.rnd_price(sym, price + sgn * dist * config.TP_R)
            lev = max(1, min(config.LEVERAGE, int(info.get("max_lev") or config.LEVERAGE)))
            risk_usdt = self.equity * config.RISK_PCT / 100.0
            qty = min(risk_usdt / dist, self.available * lev * 0.9 / price)
            qty = self.rnd_qty(sym, qty)
            if qty <= 0 or qty < (info.get("min_qty") or 0) or qty * price < (info.get("min_notional") or 2.0) * 1.05:
                return False, f"tamaño bajo el mínimo (qty {qty})"
            self._configure(sym, side, lev)
            try:
                self.api.place_order(sym, "BUY" if side == "LONG" else "SELL", self.pos_side(side), qty,
                                     "MARKET", stop_loss=sl, take_profit=tp)
            except BingXError as e:
                if e.code != "NET":
                    return False, f"orden rechazada: {e.msg or e.code}"
                log.warning("timeout al enviar orden %s: compruebo si entró", sym)
            pos = self._wait_position(sym, side)
            if pos is None:
                self.tg.error("nopos-" + sym, f"{sym}: orden enviada pero no veo la posición. Revisa BingX manualmente.")
                return False, "sin posición tras la orden"
            entry = pos["entry"] or price
            rec = {"side": side, "entry": entry, "qty": pos["qty"], "sl": sl, "tp": tp, "risk": abs(entry - sl),
                   "t": time.time(), "be": False, "kind": s.kind, "score": s.score, "sl_id": "", "tp_id": "",
                   "peak_r": 0.0, "trail_ts": 0.0, "lev": lev, "risk_usdt": risk_usdt}
            d = self.state.d
            d["positions"][sym] = rec
            d["trades_today"] += 1
            d["cooldown"][sym] = time.time() + config.COOLDOWN_MIN * 60
            self.state.save()
            self._protect(sym, rec)
            self.state.save()
            self.tg.send(
                f"🚀 <b>ENTRADA {side}</b> {esc(sym)} <i>({s.kind} · score {s.score:.0f})</i>\n"
                f"Entrada ≈ {fp(entry)} · qty {pos['qty']:g} · x{lev} {config.MARGIN_MODE.lower()}\n"
                f"SL {fp(sl)} ({-sgn * abs(entry - sl) / entry * 100:+.2f}%) · TP {fp(tp)} (+{config.TP_R:g}R)\n"
                f"Riesgo ≈ ${risk_usdt:.2f} ({config.RISK_PCT:g}% equity)\n" + " · ".join(s.notes))
            return True, "ok"

    def _wait_position(self, sym, side, tries=5):
        for _ in range(tries):
            time.sleep(0.6)
            try:
                for p in self.api.positions(sym):
                    if p["symbol"] == sym and p["side"] == side:
                        return p
            except BingXError as e:
                log.debug("esperando posición %s: %s", sym, e)
        return None

    def _place_stop(self, sym, rec, kind, price):
        opp = "SELL" if rec["side"] == "LONG" else "BUY"
        typ = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
        return self.api.place_order(sym, opp, self.pos_side(rec["side"]), rec["qty"], typ, stop_price=price,
                                    reduce_only=not self.hedge, working_type=config.SL_TRIGGER)

    def _stop_orders(self, sym, rec):
        """(orden SL, orden TP) vigentes de esa posición en el exchange."""
        opp = "SELL" if rec["side"] == "LONG" else "BUY"
        sl_o = tp_o = None
        for o in self.api.open_orders(sym):
            t = str(o.get("type", "")).upper()
            if str(o.get("side", "")).upper() != opp:
                continue
            if self.hedge and str(o.get("positionSide", "")).upper() not in ("", rec["side"]):
                continue
            if t.startswith("TAKE_PROFIT"):
                tp_o = tp_o or o
            elif t.startswith("STOP"):
                sl_o = sl_o or o
        return sl_o, tp_o

    def _protect(self, sym, rec):
        """Garantiza SL (obligatorio) y TP en el exchange. Sin SL => cierra a mercado."""
        try:
            sl_o, tp_o = self._stop_orders(sym, rec)
        except BingXError as e:
            log.warning("openOrders %s: %s", sym, e)
            sl_o = tp_o = None
        if sl_o:
            rec["sl_id"] = str(sl_o.get("orderId", ""))
        else:
            try:
                rec["sl_id"] = str(self._place_stop(sym, rec, "SL", rec["sl"]).get("orderId", ""))
            except BingXError as e:
                self.tg.send(f"🚨 {esc(sym)}: no pude colocar el SL ({esc(e.msg or str(e.code))}). Cierro la posición.")
                self.close_market(sym, rec, "sin SL")
                return False
        if tp_o:
            rec["tp_id"] = str(tp_o.get("orderId", ""))
        else:
            try:
                rec["tp_id"] = str(self._place_stop(sym, rec, "TP", rec["tp"]).get("orderId", ""))
            except BingXError as e:
                self.tg.send(f"⚠️ {esc(sym)}: TP no colocado ({esc(e.msg or str(e.code))}); la posición sigue con SL y trailing.")
        return True

    def close_market(self, sym, rec, why):
        opp = "SELL" if rec["side"] == "LONG" else "BUY"
        rec["closing"] = why
        try:
            self.api.place_order(sym, opp, self.pos_side(rec["side"]), rec["qty"], "MARKET", reduce_only=not self.hedge)
            return True
        except BingXError as e:
            self.tg.error("close-" + sym, f"{sym}: fallo al cerrar a mercado ({why}): {e.msg or e.code}", every=120)
            return False

    # ------------------------------------------------------------- gestión
    def manage(self):
        with self.lock:
            try:
                self.refresh_balance()
                self._roll_day()
                self.check_daily_limit()
            except BingXError as e:
                log.debug("balance: %s", e)
            recs = self.state.d["positions"]
            if not recs:
                return
            try:
                held = {(p["symbol"], p["side"]): p for p in self.api.positions()}
            except BingXError as e:
                log.warning("positions: %s", e)
                return
            for sym, rec in list(recs.items()):
                p = held.get((sym, rec["side"]))
                if p is None:
                    self._on_closed(sym, rec)
                    continue
                try:
                    self._manage_one(sym, rec, p)
                except BingXError as e:
                    log.warning("gestión %s: %s", sym, e)

    def _manage_one(self, sym, rec, p):
        sgn = 1 if rec["side"] == "LONG" else -1
        mark = p["mark"] or self.scanner.tickers.get(sym, {}).get("last", 0)
        if not mark or rec["risk"] <= 0:
            return
        r = sgn * (mark - rec["entry"]) / rec["risk"]
        rec["peak_r"] = max(rec.get("peak_r", 0.0), r)
        gap = 2 * (self.info(sym).get("tick") or 0)
        if not rec["be"] and r >= config.BE_AT_R:
            new_sl = rec["entry"] + sgn * config.BE_OFFSET_R * rec["risk"]
            if sgn * (mark - new_sl) > gap and self._move_sl(sym, rec, new_sl, "🛡 Breakeven"):
                rec["be"] = True
        if r >= config.TRAIL_START_R and time.time() - rec["trail_ts"] >= 60:
            rec["trail_ts"] = time.time()
            self._trail(sym, rec, mark, gap)
        age_min = (time.time() - rec["t"]) / 60.0
        if age_min >= config.TIME_STOP_MIN and rec["peak_r"] < config.TIME_STOP_MIN_R and not rec.get("closing"):
            self.close_market(sym, rec, f"time stop {age_min:.0f} min sin avanzar")

    def _trail(self, sym, rec, mark, gap):
        k = self.api.klines(sym, config.TIMEFRAME, 120)
        base = config.TF_SECONDS * 1000
        if len(k["t"]) and k["t"][-1] + base > time.time() * 1000:
            k = {x: a[:-1] for x, a in k.items()}
        if len(k["c"]) < 40:
            return
        h, l, c = k["h"], k["l"], k["c"]
        _, direction, up, dn = smart_trail(h, l, c, config.ATR_LEN, config.ATR_MULT)
        a = float(atr(h, l, c, 14)[-1])
        if rec["side"] == "LONG" and direction[-1] == 1:
            cand = float(up[-1])
        elif rec["side"] == "SHORT" and direction[-1] == -1:
            cand = float(dn[-1])
        else:
            return
        sgn = 1 if rec["side"] == "LONG" else -1
        if sgn * (cand - rec["sl"]) > gap and sgn * (mark - cand) > max(gap, 0.3 * a):
            self._move_sl(sym, rec, cand, "📈 Trailing SL")

    def _move_sl(self, sym, rec, new_sl, tag):
        new_sl = self.rnd_price(sym, new_sl)
        sgn = 1 if rec["side"] == "LONG" else -1
        old_sl, old_id = rec["sl"], rec.get("sl_id")
        if sgn * (new_sl - old_sl) <= 0:
            return False
        if old_id:
            try:
                self.api.cancel_order(sym, old_id)
            except BingXError as e:
                log.debug("cancel SL %s: %s", sym, e)
        try:
            o = self._place_stop(sym, rec, "SL", new_sl)
        except BingXError as e:
            log.warning("nuevo SL rechazado %s: %s", sym, e)
            try:
                o = self._place_stop(sym, rec, "SL", old_sl)
                rec["sl_id"] = str(o.get("orderId", ""))
            except BingXError:
                self.tg.send(f"🚨 {esc(sym)}: no pude recolocar el SL. Cierro la posición.")
                self.close_market(sym, rec, "SL no recolocable")
            return False
        rec["sl"], rec["sl_id"] = new_sl, str(o.get("orderId", ""))
        self.state.save()
        locked = sgn * (new_sl - rec["entry"]) / rec["risk"]
        self.tg.send(f"{tag} {esc(sym)} {rec['side']} → SL {fp(new_sl)} (asegura {locked:+.1f}R)", silent=True)
        return True

    def _on_closed(self, sym, rec):
        reason, exit_px = rec.get("closing"), None
        for key, label in (("sl_id", "SL"), ("tp_id", "TP")):
            oid = rec.get(key)
            if not oid:
                continue
            try:
                o = self.api.get_order(sym, oid)
            except BingXError:
                continue
            if str(o.get("status", "")).upper() == "FILLED":
                reason = reason or label
                exit_px = _fl(o.get("avgPrice")) or None
                break
        sgn = 1 if rec["side"] == "LONG" else -1
        if exit_px is None:
            last = self.scanner.tickers.get(sym, {}).get("last") or rec["entry"]
            exit_px = {"SL": rec["sl"], "TP": rec["tp"]}.get(reason, last)
        pnl_r = sgn * (exit_px - rec["entry"]) / rec["risk"] if rec["risk"] else 0.0
        pnl = sgn * (exit_px - rec["entry"]) * rec["qty"]
        if reason == "SL" and rec["be"]:
            reason = "SL (breakeven/trailing)"
        try:
            self._cleanup_orders(sym, rec)
        except BingXError as e:
            log.debug("cleanup %s: %s", sym, e)
        self.state.d["positions"].pop(sym, None)
        self.state.d["cooldown"][sym] = time.time() + config.COOLDOWN_MIN * 60
        self.state.save()
        icon = "✅" if pnl > 0 else ("🛑" if pnl < 0 else "➖")
        self.tg.send(f"{icon} <b>CERRADA</b> {esc(sym)} {rec['side']} · {esc(str(reason or 'cerrada en el exchange'))}\n"
                     f"{fp(rec['entry'])} → {fp(exit_px)} · {pnl_r:+.2f}R (≈ {pnl:+.2f} USDT, sin comisiones)")

    def _cleanup_orders(self, sym, rec):
        """Cancela SL/TP huérfanos tras cerrar la posición."""
        if any(p["symbol"] == sym and p["side"] == rec["side"] for p in self.api.positions(sym)):
            return
        sl_o, tp_o = self._stop_orders(sym, rec)
        for o in (sl_o, tp_o):
            if o:
                try:
                    self.api.cancel_order(sym, o.get("orderId"))
                except BingXError as e:
                    log.debug("cancel huérfana %s: %s", sym, e)

    # ------------------------------------------------------------- informe
    def status_text(self):
        d = self.state.d
        lines = [f"<b>Estado</b> · AUTO_TRADE {'ON' if config.AUTO_TRADE else 'OFF'}"
                 f"{' · PAUSADO' if d['paused'] else ''}{' · LÍMITE DIARIO' if d['halted'] else ''}",
                 f"Equity {self.equity:.2f} USDT · hoy {d['trades_today']}/{config.MAX_TRADES_PER_DAY} operaciones"]
        for sym, r in d["positions"].items():
            lines.append(f"• {esc(sym)} {r['side']} @ {fp(r['entry'])} SL {fp(r['sl'])} TP {fp(r['tp'])} "
                         f"(pico {r.get('peak_r', 0):+.1f}R)")
        if not d["positions"]:
            lines.append("Sin posiciones del bot.")
        return "\n".join(lines)
