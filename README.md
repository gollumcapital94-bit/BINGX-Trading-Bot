# BingX Trading Bot — Pasos 1 a 14 + Barrido de balance

Bot asincrónico para futuros perpetuos USDT-M en BingX, compuesto por dos módulos que corren en paralelo en cada ejecución, más un chequeo final de balance:

## Paso 1 — Gestión de Take Profit y Stop Loss (DOS gatillos independientes)

- Filtra solo pares de futuros perpetuos CRIPTO con USDT como referencia, excluyendo explícitamente TODO el "Standard Futures" de BingX (forex, índices, commodities, acciones sintéticas) — ver `_looks_like_forex_or_cfd()` en `bingx_bot.py`.
- Revisa tus posiciones abiertas.
- **Gatillo 1 — Take Profit:** si el precio toca la **SMA20 (1h)**, coloca el Take Profit en la EMA200(1h). Se evalúa siempre, sin importar el disparador que corrió el bot (cron interno de GitHub, disparador externo de cronjob.org, o ejecución manual).
- **Gatillo 2 — Stop Loss:** si el precio toca la **EMA50 (1h)**, coloca el Stop Loss en break even (precio de entrada exacto). **Este gatillo ahora SOLO se evalúa cuando la corrida fue disparada por el disparador EXTERNO (cronjob.org → `repository_dispatch`)** — ver la sección "Disparador del Stop Loss" más abajo.

Estos dos gatillos son **completamente independientes** entre sí: uno puede dispararse sin que el otro lo haya hecho todavía. Por ejemplo, si el precio tocó la SMA20 pero aún no la EMA50, existirá un Take Profit sin Stop Loss; si luego toca la EMA50 (en una corrida disparada externamente), se agrega el Stop Loss por separado.

## 🎯 Disparador del Stop Loss: ahora SOLO el disparador externo de cronjob.org

**Antes**, el Gatillo 2 (Stop Loss en break even, toque de la EMA50(1h)) se evaluaba en cualquier corrida que ejecutara el Paso 1 — tanto el cron interno de GitHub (`schedule`, cada hora) como el disparador externo (`repository_dispatch`) y las ejecuciones manuales (`workflow_dispatch`).

**Ahora**, el Stop Loss SOLO se coloca cuando la corrida fue disparada por el **disparador externo de cronjob.org** (`repository_dispatch`), por ser más puntual que el cron interno de GitHub (`schedule`), que puede sufrir retrasos frente al minuto exacto configurado — confirmado con el usuario. El Take Profit (Gatillo 1) **no cambió**: sigue evaluándose siempre, en cualquier disparador.

| Disparador | ¿Coloca Take Profit? | ¿Coloca Stop Loss? |
|---|---|---|
| `schedule` (cron interno de GitHub, cada hora) | ✅ Sí | ❌ No |
| `repository_dispatch` (cronjob.org) | ✅ Sí | ✅ Sí |
| `workflow_dispatch` (manual) | ✅ Sí | ❌ No |

Esto se controla con la nueva variable de entorno `RUN_STOP_LOSS_CHECK`, fijada en `.github/workflows/bingx-bot.yml`:

```yaml
RUN_STOP_LOSS_CHECK: ${{ github.event_name == 'repository_dispatch' && 'true' || 'false' }}
```

`process_position_tp()` en `bingx_bot.py` recibe este flag (`run_stop_loss_check`) y solo evalúa el toque de la EMA50(1h)/coloca el Stop Loss si viene en `True`; en caso contrario, lo omite y lo deja registrado en el log. Así, el Stop Loss se dispara desde un único disparador, nunca desde dos a la vez.

## Pasos 2 a 14 — Escáner de nuevas entradas en corto

Para cada par SIN posición abierta:

| Paso | Qué valida |
|---|---|
| 2 | Round alcista: MA20(SMA) y EMA50 cruzaron de abajo hacia arriba la EMA200 (1h) |
| 3 | La última vela 1h cerrada es el máximo (mecha) de todo el round |
| 4 | RSI6 (1h) de la vela verde relevante no está sobrecomprado (>90 descarta) |
| 5 | El máximo en 15m debe estar en vela roja y coincidir con el máximo de 1h |
| 6 | El máximo en 5m debe estar en vela roja y coincidir con el máximo de 1h |
| 7-8 | W%R6, W%R12, W%R24 de la vela roja del máximo, Y de la última vela verde que la antecede, son todos distintos entre sí (15m y 5m) — ver el detalle completo más abajo |
| 9 | Volumen 24h > 2,000,000 USDT (siempre del mercado real, ver más abajo) |
| 10 | SMA20 por encima de la EMA50, en temporalidad 4h. Si además ambas están por encima de la EMA200(4h), se exige que la SMA20 nunca haya retrocedido por debajo de la EMA50 desde el inicio del ciclo alcista actual — ver el detalle completo más abajo. Si SMA20 no está por encima de la EMA50, se descarta el símbolo por completo |
| 11-12 | Singularidad: S = ln(2k) - 1 > 0 en 15m (paso 11) y/o 5m (paso 12) — basta con que exista en UNA de las dos temporalidades (k = a/r o a/|r| según color de la vela 1h). Si existe en ambas, se trata igual que si fuera una sola |
| 13 | Apalancamiento = 80% ÷ (% de distancia). La distancia usa la SMA20(5m) si la entrada es STOP, o el precio actual si es a mercado (ver detalle más abajo), redondeado por defecto a múltiplos de 5 y limitado al máximo de BingX. Margen = el MENOR entre el 10% del margen total de la cuenta (libre + usado en otras operaciones abiertas) y el margen máximo que permite ese apalancamiento en el par (nunca se reduce el apalancamiento para poder usar más margen) |
| 14 | Si el precio ya tocó la SMA20(5m): entrada a mercado. Si no: orden STOP en la SMA20(5m), esperando a que el precio la cruce de arriba hacia abajo |

Todas las posiciones se abren en margen aislado, en corto (short).

## 🔍 Pasos 7 y 8 — W%R distintos: ahora también en la vela verde previa (4 chequeos en total)

**Antes**, los Pasos 7 (15m) y 8 (5m) solo exigían que W%R6, W%R12 y W%R24 fueran todos distintos entre sí en la vela ROJA del máximo.

**Ahora**, se agrega el mismo chequeo también en la **última vela VERDE que antecede a la roja del máximo**: si la vela inmediatamente anterior a la roja ya es verde, esa es la que se usa; si es roja, se sigue retrocediendo vela por vela hasta encontrar la primera vela verde. Por construcción de un round alcista ya confirmado (Pasos 2-3), esa vela verde siempre existe — el máximo del round solo puede alcanzarse tras un movimiento alcista (confirmado con el usuario).

En total, un símbolo debe aprobar **4 chequeos de W%R** para no ser descartado en estos pasos:

1. Vela roja del máximo en 15m (Paso 7).
2. Última vela verde previa en 15m (Paso 7).
3. Vela roja del máximo en 5m (Paso 8).
4. Última vela verde previa en 5m (Paso 8).

Basta con que **uno solo** de estos 4 chequeos falle (algún par de W%R6/12/24 coincide) para que el símbolo quede descartado en ese paso. Los valores de W%R usados después para la singularidad (Pasos 11-12) siguen siendo los de la vela ROJA del máximo — el chequeo de la vela verde es un filtro adicional, no reemplaza esos valores. Ver `check_wr_all_distinct_red_and_green()` y `_find_preceding_green_candle_idx()` en `strategy_entries.py`.

## 🔧 Paso 10 — SMA20 vs EMA50 (4h) y chequeo de retroceso

**Historial del paso:** originalmente el Paso 10 exigía SMA20(4h) > EMA50(4h) **y** ambas por debajo de la EMA200(4h). Ese segundo requisito resultó demasiado estricto (confirmado con capturas reales de ETHFIUSDT y SPACEUSDT) y se eliminó: desde entonces, la única condición era SMA20(4h) > EMA50(4h), sin importar su relación con la EMA200(4h).

**Después**, se reincorporó una condición relacionada con la EMA200, pero de forma distinta y más específica — **solo aplica en un caso particular**:

- Si SMA20(4h) > EMA50(4h) **pero no** están ambas por encima de la EMA200(4h) — como en SPACEUSDT —: el Paso 10 funciona exactamente igual que antes de este cambio, sin ningún chequeo extra.
- Si SMA20(4h) > EMA50(4h) **y además** ambas están por encima de la EMA200(4h) — como en ETHFIUSDT —: se agrega un chequeo extra, basado en la definición de "ciclo alcista" (ver más abajo).

### Definición de "ciclo alcista" (con el nuevo patrón de inicio, visto en OPUSDT)

- El ciclo alcista **EMPIEZA** en el instante más reciente en que ocurre **cualquiera de estos dos patrones**:
  1. La SMA20(4h) cruza de abajo hacia arriba la EMA50(4h) (patrón original, visto en SPACEUSDT). En ese momento, ambas pueden estar todavía por debajo de la EMA200(4h).
  2. La EMA50(4h) cruza de arriba hacia abajo la EMA200(4h) y **luego vuelve a cruzarla de abajo hacia arriba**, es decir, la EMA50 **recupera** la EMA200 después de haber estado por debajo — visto en **OPUSDT**. Aunque el cruce de la SMA20 sobre la EMA50 haya ocurrido mucho antes (incluso durante una fase todavía por debajo de la EMA200), lo que de verdad reinicia el ciclo alcista es este segundo cruce: la EMA50 recuperando la EMA200.

  Se usa el que haya ocurrido **más reciente** de los dos.

- El ciclo se **EXTIENDE** mientras la SMA20 se mantenga por encima de la EMA50 sin ninguna interrupción, e incluye el momento (si llega a ocurrir dentro de ese mismo tramo) en que la SMA20 también cruza por encima de la EMA200.
- El ciclo **TERMINA** en el instante exacto en que la SMA20 vuelve a caer por debajo de la EMA50 mientras ambas siguen por encima de la EMA200 (sin siquiera tocarla) — este es el "retroceso". Este criterio de fin de ciclo **no cambió**: ya se descarta directamente en la condición base del Paso 10 (SMA20(4h) ≤ EMA50(4h)).

**Chequeo adicional:** cuando ambas medias ya están por encima de la EMA200, se exige que el cruce de la SMA20 sobre la EMA200 haya ocurrido **dentro** del ciclo alcista actual (en o después de su inicio, según la definición de arriba) — y no en un ciclo alcista anterior que ya haya terminado por un retroceso. Si se detecta un retroceso así, **el símbolo se descarta por completo**, porque la señal de tendencia ya no se considera "limpia".

**Caso borde (supuesto adoptado, confirmado con el usuario):** si dentro de las 1200 velas de 4h consultadas **ninguno** de los dos patrones de inicio de ciclo se encuentra (la SMA20 se mantuvo siempre por encima de la EMA50 Y la EMA50 se mantuvo siempre por encima de la EMA200 durante toda la ventana), no hay evidencia de ningún retroceso y se aprueba el short con normalidad.

Si SMA20(4h) no está por encima de la EMA50(4h), o se detecta el retroceso, el símbolo se descarta por completo. Ver `_find_cycle_start_index()` y `_check_retroceso_ciclo_alcista()` en `strategy_entries.py`.

## 🏦 Barrido de balance excedente: futuros → fondos (solo mercado real, solo disparador externo)

Al terminar de leer el mercado y evaluar la estrategia (Paso 1 + Pasos 2-14), **sin importar si se abrió o no una operación nueva**, el bot calcula el balance TOTAL de tu cuenta de **futuros perpetuos**. Si ese balance supera **10,000 USDT**, transfiere automáticamente el **excedente** a tu cuenta de **fondos**.

**Cómo se calcula el balance total (confirmado con el usuario):**

```
balance total = margen LIBRE + margen retenido en CADA posición abierta (aislado)
```

**Ejemplo del usuario:** 1,000 USDT de margen aislado en un par + 500 USDT de margen aislado en otro par (1,500 USDT retenidos en posiciones) + 12,000 USDT libres = **13,500 USDT** de balance total. Con el tope de 10,000 USDT, el bot transfiere **3,500 USDT** a la cuenta de fondos.

No se usa el campo `used` genérico que devuelve la API de balance de BingX: en margen AISLADO ese campo puede no reflejar con precisión el margen ya movido a cada posición. En vez de eso, el bot suma explícitamente el margen de cada posición abierta (`collateral`/`initialMargin` de ccxt, con respaldo en los campos crudos de BingX) al margen libre — ver `_sum_positions_margin_usdt()` en `bingx_bot.py`.

Reglas importantes:

- **Solo mercado real:** este chequeo nunca se ejecuta en modo demo (`BINGX_DEMO=true`) — la cuenta VST tiene su propio balance virtual, y transferirlo no tiene ningún efecto sobre fondos reales.
- **Solo el disparador externo (cronjob.org):** el chequeo se activa únicamente cuando la corrida fue disparada por tu disparador externo configurado en cronjob.org (`repository_dispatch`). El cron interno de GitHub (`schedule`, dedicado solo a TP/SL) y las ejecuciones manuales (`workflow_dispatch`) NUNCA disparan este barrido — ver la variable `RUN_BALANCE_CHECK` en `.github/workflows/bingx-bot.yml`.
- Las posiciones abiertas se vuelven a consultar en este punto (no se reutiliza la lista de antes de escanear), porque el propio bot pudo haber abierto una posición nueva en esta misma corrida (Pasos 2-14) justo antes del chequeo.
- **Solo se puede transferir de verdad el margen LIBRE** — el margen retenido en posiciones abiertas está comprometido y la API no permite moverlo. Si en el momento del chequeo el margen libre disponible es menor que el excedente calculado, el bot transfiere como máximo lo que esté libre, deja un log de advertencia, y el resto queda pendiente para la siguiente corrida externa (por ejemplo, cuando se libere margen al cerrarse una posición).
- Cada transferencia exitosa se notifica por Telegram, igual que las entradas y el TP/SL.

Ver `check_and_sweep_futures_balance()` en `bingx_bot.py` para el detalle de la implementación.

## Supuestos adoptados (confirmados con el usuario)

- MA20 = SMA20 (media simple).
- "Máximo" del round / de 15m / de 5m = el high (mecha) de la vela, no el cierre.
- El apalancamiento calculado se limita al máximo que permite BingX para ese par cuando el cálculo da un valor mayor.
- El margen total de la cuenta (Paso 13) se calcula como margen libre + margen ya usado en otras operaciones abiertas, no solo el saldo libre.
- El Paso 10 (SMA20 vs EMA50 en 4h) se evalúa solo sobre la última vela 4h ya cerrada, con un histórico fijo de 1200 velas.
- Chequeo de retroceso del Paso 10 (ver detalle más arriba): si dentro de las 1200 velas de 4h consultadas no se encuentra ninguno de los dos patrones de inicio de ciclo alcista, se asume que no hay evidencia de un retroceso y se aprueba el short con normalidad.
- El chequeo de W%R de los Pasos 7 y 8 (ver detalle más arriba) asume que la vela verde previa a la roja del máximo siempre existe dentro de la ventana consultada, por la propia lógica de un round alcista ya confirmado.
- El Stop Loss (Gatillo 2 del Paso 1) solo se coloca en corridas disparadas por el disparador externo de cronjob.org (`repository_dispatch`), nunca desde el cron interno de GitHub ni desde ejecuciones manuales — ver detalle más arriba.
- El barrido de balance (ver arriba) usa un tope fijo de 10,000 USDT y transfiere el excedente calculado sobre el balance total, limitado al margen libre realmente disponible en ese momento.

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

La Action corre cada hora por defecto vía el cron interno de GitHub (dedicado solo a TP, ya no a SL — ver arriba), se puede disparar manualmente desde **Actions → Run workflow**, o de forma externa vía `repository_dispatch` (por ejemplo, usando cronjob.org para mayor puntualidad — este es ahora el único disparador que coloca el Stop Loss y el único que ejecuta el barrido de balance). Cada entrada exitosa (a mercado o STOP) se notifica automáticamente por Telegram, igual que cada transferencia del barrido de balance.

## 📡 El bot SIEMPRE analiza el mercado REAL — el modo demo/real solo afecta a la cuenta

Todo el **análisis** del bot (Pasos 1 a 14 por igual, sin excepción) — la lista de pares válidos, las velas OHLCV de cualquier temporalidad (1h, 15m, 5m, 4h), los indicadores derivados de ellas, y el volumen 24h del Paso 9 — se calcula **siempre contra el mercado REAL** de BingX, sin importar si `BINGX_DEMO` está en `true` o `false`.

La variable `BINGX_DEMO` decide **únicamente** dónde ocurren las acciones que dependen de la **cuenta**:

- Leer posiciones abiertas y balance.
- Colocar el Take Profit y el Stop Loss (Paso 1).
- Configurar apalancamiento y margen, y ejecutar la orden de entrada (Pasos 13-14).
- Ejecutar el barrido de balance (futuros → fondos) — este último, además, nunca corre en modo demo (ver la sección de arriba).

Motivo: un round alcista, un patrón técnico o el volumen real de un par no cambian porque el bot esté operando con dinero virtual. La cuenta VST (demo) tiene su propio historial y volumen simulados, que no reflejan el mercado real — usarlos para el análisis daría señales distintas a las que se generarían en producción, justo lo contrario de lo que se busca al probar en demo antes de ir a real.

En la práctica, esto se resuelve con dos conexiones a BingX cuando el bot corre en modo demo:

- **Conexión de mercado** (`market_exchange` en el código): siempre SIN sandbox, apunta a producción. Se usa para todo el escaneo y análisis (Pasos 1 a 14).
- **Conexión de cuenta** (`exchange` en el código): con sandbox activado si `BINGX_DEMO=true`. Se usa solo para posiciones, balance, TP/SL, la ejecución final de la orden, y el barrido de balance (que, además, se salta por completo en modo demo).

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

- Toque de la **SMA20(1h)** → coloca el **Take Profit** (en la EMA200(1h), sin cambios en dónde se coloca). Se evalúa en cualquier disparador.
- Toque de la **EMA50(1h)** → coloca el **Stop Loss** en break even (sin cambios en cómo se coloca: tipo `STOP` límite, ver el bug de TAUSDT más abajo). Se evalúa SOLO en el disparador externo de cronjob.org — ver la sección "Disparador del Stop Loss" más arriba.

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

## ⏱️ Cron interno de GitHub dedicado solo a Take Profit (evita entradas duplicadas)

Además del disparador externo (cronjob.org → `repository_dispatch`, que corre el bot **completo**: Paso 1 + Pasos 2-14, coloca el Stop Loss, y además dispara el barrido de balance), el workflow incluye un cron **interno** de GitHub Actions que corre **cada hora** (`0 * * * *`) usando `schedule`, en sintonía con el timeframe 1h del Paso 1.

Ese cron interno corre el bot en modo `BOT_MODE=tp_only`: **solo** ejecuta el Paso 1 (gestión de Take Profit sobre posiciones ya abiertas — el Stop Loss ya no se coloca desde este disparador, ver la sección "Disparador del Stop Loss" más arriba). **Nunca** escanea ni abre nuevas entradas (Pasos 2-14), precisamente para que no compita con tu disparador externo de cronjob.org y termine abriendo una posición duplicada. Por el mismo motivo, **nunca** dispara el barrido de balance (`RUN_BALANCE_CHECK` siempre viene en `false` para este disparador).

El filtro de "solo futuros perpetuos cripto con USDT como referencia" (ver más abajo) es **una única función**, compartida por todo el script — se aplica igual sin importar qué disparador o `BOT_MODE` esté corriendo, tanto al Paso 1 como a los Pasos 2-14.

En resumen:

| Disparador | `BOT_MODE` | `RUN_STOP_LOSS_CHECK` | `RUN_BALANCE_CHECK` | Qué corre |
|---|---|---|---|---|
| `schedule` (cron interno de GitHub, cada hora) | `tp_only` | `false` (nunca) | `false` (nunca) | Solo Paso 1, y solo su Gatillo 1 (Take Profit) |
| `repository_dispatch` (cronjob.org) | `full` | `true` | `true` (solo si además `BINGX_DEMO=false`) | Paso 1 completo (TP + SL) + Pasos 2-14 (nuevas entradas short) + barrido de balance |
| `workflow_dispatch` (manual) | `full` | `false` (nunca) | `false` (nunca) | Paso 1, pero solo su Gatillo 1 (Take Profit) + Pasos 2-14 (nuevas entradas short) |

Si quieres cambiar la frecuencia del cron interno, ajusta la línea `cron: "0 * * * *"` en `.github/workflows/bingx-bot.yml` (formato estándar de cron; GitHub Actions no garantiza precisión exacta al minuto en horarios de alta demanda — precisamente el motivo por el que el Stop Loss ya no se coloca desde este disparador).

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

## ⚠️ Advertencias importantes

- Este bot abre posiciones apalancadas en corto de forma 100% automática, sin confirmación manual. Revisa cuidadosamente la lógica antes de usarlo con capital real.
- El API key debe tener permisos de lectura, trading de futuros, y **transferencia entre cuentas** (necesario para el barrido de balance), pero nunca de retiro de fondos (withdraw) hacia fuera de BingX.
- El bot fuerza automáticamente el **Hedge Mode** en la cuenta al arrancar (necesario porque todo el código usa `positionSide` LONG/SHORT). Si tienes posiciones abiertas al momento de correrlo por primera vez, ese cambio puede no aplicarse hasta que las cierres.
- BingX exige el parámetro `side` (LONG/SHORT/BOTH) en `set_leverage()`; el bot ya lo envía como `"SHORT"`.
- El tipo de orden correcto para la entrada condicional (Paso 14, cuando el precio aún no tocó la SMA20) es `"STOP"` en la API de BingX (no `"STOP_LIMIT"`, que el exchange rechaza).
- En Hedge Mode, BingX rechaza el campo `reduceOnly` en las órdenes de cierre (error 109400) — no debe enviarse; `positionSide` ya es suficiente para identificar que la orden cierra la posición.
- El barrido de balance transfiere fondos DENTRO de tu propia cuenta de BingX (de futuros a fondos) — nunca retira dinero fuera del exchange. Aun así, revisa el historial de transferencias en BingX después de las primeras corridas para confirmar que el monto y el destino son los esperados.
- Recuerda que el Stop Loss (Gatillo 2 del Paso 1) solo se coloca cuando corres el bot vía tu disparador externo (cronjob.org); si dependes únicamente del cron interno de GitHub o de ejecuciones manuales, tus posiciones abiertas solo tendrán Take Profit hasta que cronjob.org corra.
- Prueba primero en cuenta demo/testnet o con montos pequeños, y revisa tanto "Posiciones" como el "Historial de órdenes" en BingX — una orden STOP puede quedar "colocada" en el log sin llegar a ejecutarse si el precio nunca la cruza.
- El trading con apalancamiento conlleva riesgo real de pérdida total del capital, especialmente con una estrategia con tantas variables y ejecución automática.
- La interpretación de "máximo en 15m/5m" se hizo tomando como ventana de búsqueda la hora exacta de la vela 1h que se confirmó como máximo del round (Paso 3).
