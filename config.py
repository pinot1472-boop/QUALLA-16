"""Configuración por variables de entorno (Railway -> Variables). Sin dependencias."""
from __future__ import annotations

import os


def _s(name, default=""):
    v = os.getenv(name)
    return default if v is None or str(v).strip() == "" else str(v).strip()


def _f(name, default):
    try:
        return float(_s(name, str(default)).replace(",", "."))
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(float(_s(name, str(default)).replace(",", ".")))
    except ValueError:
        return int(default)


def _b(name, default):
    return _s(name, "true" if default else "false").lower() in ("1", "true", "yes", "si", "sí", "on", "y")


def _l(name, default=""):
    return [x.strip().upper() for x in _s(name, default).split(",") if x.strip()]


# ------------------------------------------------------------------ credenciales
BINGX_API_KEY = _s("BINGX_API_KEY")
BINGX_API_SECRET = _s("BINGX_API_SECRET")
BINGX_DEMO = _b("BINGX_DEMO", False)  # cuenta demo VST de BingX
BINGX_BASE_URL = _s(
    "BINGX_BASE_URL",
    "https://open-api-vst.bingx.com" if BINGX_DEMO else "https://open-api.bingx.com",
).rstrip("/")
TELEGRAM_TOKEN = _s("TELEGRAM_BOT_TOKEN") or _s("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _s("TELEGRAM_CHAT_ID")
TELEGRAM_BASE_URL = _s("TELEGRAM_BASE_URL", "https://api.telegram.org").rstrip("/")
# Comandos /status /top /pause /resume (long-polling). Desactivado por defecto: si otro bot usa el mismo token
# con getUpdates, Telegram devuelve 409 y se pisan. Actívalo solo con un bot de Telegram dedicado.
TELEGRAM_COMMANDS = _b("TELEGRAM_COMMANDS", False)

# ------------------------------------------------------------------ operativa
AUTO_TRADE = _b("AUTO_TRADE", False)          # false = solo señales a Telegram
LEVERAGE = _i("LEVERAGE", 5)
MARGIN_MODE = _s("MARGIN_MODE", "ISOLATED").upper()   # ISOLATED | CROSSED
POSITION_MODE = _s("POSITION_MODE", "auto").lower()   # auto | hedge | oneway
RISK_PCT = _f("RISK_PCT", 1.0)                # % del equity arriesgado por operación
MAX_OPEN_POSITIONS = _i("MAX_OPEN_POSITIONS", 3)
MAX_TRADES_PER_DAY = _i("MAX_TRADES_PER_DAY", 8)
DAILY_LOSS_LIMIT_PCT = _f("DAILY_LOSS_LIMIT_PCT", 4.0)  # pausa entradas hasta el día UTC siguiente
COOLDOWN_MIN = _i("COOLDOWN_MIN", 120)        # minutos entre operaciones del mismo símbolo
SL_ATR_MULT = _f("SL_ATR_MULT", 2.0)          # tope de distancia al SL en ATR(14) de 15m
MIN_SL_PCT = _f("MIN_SL_PCT", 0.5)            # distancia mínima al SL (% del precio)
MAX_SL_PCT = _f("MAX_SL_PCT", 6.0)            # distancia máxima al SL (% del precio)
TP_R = _f("TP_R", 3.0)                        # take profit en múltiplos de R
BE_AT_R = _f("BE_AT_R", 1.0)                  # mover SL a breakeven al llegar a +R
BE_OFFSET_R = _f("BE_OFFSET_R", 0.1)          # colchón sobre la entrada (en R)
TRAIL_START_R = _f("TRAIL_START_R", 1.5)      # empieza el trailing (Smart Trail) a +R
TIME_STOP_MIN = _i("TIME_STOP_MIN", 360)      # cierra si pasa este tiempo sin avanzar
TIME_STOP_MIN_R = _f("TIME_STOP_MIN_R", 0.5)  # ...y no llegó a este R
SL_TRIGGER = _s("SL_TRIGGER", "MARK_PRICE").upper()   # MARK_PRICE | CONTRACT_PRICE
MAX_SPREAD_PCT = _f("MAX_SPREAD_PCT", 0.15)   # no entra si el spread bid/ask supera esto
MAX_CHASE_ATR = _f("MAX_CHASE_ATR", 0.8)      # no persigue rupturas ya extendidas (ATR)
TRIGGER_RVOL = _f("TRIGGER_RVOL", 1.3)        # volumen proyectado mínimo para confirmar ruptura

# ------------------------------------------------------------------ escáner
TIMEFRAME = _s("TIMEFRAME", "15m")
KLINE_LIMIT = _i("KLINE_LIMIT", 300)
MIN_QUOTE_VOL = _f("MIN_QUOTE_VOL_USDT", 1_000_000)   # volumen 24h mínimo (USDT); 0 = todas
MAX_SYMBOLS = _i("MAX_SYMBOLS", 0)                    # tope de monedas por escaneo; 0 = sin tope (todas)
EXCLUDE_SYMBOLS = _l("EXCLUDE_SYMBOLS", "")
INCLUDE_TRADFI = _b("INCLUDE_TRADFI", False)  # acciones / materias primas / forex de BingX
SCAN_INTERVAL = _i("SCAN_INTERVAL_SEC", 300)
TRIGGER_INTERVAL = _i("TRIGGER_INTERVAL_SEC", 15)
MANAGE_INTERVAL = _i("MANAGE_INTERVAL_SEC", 10)
MIN_SCORE = _f("MIN_SCORE", 62)               # mínimo para entrar en la watchlist
ALERT_SCORE = _f("ALERT_SCORE", 72)           # mínimo para alerta individual a Telegram
TRADE_SCORE = _f("TRADE_SCORE", 70)           # mínimo para operar
MAX_ALERTS_PER_SCAN = _i("MAX_ALERTS_PER_SCAN", 3)
WATCHLIST_SIZE = _i("WATCHLIST_SIZE", 12)
DIGEST_MIN = _i("DIGEST_MIN", 30)             # minutos entre resúmenes del radar
DIGEST_TOP = _i("DIGEST_TOP", 8)
RPS_PUBLIC = _f("RPS_PUBLIC", 8.0)            # peticiones/seg de datos de mercado
RPS_PRIVATE = _f("RPS_PRIVATE", 6.0)          # peticiones/seg de cuenta y órdenes
WORKERS = _i("WORKERS", 6)
MIN_ADR_PCT = _f("MIN_ADR_PCT", 3.0)          # ADR diario mínimo (potencial de explosión)

# ------------------------------------------------------------------ Smart Trail / EP / Parabolic (Pine OMNIX)
ATR_LEN = _i("ATR_LEN", 10)
ATR_MULT = _f("ATR_MULT", 3.0)
ADX_LEN = _i("ADX_LEN", 14)
ADX_THRESHOLD = _f("ADX_THRESHOLD", 22.0)
EMA_FAST = _i("EMA_FAST", 9)
EMA_SLOW = _i("EMA_SLOW", 21)
VOL_MULT = _f("VOL_MULT", 1.4)
ENABLE_EP = _b("ENABLE_EP", True)
MIN_CHANGE_EP = _f("MIN_CHANGE_EP", 7.5)
MIN_ADR_EP = _f("MIN_ADR_EP", 3.5)
EMA_EP_FAST = _i("EMA_EP_FAST", 21)
EMA_EP_SLOW = _i("EMA_EP_SLOW", 50)
ENABLE_PARA = _b("ENABLE_PARA_SHORT", True)
MIN_RUN_PCT = _f("MIN_RUN_PCT", 50.0)
MIN_UP_DAYS = _i("MIN_UP_DAYS", 3)
EMA_PARA_SLOW = _i("EMA_PARA_SLOW", 20)
EXT_THRESHOLD = _f("EXT_THRESHOLD", 15.0)
PARA_LOOKBACK = _i("PARA_LOOKBACK", 10)

# ------------------------------------------------------------------ compresión (squeeze)
BOX_BARS = _i("BOX_BARS", 16)                 # barras de la caja de consolidación
SQ_LOOKBACK = _i("SQ_LOOKBACK", 200)          # ventana del percentil de ancho de Bollinger (≈2 días en 15m)
TRIGGER_BUFFER_ATR = _f("TRIGGER_BUFFER_ATR", 0.1)

# ------------------------------------------------------------------ varios
STATE_FILE = _s("STATE_FILE", "state.json")   # con volumen en Railway: /data/state.json
LOG_LEVEL = _s("LOG_LEVEL", "INFO").upper()
HTTP_TIMEOUT = _f("HTTP_TIMEOUT", 15.0)

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}.get(TIMEFRAME, 900)


def is_tradfi(symbol: str) -> bool:
    """Perpetuos TradFi de BingX (acciones, materias primas, forex, índices): NCSK/NCCO/NCFX/NCSI..."""
    return symbol.upper().startswith(("NCSK", "NCCO", "NCFX", "NCSI"))


def summary() -> str:
    return (
        f"AUTO_TRADE={AUTO_TRADE} demo={BINGX_DEMO} tf={TIMEFRAME} lev={LEVERAGE}x {MARGIN_MODE} "
        f"riesgo={RISK_PCT}% maxPos={MAX_OPEN_POSITIONS} minScore={MIN_SCORE}/{TRADE_SCORE} "
        f"vol24h>={MIN_QUOTE_VOL:,.0f} maxSymbols={MAX_SYMBOLS or 'sin tope'} TP={TP_R}R scan={SCAN_INTERVAL}s"
    )
