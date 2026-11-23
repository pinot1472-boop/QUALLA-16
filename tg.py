"""Telegram: envío de señales (HTML) y comandos básicos por long-polling."""
from __future__ import annotations

import html
import logging
import threading
import time

import requests

import config

log = logging.getLogger("tg")
esc = html.escape


class Telegram:
    def __init__(self, token=None, chat_id=None, base=None):
        self.token = token if token is not None else config.TELEGRAM_TOKEN
        self.chat_id = str(chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID)
        self.base = (base or config.TELEGRAM_BASE_URL).rstrip("/")
        self.enabled = bool(self.token and self.chat_id)
        self._last_err = {}
        self._stop = threading.Event()
        if not self.enabled:
            log.warning("Telegram sin configurar (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): solo logs")

    def _url(self, method):
        return f"{self.base}/bot{self.token}/{method}"

    def send(self, text: str, silent: bool = False) -> bool:
        if not self.enabled:
            log.info("[TG-off] %s", text.replace("\n", " | ")[:300])
            return False
        ok = True
        for chunk in self._split(text):
            ok = self._send_chunk(chunk, silent) and ok
        return ok

    @staticmethod
    def _split(text, limit=3900):
        if len(text) <= limit:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > limit:
                parts.append(cur)
                cur = ""
            cur += line + "\n"
        if cur:
            parts.append(cur)
        return parts

    def _send_chunk(self, text, silent):
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": silent}
        for attempt in range(3):
            try:
                r = requests.post(self._url("sendMessage"), json=payload, timeout=15)
                if r.status_code == 429:
                    wait = 3
                    try:
                        wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(min(wait, 30) + 0.5)
                    continue
                if r.status_code == 400 and "parse" in r.text.lower():
                    payload.pop("parse_mode", None)       # reintenta sin HTML
                    continue
                if r.ok:
                    return True
                log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.warning("Telegram error: %s", e)
            time.sleep(1.5 * (attempt + 1))
        return False

    def error(self, key: str, text: str, every: int = 600):
        """Aviso de error con antispam por clave."""
        now = time.time()
        if now - self._last_err.get(key, 0) < every:
            log.warning("(antispam) %s", text)
            return
        self._last_err[key] = now
        self.send("⚠️ " + text)

    # ------------------------------------------------------------- comandos
    def start_polling(self, handler):
        if not self.enabled:
            return
        threading.Thread(target=self._poll, args=(handler,), daemon=True, name="tg-poll").start()

    def stop(self):
        self._stop.set()

    def _poll(self, handler):
        offset = None
        while not self._stop.is_set():
            try:
                params = {"timeout": 25, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                r = requests.get(self._url("getUpdates"), params=params, timeout=35)
                data = r.json() if r.ok else {}
                for u in data.get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                        continue                              # solo el dueño
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/"):
                        try:
                            reply = handler(text)
                        except Exception as e:  # noqa: BLE001
                            reply = f"Error: {e}"
                        if reply:
                            self.send(reply)
                if not r.ok:
                    time.sleep(5)
            except Exception as e:  # noqa: BLE001
                log.debug("poll error: %s", e)
                time.sleep(5)
