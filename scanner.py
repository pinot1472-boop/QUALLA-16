"""Escáner de todo el universo USDT-M de BingX -> ranking de monedas a punto de explotar."""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import config
import strategy

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self, api):
        self.api = api
        self.contracts = {}
        self._contracts_ts = 0.0
        self._daily = {}                 # sym -> (expira, día_utc, velas)
        self.lock = threading.Lock()
        self.watchlist = []              # Setups con score >= MIN_SCORE (los mejores WATCHLIST_SIZE)
        self.ranked = []                 # top 30 para /top
        self.tickers = {}
        self.stats = {}
        self.funnel = {}
        self.last_scan = 0.0

    # ------------------------------------------------------------- universo
    def refresh_contracts(self, force=False):
        if force or not self.contracts or time.time() - self._contracts_ts > 3600:
            self.contracts = self.api.contracts()
            self._contracts_ts = time.time()

    def universe(self, tickers):
        """Monedas a analizar. Deja en self.funnel cuántas se descartan en cada filtro."""
        f = Counter()
        f["contratos"] = len(self.contracts)
        rows = []
        for sym in self.contracts:
            base = sym.split("-")[0]
            if sym in config.EXCLUDE_SYMBOLS or base in config.EXCLUDE_SYMBOLS:
                f["excluidas"] += 1
                continue
            if not config.INCLUDE_TRADFI and config.is_tradfi(sym):
                f["tradfi"] += 1
                continue
            t = tickers.get(sym)
            if not t or t["last"] <= 0:
                f["sin_ticker"] += 1
                continue
            if t["qvol"] < config.MIN_QUOTE_VOL:
                f["poco_volumen"] += 1
                continue
            rows.append((t["qvol"], sym))
        rows.sort(reverse=True)
        cap = config.MAX_SYMBOLS
        if cap > 0 and len(rows) > cap:
            f["recortadas_por_tope"] = len(rows) - cap
            rows = rows[:cap]
        f["analizables"] = len(rows)
        self.funnel = dict(f)
        return [s for _, s in rows]

    def get_daily(self, sym):
        day = int(time.time() // 86400)
        hit = self._daily.get(sym)
        if hit and hit[0] > time.time() and hit[1] == day:
            return hit[2]
        try:
            d = self.api.klines(sym, "1d", 100)
        except Exception as e:  # noqa: BLE001
            log.debug("daily %s: %s", sym, e)
            return hit[2] if hit else None
        self._daily[sym] = (time.time() + 6 * 3600 + random.uniform(0, 3600), day, d)
        return d

    # ------------------------------------------------------------- escaneo
    def _one(self, sym):
        """-> (Setup | None, dict con el motivo de descarte)."""
        why = {}
        k = self.api.klines(sym, config.TIMEFRAME, config.KLINE_LIMIT)
        if len(k["t"]) < 150:
            why["reason"] = "pocas_velas"
            return None, why
        return strategy.analyze(sym, k, self.get_daily(sym), why=why), why

    def scan(self):
        t0 = time.time()
        self.refresh_contracts()
        tickers = self.api.tickers()
        syms = self.universe(tickers)
        setups, errors = [], 0
        reasons, near, no_daily = Counter(), [], 0
        with ThreadPoolExecutor(max_workers=max(1, config.WORKERS)) as ex:
            futs = {ex.submit(self._one, s): s for s in syms}
            for f, s in futs.items():
                try:
                    r, why = f.result()
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.debug("scan %s: %s", s, e)
                    continue
                no_daily += 1 if why.get("sin_diario") else 0
                if r is not None:
                    setups.append(r)
                    continue
                reasons[why.get("reason", "?")] += 1
                if "score" in why:                      # casi-candidatas: puntuadas pero sin setup
                    near.append((why["score"], s, why.get("side", "?"), why.get("reason", "?")))
        setups.sort(key=lambda x: x.score, reverse=True)
        near.sort(reverse=True)
        watch = [s for s in setups if s.score >= config.MIN_SCORE][:config.WATCHLIST_SIZE]
        with self.lock:
            self.tickers = tickers
            self.watchlist = watch
            self.ranked = setups[:30]
            self.last_scan = time.time()
            self.stats = {"universe": len(syms), "setups": len(setups), "watch": len(watch),
                          "errors": errors, "secs": round(time.time() - t0, 1),
                          "funnel": dict(self.funnel), "descartes": dict(reasons),
                          "sin_diario": no_daily, "near": near[:5]}
        log.info("scan: universe=%d setups=%d watch=%d errors=%d secs=%.1f", len(syms), len(setups), len(watch),
                 errors, self.stats["secs"])
        log.info("scan filtros universo: %s", self.funnel)
        log.info("scan descartes: %s%s", dict(reasons), f" · sin velas diarias: {no_daily}" if no_daily else "")
        if near:
            log.info("scan más cerca: %s", ", ".join(f"{sym} {sc:.0f} {side} ({why})" for sc, sym, side, why in near[:5]))
        if self.funnel.get("recortadas_por_tope"):
            log.warning("universo recortado: MAX_SYMBOLS=%s deja fuera %d monedas (pon MAX_SYMBOLS=0 para analizarlas todas)",
                        config.MAX_SYMBOLS, self.funnel["recortadas_por_tope"])
        elif len(syms) < 100 and self.funnel.get("poco_volumen", 0) > len(syms):
            log.warning("universo pequeño (%d): MIN_QUOTE_VOL_USDT=%s descarta %d monedas por volumen",
                        len(syms), f"{config.MIN_QUOTE_VOL:,.0f}", self.funnel["poco_volumen"])
        return list(watch)

    def snapshot(self):
        with self.lock:
            return list(self.watchlist), list(self.ranked), dict(self.stats)
