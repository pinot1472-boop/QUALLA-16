"""Cliente REST mínimo de BingX Perpetual Futures (USDT-M). Solo `requests` + stdlib."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from urllib.parse import quote

import numpy as np
import requests

import config

log = logging.getLogger("bingx")

RATE_CODES = {100410}            # rate limit (no se procesó la petición)
TIME_CODES = {100421}            # timestamp fuera de ventana (no se procesó)
RETRY_HTTP = {429, 500, 502, 503, 504}


class BingXError(Exception):
    def __init__(self, code, msg, path=""):
        super().__init__(f"BingX {path} code={code} msg={msg}")
        self.code = code
        self.msg = str(msg)
        self.path = path


class RateLimiter:
    """Cubo simple: como mucho `rps` peticiones por segundo entre todos los hilos."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.1)
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def num(v) -> str:
    """Número en decimal plano (sin notación científica)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = f"{v:.12f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    return str(v)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class BingX:
    def __init__(self, key="", secret="", base_url=None, rps_public=None, rps_private=None):
        self.key = key
        self.secret = secret
        self.base = (base_url or config.BINGX_BASE_URL).rstrip("/")
        self.pub = RateLimiter(rps_public or config.RPS_PUBLIC)
        self.prv = RateLimiter(rps_private or config.RPS_PRIVATE)
        self.offset = 0
        self.local = threading.local()

    # ------------------------------------------------------------- bajo nivel
    def _session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": "omnix-explosion-bot/1.0"})
            self.local.s = s
        return s

    def _ts(self):
        return int(time.time() * 1000) + self.offset

    def _query(self, params, signed):
        items = {k: v for k, v in (params or {}).items() if v is not None}
        items["timestamp"] = self._ts()
        keys = sorted(items)
        raw = "&".join(f"{k}={self._val(items[k])}" for k in keys)
        qs = "&".join(f"{k}={quote(self._val(items[k]), safe='')}" for k in keys)
        if signed:
            sig = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
            qs += "&signature=" + sig
        return qs

    @staticmethod
    def _val(v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, separators=(",", ":"))
        return num(v)

    def request(self, method, path, params=None, signed=False, retries=3):
        """Reintenta solo GET (y errores que garantizan que la orden no se procesó)."""
        limiter = self.prv if signed else self.pub
        idempotent = method == "GET"
        last = None
        for attempt in range(retries + 1):
            limiter.wait()
            url = f"{self.base}{path}?{self._query(params, signed)}"
            headers = {"X-BX-APIKEY": self.key} if signed else {}
            try:
                r = self._session().request(method, url, headers=headers, timeout=config.HTTP_TIMEOUT)
            except requests.RequestException as e:
                last = e
                if idempotent and attempt < retries:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise BingXError("NET", f"{type(e).__name__}: {e}", path)
            if r.status_code in RETRY_HTTP:
                last = BingXError(r.status_code, r.text[:200], path)
                if (idempotent or r.status_code == 429) and attempt < retries:
                    time.sleep(1.0 * (attempt + 1) * (2 if r.status_code == 429 else 1))
                    continue
                raise last
            try:
                data = r.json()
            except ValueError:
                raise BingXError(r.status_code, f"respuesta no JSON: {r.text[:160]}", path)
            code = data.get("code", 0) if isinstance(data, dict) else 0
            if str(code) in ("0", ""):
                return data
            code_i = int(code) if str(code).lstrip("-").isdigit() else code
            if code_i in TIME_CODES and attempt < retries:
                self.sync_time()
                continue
            if code_i in RATE_CODES and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise BingXError(code, data.get("msg", ""), path)
        raise last or BingXError("?", "sin respuesta", path)

    def sync_time(self):
        try:
            d = self.request("GET", "/openApi/swap/v2/server/time", retries=1)
            st = int((d.get("data") or {}).get("serverTime"))
            self.offset = st - int(time.time() * 1000)
        except Exception as e:  # noqa: BLE001
            log.warning("sync_time falló: %s", e)

    # ------------------------------------------------------------- mercado
    def contracts(self):
        """{symbol: info} de perpetuos USDT activos."""
        d = self.request("GET", "/openApi/swap/v2/quote/contracts")
        out = {}
        for c in d.get("data") or []:
            sym = c.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            if str(c.get("status", "1")) not in ("1", "1.0"):
                continue
            if str(c.get("apiStateOpen", "true")).lower() == "false":
                continue
            qp = int(_f(c.get("quantityPrecision"), 4))
            pp = int(_f(c.get("pricePrecision"), 4))
            tick = _f(c.get("tickSize")) or 10.0 ** (-pp)
            step = _f(c.get("stepSize")) or 10.0 ** (-qp)
            out[sym] = {
                "symbol": sym,
                "tick": tick,
                "step": step,
                "min_qty": _f(c.get("tradeMinQuantity"), step),
                "min_notional": _f(c.get("tradeMinUSDT"), 2.0),
                "max_lev": int(_f(c.get("maxLongLeverage"), 20) or 20),
            }
        return out

    def tickers(self):
        """{symbol: {last, chg, qvol, high, low, open, bid, ask}} de todos los perpetuos."""
        d = self.request("GET", "/openApi/swap/v2/quote/ticker")
        out = {}
        for t in d.get("data") or []:
            sym = t.get("symbol")
            if not sym:
                continue
            chg = str(t.get("priceChangePercent", "0")).replace("%", "")
            out[sym] = {
                "last": _f(t.get("lastPrice")),
                "chg": _f(chg),
                "qvol": _f(t.get("quoteVolume")),
                "high": _f(t.get("highPrice")),
                "low": _f(t.get("lowPrice")),
                "open": _f(t.get("openPrice")),
                "bid": _f(t.get("bidPrice")),
                "ask": _f(t.get("askPrice")),
            }
        return out

    def klines(self, symbol, interval, limit=300):
        """Velas ordenadas de antigua a reciente: dict de arrays numpy (t, o, h, l, c, v)."""
        d = self.request("GET", "/openApi/swap/v3/quote/klines",
                         {"symbol": symbol, "interval": interval, "limit": int(limit)})
        rows = {}
        for k in d.get("data") or []:
            try:
                rows[int(k["time"])] = (_f(k["open"]), _f(k["high"]), _f(k["low"]), _f(k["close"]), _f(k["volume"]))
            except (KeyError, TypeError, ValueError):
                continue
        ts = sorted(rows)
        arr = np.array([rows[t] for t in ts], dtype=float).reshape(-1, 5)
        return {
            "t": np.array(ts, dtype=np.int64),
            "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2], "c": arr[:, 3], "v": arr[:, 4],
        }

    # ------------------------------------------------------------- cuenta
    def balance(self):
        """{equity, available} en USDT. Soporta respuesta v3 (lista) y v2 (objeto)."""
        d = self.request("GET", "/openApi/swap/v3/user/balance", signed=True)
        data = d.get("data")
        if isinstance(data, dict):
            data = data.get("balance", data)
        if isinstance(data, list):
            pick = next((x for x in data if str(x.get("asset", "")).upper() in ("USDT", "VST")), None)
            data = pick or (data[0] if data else {})
        data = data or {}
        eq = _f(data.get("equity"), _f(data.get("balance")))
        av = _f(data.get("availableMargin"), eq)
        return {"equity": eq, "available": av, "upnl": _f(data.get("unrealizedProfit"))}

    def positions(self, symbol=None):
        d = self.request("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol}, signed=True)
        out = []
        for p in d.get("data") or []:
            amt = _f(p.get("positionAmt"))
            if amt == 0:
                continue
            ps = str(p.get("positionSide", "BOTH")).upper()
            side = ps if ps in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
            out.append({
                "symbol": p.get("symbol"), "side": side, "pos_side": ps, "qty": abs(amt),
                "entry": _f(p.get("avgPrice")), "mark": _f(p.get("markPrice")),
                "upnl": _f(p.get("unrealizedProfit")), "lev": _f(p.get("leverage")),
            })
        return out

    def open_orders(self, symbol=None):
        d = self.request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol}, signed=True)
        data = d.get("data") or {}
        orders = data.get("orders") if isinstance(data, dict) else data
        return orders or []

    def get_order(self, symbol, order_id):
        d = self.request("GET", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id}, signed=True)
        data = d.get("data") or {}
        return data.get("order", data) if isinstance(data, dict) else {}

    def position_mode(self):
        """True si la cuenta está en modo hedge (posición doble)."""
        d = self.request("GET", "/openApi/swap/v1/positionSide/dual", signed=True)
        return str((d.get("data") or {}).get("dualSidePosition", "false")).lower() == "true"

    def set_leverage(self, symbol, side, leverage):
        return self.request("POST", "/openApi/swap/v2/trade/leverage",
                            {"symbol": symbol, "side": side, "leverage": int(leverage)}, signed=True, retries=1)

    def set_margin_mode(self, symbol, mode):
        return self.request("POST", "/openApi/swap/v2/trade/marginType",
                            {"symbol": symbol, "marginType": mode}, signed=True, retries=1)

    # ------------------------------------------------------------- órdenes
    def place_order(self, symbol, side, position_side, quantity, order_type="MARKET", stop_price=None,
                    reduce_only=False, stop_loss=None, take_profit=None, working_type=None):
        """stop_loss / take_profit: stopPrice (float) -> se adjuntan a la orden como en ccxt."""
        p = {"symbol": symbol, "side": side, "positionSide": position_side, "type": order_type,
             "quantity": float(quantity)}
        if stop_price is not None:
            p["stopPrice"] = float(stop_price)
        if working_type:
            p["workingType"] = working_type
        if reduce_only:
            p["reduceOnly"] = "true"
        wt = working_type or config.SL_TRIGGER
        if stop_loss is not None:
            p["stopLoss"] = ('{"type":"STOP_MARKET","stopPrice":%s,"workingType":"%s","quantity":%s}'
                             % (num(float(stop_loss)), wt, num(float(quantity))))
        if take_profit is not None:
            p["takeProfit"] = ('{"type":"TAKE_PROFIT_MARKET","stopPrice":%s,"workingType":"%s","quantity":%s}'
                               % (num(float(take_profit)), wt, num(float(quantity))))
        d = self.request("POST", "/openApi/swap/v2/trade/order", p, signed=True, retries=1)
        data = d.get("data") or {}
        return data.get("order", data) if isinstance(data, dict) else {}

    def cancel_order(self, symbol, order_id):
        return self.request("DELETE", "/openApi/swap/v2/trade/order",
                            {"symbol": symbol, "orderId": order_id}, signed=True, retries=1)
