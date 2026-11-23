"""Prueba de humo SIN red ni dinero: levanta un BingX + Telegram falsos y ejecuta el bot completo.

    python test_offline.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import numpy as np

import fake_bingx as fb

os.environ.update({
    "BINGX_API_KEY": fb.KEY, "BINGX_API_SECRET": fb.SECRET, "TELEGRAM_BOT_TOKEN": fb.TOKEN,
    "TELEGRAM_CHAT_ID": fb.CHAT, "AUTO_TRADE": "true", "MIN_SCORE": "45", "TRADE_SCORE": "45",
    "ALERT_SCORE": "45", "MIN_QUOTE_VOL_USDT": "1000000", "WORKERS": "4", "RPS_PUBLIC": "300",
    "RPS_PRIVATE": "300", "COOLDOWN_MIN": "1", "LOG_LEVEL": "ERROR", "MAX_OPEN_POSITIONS": "3",
})
import config  # noqa: E402
import indicators as ind  # noqa: E402
import main as botmain  # noqa: E402
import trader as trmod  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        FAILS.append(msg)


def tg_has(world, text):
    return any(text in m for m in world.telegram)


def new_bot(hedge=True, auto=True):
    srv, world, base = fb.start(hedge)
    config.BINGX_BASE_URL = config.TELEGRAM_BASE_URL = base
    config.AUTO_TRADE = auto
    config.MAX_OPEN_POSITIONS = 3
    config.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
    bot = botmain.Bot()
    bot.startup()
    return srv, world, bot


def orders_of(world, sym, status="NEW"):
    return [o for o in world.orders.values() if o["symbol"] == sym and o["status"] == status]


def fire(world, bot, sym, side, vol=5000.0):
    """Mueve el precio justo por encima/debajo del gatillo y lanza el ciclo de disparo."""
    s = next(x for x in bot.scanner.watchlist if x.symbol == sym)
    world.prices[sym] = s.trigger * (1.0003 if side == "LONG" else 0.9997)
    world.forming_vol[sym] = vol
    bot.trigger_cycle()
    return s


# ---------------------------------------------------------------- indicadores
def test_indicators():
    print("\n[1] Indicadores")
    x = np.full(60, 5.0)
    check(abs(ind.ema(x, 9)[-1] - 5.0) < 1e-9 and abs(ind.rma(x, 14)[-1] - 5.0) < 1e-9, "EMA/RMA de una constante = constante")
    up = np.linspace(100, 160, 120)
    h, l = up + 0.5, up - 0.5
    _, _, adx = ind.dmi(h, l, up)
    check(adx[-1] > 40, f"ADX alto en tendencia limpia ({adx[-1]:.0f})")
    flat = 100 + np.sin(np.arange(120) / 2.0)
    _, _, adx2 = ind.dmi(flat + 0.5, flat - 0.5, flat)
    check(adx2[-1] < 25, f"ADX bajo en rango ({adx2[-1]:.0f})")
    path = np.concatenate([np.linspace(100, 130, 60), np.linspace(130, 90, 60)])
    _, d, _, _ = ind.smart_trail(path + 1, path - 1, path, 10, 3.0)
    check(d[40] == 1 and d[-1] == -1, "Smart Trail: alcista en la subida y gira a bajista tras la caída")
    t = np.arange(0, 3600_000 * 5, 900_000, dtype=np.int64) + 3600_000 * 100
    k = {"t": t, "o": np.arange(len(t), dtype=float), "h": np.arange(len(t), dtype=float) + 1,
         "l": np.arange(len(t), dtype=float) - 1, "c": np.arange(len(t), dtype=float) + 0.5, "v": np.ones(len(t))}
    r = ind.resample(k, 3600_000, 900_000)
    check(len(r["c"]) == 5 and r["v"][0] == 4, "resample 15m -> 1h agrupa 4 velas alineadas")


# ---------------------------------------------------------------- flujo completo
def test_full_flow(hedge):
    mode = "HEDGE" if hedge else "ONE-WAY"
    print(f"\n[2] Flujo completo en modo {mode}")
    srv, world, bot = new_bot(hedge)
    tr = bot.trader
    check(tr.hedge is hedge, f"detecta el modo de posición ({mode})")
    check(tg_has(world, "iniciado"), "mensaje de arranque en Telegram")

    watch = bot.scanner.scan()
    bot.after_scan(watch)
    syms = {s.symbol: s for s in watch}
    uni = bot.scanner.universe(bot.scanner.tickers)
    check("LOW-USDT" not in uni and "NCSKTSLA2USD-USDT" not in uni, "excluye poco volumen y TradFi")
    check("SIG-USDT" in syms and syms["SIG-USDT"].side == "LONG", "detecta la compresión alcista (LONG)")
    check("SHT-USDT" in syms and syms["SHT-USDT"].side == "SHORT", "detecta la compresión bajista (SHORT)")
    check(not any(s.symbol.startswith("RN") for s in watch), "ignora los paseos aleatorios")
    check(tg_has(world, "A PUNTO DE EXPLOTAR"), "alerta de radar a Telegram")

    bot.trigger_cycle()
    check(not world.positions, "sin ruptura no entra")

    s = fire(world, bot, "SIG-USDT", "LONG")
    pos = [p for p in world.positions.values() if p["qty"] and True]
    check(("SIG-USDT", "LONG" if hedge else "LONG") in world.positions, "abre LONG de SIG-USDT a mercado")
    kinds = sorted(o["type"] for o in orders_of(world, "SIG-USDT"))
    check(kinds == ["STOP_MARKET", "TAKE_PROFIT_MARKET"], "SL y TP vigentes en el exchange")
    check(tg_has(world, "ENTRADA LONG"), "aviso de entrada en Telegram")
    rec = tr.state.d["positions"]["SIG-USDT"]
    risk_usdt = rec["qty"] * rec["risk"]
    check(abs(risk_usdt - 10.0) / 10.0 < 0.06, f"tamaño por riesgo: {risk_usdt:.2f} USDT ≈ 1% de 1000")
    check(rec["tp"] > rec["entry"] > rec["sl"] and abs((rec["tp"] - rec["entry"]) / rec["risk"] - config.TP_R) < 0.05,
          "TP a 3R y SL por debajo de la entrada")
    lev_calls = [c for c in world.calls if c[1].endswith("/trade/leverage")]
    check(lev_calls and lev_calls[-1][2]["side"] == ("LONG" if hedge else "BOTH"), "apalancamiento con el lado correcto")
    n_before = len(world.positions)
    bot.trigger_cycle()
    check(len(world.positions) == n_before, "no reentra en el mismo símbolo (cooldown)")

    # breakeven
    R = rec["risk"]
    world.prices["SIG-USDT"] = rec["entry"] + 1.2 * R
    tr.manage()
    sl_orders = [o for o in orders_of(world, "SIG-USDT") if o["type"] == "STOP_MARKET"]
    check(len(sl_orders) == 1 and float(sl_orders[0]["stopPrice"]) >= rec["entry"], "mueve el SL a breakeven (y cancela el anterior)")
    check(rec["be"] and tg_has(world, "Breakeven"), "aviso de breakeven")

    # trailing (Smart Trail forzado)
    world.prices["SIG-USDT"] = rec["entry"] + 2.5 * R
    target = rec["entry"] + 1.2 * R
    real = trmod.smart_trail

    def fake_trail(h, l, c, a, m):
        n = len(c)
        up = np.full(n, target)
        return up, np.ones(n, dtype=int), up, up
    trmod.smart_trail = fake_trail
    rec["trail_ts"] = 0
    tr.manage()
    trmod.smart_trail = real
    sl_orders = [o for o in orders_of(world, "SIG-USDT") if o["type"] == "STOP_MARKET"]
    check(len(sl_orders) == 1 and abs(float(sl_orders[0]["stopPrice"]) - target) < 1e-3, "trailing sube el SL al Smart Trail")
    check(tg_has(world, "Trailing SL"), "aviso de trailing")

    # el TP salta -> cierre y limpieza de órdenes huérfanas
    tp_id = next(o["orderId"] for o in orders_of(world, "SIG-USDT") if o["type"] == "TAKE_PROFIT_MARKET")
    world.fill_conditional(tp_id)
    tr.manage()
    check("SIG-USDT" not in tr.state.d["positions"], "detecta el cierre por TP y limpia el estado")
    check(not orders_of(world, "SIG-USDT"), "cancela el SL huérfano")
    check(tg_has(world, "CERRADA") and tg_has(world, "TP"), "aviso de cierre con motivo TP")

    # SHORT con cierre por SL
    fire(world, bot, "SHT-USDT", "SHORT")
    check(("SHT-USDT", "SHORT") in world.positions, "abre SHORT de SHT-USDT")
    rec2 = tr.state.d["positions"].get("SHT-USDT")
    check(rec2 is not None and rec2["tp"] < rec2["entry"] < rec2["sl"], "SHORT: SL por encima y TP por debajo")
    sl_id = next(o["orderId"] for o in orders_of(world, "SHT-USDT") if o["type"] == "STOP_MARKET")
    world.fill_conditional(sl_id)
    tr.manage()
    check("SHT-USDT" not in tr.state.d["positions"] and tg_has(world, "SL"), "cierre del SHORT por SL")
    check(world.bad_sig == 0, "todas las firmas HMAC válidas para el servidor")
    srv.shutdown()


# ---------------------------------------------------------------- caminos de fallo / límites
def test_unprotected_entry_is_closed():
    print("\n[3] Sin SL posible -> cierra la posición")
    srv, world, bot = new_bot(True)
    bot.scanner.scan()
    world.no_attach = True
    world.reject_stops = True
    fire(world, bot, "SIG-USDT", "LONG")
    check(not world.positions, "cierra a mercado una posición que no pudo protegerse")
    check(tg_has(world, "no pude colocar el SL"), "avisa del problema por Telegram")
    bot.trader.manage()
    check("SIG-USDT" not in bot.trader.state.d["positions"], "estado limpio tras el cierre de emergencia")
    srv.shutdown()


def test_signal_only():
    print("\n[4] AUTO_TRADE=false -> solo señales")
    srv, world, bot = new_bot(True, auto=False)
    bot.scanner.scan()
    fire(world, bot, "SIG-USDT", "LONG")
    check(not world.positions and not [c for c in world.calls if c[0] == "POST" and c[1].endswith("/trade/order")],
          "no envía ninguna orden")
    check(tg_has(world, "RUPTURA CONFIRMADA"), "envía la señal de ruptura a Telegram")
    srv.shutdown()
    config.AUTO_TRADE = True


def test_limits_and_commands():
    print("\n[5] Límites de riesgo y comandos de Telegram")
    srv, world, bot = new_bot(True)
    bot.scanner.scan()
    config.MAX_OPEN_POSITIONS = 1
    fire(world, bot, "SIG-USDT", "LONG")
    fire(world, bot, "SHT-USDT", "SHORT")
    check(("SIG-USDT", "LONG") in world.positions and ("SHT-USDT", "SHORT") not in world.positions,
          "respeta MAX_OPEN_POSITIONS")
    check(tg_has(world, "SEÑAL no operada"), "avisa de la señal no operada")
    world.equity = 900.0
    bot.trader.refresh_balance(force=True)
    bot.trader.check_daily_limit()
    check(bot.state.d["halted"] and tg_has(world, "Límite de pérdida diaria"), "frena las entradas al superar la pérdida diaria")
    check(bot.trader.can_enter(next(iter(bot.scanner.watchlist)))[0] is False, "no permite nuevas entradas con el límite activo")
    world.updates = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/pause"}},
                     {"update_id": 2, "message": {"chat": {"id": int(fb.CHAT)}, "text": "/status"}}]
    bot.tg.start_polling(bot.on_command)
    t0 = time.time()
    while time.time() - t0 < 5 and not tg_has(world, "Estado"):
        time.sleep(0.1)
    check(tg_has(world, "Estado"), "responde a /status del dueño")
    check(bot.state.d["paused"] is False, "ignora comandos de otros chats")
    bot.tg.stop()
    check(bot.on_command("/pause").startswith("⏸") and bot.state.d["paused"], "/pause detiene las entradas")
    check(bot.on_command("/resume").startswith("▶️") and not bot.state.d["paused"], "/resume las reanuda")
    srv.shutdown()


def test_run_loop():
    print("\n[6] Bucle real: Bot.run() con hilos de escaneo, disparo y gestión")
    import threading
    srv, world, base = fb.start(True)
    config.BINGX_BASE_URL = config.TELEGRAM_BASE_URL = base
    config.AUTO_TRADE, config.MAX_OPEN_POSITIONS = True, 3
    config.SCAN_INTERVAL, config.TRIGGER_INTERVAL, config.MANAGE_INTERVAL = 2, 1, 1
    config.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
    bot = botmain.Bot()
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()

    def wait(cond, secs):
        t0 = time.time()
        while time.time() - t0 < secs and not cond():
            time.sleep(0.1)
        return cond()

    check(wait(lambda: any(s.symbol == "SIG-USDT" for s in bot.scanner.watchlist), 20), "el hilo de escaneo llena la watchlist")
    s = next(x for x in bot.scanner.watchlist if x.symbol == "SIG-USDT")
    world.forming_vol["SIG-USDT"] = 5000.0
    world.prices["SIG-USDT"] = s.trigger * 1.0003
    check(wait(lambda: ("SIG-USDT", "LONG") in world.positions, 15), "el ciclo de disparo abre la operación solo")
    wait(lambda: "SIG-USDT" in bot.state.d["positions"], 10)      # el bot registra tras confirmar la posición
    rec = bot.state.d["positions"].get("SIG-USDT")
    check(rec is not None and len(orders_of(world, "SIG-USDT")) == 2,
          f"queda registrada y con SL/TP (registro={'sí' if rec else 'NO'}, órdenes={len(orders_of(world, 'SIG-USDT'))})")
    if rec:
        world.prices["SIG-USDT"] = rec["entry"] + 1.2 * rec["risk"]
        check(wait(lambda: rec["be"], 15), "el ciclo de gestión mueve a breakeven solo")
        world.fill_conditional(next(o["orderId"] for o in orders_of(world, "SIG-USDT") if o["type"] == "TAKE_PROFIT_MARKET"))
        check(wait(lambda: "SIG-USDT" not in bot.state.d["positions"], 15), "el ciclo de gestión detecta el cierre solo")
    bot.stop.set()
    th.join(5)
    check(not th.is_alive() and tg_has(world, "Bot detenido"), "parada limpia")
    check(world.bad_sig == 0, "firmas HMAC válidas en el bucle real")
    srv.shutdown()


if __name__ == "__main__":
    test_indicators()
    test_full_flow(True)
    test_full_flow(False)
    test_unprotected_entry_is_closed()
    test_signal_only()
    test_limits_and_commands()
    test_run_loop()
    print("\n" + ("TODO OK" if not FAILS else f"FALLOS ({len(FAILS)}):\n - " + "\n - ".join(FAILS)))
    sys.exit(1 if FAILS else 0)
