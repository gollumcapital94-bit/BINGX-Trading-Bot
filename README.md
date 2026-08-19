# BingX Trading Bot — Pasos 1 a 13

Bot asincrónico para futuros perpetuos USDT-M en BingX, compuesto por dos módulos que corren en paralelo en cada ejecución:

## Paso 1 — Gestión de Take Profit

- Filtra solo pares de futuros perpetuos CRIPTO con USDT como referencia, excluyendo explícitamente el "Standard Futures" de BingX (forex, índices, commodities) — ver `_looks_like_forex_or_cfd()` en `bingx_bot.py`.
- Revisa tus posiciones abiertas.
- Si el precio ya tocó la EMA50 (1h), coloca el Take Profit en la EMA200 (1h).

## Pasos 2 a 13 — Escáner de nuevas entradas en corto

Para cada par SIN posición abierta:

| Paso | Qué valida |
|---|---|
| 2 | Round alcista: MA20(SMA) y EMA50 cruzaron de abajo hacia arriba la EMA200 (1h) |
| 3 | La última vela 1h cerrada es el máximo (mecha) de todo el round |
| 4 | RSI6 (1h) de la vela verde relevante no está sobrecomprado (>90 descarta) |
| 5 | El máximo en 15m debe estar en vela roja y coincidir con el máximo de 1h |
| 6 | El máximo en 5m debe estar en vela roja y coincidir con el máximo de 1h |
| 7-8 | W%R6, W%R12, W%R24 de esa vela roja son todos distintos entre sí (15m y 5m) |
| 9 | Volumen 24h > 2,000,000 USDT |
| 10-11 | Singularidad: S = ln(2k) - 1 > 0 en 15m (paso 10) y/o 5m (paso 11) — basta con que exista en UNA de las dos temporalidades (k = a/r o a/\|r\| según color de la vela 1h). Si existe en ambas, se trata igual que si fuera una sola |
| 12 | Apalancamiento = 80% ÷ (% de distancia entre el máximo y la SMA20 en 5m), redondeado por defecto a múltiplos de 5 y limitado al máximo de BingX. Margen = el MENOR entre el 10% del margen total de la cuenta (libre + usado en otras operaciones abiertas) y el margen máximo que permite ese apalancamiento en el par (nunca se reduce el apalancamiento para poder usar más margen) |
| 13 | Si el precio ya tocó la SMA20(5m): entrada a mercado. Si no: orden STOP en la SMA20(5m), esperando a que el precio la cruce de arriba hacia abajo |

Todas las posiciones se abren en margen aislado, en corto (short).

## Supuestos adoptados (confirmados con el usuario)

- MA20 = SMA20 (media simple).
- "Máximo" del round / de 15m / de 5m = el high (mecha) de la vela, no el cierre.
- El apalancamiento calculado se limita al máximo que permite BingX para ese par cuando el cálculo da un valor mayor.
- El margen total de la cuenta (Paso 12) se calcula como margen libre + margen ya usado en otras operaciones abiertas, no solo el saldo libre.

## Configuración

Sube estos archivos a tu repositorio manteniendo la estructura:

- `bingx_bot.py`
- `strategy_entries.py`
- `indicators.py`
- `requirements.txt`
- `.github/workflows/bingx-bot.yml`

En **Settings → Secrets and variables → Actions**, agrega:

- `BINGX_API_KEY`
- `BINGX_API_SECRET`
- `TELEGRAM_BOT_TOKEN` (token de tu bot de Telegram)
- `TELEGRAM_CHAT_ID` (chat_id de tu conversación PRIVADA con el bot — no de un grupo)

La Action corre cada 15 minutos por defecto (ajustable en el cron), se puede disparar manualmente desde **Actions → Run workflow**, o de forma externa vía `repository_dispatch` (por ejemplo, usando cronjob.org para mayor puntualidad). Cada entrada exitosa (a mercado o STOP) se notifica automáticamente por Telegram.

## 🧪 Probar en cuenta DEMO (VST) antes de ir a real

El bot soporta la cuenta demo de BingX (VST = Virtual USDT) usando el modo sandbox nativo de ccxt. Usa las mismas API keys de tu cuenta real; no hace falta generar credenciales separadas.

- **Ejecución manual:** ve a **Actions → BingX Trading Bot → Run workflow** y deja marcado el checkbox `demo_mode` (viene activado por defecto).
- **Ejecución programada (cron):** crea, en **Settings → Secrets and variables → Actions → Variables**, una variable llamada `BINGX_DEMO` con valor `true`. Cuando quieras pasar a producción, cámbiala a `false` (el workflow usa `true` por defecto si no configuras nada, como medida de seguridad).
- **Localmente:**

```bash
export BINGX_API_KEY="tu_api_key"
export BINGX_API_SECRET="tu_api_secret"
export BINGX_DEMO=true
python bingx_bot.py
```

Cuando `BINGX_DEMO=true`, el bot llama a `exchange.set_sandbox_mode(True)` justo después de crear la conexión, lo que redirige las peticiones de futuros al entorno VST de BingX (`open-api-vst.bingx.com`). Todo lo demás —lectura de posiciones, órdenes, indicadores— funciona igual que en producción, pero con dinero virtual.

Verifica en los logs que aparece `MODO DEMO (VST) ACTIVADO`, y confirma en la app/web de BingX (sección Demo Trading) que las órdenes y posiciones se reflejan ahí antes de pasar a real.

## 🐛 Bug corregido: entradas en pares de forex (NCFX)

En producción el bot llegó a abrir posiciones en `NCFXCAD2JPY/USDT:USDT` y `NCFXEURSGD2USD/USDT:USDT` (CAD/JPY y EUR/SGD), es decir, en el producto "Standard Futures" de BingX (forex/índices/commodities), no en futuros perpetuos cripto. Causa: ese producto usa exactamente los mismos flags `swap=True` / `linear=True` / `quote=USDT` / `settle=USDT` que ccxt reporta para los futuros cripto reales, así que el filtro original no los distinguía. Se corrigió excluyendo explícitamente cualquier símbolo con prefijo `NCFX` y cualquier símbolo cuya moneda base sea una divisa fiat (CAD, EUR, JPY, etc.), con una segunda verificación justo antes de ejecutar la orden como defensa adicional.

## ⚠️ Advertencias importantes

- Este bot abre posiciones apalancadas en corto de forma 100% automática, sin confirmación manual. Revisa cuidadosamente la lógica antes de usarlo con capital real.
- El API key debe tener permisos de lectura y trading de futuros (nunca de retiro de fondos).
- El bot fuerza automáticamente el **Hedge Mode** en la cuenta al arrancar (necesario porque todo el código usa `positionSide` LONG/SHORT). Si tienes posiciones abiertas al momento de correrlo por primera vez, ese cambio puede no aplicarse hasta que las cierres.
- BingX exige el parámetro `side` (LONG/SHORT/BOTH) en `set_leverage()`; el bot ya lo envía como `"SHORT"`, ya que la estrategia siempre abre en corto.
- El tipo de orden correcto para la entrada condicional (Paso 13, cuando el precio aún no tocó la SMA20) es `"STOP"` en la API de BingX (no `"STOP_LIMIT"`, que el exchange rechaza).
- Prueba primero en cuenta demo/testnet o con montos pequeños, y revisa tanto "Posiciones" como el "Historial de órdenes" en BingX — una orden STOP puede quedar "colocada" en el log sin llegar a ejecutarse si el precio nunca la cruza.
- El trading con apalancamiento conlleva riesgo real de pérdida total del capital, especialmente con una estrategia con tantas variables y ejecución automática.
- La interpretación de "máximo en 15m/5m" se hizo tomando como ventana de búsqueda la hora exacta de la vela 1h que se confirmó como máximo del round (Paso 3).
