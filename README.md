# BingX Trading Bot — Pasos 1 a 14

Bot asincrónico para futuros perpetuos USDT-M en BingX, compuesto por dos módulos que corren en paralelo en cada ejecución:

## Paso 1 — Gestión de Take Profit

- Filtra solo pares de futuros perpetuos CRIPTO con USDT como referencia, excluyendo explícitamente TODO el "Standard Futures" de BingX (forex, índices, commodities, acciones sintéticas) — ver `_looks_like_forex_or_cfd()` en `bingx_bot.py`.
- Revisa tus posiciones abiertas.
- Si el precio ya tocó la EMA50 (1h), coloca el Take Profit en la EMA200 (1h).

## Pasos 2 a 14 — Escáner de nuevas entradas en corto

Para cada par SIN posición abierta:

| Paso | Qué valida |
|---|---|
| 2 | Round alcista: MA20(SMA) y EMA50 cruzaron de abajo hacia arriba la EMA200 (1h) |
| 3 | La última vela 1h cerrada es el máximo (mecha) de todo el round |
| 4 | RSI6 (1h) de la vela verde relevante no está sobrecomprado (>90 descarta) |
| 5 | El máximo en 15m debe estar en vela roja y coincidir con el máximo de 1h |
| 6 | El máximo en 5m debe estar en vela roja y coincidir con el máximo de 1h |
| 7-8 | W%R6, W%R12, W%R24 de esa vela roja son todos distintos entre sí (15m y 5m) |
| 9 | Volumen 24h > 2,000,000 USDT (siempre del mercado real, ver más abajo) |
| 10 | SMA20 por encima de la EMA50, en temporalidad 4h (histórico fijo de 1200 velas). La EMA200(4h) ya NO participa en este filtro — ver detalle más abajo |
| 11-12 | Singularidad: S = ln(2k) - 1 > 0 en 15m (paso 11) y/o 5m (paso 12) — basta con que exista en UNA de las dos temporalidades (k = a/r o a/|r| según color de la vela 1h). Si existe en ambas, se trata igual que si fuera una sola |
| 13 | Apalancamiento = 80% ÷ (% de distancia). La distancia usa la SMA20(5m) si la entrada es STOP, o el precio actual si es a mercado (ver detalle más abajo), redondeado por defecto a múltiplos de 5 y limitado al máximo de BingX. Margen = el MENOR entre el 10% del margen total de la cuenta (libre + usado en otras operaciones abiertas) y el margen máximo que permite ese apalancamiento en el par (nunca se reduce el apalancamiento para poder usar más margen) |
| 14 | Si el precio ya tocó la SMA20(5m): entrada a mercado. Si no: orden STOP en la SMA20(5m), esperando a que el precio la cruce de arriba hacia abajo |

Todas las posiciones se abren en margen aislado, en corto (short).

## Supuestos adoptados (confirmados con el usuario)

- MA20 = SMA20 (media simple).
- "Máximo" del round / de 15m / de 5m = el high (mecha) de la vela, no el cierre.
- El apalancamiento calculado se limita al máximo que permite BingX para ese par cuando el cálculo da un valor mayor.
- El margen total de la cuenta (Paso 13) se calcula como margen libre + margen ya usado en otras operaciones abiertas, no solo el saldo libre.
- El Paso 10 (SMA20 vs EMA50 en 4h) se evalúa solo sobre la última vela 4h ya cerrada, con un histórico fijo de 1200 velas (mismo criterio de "calentamiento" que se usa para la EMA50/EMA200 en 1h). Su única condición es SMA20(4h) > EMA50(4h); no depende de la relación de ninguna de las dos con la EMA200(4h) — ver el cambio detallado más abajo.

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

## 📡 El bot SIEMPRE analiza el mercado REAL — el modo demo/real solo afecta a la cuenta

Todo el **análisis** del bot (Pasos 1 a 14 por igual, sin excepción) — la lista de pares válidos, las velas OHLCV de cualquier temporalidad (1h, 15m, 5m, 4h), los indicadores derivados de ellas, y el volumen 24h del Paso 9 — se calcula **siempre contra el mercado REAL** de BingX, sin importar si `BINGX_DEMO` está en `true` o `false`.

La variable `BINGX_DEMO` decide **únicamente** dónde ocurren las acciones que dependen de la **cuenta**:

- Leer posiciones abiertas y balance.
- Colocar el Take Profit y el Stop Loss (Paso 1).
- Configurar apalancamiento y margen, y ejecutar la orden de entrada (Pasos 13-14).

Motivo: un round alcista, un patrón técnico o el volumen real de un par no cambian porque el bot esté operando con dinero virtual. La cuenta VST (demo) tiene su propio historial y volumen simulados, que no reflejan el mercado real — usarlos para el análisis daría señales distintas a las que se generarían en producción, justo lo contrario de lo que se busca al probar en demo antes de ir a real.

En la práctica, esto se resuelve con dos conexiones a BingX cuando el bot corre en modo demo:

- **Conexión de mercado** (`market_exchange` en el código): siempre SIN sandbox, apunta a producción. Se usa para todo el escaneo y análisis (Pasos 1 a 14).
- **Conexión de cuenta** (`exchange` en el código): con sandbox activado si `BINGX_DEMO=true`. Se usa solo para posiciones, balance, TP/SL y la ejecución final de la orden.

En modo real, ambas conexiones son la misma instancia (no hace falta abrir una segunda).

## 🧪 Probar en cuenta DEMO (VST) antes de ir a real

El bot soporta la cuenta demo de BingX (VST = Virtual USDT) usando el modo sandbox nativo de ccxt para la conexión de cuenta (ver sección anterior). Usa las mismas API keys de tu cuenta real; no hace falta generar credenciales separadas.

- **Ejecución manual:** ve a **Actions → BingX Trading Bot → Run workflow** y deja marcado el checkbox `demo_mode` (viene activado por defecto).
- **Ejecución programada (cron):** crea, en **Settings → Secrets and variables → Actions → Variables**, una variable llamada `BINGX_DEMO` con valor `true`. Cuando quieras pasar a producción, cámbiala a `false` (el workflow usa `true` por defecto si no configuras nada, como medida de seguridad).
- **Localmente:**

```bash
export BINGX_API_KEY="tu_api_key"
export BINGX_API_SECRET="tu_api_secret"
export BINGX_DEMO=true
python bingx_bot.py
```

Cuando `BINGX_DEMO=true`, el bot llama a `exchange.set_sandbox_mode(True)` en la conexión de cuenta justo después de crearla, lo que redirige las peticiones de posiciones/órdenes/balance al entorno VST de BingX (`open-api-vst.bingx.com`), y abre además una conexión adicional sin sandbox dedicada a todo el análisis de mercado (ver sección anterior). Todo lo demás —lectura de posiciones, órdenes— funciona igual que en producción, pero con dinero virtual.

Verifica en los logs que aparece `MODO DEMO (VST) ACTIVADO`, y confirma en la app/web de BingX (sección Demo Trading) que las órdenes y posiciones se reflejan ahí antes de pasar a real.

## 🐛 Bug corregido: el bot no detectaba el toque de EMA50 si el precio ya se había alejado

El Paso 1 solo comparaba el high/low de la **última vela** contra el valor **actual** de la EMA50(1h). Si el precio tocaba la EMA50 en una vela ya cerrada y para la siguiente corrida del bot ya se había alejado, la señal se perdía y nunca se colocaban el Take Profit ni el Stop Loss.

Se corrigió para que el bot revise **todo el historial de velas cerradas** consultado, comparando cada vela contra el valor que tenía la EMA50 **en ese momento exacto** (la línea se mueve vela a vela). Basta con un solo toque en cualquier punto del historial — no importa si el precio ya se alejó después, la señal sigue siendo válida.

Como esta detección ahora puede "recordar" un toque de corridas anteriores, se agregó una verificación adicional: antes de crear el Take Profit o el Stop Loss, el bot revisa si ya existe una orden abierta de ese tipo para la posición, y si ya existe, no la vuelve a crear. Esto evita generar órdenes duplicadas en cada ejecución del cron mientras la posición siga abierta.

## 🐛 Bug corregido: BingX rechazaba TODAS las órdenes de Take Profit y Stop Loss

En producción, cada intento de colocar el Take Profit o el Stop Loss (Paso 1) fallaba con:

```
bingx {"code":109400,"msg":"In the Hedge mode, the 'ReduceOnly' field can not be filled.","data":{}}
```

Causa: el bot enviaba el parámetro `reduceOnly: true` junto con `positionSide` en las órdenes de cierre. En Hedge Mode, `positionSide` (LONG/SHORT) ya identifica sin ambigüedad que la orden cierra esa posición — BingX no solo no necesita `reduceOnly` en este modo, sino que **lo prohíbe explícitamente** si se envía. Se corrigió quitando `reduceOnly` de los parámetros de ambas órdenes (Take Profit y Stop Loss); el resto de la lógica (verificación de duplicados, break even, `workingType=CONTRACT_PRICE`) no cambia.

## ⏱️ Cron interno de GitHub dedicado solo a TP/SL (evita entradas duplicadas)

Además del disparador externo (cronjob.org → `repository_dispatch`, que corre el bot **completo**: Paso 1 + Pasos 2-14), el workflow incluye un cron **interno** de GitHub Actions que corre **cada hora** (`0 * * * *`) usando `schedule`, en sintonía con el timeframe 1h del Paso 1.

Ese cron interno corre el bot en modo `BOT_MODE=tp_only`: **solo** ejecuta el Paso 1 (gestión de Take Profit / Stop Loss sobre posiciones ya abiertas). **Nunca** escanea ni abre nuevas entradas (Pasos 2-14), precisamente para que no compita con tu disparador externo de cronjob.org y termine abriendo una posición duplicada.

El filtro de "solo futuros perpetuos cripto con USDT como referencia" (ver más abajo) es **una única función**, compartida por todo el script — se aplica igual sin importar qué disparador o `BOT_MODE` esté corriendo, tanto al Paso 1 como a los Pasos 2-14.

En resumen:

| Disparador | `BOT_MODE` | Qué corre |
|---|---|---|
| `schedule` (cron interno de GitHub, cada hora) | `tp_only` | Solo Paso 1 (TP/SL) |
| `repository_dispatch` (cronjob.org) | `full` | Paso 1 + Pasos 2-14 (TP/SL y nuevas entradas) |
| `workflow_dispatch` (manual) | `full` | Paso 1 + Pasos 2-14 (TP/SL y nuevas entradas) |

Si quieres cambiar la frecuencia del cron interno, ajusta la línea `cron: "0 * * * *"` en `.github/workflows/bingx-bot.yml` (formato estándar de cron; GitHub Actions no garantiza precisión exacta al minuto en horarios de alta demanda).

## 📊 El Paso 9 (volumen 24h) siempre usa el mercado REAL

El filtro de volumen del Paso 9 (`MIN_VOLUME_24H_USDT = 2,000,000`) consulta **siempre el volumen real del mercado** — este es un caso particular de la regla general explicada arriba ("El bot SIEMPRE analiza el mercado REAL"), que aplica a todo el análisis del bot, no solo a este paso.

## 📐 Apalancamiento (Paso 13): la distancia depende del tipo de entrada

La fórmula sigue siendo **80% ÷ distancia%**, pero la **distancia** usada cambia según cómo termine ejecutándose la entrada (Paso 14):

- **Orden STOP** (el precio aún no tocó la SMA20(5m)): distancia = máximo → **SMA20(5m)**. Fórmula original, sin cambios.
- **Entrada a MERCADO** (el precio ya tocó la SMA20(5m)): distancia = máximo → **precio actual** en el momento exacto de abrir la orden — **no** la SMA20.

Por esto, el cálculo de apalancamiento y margen (Paso 13) ya **no** se hace durante el escaneo (Pasos 2-12, en `strategy_entries.evaluate_symbol`), sino justo antes de ejecutar la orden (Paso 14, en `bingx_bot.execute_entry`): solo ahí se conoce el precio en vivo necesario para decidir qué tipo de entrada corresponde y, por lo tanto, qué distancia usar. El escaneo (Paso 13 "preliminar") solo deja preparados el máximo de 5m y el valor de la SMA20(5m); el apalancamiento y el margen se calculan con el mismo precio en vivo que decide si la orden es a mercado o STOP, para que ambos queden siempre alineados. Este precio en vivo, el apalancamiento máximo del par y el balance disponible se consultan contra la **cuenta** (demo o real, según `BINGX_DEMO`), ya que dependen de qué cuenta va a operar — es la única parte del bot donde el modo demo/real sí influye en el análisis, y solo porque es inseparable de la ejecución misma.

## 🔍 Diagnóstico: resumen de descartes por paso

Cada corrida del escáner de entradas (Pasos 2-14) ahora termina con una línea de resumen en el log:

```
[Pasos 2-14] Resumen de descartes por paso -> Paso 2/3: 340, Paso 4: 12, Paso 9: 5, Paso 10: 48, Paso 12: 3
```

Esto permite ver de un vistazo en qué paso se está cayendo la mayoría de los candidatos, sin tener que leer el log completo símbolo por símbolo — útil sobre todo para confirmar si un filtro nuevo (como el Paso 10) está funcionando como se espera o si algo se está descartando de forma anómala.

También se agregaron dos logs que antes no existían:
- `[Paso 10]` ahora deja rastro tanto cuando **aprueba** (mostrando los valores de SMA20/EMA50 en 4h) como cuando descarta — antes solo se veía el rechazo.
- Un log de diagnóstico muestra cuántas velas de 4h devolvió realmente BingX frente a las 1200 solicitadas, para descartar que la API esté truncando el histórico silenciosamente.

Si en algún momento el resumen muestra que el 100% de los candidatos se cae en un mismo paso (por ejemplo, "Paso 10: 400" cuando había 400 candidatos), eso es una señal fuerte de revisar ese paso específico — ya sea porque el filtro es legítimamente muy estricto para las condiciones actuales del mercado, o porque hay un problema real que vale la pena investigar con esos números en la mano.

## 🔧 Cambio de lógica: Paso 10 simplificado (ya no exige relación con la EMA200 en 4h)

**Antes**, el Paso 10 exigía DOS condiciones a la vez en la última vela 4h cerrada:
1. SMA20(4h) > EMA50(4h)
2. Y ADEMÁS, ambas (SMA20 y EMA50) por DEBAJO de la EMA200(4h)

En la práctica, ese segundo requisito resultó ser demasiado estricto: se confirmó con capturas reales del gráfico de BingX en 4h (ETHFIUSDT y SPACEUSDT) que, en rounds alcistas donde el precio ya recorrió bastante camino, la EMA50 y/o la SMA20 pueden alcanzar o incluso cruzar por encima de la EMA200 mientras la SMA20 se mantiene por encima de la EMA50 — es decir, la señal de tendencia (SMA20 > EMA50) seguía siendo válida, pero el filtro la descartaba igual solo por la posición relativa con la EMA200.

**Ahora**, el Paso 10 valida una única condición:

- SMA20(4h) > EMA50(4h)

La EMA200(4h) se sigue calculando y se sigue mostrando en los logs de diagnóstico (aprobación y descarte), pero ya **no** participa en la decisión de aprobar o descartar el símbolo. El histórico fijo de 1200 velas de 4h (con el mínimo de 210 velas cerradas para el cálculo) no cambia, porque la EMA200 se sigue necesitando para el log informativo.

## 🐛 Bug corregido: entradas en pares de forex y acciones sintéticas (NCFX / NCSK)

En producción el bot llegó a incluir en su universo de símbolos pares como `NCFXCAD2JPY/USDT:USDT` y `NCFXEURSGD2USD/USDT:USDT` (CAD/JPY y EUR/SGD, forex), y más adelante también `NCSKAPP2USD/USDT:USDT` y `NCSKBRKB2USD/USDT:USDT` (acciones sintéticas de AppLovin y Berkshire Hathaway B "contra USD") — es decir, en el producto "Standard Futures" de BingX (forex, acciones sintéticas, índices, commodities), no en futuros perpetuos cripto. Causa: ese producto usa exactamente los mismos flags `swap=True` / `linear=True` / `quote=USDT` / `settle=USDT` que ccxt reporta para los futuros cripto reales, así que el filtro original no los distinguía, y el primer fix (que solo buscaba el prefijo `NCFX`) no cubría el nuevo subtipo `NCSK`.

Se corrigió generalizando el filtro con dos señales independientes: (1) el prefijo del id se amplió de `NCFX` a `NC`, cubriendo cualquier subtipo de este producto, actual o futuro; y (2) independientemente del prefijo, se descarta cualquier símbolo cuya moneda base siga el patrón `SUBYACENTE2REFERENCIA` (por ejemplo `EUR2USD`, `APP2USD`, `BRKB2USD`) o sea directamente una divisa fiat (CAD, EUR, JPY, etc.) — ningún futuro perpetuo cripto real usa ese patrón de nombre ni tiene una fiat como base. Esta función (`_looks_like_forex_or_cfd`) es **una sola** y se usa para construir el universo de símbolos del bot completo (Pasos 1 a 14), además de una segunda verificación justo antes de ejecutar la orden como defensa adicional.

## ⚠️ Advertencias importantes

- Este bot abre posiciones apalancadas en corto de forma 100% automática, sin confirmación manual. Revisa cuidadosamente la lógica antes de usarlo con capital real.
- El API key debe tener permisos de lectura y trading de futuros (nunca de retiro de fondos).
- El bot fuerza automáticamente el **Hedge Mode** en la cuenta al arrancar (necesario porque todo el código usa `positionSide` LONG/SHORT). Si tienes posiciones abiertas al momento de correrlo por primera vez, ese cambio puede no aplicarse hasta que las cierres.
- BingX exige el parámetro `side` (LONG/SHORT/BOTH) en `set_leverage()`; el bot ya lo envía como `"SHORT"`, ya que la estrategia siempre abre en corto.
- El tipo de orden correcto para la entrada condicional (Paso 14, cuando el precio aún no tocó la SMA20) es `"STOP"` en la API de BingX (no `"STOP_LIMIT"`, que el exchange rechaza).
- En Hedge Mode, BingX rechaza el campo `reduceOnly` en las órdenes de cierre (error 109400) — no debe enviarse; `positionSide` ya es suficiente para identificar que la orden cierra la posición.
- Prueba primero en cuenta demo/testnet o con montos pequeños, y revisa tanto "Posiciones" como el "Historial de órdenes" en BingX — una orden STOP puede quedar "colocada" en el log sin llegar a ejecutarse si el precio nunca la cruza.
- El trading con apalancamiento conlleva riesgo real de pérdida total del capital, especialmente con una estrategia con tantas variables y ejecución automática.
- La interpretación de "máximo en 15m/5m" se hizo tomando como ventana de búsqueda la hora exacta de la vela 1h que se confirmó como máximo del round (Paso 3).
