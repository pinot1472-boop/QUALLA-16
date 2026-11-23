# OMNIX Explosion Bot — BingX · Telegram · Railway

Escanea **todos los perpetuos USDT de BingX** (con volumen 24h mínimo), puntúa de 0 a 100 qué monedas están
**a punto de explotar en el día, por arriba o por abajo**, avisa por Telegram y (opcional) opera en BingX con SL/TP,
breakeven y trailing. Usa la lógica de tu Pine *OMNIX Fusion* (Smart Trail, ADX, EP y Parabolic/PARA SHORT).

## Archivos (todo plano, sin carpetas)

| Archivo | Para qué |
|---|---|
| `main.py` | Arranque: hilo de escaneo + ciclo de disparo y gestión |
| `config.py` | Todas las variables de entorno y sus valores por defecto |
| `bingx.py` | Cliente REST de BingX (firma HMAC, límites de tasa) |
| `indicators.py` | ATR/RMA, DMI/ADX, EMA, Smart Trail (port del Pine) |
| `strategy.py` | Puntuación de "explosión", EP, Parabolic, confirmación del disparo |
| `scanner.py` | Recorre el universo en paralelo y arma la watchlist |
| `trader.py` | Tamaño por riesgo, órdenes, SL/TP verificados, breakeven, trailing |
| `tg.py` | Telegram (señales y comandos) |
| `requirements.txt`, `Procfile`, `railway.json` | Despliegue en Railway |
| `env.example` | Plantilla de variables |
| `fake_bingx.py`, `test_offline.py` | Prueba de humo sin red ni dinero (`python test_offline.py`) |

## Despliegue en Railway

1. Sube **todos estos archivos a la raíz** de un repo de GitHub (sin carpetas).
2. Railway → *New Project* → *Deploy from GitHub repo*. No necesita puerto: es un *worker*.
3. En *Variables* pon como mínimo `BINGX_API_KEY`, `BINGX_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (resto en `env.example`).
4. **Primer día con `AUTO_TRADE=false`**: solo señales. Cuando veas que el radar te convence, pon `AUTO_TRADE=true`.
   Puedes probar antes con la cuenta demo de BingX (`BINGX_DEMO=true`).
5. Clave de API de BingX: permisos de **lectura y Futures/trade**, **sin retiros**.
6. Opcional: volumen en Railway montado en `/data` y `STATE_FILE=/data/state.json` para conservar el estado entre redeploys.

## Cómo decide

**Score (0-100)** = compresión (30) + despertar de volumen (12) + ADX (8) + sesgo direccional (50).

- *Compresión*: ancho de Bollinger en percentil bajo (≈2 días), ATR corto/largo, velas en squeeze (Bollinger dentro de Keltner).
- *Volumen*: la media de las últimas 3 velas sube sobre las 40 previas.
- *ADX*: ADX 12-40 y subiendo (nace tendencia). Umbral fuerte 22 como en tu Pine.
- *Sesgo*: Smart Trail (ATR 10 × 3), EMAs 9/21, tendencia 1h, posición dentro de la caja, mínimos/máximos que se aprietan,
  cercanía al máximo/mínimo del día anterior, *Precise Entries* fuerte reciente (giro + EMAs + ADX>22 + volumen×1.4) y EP.
- *EP* (diario): cambio del día ≥ 7.5 % con ADR ≥ 3.5 % y sobre EMA21/50 (espejo para bajista) → suma puntos y permite setups menos comprimidos.
- *PARA SHORT* (diario): corrida ≥ 50 % en 10 días, ≥ 3 días verdes y ≥ 15 % sobre EMA20 que **se agrieta** → setup SHORT propio.
- Penaliza las que ya se movieron (vela > 2.2 ATR o muy extendidas): buscamos "a punto de", no "ya fue".

**Disparo**: ruptura del máximo/mínimo de las últimas 16 velas (+0.1 ATR) **y** volumen proyectado ≥ 1.3× la media,
spread ≤ 0.15 %, sin perseguir rupturas ya extendidas (> 0.8 ATR). Se comprueba cada 15 s sobre las 12 mejores.

**Operación**: entrada a mercado con SL y TP adjuntos en BingX. Riesgo fijo por operación (`RISK_PCT` del equity),
SL = estructura de la caja limitado a 1-2 ATR, TP a `TP_R` = 3R. A +1R el SL pasa a breakeven; desde +1.5R sube con el Smart Trail.
Si el SL no se puede colocar, **cierra la posición**. Cierre por tiempo si en 6 h no llegó a +0.5R.

**Protecciones**: máx. posiciones abiertas, máx. operaciones/día, pérdida diaria máxima (frena entradas hasta 00:00 UTC),
cooldown por símbolo.

## Telegram

- 🔭 *A punto de explotar*: setup nuevo con score alto (máx. 3 por escaneo).
- 📡 *Radar*: resumen de las mejores cada `DIGEST_MIN` minutos.
- 🎯/🚀 Ruptura confirmada / entrada ejecutada, 🛡 breakeven, 📈 trailing, ✅/🛑 cierre.
- Comandos `/status /top /pause /resume` solo con `TELEGRAM_COMMANDS=true` (usa un bot de Telegram dedicado: dos programas con el mismo token y `getUpdates` se pisan).

## Ajuste fino (variables)

`MIN_SCORE` / `ALERT_SCORE` / `TRADE_SCORE` (62/72/70) controlan cuántas señales ves y cuántas se operan; `MIN_QUOTE_VOL_USDT`
filtra liquidez; `LEVERAGE`, `RISK_PCT`, `TP_R`, `SL_ATR_MULT` el riesgo; los parámetros del Pine (`ATR_LEN`, `ATR_MULT`,
`ADX_THRESHOLD`, `MIN_CHANGE_EP`, `MIN_RUN_PCT`…) están en `config.py`.

## Avisos honestos

- Los umbrales no están optimizados con datos reales de BingX: pruébalos primero en modo señales/demo y ajústalos mirando el radar.
- Verificado con un BingX y un Telegram **simulados** (flujo completo en hedge y one-way, fallos y límites) y con la firma HMAC
  contrastada byte a byte con ccxt; no he podido probarlo contra la API real de BingX desde aquí.
- Operar futuros con apalancamiento puede perder todo el margen. Nada de esto es asesoramiento financiero.
