"""Escáner de todo el universo USDT-M de BingX -> ranking de monedas a punto de explotar."""
from __future__ import annotations

import logging
import random
import threading
import time
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
        self.last_scan = 0.0

    # ------------------------------------------------------------- universo
    def refresh_contracts(self, force=False):
        if force or not self.contracts or time.time() - self._contracts_ts > 3600:
            self.contracts = self.api.contracts()
            self._contracts_ts = time.time()

    def universe(self, tickers):
        rows = []
        for sym in self.contracts:
            base = sym.split("-")[0]
            if sym in config.EXCLUDE_SYMBOLS or base in config.EXCLUDE_SYMBOLS:
                continue
            if not config.INCLUDE_TRADFI and config.is_tradfi(sym):
                continue
            t = tickers.get(sym)
            if not t or t["last"] <= 0 or t["qvol"] < config.MIN_QUOTE_VOL:
                continue
            rows.append((t["qvol"], sym))
        rows.sort(reverse=True)
        return [s for _, s in rows[:config.MAX_SYMBOLS]]

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
        k = self.api.klines(sym, config.TIMEFRAME, config.KLINE_LIMIT)
        if len(k["t"]) < 150:
            return None
        return strategy.analyze(sym, k, self.get_daily(sym))

    def scan(self):
        t0 = time.time()
        self.refresh_contracts()
        tickers = self.api.tickers()
        syms = self.universe(tickers)
        setups, errors = [], 0
        with ThreadPoolExecutor(max_workers=max(1, config.WORKERS)) as ex:
            futs = {ex.submit(self._one, s): s for s in syms}
            for f, s in futs.items():
                try:
                    r = f.result()
                    if r is not None:
                        setups.append(r)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.debug("scan %s: %s", s, e)
        setups.sort(key=lambda x: x.score, reverse=True)
        watch = [s for s in setups if s.score >= config.MIN_SCORE][:config.WATCHLIST_SIZE]
        with self.lock:
            self.tickers = tickers
            self.watchlist = watch
            self.ranked = setups[:30]
            self.last_scan = time.time()
            self.stats = {"universe": len(syms), "setups": len(setups), "watch": len(watch),
                          "errors": errors, "secs": round(time.time() - t0, 1)}
        log.info("scan: %s", self.stats)
        return list(watch)

    def snapshot(self):
        with self.lock:
            return list(self.watchlist), list(self.ranked), dict(self.stats)
