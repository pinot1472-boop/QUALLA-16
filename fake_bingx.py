"""Exchange BingX + Telegram simulados en local (para test_offline.py). No se usa en producción.

Valida la firma HMAC como lo hace BingX y emula sus reglas de hedge / one-way (reduceOnly).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import numpy as np

KEY, SECRET, TOKEN, CHAT = "fakekey", "fakesecret", "123:FAKE", "777"
BASE_MS = 900_000
N = 400


def make_series(kind, seed):
    """400 velas de 15m cerradas. 'squeeze_up' / 'squeeze_dn' = tendencia, compresión y volumen que despierta."""
    rng = np.random.default_rng(seed)
    px, rows = 100.0, []
    sign = {"squeeze_up": 1, "squeeze_dn": -1}.get(kind, 0)
    level = None
    for i in range(N):
        o = px
        if sign and i >= N - 70:                      # consolidación: cierres pegados al nivel, mechas grandes
            k = (i - (N - 70)) / 70.0
            level = (level or px) * (1 + sign * 0.00006)
            c = level * (1 + rng.normal(0, 0.0004 * (1 - k) + 0.0002))
            wick = 0.0022 * (1 - k) + 0.0010 * k
            h = max(o, c) * (1 + abs(rng.normal(wick * 0.6, wick * 0.3)))
            lo = min(o, c) * (1 - abs(rng.normal(wick * 0.6, wick * 0.3)))
            v = abs(rng.normal(600, 60))
            if i >= N - 3:                            # el volumen despierta y cierra pegado al extremo
                v = abs(rng.normal(1500, 60))
                c = o * (1 + sign * 0.0007)
                h, lo = max(o, c) * 1.0003, min(o, c) * 0.9997
        else:
            vol, drift = (0.0045, sign * 0.0008) if sign else (0.0055, 0.0)
            c = o * (1 + rng.normal(drift, vol))
            h = max(o, c) * (1 + abs(rng.normal(0, vol * 0.35)))
            lo = min(o, c) * (1 - abs(rng.normal(0, vol * 0.35)))
            v = abs(rng.normal(1000, 150))
        rows.append((o, h, lo, c, v))
        px = c
    return rows


def make_daily(seed, last_close, drift=0.004):
    """100 velas diarias que terminan en `last_close` (coherentes con las de 15m)."""
    rng = np.random.default_rng(seed)
    closes = [last_close]
    for _ in range(99):
        closes.append(closes[-1] / (1 + rng.normal(drift, 0.02)))
    closes.reverse()
    rows = []
    for j, c in enumerate(closes):
        o = closes[j - 1] if j else c / 1.003
        rows.append((o, max(o, c) * 1.03, min(o, c) * 0.97, c, 5e4))
    return rows


class World:
    def __init__(self, hedge=True):
        self.hedge = hedge
        self.lock = threading.RLock()
        self.bad_sig = 0
        self.calls = []
        self.telegram = []
        self.updates = []
        self.positions = {}
        self.orders = {}
        self.next_id = 1000
        self.prices = {}
        self.forming_vol = {}
        self.reject_stops = False
        self.no_attach = False
        self.equity = 1000.0
        self.series = {"SIG-USDT": make_series("squeeze_up", 11), "SHT-USDT": make_series("squeeze_dn", 12),
                       "RND-USDT": make_series("random", 13), "RN2-USDT": make_series("random", 14),
                       "LOW-USDT": make_series("random", 15), "NCSKTSLA2USD-USDT": make_series("random", 16)}
        self.daily = {s: make_daily(i, rows[-1][3]) for i, (s, rows) in enumerate(self.series.items())}
        self.qvol = {s: 5e7 for s in self.series}
        self.qvol["LOW-USDT"] = 1e5
        for s, rows in self.series.items():
            self.prices[s] = rows[-1][3]

    def oid(self):
        self.next_id += 1
        return self.next_id

    def contracts(self):
        return [{"symbol": s, "status": 1, "apiStateOpen": "true", "pricePrecision": 4, "quantityPrecision": 2,
                 "tradeMinQuantity": 0.01, "tradeMinUSDT": 2, "maxLongLeverage": 50} for s in self.series]

    def klines(self, sym, interval, limit):
        now = int(time.time() * 1000)
        if interval == "1d":
            day0 = now // 86_400_000 * 86_400_000
            rows = self.daily[sym]
            out = [{"time": day0 - (len(rows) - 1 - i) * 86_400_000, "open": o, "high": h, "low": l, "close": c,
                    "volume": v} for i, (o, h, l, c, v) in enumerate(rows)]
            return out[-limit:]
        last_open = now // BASE_MS * BASE_MS
        rows = self.series[sym]
        out = []
        for i, (o, h, l, c, v) in enumerate(rows):          # N velas cerradas
            out.append({"time": last_open - (len(rows) - i) * BASE_MS, "open": o, "high": h, "low": l,
                        "close": c, "volume": v})
        p = self.prices[sym]
        o = rows[-1][3]
        out.append({"time": last_open, "open": o, "high": max(o, p), "low": min(o, p), "close": p,
                    "volume": self.forming_vol.get(sym, 300.0)})
        return list(reversed(out[-limit:]))                  # BingX devuelve a veces la más reciente primero

    # ---- órdenes
    def add_order(self, sym, side, ps, typ, qty, stop=None, status="NEW"):
        oid = self.oid()
        self.orders[oid] = {"symbol": sym, "orderId": oid, "side": side, "positionSide": ps, "type": typ,
                            "origQty": str(qty), "stopPrice": str(stop) if stop is not None else "0",
                            "status": status, "avgPrice": "0"}
        return oid

    def fill_conditional(self, oid):
        """Simula que salta un SL/TP: cierra la posición y marca la orden FILLED."""
        with self.lock:
            o = self.orders[oid]
            o["status"], o["avgPrice"] = "FILLED", o["stopPrice"]
            for key in list(self.positions):
                if key[0] == o["symbol"]:
                    self.positions.pop(key)
            self.prices[o["symbol"]] = float(o["stopPrice"])

    def place(self, p):
        sym, side, ps, typ = p.get("symbol"), p.get("side"), p.get("positionSide"), p.get("type")
        qty = float(p.get("quantity", 0))
        ro = str(p.get("reduceOnly", "")).lower() == "true"
        if sym not in self.series:
            return {"code": 80014, "msg": "invalid symbol"}
        if self.hedge:
            if ps not in ("LONG", "SHORT"):
                return {"code": 109400, "msg": "In the Hedge mode, positionSide must be LONG or SHORT"}
            if "reduceOnly" in p:
                return {"code": 109400, "msg": "In the Hedge mode, the 'ReduceOnly' field can not be true."}
        elif ps != "BOTH":
            return {"code": 109400, "msg": "In the One-way mode, positionSide must be BOTH"}
        if typ == "MARKET":
            closing = (self.hedge and ((ps == "LONG" and side == "SELL") or (ps == "SHORT" and side == "BUY"))) or \
                      (not self.hedge and ro)
            if closing:
                for key in list(self.positions):
                    if key[0] == sym:
                        self.positions.pop(key)
                return {"code": 0, "data": {"order": {"symbol": sym, "orderId": self.add_order(sym, side, ps, typ, qty, status="FILLED")}}}
            if qty * self.prices[sym] / 5.0 > self.equity:
                return {"code": 101204, "msg": "Insufficient margin"}
            side_l = ("LONG" if side == "BUY" else "SHORT")
            self.positions[(sym, ps if self.hedge else side_l)] = {"qty": qty, "entry": self.prices[sym], "side": side_l, "ps": ps}
            oid = self.add_order(sym, side, ps, typ, qty, status="FILLED")
            if not self.no_attach:
                opp = "SELL" if side == "BUY" else "BUY"
                for key, t in (("stopLoss", "STOP_MARKET"), ("takeProfit", "TAKE_PROFIT_MARKET")):
                    if key in p:
                        d = json.loads(p[key])
                        assert d["type"] == t, d
                        self.add_order(sym, opp, ps, t, d.get("quantity", qty), d["stopPrice"])
            return {"code": 0, "data": {"order": {"symbol": sym, "orderId": oid, "side": side, "positionSide": ps}}}
        if typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if not self.hedge and not ro:
                return {"code": 109400, "msg": "One-way mode: conditional close orders need reduceOnly=true"}
            if self.reject_stops:
                return {"code": 80014, "msg": "stop order rejected (test)"}
            return {"code": 0, "data": {"order": {"symbol": sym, "orderId": self.add_order(sym, side, ps, typ, qty, p["stopPrice"])}}}
        return {"code": 80014, "msg": f"unsupported type {typ}"}

    def positions_payload(self, symbol=None):
        out = []
        for (sym, _), p in self.positions.items():
            if symbol and sym != symbol:
                continue
            amt = p["qty"] if (self.hedge or p["side"] == "LONG") else -p["qty"]
            out.append({"symbol": sym, "positionSide": p["ps"], "positionAmt": str(amt), "avgPrice": str(p["entry"]),
                        "markPrice": str(self.prices[sym]), "unrealizedProfit": "0", "leverage": 5})
        return out


def make_server(world):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method):
            u = urlsplit(self.path)
            path, q = u.path, dict(parse_qsl(u.query, keep_blank_values=True))
            n = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(n) if n else b""
            with world.lock:
                if path.startswith(f"/bot{TOKEN}/"):
                    return self._telegram(path.rsplit("/", 1)[1], raw_body)
                return self._bingx(method, path, q)

        def _telegram(self, name, raw):
            if name == "sendMessage":
                world.telegram.append(json.loads(raw)["text"])
                return self._send({"ok": True, "result": {}})
            if name == "getUpdates":
                ups, world.updates = world.updates, []
                if not ups:
                    world.lock.release()
                    try:
                        time.sleep(0.3)
                    finally:
                        world.lock.acquire()
                return self._send({"ok": True, "result": ups})
            return self._send({"ok": False}, 404)

        def _bingx(self, method, path, q):
            private = any(s in path for s in ("/user/", "/trade/", "/positionSide/"))
            if private:
                if self.headers.get("X-BX-APIKEY") != KEY:
                    return self._send({"code": 100413, "msg": "Incorrect apiKey"})
                sig = q.pop("signature", "")
                raw = "&".join(f"{k}={q[k]}" for k in sorted(q))
                good = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
                if sig != good:
                    world.bad_sig += 1
                    return self._send({"code": 100001, "msg": "Signature verification failed"})
            world.calls.append((method, path, dict(q)))
            if path.endswith("/server/time"):
                return self._send({"code": 0, "data": {"serverTime": int(time.time() * 1000)}})
            if path.endswith("/quote/contracts"):
                return self._send({"code": 0, "data": world.contracts()})
            if path.endswith("/quote/ticker"):
                data = [{"symbol": s, "lastPrice": str(world.prices[s]), "priceChangePercent": "1.50%",
                         "quoteVolume": str(world.qvol[s]), "highPrice": "1", "lowPrice": "1", "openPrice": "1",
                         "bidPrice": str(world.prices[s] * 0.99995), "askPrice": str(world.prices[s] * 1.00005)}
                        for s in world.series]
                return self._send({"code": 0, "data": data})
            if path.endswith("/quote/klines"):
                return self._send({"code": 0, "data": world.klines(q["symbol"], q["interval"], int(q.get("limit", 500)))})
            if path.endswith("/user/balance"):
                return self._send({"code": 0, "data": [{"asset": "USDT", "balance": str(world.equity),
                                                        "equity": str(world.equity), "unrealizedProfit": "0",
                                                        "availableMargin": str(world.equity * 0.9)}]})
            if path.endswith("/positionSide/dual"):
                return self._send({"code": 0, "data": {"dualSidePosition": "true" if world.hedge else "false"}})
            if path.endswith("/trade/leverage") or path.endswith("/trade/marginType"):
                return self._send({"code": 0, "data": {}})
            if path.endswith("/user/positions"):
                return self._send({"code": 0, "data": world.positions_payload(q.get("symbol"))})
            if path.endswith("/trade/openOrders"):
                data = [o for o in world.orders.values() if o["status"] == "NEW" and
                        (not q.get("symbol") or o["symbol"] == q["symbol"])]
                return self._send({"code": 0, "data": {"orders": data}})
            if path.endswith("/trade/order"):
                if method == "POST":
                    return self._send(world.place(q))
                oid = int(q.get("orderId", 0))
                o = world.orders.get(oid)
                if o is None:
                    return self._send({"code": 80016, "msg": "order not exist"})
                if method == "DELETE":
                    o["status"] = "CANCELLED"
                return self._send({"code": 0, "data": {"order": o}})
            return self._send({"code": 80014, "msg": f"unknown path {path}"}, 404)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return ThreadingHTTPServer(("127.0.0.1", 0), H)


def start(hedge=True):
    world = World(hedge)
    srv = make_server(world)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, world, f"http://127.0.0.1:{srv.server_address[1]}"
