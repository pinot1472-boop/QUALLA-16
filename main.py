"""OMNIX Explosion Bot - escáner de BingX (monedas a punto de explotar) -> Telegram -> órdenes en BingX."""
from __future__ import annotations

import logging
import signal
import threading
import time

import config
import strategy
from bingx import BingX, BingXError
from scanner import Scanner
from tg import Telegram, esc
from trader import State, Trader, fp

log = logging.getLogger("main")

HELP = ("<b>OMNIX Explosion Bot</b>\n/status · posiciones y estado\n/top · mejores candidatas ahora\n"
        "/pause · detiene nuevas entradas\n/resume · reanuda entradas")


def arrow(side):
    return "↑" if side == "LONG" else "↓"


def dot(side):
    return "🟢" if side == "LONG" else "🔴"


class Bot:
    def __init__(self):
        self.api = BingX(config.BINGX_API_KEY, config.BINGX_API_SECRET)
        self.tg = Telegram()
        self.scanner = Scanner(self.api)
        self.state = State(config.STATE_FILE)
        self.trader = Trader(self.api, self.tg, self.scanner, self.state)
        self.stop = threading.Event()
        self.fired = {}          # símbolo -> ts del último gatillo
        self.alerted = {}        # (símbolo, lado) -> ts de la última alerta de radar
        self.last_digest = 0.0

    # ------------------------------------------------------------- mensajes
    def fmt_setup(self, s, head):
        gap = (s.trigger - s.price) / s.price * 100 * (1 if s.side == "LONG" else -1)
        return (f"{head} {dot(s.side)} <b>{esc(s.symbol)}</b> {arrow(s.side)} · score {s.score:.0f} <i>({s.kind})</i>\n"
                f"Precio {fp(s.price)} · gatillo {fp(s.trigger)} ({gap:+.2f}%) · SL {fp(s.sl)} · TP {fp(s.tp)}\n"
                + " · ".join(s.notes))

    def after_scan(self, watch):
        now = time.time()
        sent = 0
        for s in watch:                                   # ya viene ordenada por score
            key = (s.symbol, s.side)
            if s.score >= config.ALERT_SCORE and now - self.alerted.get(key, 0) > 3600 and sent < config.MAX_ALERTS_PER_SCAN:
                self.alerted[key] = now
                sent += 1
                self.tg.send(self.fmt_setup(s, "🔭 <b>A PUNTO DE EXPLOTAR</b>"), silent=s.score < 85)
        if now - self.last_digest >= config.DIGEST_MIN * 60:
            self.last_digest = now
            self.tg.send(self.digest_text(), silent=True)

    def digest_text(self):
        watch, ranked, st = self.scanner.snapshot()
        rows = (watch or ranked)[:config.DIGEST_TOP]
        head = (f"📡 <b>RADAR {config.TIMEFRAME}</b> · {st.get('universe', 0)} monedas · "
                f"{st.get('watch', 0)} en vigilancia · {st.get('secs', 0)}s")
        if not rows:
            return head + "\nNinguna moneda cumple las condiciones ahora." + self.diagnosis_text(st)
        lines = [head]
        for i, s in enumerate(rows, 1):
            gap = (s.trigger - s.price) / s.price * 100 * (1 if s.side == "LONG" else -1)
            lines.append(f"{i}. {dot(s.side)} {esc(s.symbol)} {arrow(s.side)} <b>{s.score:.0f}</b> · gatillo {fp(s.trigger)} "
                         f"({gap:+.2f}%) · BB {s.feats['bbw_pct'] * 100:.0f}% · rvol {s.feats['rvol3']:.1f}x · {s.kind}")
        return "\n".join(lines)

    @staticmethod
    def diagnosis_text(st):
        """Por qué no hay candidatas: casi-candidatas, motivos de descarte y filtros del universo."""
        out = []
        near = st.get("near") or []
        if near:
            out.append("Más cerca (aún sin setup): "
                       + " · ".join(f"{esc(sym)} {sc:.0f} {side}" for sc, sym, side, _ in near[:5]))
        d = st.get("descartes") or {}
        if d:
            out.append("Descartes: " + ", ".join(f"{k} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:5]))
        fu = st.get("funnel") or {}
        if fu:
            out.append(f"Universo: {fu.get('analizables', 0)} de {fu.get('contratos', 0)} contratos"
                       f" (bajo volumen {fu.get('poco_volumen', 0)}, TradFi {fu.get('tradfi', 0)}"
                       f", recortadas por tope {fu.get('recortadas_por_tope', 0)})")
        return ("\n" + "\n".join(out)) if out else ""

    # ------------------------------------------------------------- bucles
    def scan_loop(self):
        while not self.stop.is_set():
            t0 = time.time()
            try:
                self.after_scan(self.scanner.scan())
            except Exception as e:  # noqa: BLE001
                log.exception("fallo en el escaneo")
                self.tg.error("scan", f"Error en el escaneo: {esc(str(e))[:200]}")
            self.stop.wait(max(5.0, config.SCAN_INTERVAL - (time.time() - t0)))

    def trigger_cycle(self):
        watch, _, _ = self.scanner.snapshot()
        if not watch:
            return
        ticks = self.api.tickers()
        self.scanner.tickers = ticks
        now = time.time()
        for s in watch:
            t = ticks.get(s.symbol)
            if not t or t["last"] <= 0 or now - self.fired.get(s.symbol, 0) < config.COOLDOWN_MIN * 60:
                continue
            price = t["last"]
            sgn = 1 if s.side == "LONG" else -1
            if sgn * (price - s.trigger) < 0:
                continue                                    # aún no rompe
            if sgn * (price - s.trigger) > config.MAX_CHASE_ATR * s.atr:
                self.fired[s.symbol] = now - config.COOLDOWN_MIN * 60 + 600   # ruptura ya extendida: no perseguir
                log.info("%s: ruptura ya extendida, no se persigue", s.symbol)
                continue
            if t["bid"] > 0 and t["ask"] > 0 and (t["ask"] - t["bid"]) / price * 100 > config.MAX_SPREAD_PCT:
                log.info("%s: spread alto, espero", s.symbol)
                continue
            try:
                ok, why = strategy.confirm_breakout(s, self.api.klines(s.symbol, config.TIMEFRAME, 40))
            except BingXError as e:
                log.warning("confirmación %s: %s", s.symbol, e)
                continue
            if not ok:
                log.info("%s: gatillo sin confirmar (%s)", s.symbol, why)
                continue
            self.fired[s.symbol] = now
            if config.AUTO_TRADE:
                done, msg = self.trader.enter(s, price)
                if not done:
                    self.tg.send(self.fmt_setup(s, "📣 <b>SEÑAL no operada</b>") + f"\nMotivo: {esc(msg)}")
            else:
                self.tg.send(self.fmt_setup(s, "🎯 <b>RUPTURA CONFIRMADA</b>") + f"\n{why} · AUTO_TRADE=OFF (solo señal)")

    # ------------------------------------------------------------- comandos
    def on_command(self, text):
        cmd = text.split()[0].lower().split("@")[0]
        d = self.state.d
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self.trader.status_text()
        if cmd == "/top":
            return self.digest_text()
        if cmd == "/pause":
            d["paused"] = True
            self.state.save()
            return "⏸ Entradas pausadas. Las posiciones abiertas siguen gestionándose."
        if cmd == "/resume":
            d["paused"] = False
            self.state.save()
            return "▶️ Entradas reanudadas."
        return HELP

    # ------------------------------------------------------------- arranque
    def startup(self):
        have_keys = bool(config.BINGX_API_KEY and config.BINGX_API_SECRET)
        status = ""
        if config.AUTO_TRADE:
            if not have_keys:
                config.AUTO_TRADE = False
                status = "⚠️ AUTO_TRADE=true pero faltan BINGX_API_KEY/BINGX_API_SECRET: solo señales."
            else:
                try:
                    status = "Cuenta OK · " + self.trader.startup_check()
                except BingXError as e:
                    config.AUTO_TRADE = False
                    status = f"⚠️ No pude validar la cuenta de BingX ({esc(e.msg or str(e.code))}): solo señales."
        else:
            self.api.sync_time()
        self.tg.send(f"🤖 <b>OMNIX Explosion Bot</b> iniciado\n{esc(config.summary())}\n{status}", silent=True)

    def run(self):
        self.startup()
        if config.TELEGRAM_COMMANDS:
            self.tg.start_polling(self.on_command)
        threading.Thread(target=self.scan_loop, daemon=True, name="scan").start()
        nt = nm = 0.0
        while not self.stop.is_set():
            now = time.time()
            try:
                if now >= nt:
                    nt = now + config.TRIGGER_INTERVAL
                    self.trigger_cycle()
                if config.AUTO_TRADE and now >= nm:
                    nm = now + config.MANAGE_INTERVAL
                    self.trader.manage()
            except BingXError as e:
                log.warning("ciclo: %s", e)
            except Exception as e:  # noqa: BLE001
                log.exception("fallo en el ciclo principal")
                self.tg.error("loop", f"Error en el ciclo principal: {esc(str(e))[:200]}")
            self.stop.wait(1.0)
        self.tg.send("🛑 Bot detenido.", silent=True)


def main():
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: bot.stop.set())
    bot.run()


if __name__ == "__main__":
    main()
