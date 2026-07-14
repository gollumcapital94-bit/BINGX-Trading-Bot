# BingX Trading Bot — Pasos 1 a 13

Bot asincrónico para futuros perpetuos USDT-M en BingX, compuesto por
dos módulos que corren en paralelo en cada ejecución:

## Paso 1 — Gestión de Take Profit
- Filtra **solo** pares de futuros perpetuos con USDT como referencia
  (descarta CFDs de acciones, forex, commodities e índices).
- Revisa tus posiciones abiertas.
- Si el precio ya tocó la EMA50 (1h), coloca el Take Profit en la EMA200 (1h).

## Pasos 2 a 13 — Escáner de nuevas entradas en corto
Para cada par SIN posición abierta:

| Paso | Qué valida |
|------|------------|
| 2 | Round alcista: MA20(SMA) y EMA50 cruzaron de abajo hacia arriba la EMA200 (1h) |
| 3 | La última vela 1h cerrada es el máximo (mecha) de todo el round |
| 4 | RSI6 (1h) de la vela verde relevante no está sobrecomprado (>90 descarta) |
| 5 | El máximo en 15m **y** en 5m debe estar en vela roja |
| 6-7 | W%R6, W%R12, W%R24 de esa vela roja son todos distintos entre sí (15m y 5m) |
| 8-9 | \|RSI6\| > \|W%R6\| en esa vela roja (15m y 5m) |
| 10 | Volumen 24h > 2,000,000 USDT |
| 11 | Singularidad: S = ln(2k) - 1 > 0 en 15m y/o 5m (k = a/r o a/\|r\| según color de la vela 1h) |
| 12 | Cálculo de apalancamiento (fórmula 90% / distancia a MA20 5m, redondeado a múltiplos de 5, limitado al máximo de BingX) y margen (mín. entre 20% de la cuenta y máximo permitido por el apalancamiento) |
| 13 | Si el precio ya tocó la MA20(5m): entrada a mercado. Si no: orden STOP-LIMIT en la MA20(5m) |

Todas las posiciones se abren en **margen aislado**, en corto (short).

## Supuestos adoptados (confirmados con el usuario)
- `MA20` = SMA20 (media simple).
- "Máximo" del round / de 15m / de 5m = el **high (mecha)** de la vela, no el cierre.
- El apalancamiento calculado se **limita** al máximo que permite BingX
  para ese par cuando el cálculo da un valor mayor.

## Configuración

1. Sube estos archivos a tu repositorio manteniendo la estructura:
   - `bingx_bot.py`
   - `strategy_entries.py`
   - `indicators.py`
   - `requirements.txt`
   - `.github/workflows/bingx-bot.yml`
2. En **Settings → Secrets and variables → Actions**, agrega:
   - `BINGX_API_KEY`
   - `BINGX_API_SECRET`
3. La Action corre cada 15 minutos por defecto (ajustable en el `cron`)
   o se puede disparar manualmente desde **Actions → Run workflow**.

## 🧪 Probar en cuenta DEMO (VST) antes de ir a real

El bot soporta la cuenta demo de BingX (VST = Virtual USDT) usando el
modo *sandbox* nativo de ccxt. **Usa las mismas API keys** de tu cuenta
real; no hace falta generar credenciales separadas.

- **Ejecución manual**: ve a **Actions → BingX Trading Bot → Run workflow**
  y deja marcado el checkbox `demo_mode` (viene activado por defecto).
- **Ejecución programada (cron)**: crea, en **Settings → Secrets and
  variables → Actions → Variables**, una variable llamada `BINGX_DEMO`
  con valor `true`. Cuando quieras pasar a producción, cámbiala a
  `false` (el workflow usa `true` por defecto si no configuras nada,
  como medida de seguridad).
- **Localmente**:
  ```bash
  export BINGX_API_KEY="tu_api_key"
  export BINGX_API_SECRET="tu_api_secret"
  export BINGX_DEMO=true
  python bingx_bot.py
  ```

Cuando `BINGX_DEMO=true`, el bot llama a `exchange.set_sandbox_mode(True)`
justo después de crear la conexión, lo que redirige las peticiones de
futuros al entorno VST de BingX (`open-api-vst.bingx.com`). Todo lo
demás —lectura de posiciones, órdenes, indicadores— funciona igual que
en producción, pero con dinero virtual.

Verifica en los logs que aparece `MODO DEMO (VST) ACTIVADO`, y confirma
en la app/web de BingX (sección Demo Trading) que las órdenes y
posiciones se reflejan ahí antes de pasar a real.

## ⚠️ Advertencias importantes

- **Este bot abre posiciones apalancadas en corto de forma 100% automática**,
  sin confirmación manual. Revisa cuidadosamente la lógica antes de usarlo
  con capital real.
- El API key debe tener permisos de **lectura y trading de futuros**
  (nunca de retiro de fondos).
- Los nombres exactos de parámetros para órdenes `STOP_LIMIT` y
  `TAKE_PROFIT_MARKET` pueden variar según la versión de ccxt/BingX;
  si el exchange rechaza una orden, revisa el mensaje de error y ajusta
  los `params` en `bingx_bot.py`.
- Prueba primero en cuenta demo/testnet o con montos pequeños. El
  trading con apalancamiento conlleva riesgo real de pérdida total del
  capital, especialmente con una estrategia con tantas variables y
  ejecución automática.
- La interpretación de "máximo en 15m/5m" se hizo tomando como ventana
  de búsqueda el rango desde el inicio del round (1h) hasta la vela
  actual; si tu intención era otra ventana, dímelo y lo ajusto.
