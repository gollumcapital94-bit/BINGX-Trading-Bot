# BingX Trading Bot — Pasos 1 a 14

Bot asincrónico para futuros perpetuos USDT-M en BingX, compuesto por dos módulos que corren en paralelo en cada ejecución:

## Paso 1 — Gestión de Take Profit y Stop Loss (DOS gatillos independientes)

- Filtra solo pares de futuros perpetuos CRIPTO con USDT como referencia, excluyendo explícitamente TODO el "Standard Futures" de BingX (forex, índices, commodities, acciones sintéticas) — ver `_looks_like_forex_or_cfd()` en `bingx_bot.py`.
- Revisa tus posiciones abiertas.
- **Gatillo 1 — Take Profit:** si el precio toca la **SMA20 (1h)**, coloca el Take Profit en la EMA200(1h).
- **Gatillo 2 — Stop Loss:** si el precio toca la **EMA50 (1h)**, coloca el Stop Loss en break even (precio de entrada exacto).

Estos dos gatillos son **completamente independientes** entre sí: uno puede dispararse sin que el otro lo haya hecho todavía. Por ejemplo, si el precio tocó la SMA20 pero aún no la EMA50, existirá un Take Profit sin Stop Loss; si luego toca la EMA50, se agrega el Stop Loss por separado.

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
| 10 | SMA20 por encima de la EMA50, en temporalidad 4h. Si además ambas están por encima de la EMA200(4h), se exige que la SMA20 nunca haya retrocedido por debajo de la EMA50 desde que cruzó la EMA200 — ver el detalle completo más abajo |
| 11-12 | Singularidad: S = ln(2k) - 1 > 0 en 15m (paso 11) y/o 5m (paso 12) — basta con que exista en UNA de las dos temporalidades (k = a/r o a/|r| según color de la vela 1h). Si existe en ambas, se trata igual que si fuera una sola |
| 13 | Apalancamiento = 80% ÷ (% de distancia). La distancia usa la SMA20(5m) si la entrada es STOP, o el precio actual si es a mercado (ver detalle más abajo), redondeado por defecto a múltiplos de 5 y limitado al máximo de BingX. Margen = el MENOR entre el 10% del margen total de la cuenta (libre + usado en otras operaciones abiertas) y el margen máximo que permite ese apalancamiento en el par (nunca se reduce el apalancamiento para poder usar más margen) |
| 14 | Si el precio ya tocó la SMA20(5m): entrada a mercado. Si no: orden STOP en la SMA20(5m), esperando a que el precio la cruce de arriba hacia abajo |

Todas las posiciones se abren en margen aislado, en corto (short).

## Supuestos adoptados (confirmados con el usuario)

- MA20 = SMA20 (media simple).
- "Máximo" del round / de 15m / de 5m = el high (mecha) de la vela, no el cierre.
- El apalancamiento calculado se limita al máximo que permite BingX para ese par cuando el cálculo da un valor mayor.
- El margen total de la cuenta (Paso 13) se calcula como margen libre + margen ya usado en otras operaciones abiertas, no solo el saldo libre.
- El Paso 10 (SMA20 vs EMA50 en 4h) se evalúa solo sobre la última vela 4h ya cerrada, con un histórico fijo de 1200 velas.
- Chequeo de retroceso del Paso 10 (ver detalle más abajo): si dentro de las 1200 velas de 4h consultadas nunca se encuentra el cruce de la SMA20 sobre la EMA200 (por ejemplo, la SMA20 ya empezaba por encima desde el inicio del historial disponible), se asume que no hay evidencia de un retroceso y se aprueba el short con normalidad.

## Configuración

Sube estos archivos a tu repositorio manteniendo la estructura:

- `bingx_bot.py`
- `strategy_entries.py`
- `indicators.py`
- `long_candidates.py`
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

## 🐛 Bug corregido: la EMA/SMA de calentamiento generaba toques falsos (visto en SXTUSDT)

El Paso 1 revisa todo el historial de velas cerradas consultado, comparando cada vela contra el valor que tenía la línea (SMA20 o EMA50) **en ese momento exacto**. El problema: la EMA/SMA de pandas se "siembra" con el primer cierre de la ventana pedida y tarda varias decenas/centenas de velas en converger al valor real (el que calcula BingX con todo el historial del par desde su listado). Mientras no converge, la línea calculada puede estar visiblemente "torcida" respecto al gráfico real — y como la búsqueda de toques revisaba **todo** el historial pedido, incluidas esas primeras velas mal calentadas, un toque falso ahí podía disparar el Take Profit o el Stop Loss sin que el precio hubiera tocado de verdad la línea que se ve en BingX. Esto ocurrió en producción con SXTUSDT: el Stop Loss se colocó en break even antes de que el precio tocara realmente la EMA50 en el gráfico.

Se corrigió pidiendo `EMA_WARMUP_CANDLES` (400) velas EXTRA, **antes** de la ventana donde de verdad se busca un toque: esas velas de más se usan para que la SMA20/EMA50/EMA200 converjan correctamente, pero se **descartan** a la hora de buscar la señal — nunca cuentan como un toque válido.

## 🔀 Cambio de lógica: Paso 1 ahora tiene DOS gatillos independientes (antes uno solo disparaba ambas órdenes)

**Antes**, un único toque de la EMA50(1h) disparaba a la vez el Take Profit (en la EMA200) y el Stop Loss (en break even).

**Ahora**, cada orden tiene su propio gatillo, evaluado por separado:

- Toque de la **SMA20(1h)** → coloca el **Take Profit** (en la EMA200(1h), sin cambios en dónde se coloca).
- Toque de la **EMA50(1h)** → coloca el **Stop Loss** en break even (sin cambios en cómo se coloca: tipo `STOP` límite, ver el bug de TAUSDT más abajo).

Como la SMA20 suele tocarse antes que la EMA50 en un retroceso normal, en la práctica esto adelanta el momento en que se asegura el Take Profit, y deja el Stop Loss en break even para un retroceso más profundo. Cada gatillo tiene su propio chequeo anti-duplicados (uno para `TAKE_PROFIT_MARKET`, otro para `STOP`), así que uno puede existir sin el otro sin generar conflictos.

## 🐛 Bug corregido: BingX rechazaba TODAS las órdenes de Take Profit y Stop Loss

En producción, cada intento de colocar el Take Profit o el Stop Loss (Paso 1) fallaba con:

```
bingx {"code":109400,"msg":"In the Hedge mode, the 'ReduceOnly' field can not be filled.","data":{}}
```

Causa: el bot enviaba el parámetro `reduceOnly: true` junto con `positionSide` en las órdenes de cierre. En Hedge Mode, `positionSide` (LONG/SHORT) ya identifica sin ambigüedad que la orden cierra esa posición — BingX no solo no necesita `reduceOnly` en este modo, sino que **lo prohíbe explícitamente** si se envía. Se corrigió quitando `reduceOnly` de los parámetros de ambas órdenes (Take Profit y Stop Loss); el resto de la lógica (verificación de duplicados, break even, `workingType=CONTRACT_PRICE`) no cambia.

## 🐛 Bug corregido: el Stop Loss en break even cerraba con pérdida por slippage (STOP_MARKET → STOP)

En producción se detectó un caso real (TAUSDT, 45x) donde el Stop Loss en break even se ejecutó con **-40.67% de pérdida sobre el margen**, en vez de cerrar en cero como se esperaba.

Causa: el SL usaba el tipo de orden `STOP_MARKET`. Este tipo se dispara correctamente cuando el precio último toca `stopPrice=entry_price`, pero **una vez disparado se convierte en una orden A MERCADO**, cuya ejecución no está garantizada exactamente en ese precio — se llena al primer precio disponible, con el slippage que haya en ese instante. En el caso real, el precio solo se movió ~0.9% de más entre el disparo y el llenado (-40.67% ÷ 45x ≈ 0.9%) — pero con 45x de apalancamiento, ese pequeño slippage se amplificó a una pérdida grande sobre el margen.

Se corrigió cambiando el tipo de orden de `STOP_MARKET` a **`STOP`** (límite): se sigue disparando con `stopPrice=entry_price` (`workingType=CONTRACT_PRICE`, precio último), pero ahora el **precio límite de la orden también se fija en `entry_price`** — BingX solo puede llenarla exactamente ahí, nunca peor.

**Trade-off aceptado (confirmado con el usuario):** si el precio salta de golpe más allá del precio de entrada en una sola vela (gap fuerte, típico de pares con poca liquidez), esta orden puede quedar **sin ejecutarse**, y la posición seguiría abierta sin protección hasta que el precio regrese exactamente a ese nivel. Se prioriza "nunca cerrar peor que break even" sobre "siempre garantizar el cierre" — lo opuesto exacto del comportamiento anterior con `STOP_MARKET`.

## ⏱️ Cron interno de GitHub dedicado solo a TP/SL (evita entradas duplicadas)

Además del disparador externo (cronjob.org → `repository_dispatch`, que corre el bot **completo**: Paso 1 + Pasos 2-14), el workflow incluye un cron **interno** de GitHub Actions que corre **cada hora** (`0 * * * *`) usando `schedule`, en sintonía con el timeframe 1h del Paso 1.

Ese cron interno corre el bot en modo `BOT_MODE=tp_only`: **solo** ejecuta el Paso 1 (gestión de Take Profit / Stop Loss sobre posiciones ya abiertas, con sus dos gatillos independientes). **Nunca** escanea ni abre nuevas entradas (Pasos 2-14) **ni revisa la memoria de candidatos a largo** (`long_candidates.json`), precisamente para que no compita con tu disparador externo de cronjob.org y termine abriendo una posición duplicada.

El filtro de "solo futuros perpetuos cripto con USDT como referencia" (ver más abajo) es **una única función**, compartida por todo el script — se aplica igual sin importar qué disparador o `BOT_MODE` esté corriendo, tanto al Paso 1 como a los Pasos 2-14.

En resumen:

| Disparador | `BOT_MODE` | Qué corre |
|---|---|---|
| `schedule` (cron interno de GitHub, cada hora) | `tp_only` | Solo Paso 1 (TP/SL) |
| `repository_dispatch` (cronjob.org) | `full` | Paso 1 + Pasos 2-14 (TP/SL, nuevas entradas short) + escaneo de candidatos a largo (`long_candidates.json`) |
| `workflow_dispatch` (manual) | `full` | Paso 1 + Pasos 2-14 (TP/SL, nuevas entradas short) + escaneo de candidatos a largo (`long_candidates.json`) |

**Nota sobre la rama de largos:** el escaneo de `long_candidates.json` (búsqueda del cruce SMA20/EMA50 en 5m, ver más abajo) corre **solo** en modo `full` — es decir, únicamente con el disparador externo de cronjob.org o de forma manual, nunca con el cron interno de GitHub. Motivo: cronjob.org es extremadamente puntual en sus ejecuciones, y esta estrategia necesita esa puntualidad para no perderse el momento del cruce en 5m.

Si quieres cambiar la frecuencia del cron interno, ajusta la línea `cron: "0 * * * *"` en `.github/workflows/bingx-bot.yml` (formato estándar de cron; GitHub Actions no garantiza precisión exacta al minuto en horarios de alta demanda).

## 📊 El Paso 9 (volumen 24h) siempre usa el mercado REAL

El filtro de volumen del Paso 9 (`MIN_VOLUME_24H_USDT = 2,000,000`) consulta **siempre el volumen real del mercado** — este es un caso particular de la regla general explicada arriba ("El bot SIEMPRE analiza el mercado REAL"), que aplica a todo el análisis del bot, no solo a este paso.

## 📐 Apalancamiento (Paso 13): la distancia depende del tipo de entrada

La fórmula sigue siendo **80% ÷ distancia%**, pero la **distancia** usada cambia según cómo termine ejecutándose la entrada (Paso 14):

- **Orden STOP** (el precio aún no tocó la SMA20(5m)): distancia = máximo → **SMA20(5m)**. Fórmula original, sin cambios.
- **Entrada a MERCADO** (el precio ya tocó la SMA20(5m)): distancia = máximo → **precio actual** en el momento exacto de abrir la orden — **no** la SMA20.

Por esto, el cálculo de apalancamiento y margen (Paso 13) ya **no** se hace durante el escaneo (Pasos 2-12, en `strategy_entries.evaluate_symbol`), sino justo antes de ejecutar la orden (Paso 14, en `bingx_bot.execute_entry`): solo ahí se conoce el precio en vivo necesario para decidir qué tipo de entrada corresponde y, por lo tanto, qué distancia usar.

## 🔍 Diagnóstico: resumen de descartes por paso

Cada corrida del escáner de entradas (Pasos 2-14) ahora termina con una línea de resumen en el log:

```
[Pasos 2-14] Resumen de descartes por paso -> Paso 2/3: 340, Paso 4: 12, Paso 9: 5, Paso 10: 48, Paso 12: 3
```

Esto permite ver de un vistazo en qué paso se está cayendo la mayoría de los candidatos, sin tener que leer el log completo símbolo por símbolo.

## 🐛 Bug corregido: entradas en pares de forex y acciones sintéticas (NCFX / NCSK)

En producción el bot llegó a incluir en su universo de símbolos pares del producto "Standard Futures" de BingX (forex, acciones sintéticas, índices, commodities), que usa exactamente los mismos flags `swap=True` / `linear=True` / `quote=USDT` / `settle=USDT` que ccxt reporta para los futuros cripto reales.

Se corrigió generalizando el filtro con dos señales independientes: (1) el prefijo del id se amplió a `NC`, cubriendo cualquier subtipo de este producto; y (2) se descarta cualquier símbolo cuya moneda base siga el patrón `SUBYACENTE2REFERENCIA` (ej. `EUR2USD`, `APP2USD`) o sea directamente una divisa fiat. Esta función (`_looks_like_forex_or_cfd`) es **una sola** y se usa para construir el universo de símbolos del bot completo, además de una segunda verificación justo antes de ejecutar la orden.

## 🔧 Cambio de lógica: Paso 10 — chequeo de retroceso (rama SHORT únicamente)

**Historial del paso:** originalmente el Paso 10 exigía SMA20(4h) > EMA50(4h) **y** ambas por debajo de la EMA200(4h). Ese segundo requisito resultó demasiado estricto (confirmado con capturas reales de ETHFIUSDT y SPACEUSDT) y se eliminó: desde entonces, la única condición era SMA20(4h) > EMA50(4h), sin importar su relación con la EMA200(4h).

**Ahora**, se reincorpora una condición relacionada con la EMA200, pero de forma distinta y más específica — **solo aplica en un caso particular**:

- Si SMA20(4h) > EMA50(4h) **pero no** están ambas por encima de la EMA200(4h): el Paso 10 funciona exactamente igual que antes de este cambio, sin ningún chequeo extra.
- Si SMA20(4h) > EMA50(4h) **y además** ambas están por encima de la EMA200(4h): se agrega un chequeo extra. Se busca el punto donde la SMA20 cruzó de abajo hacia arriba la EMA200, y se revisa **todo el tramo** desde ese cruce hasta la vela actual: la SMA20 debe haberse mantenido **siempre** por encima de la EMA50 en ese tramo, sin ningún retroceso por debajo (aunque después haya vuelto a subir). Si se detecta un retroceso así, **el símbolo se descarta por completo — ni short ni long** — porque la señal de tendencia ya no se considera "limpia".
- **Caso borde (supuesto adoptado, confirmado con el usuario):** si dentro de las 1200 velas de 4h consultadas nunca se encuentra el cruce de la SMA20 sobre la EMA200 (por ejemplo, porque la SMA20 ya empezaba por encima desde el inicio del historial disponible), no hay forma de confirmar ni negar un retroceso — se aprueba el short con normalidad en ese caso.

**Importante:** este chequeo aplica únicamente a la rama SHORT del Paso 10. La rama LONG (Paso 10-B, patrón espejo) **no fue modificada** — sigue exigiendo SMA20(4h) < EMA50(4h) y ambas por debajo de la EMA200(4h), sin ningún chequeo adicional.

## 🆕 Rama de entradas en LARGO (Paso 10-B): patrón espejo del Paso 10 — SIN CAMBIOS

Además de la estrategia en corto ya descrita, el bot detecta también su patrón **espejo** para entradas en **largo**. Los Pasos 2 a 9 son exactamente los mismos para todos los símbolos; el **Paso 10** bifurca en tres caminos posibles:

| Condición en 4h | Dirección |
|---|---|
| SMA20(4h) > EMA50(4h) | **SHORT** (Pasos 11-14 tal como se describieron arriba, con el chequeo de retroceso cuando aplica) |
| SMA20(4h) < EMA50(4h) **y** ambas < EMA200(4h) | **LONG** (espejo, Paso 10-B) |
| Ninguna de las dos anteriores, o retroceso detectado en la rama short | Se descarta el símbolo por completo |

Si el símbolo cae en el camino LONG, se le calcula la **misma fórmula de singularidad** (S = ln(2k) - 1, Pasos 11-12, sin ningún cambio en el cálculo) usando los mismos datos de 15m/5m. Si aprueba, en vez de ejecutarse de inmediato **se guarda como candidato en `long_candidates.json`** — una lista que actúa como "memoria" persistente del bot, ya que las entradas en largo dependen de un patrón que puede tardar horas en aparecer en 5m.

### Patrón de entrada para los candidatos guardados

**Importante:** a diferencia del Paso 1, esta revisión **NO** corre en el cron interno de GitHub (`schedule`, `BOT_MODE=tp_only`) — corre **únicamente** cuando el bot se dispara en modo `full`, es decir, desde el cron **externo** configurado en cronjob.org (`repository_dispatch`) o de forma manual (`workflow_dispatch`).

Cada corrida en modo `full` revisa `long_candidates.json` buscando, en temporalidad 5m:

- **SMA20(5m) cruza de ARRIBA hacia ABAJO la EMA50(5m).**

Antes de aceptar ese cruce como válido, se verifica que el precio **no haya roto por encima** el `max_high_5m` original (el mismo máximo del round que dio origen a la singularidad) — si ya lo rompió, la señal se invalida y el candidato se elimina de la memoria sin abrir ninguna orden.

Si el cruce es válido:

- Si el precio en vivo está **por debajo** de la SMA20(5m) del cruce → orden **STOP LIMIT de compra**, esperando a que el precio suba y la cruce.
- Si el precio en vivo está **por encima** → entrada **a mercado**.

**Apalancamiento** = 80% ÷ distancia%, donde la distancia (en porcentaje, igual que en cualquier gráfico de velas) se mide desde la **SMA20(5m) en el momento exacto del cruce** hasta la **EMA200(5m)** en ese mismo instante. Redondeado a múltiplos de 5 y limitado al máximo de BingX, igual que en el camino short.

**Margen**: mismo criterio que en corto — el 10% del margen total de la cuenta, o el máximo que permite ese apalancamiento, lo que sea menor.

### Take Profit único (sin Stop Loss)

A diferencia del camino en corto, las entradas en largo **solo colocan Take Profit — nunca Stop Loss**. La distancia se mide, en porcentaje, desde el precio de entrada hasta la **EMA200(1h) en vivo** (recalculada en el momento exacto de la ejecución, no un valor congelado de cuando el candidato entró a la memoria):

```
distancia_pct = (precio_entrada - EMA200(1h)) / precio_entrada × 100
TP = precio_entrada × (1 + (apalancamiento × distancia_pct) / 100)
```

### Memoria persistente (`long_candidates.json`) y GitHub Actions

Como el patrón de entrada puede tardar horas en aparecer, los candidatos se guardan en `long_candidates.json` en la raíz del repositorio. Cada candidato se elimina de esa lista en cuanto:

- Se ejecuta su orden de entrada (a mercado o STOP), o
- Se invalida porque el precio rompió el máximo original.

**Importante:** el runner de GitHub Actions es efímero — cualquier cambio a este archivo durante una corrida se pierde a menos que se guarde de vuelta en el repositorio. Por eso el workflow (`.github/workflows/bingx-bot.yml`) incluye un paso final que hace `git commit` y `git push` de `long_candidates.json` si cambió durante la ejecución. Esto requiere el permiso `contents: write`, ya configurado en el workflow.

## ⚠️ Advertencias importantes

- Este bot abre posiciones apalancadas en corto de forma 100% automática, sin confirmación manual. Revisa cuidadosamente la lógica antes de usarlo con capital real.
- El API key debe tener permisos de lectura y trading de futuros (nunca de retiro de fondos).
- El bot fuerza automáticamente el **Hedge Mode** en la cuenta al arrancar (necesario porque todo el código usa `positionSide` LONG/SHORT). Si tienes posiciones abiertas al momento de correrlo por primera vez, ese cambio puede no aplicarse hasta que las cierres.
- BingX exige el parámetro `side` (LONG/SHORT/BOTH) en `set_leverage()`; el bot ya lo envía como `"SHORT"` (o `"LONG"` en la rama de largos).
- El tipo de orden correcto para la entrada condicional (Paso 14, cuando el precio aún no tocó la SMA20) es `"STOP"` en la API de BingX (no `"STOP_LIMIT"`, que el exchange rechaza).
- En Hedge Mode, BingX rechaza el campo `reduceOnly` en las órdenes de cierre (error 109400) — no debe enviarse; `positionSide` ya es suficiente para identificar que la orden cierra la posición.
- Prueba primero en cuenta demo/testnet o con montos pequeños, y revisa tanto "Posiciones" como el "Historial de órdenes" en BingX — una orden STOP puede quedar "colocada" en el log sin llegar a ejecutarse si el precio nunca la cruza.
- El trading con apalancamiento conlleva riesgo real de pérdida total del capital, especialmente con una estrategia con tantas variables y ejecución automática.
- La interpretación de "máximo en 15m/5m" se hizo tomando como ventana de búsqueda la hora exacta de la vela 1h que se confirmó como máximo del round (Paso 3).
