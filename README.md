# BingX Trading Bot — SHORT + LONG (Pasos 1 a 14) + Barrido de balance

Bot asincrónico para futuros perpetuos USDT-M en BingX. Opera **dos
direcciones independientes en paralelo**, cada una con su propio motor
de estrategia y su propia conexión de cuenta:

- **SHORT** → `short_strategy.py` (antes `strategy_entries.py`), cuenta
  controlada por `BINGX_DEMO_SHORT`.
- **LONG** → `long_strategy.py`, cuenta controlada por `BINGX_DEMO_LONG`.

Ambas comparten las mismas API keys (es la misma cuenta de BingX), el
mismo Paso 1 (gestión de TP/SL) y la misma lectura de mercado (siempre
real), pero cada una puede estar en modo demo o real de forma
**totalmente independiente** — por ejemplo, SHORT en real (ya probada)
y LONG en demo (todavía en pruebas), sin que un cambio de modo en una
afecte a la otra.

## Paso 1 — Gestión de Take Profit y Stop Loss (DOS gatillos independientes)

- Filtra solo pares de futuros perpetuos CRIPTO con USDT como referencia, excluyendo explícitamente TODO el "Standard Futures" de BingX (forex, índices, commodities, acciones sintéticas) — ver `_looks_like_forex_or_cfd()` en `bingx_bot.py`.
- Revisa las posiciones abiertas de **cada cuenta** (short y long) por separado. Es genérico por lado de posición (LONG/SHORT), así que gestiona igual las posiciones abiertas por cualquiera de las dos estrategias de entrada.
- **Gatillo 1 — Take Profit:** si el precio toca la **SMA20 (1h)**, coloca el Take Profit en la EMA200(1h). Se evalúa siempre, sin importar el disparador que corrió el bot (cron interno de GitHub, disparador externo de cronjob.org, o ejecución manual).
- **Gatillo 2 — Stop Loss:** si el precio toca la **EMA50 (1h)**, coloca el Stop Loss en break even (precio de entrada exacto). **Este gatillo ahora SOLO se evalúa cuando la corrida fue disparada por el disparador EXTERNO (cronjob.org → `repository_dispatch`)** — ver la sección "Disparador del Stop Loss" más abajo.

Estos dos gatillos son **completamente independientes** entre sí: uno puede dispararse sin que el otro lo haya hecho todavía.

## 🎯 Disparador del Stop Loss: solo el disparador externo de cronjob.org

El Stop Loss (Gatillo 2 del Paso 1) SOLO se coloca cuando la corrida fue disparada por el **disparador externo de cronjob.org** (`repository_dispatch`), por ser más puntual que el cron interno de GitHub (`schedule`), que puede sufrir retrasos frente al minuto exacto configurado. El Take Profit (Gatillo 1) **no cambió**: sigue evaluándose siempre, en cualquier disparador. Esto aplica igual a las posiciones de ambas cuentas (short y long).

| Disparador | ¿Coloca Take Profit? | ¿Coloca Stop Loss? |
|---|---|---|
| `schedule` (cron interno de GitHub, cada hora) | ✅ Sí | ❌ No |
| `repository_dispatch` (cronjob.org) | ✅ Sí | ✅ Sí |
| `workflow_dispatch` (manual) | ✅ Sí | ❌ No |

Esto se controla con la variable de entorno `RUN_STOP_LOSS_CHECK`, fijada en `.github/workflows/bingx-bot.yml`:

```yaml
RUN_STOP_LOSS_CHECK: ${{ github.event_name == 'repository_dispatch' && 'true' || 'false' }}
```

`process_position_tp()` en `bingx_bot.py` recibe este flag (`run_stop_loss_check`) y solo evalúa el toque de la EMA50(1h)/coloca el Stop Loss si viene en `True`; en caso contrario, lo omite y lo deja registrado en el log.

## Pasos 2 a 14 — Dos escáneres de nuevas entradas, uno por dirección

Para cada par SIN posición abierta **en la cuenta correspondiente**:

### SHORT (`short_strategy.py`) — busca un "round alcista" que ya tocó techo

| Paso | Qué valida |
|---|---|
| 2 | Round alcista: MA20(SMA) y EMA50 cruzaron de abajo hacia arriba la EMA200 (1h) |
| 3 | La última vela 1h cerrada es el máximo (mecha) de todo el round |
| 4 | RSI6 (1h) de la vela verde relevante no está sobrecomprado (>90 descarta) |
| 5 | El máximo en 15m debe estar en vela roja y coincidir con el máximo de 1h |
| 6 | El máximo en 5m debe estar en vela roja y coincidir con el máximo de 1h |
| 7 | W%R6, W%R12, W%R24 de la vela roja del máximo son todos distintos entre sí, en 15m |
| 8 | Mismo chequeo que el Paso 7, pero en 5m |
| 9 | Volumen 24h > 2,000,000 USDT (siempre del mercado real) |
| 10 | El mercado debe estar DENTRO de un ciclo alcista activo (4h): SMA20 por encima de la EMA50 — ver el detalle completo más abajo |
| 11-12 | Singularidad: S = ln(2k) - 1 > 0 en 15m (paso 11) y/o 5m (paso 12) |
| 13 | Apalancamiento = 80% ÷ (% de distancia), medida desde el máximo hacia el precio de referencia |
| 14 | Entrada a mercado o STOP en la SMA20(5m), esperando a que el precio la cruce de arriba hacia abajo |

### LONG (`long_strategy.py`) — espejo invertido, busca un "round bajista" que ya tocó fondo

| Paso | Qué valida |
|---|---|
| 2 | Round bajista: MA20(SMA) y EMA50 cruzaron de arriba hacia abajo la EMA200 (1h) |
| 3 | La última vela 1h cerrada es el mínimo (mecha) de todo el round |
| 4 | \|W%R6\| (1h) de la vela roja relevante no está sobrevendido (>90 descarta) |
| 5 | El mínimo en 15m debe estar en vela verde y coincidir con el mínimo de 1h |
| 6 | El mínimo en 5m debe estar en vela verde y coincidir con el mínimo de 1h |
| 7 | RSI6, RSI12, RSI24 de la vela verde del mínimo son todos distintos entre sí, en 15m |
| 8 | Mismo chequeo que el Paso 7, pero en 5m |
| 9 | Volumen 24h > 2,000,000 USDT (sin cambios respecto al short) |
| 10 | El mercado debe estar DENTRO de un ciclo bajista activo (4h): SMA20 por debajo de la EMA50 — espejo del Paso 10 del short |
| 11-12 | Singularidad (fórmula espejo): S = ln(2/k) - 1 > 0 en 15m y/o 5m |
| 13 | Apalancamiento = 80% ÷ (% de distancia), medida desde el mínimo hacia el precio de referencia |
| 14 | Entrada a mercado (compra) o STOP tipo "buy" en la SMA20(5m), esperando a que el precio la cruce de abajo hacia arriba |

Ambas estrategias abren sus posiciones en margen aislado.

## 🔍 Pasos 7 y 8 — chequeo de un solo extremo (revertido)

Ambos pasos chequean W%R6/12/24 (short) o RSI6/12/24 (long) **únicamente en la vela del extremo del round** (la roja del máximo en short, la verde del mínimo en long). Se probó en su momento agregar un segundo chequeo en la vela previa de signo opuesto, pero se **revirtió** (confirmado con el usuario) porque reducía demasiado el número de entradas — hoy cada paso tiene un solo chequeo, como en la versión original.

## 🔧 Paso 10 — SMA20 vs EMA50 (4h): el mercado debe estar DENTRO de un ciclo alcista/bajista

**Historial del paso (SHORT):** originalmente el Paso 10 exigía SMA20(4h) > EMA50(4h) **y** ambas por debajo de la EMA200(4h). Ese segundo requisito resultó demasiado estricto y se eliminó: la condición base pasó a ser solo SMA20(4h) > EMA50(4h). Después se reincorporó un chequeo adicional, pero solo para el caso en que ambas medias ya están por encima de la EMA200 — ver el detalle completo en los comentarios de `short_strategy.py` (`check_step_10`, `_check_retroceso_ciclo_alcista`, `_classify_fin_ciclo_alcista`).

**LONG** implementa el espejo exacto de esta lógica, invirtiendo cruces y comparaciones — ver `check_step_10`, `_check_avance_ciclo_bajista` y `_classify_fin_ciclo_bajista` en `long_strategy.py`.

**Regla general para ambas direcciones:** si el mercado está FUERA del ciclo correspondiente (alcista para short, bajista para long), no se entra en ninguna operación para ese par en esa corrida.

## 🏦 Barrido de balance excedente: futuros → fondos (solo mercado real, solo disparador externo, POR CUENTA)

Al terminar de leer el mercado y evaluar ambas estrategias (Paso 1 + Pasos 2-14, en las dos direcciones), sin importar si se abrió o no una operación nueva, el bot calcula el balance TOTAL de **cada cuenta de futuros perpetuos por separado** (la de short y la de long). Si el balance de una cuenta supera **10,000 USDT**, transfiere automáticamente el **excedente de esa cuenta** a su cuenta de **fondos**.

**Cómo se calcula el balance total (por cuenta):**

```
balance total = margen LIBRE + margen retenido en CADA posición abierta (aislado)
```

Reglas importantes:

- **Solo mercado real:** este chequeo nunca se ejecuta para una cuenta en modo demo (`BINGX_DEMO_SHORT=true` o `BINGX_DEMO_LONG=true`, según corresponda) — la cuenta VST tiene su propio balance virtual.
- **Solo el disparador externo (cronjob.org):** el chequeo se activa únicamente cuando la corrida fue disparada por tu disparador externo configurado en cronjob.org (`repository_dispatch`). El cron interno de GitHub (`schedule`) y las ejecuciones manuales (`workflow_dispatch`) NUNCA disparan este barrido — ver la variable `RUN_BALANCE_CHECK` en `.github/workflows/bingx-bot.yml`.
- Cada cuenta se evalúa de forma completamente independiente: es posible que se transfiera excedente de la cuenta short y no de la long (o viceversa) en la misma corrida, según cada balance.
- **Solo se puede transferir de verdad el margen LIBRE** de esa cuenta — si es menor que el excedente calculado, se transfiere como máximo lo que esté libre y el resto queda pendiente para la siguiente corrida externa.
- Cada transferencia exitosa se notifica por Telegram, igual que las entradas y el TP/SL, identificando de qué cuenta (SHORT/LONG) se trata.

Ver `check_and_sweep_futures_balance()` en `bingx_bot.py`.

## Supuestos adoptados (confirmados con el usuario)

- MA20 = SMA20 (media simple).
- "Máximo"/"mínimo" del round / de 15m / de 5m = el high/low (mecha) de la vela, no el cierre.
- El apalancamiento calculado se limita al máximo que permite BingX para ese par cuando el cálculo da un valor mayor.
- El margen total de la cuenta (Paso 13) se calcula como margen libre + margen ya usado en otras operaciones abiertas, no solo el saldo libre.
- El Paso 10 se evalúa solo sobre la última vela 4h ya cerrada, con un histórico fijo de 1200 velas, en ambas direcciones.
- Pasos 7 y 8: chequeo de un solo extremo (revertido el chequeo adicional en la vela previa de signo opuesto) — ver la sección correspondiente más arriba.
- El Stop Loss (Gatillo 2 del Paso 1) solo se coloca en corridas disparadas por el disparador externo de cronjob.org (`repository_dispatch`), para ambas cuentas.
- El barrido de balance usa un tope fijo de 10,000 USDT por cuenta y se evalúa de forma independiente para short y long.
- SHORT y LONG comparten las mismas API keys pero usan conexiones de cuenta separadas, cada una con su propio modo demo/real (`BINGX_DEMO_SHORT` / `BINGX_DEMO_LONG`).

## Configuración

Sube estos archivos a tu repositorio manteniendo la estructura:

- `bingx_bot.py`
- `short_strategy.py` (antes `strategy_entries.py`)
- `long_strategy.py`
- `utils/indicators.py`
- `utils/requirements.txt`
- `.github/workflows/bingx-bot.yml`

En **Settings → Secrets and variables → Actions**, agrega:

**Secrets:**
- `BINGX_API_KEY`
- `BINGX_API_SECRET`
- `TELEGRAM_BOT_TOKEN` (token de tu bot de Telegram)
- `TELEGRAM_CHAT_ID` (chat_id de tu conversación PRIVADA con el bot — no de un grupo)

**Variables:**
- `BINGX_DEMO_SHORT` (`true`/`false` — antes era `BINGX_DEMO`)
- `BINGX_DEMO_LONG` (`true`/`false` — nueva, independiente de la anterior)

La Action corre cada hora por defecto vía el cron interno de GitHub (dedicado solo a TP, para ambas cuentas), se puede disparar manualmente desde **Actions → Run workflow** (con dos checkboxes independientes, `demo_mode_short` y `demo_mode_long`), o de forma externa vía `repository_dispatch` (por ejemplo, usando cronjob.org para mayor puntualidad — este es el único disparador que coloca el Stop Loss y el único que ejecuta el barrido de balance). Cada entrada exitosa (short o long, a mercado o STOP) se notifica automáticamente por Telegram, igual que cada transferencia del barrido de balance.

## 📡 El bot SIEMPRE analiza el mercado REAL — el modo demo/real solo afecta a la cuenta

Todo el **análisis** del bot (Pasos 1 a 14 por igual, sin excepción, en ambas direcciones) — la lista de pares válidos, las velas OHLCV de cualquier temporalidad (1h, 15m, 5m, 4h), los indicadores derivados de ellas, y el volumen 24h del Paso 9 — se calcula **siempre contra el mercado REAL** de BingX, sin importar `BINGX_DEMO_SHORT` ni `BINGX_DEMO_LONG`.

Cada una de esas dos variables decide **únicamente** dónde ocurren las acciones que dependen de **su respectiva cuenta** (short o long):

- Leer posiciones abiertas y balance de esa cuenta.
- Colocar el Take Profit y el Stop Loss (Paso 1) de las posiciones de esa cuenta.
- Configurar apalancamiento y margen, y ejecutar la orden de entrada (Pasos 13-14) de esa dirección.
- Ejecutar el barrido de balance de esa cuenta — este último, además, nunca corre en modo demo.

En la práctica, esto se resuelve con:

- **Conexión de mercado** (`market_exchange` en el código): siempre SIN sandbox, apunta a producción. Se usa para todo el escaneo y análisis (Pasos 1 a 14, ambas direcciones). Se reutiliza la conexión de cuenta que ya esté en real (short o long); si ambas están en demo, se abre una tercera conexión dedicada.
- **Conexión de cuenta SHORT** (`exchange_short`): con sandbox activado si `BINGX_DEMO_SHORT=true`. Se usa solo para posiciones, balance, TP/SL, entradas y el barrido de balance del lado short.
- **Conexión de cuenta LONG** (`exchange_long`): con sandbox activado si `BINGX_DEMO_LONG=true`. Se usa solo para lo mismo, del lado long.

## 🧪 Probar en cuenta DEMO (VST) antes de ir a real

El bot soporta la cuenta demo de BingX (VST = Virtual USDT) usando el modo sandbox nativo de ccxt, de forma independiente para cada dirección. Usa las mismas API keys de tu cuenta real; no hace falta generar credenciales separadas.

- **Ejecución manual:** ve a **Actions → BingX Trading Bot → Run workflow** y marca (o desmarca) `demo_mode_short` y `demo_mode_long` según lo que quieras probar.
- **Ejecución programada (cron):** configura las variables `BINGX_DEMO_SHORT` y `BINGX_DEMO_LONG` en **Settings → Secrets and variables → Actions → Variables**.
- **Localmente:**

```bash
export BINGX_API_KEY="tu_api_key"
export BINGX_API_SECRET="tu_api_secret"
export BINGX_DEMO_SHORT=false
export BINGX_DEMO_LONG=true
python bingx_bot.py
```

Verifica en los logs que aparece `[SHORT] MODO REAL ACTIVADO` / `[LONG] MODO DEMO (VST) ACTIVADO` (o la combinación que corresponda), y confirma en la app/web de BingX (sección Demo Trading) que las órdenes y posiciones se reflejan ahí antes de pasar a real.

## 🔍 Diagnóstico: resumen de descartes por paso

Cada corrida de cada escáner de entradas (Pasos 2-14) termina con una línea de resumen en el log, identificada por dirección:

```
[Pasos 2-14][SHORT] Resumen de descartes por paso -> Paso 2/3: 340, Paso 4: 12, Paso 9: 5, Paso 10: 48, Paso 12: 3
[Pasos 2-14][LONG] Resumen de descartes por paso -> Paso 2/3: 290, Paso 4: 8, Paso 10: 55
```

Esto permite ver de un vistazo en qué paso se está cayendo la mayoría de los candidatos de cada dirección, sin tener que leer el log completo símbolo por símbolo.

## 🐛 Bug corregido: la EMA/SMA de calentamiento generaba toques falsos (visto en SXTUSDT)

El Paso 1 revisa todo el historial de velas cerradas consultado, comparando cada vela contra el valor que tenía la línea (SMA20 o EMA50) **en ese momento exacto**. El problema: la EMA/SMA de pandas se "siembra" con el primer cierre de la ventana pedida y tarda varias decenas/centenas de velas en converger al valor real. Se corrigió pidiendo `EMA_WARMUP_CANDLES` (400) velas EXTRA, **antes** de la ventana donde de verdad se busca un toque: esas velas de más se usan para que la SMA20/EMA50/EMA200 converjan correctamente, pero se **descartan** a la hora de buscar la señal.

## 🐛 Bug corregido: BingX rechazaba TODAS las órdenes de Take Profit y Stop Loss

En producción, cada intento de colocar el Take Profit o el Stop Loss (Paso 1) fallaba con:

```
bingx {"code":109400,"msg":"In the Hedge mode, the 'ReduceOnly' field can not be filled.","data":{}}
```

Se corrigió quitando `reduceOnly` de los parámetros de ambas órdenes (Take Profit y Stop Loss); en Hedge Mode, `positionSide` ya identifica sin ambigüedad que la orden cierra esa posición.

## 🐛 Bug corregido: el Stop Loss en break even cerraba con pérdida por slippage (STOP_MARKET → STOP)

En producción se detectó un caso real (TAUSDT, 45x) donde el Stop Loss en break even se ejecutó con **-40.67% de pérdida sobre el margen**, en vez de cerrar en cero como se esperaba. Se corrigió cambiando el tipo de orden de `STOP_MARKET` a **`STOP`** (límite): se sigue disparando con `stopPrice=entry_price`, pero ahora el **precio límite de la orden también se fija en `entry_price`** — BingX solo puede llenarla exactamente ahí, nunca peor.

**Trade-off aceptado:** si el precio salta de golpe más allá del precio de entrada en una sola vela (gap fuerte), esta orden puede quedar sin ejecutarse, y la posición seguiría abierta sin protección hasta que el precio regrese exactamente a ese nivel.

## ⏱️ Cron interno de GitHub dedicado solo a Take Profit (evita entradas duplicadas)

Además del disparador externo (cronjob.org → `repository_dispatch`, que corre el bot **completo**: Paso 1 + Pasos 2-14 en ambas direcciones, coloca el Stop Loss, y además dispara el barrido de balance de ambas cuentas), el workflow incluye un cron **interno** de GitHub Actions que corre **cada hora** (`0 * * * *`) usando `schedule`.

Ese cron interno corre el bot en modo `BOT_MODE=tp_only`: **solo** ejecuta el Paso 1 (gestión de Take Profit sobre posiciones ya abiertas de ambas cuentas). **Nunca** escanea ni abre nuevas entradas (Pasos 2-14, en ninguna dirección), precisamente para que no compita con tu disparador externo de cronjob.org.

| Disparador | `BOT_MODE` | `RUN_STOP_LOSS_CHECK` | `RUN_BALANCE_CHECK` | Qué corre |
|---|---|---|---|---|
| `schedule` (cron interno de GitHub, cada hora) | `tp_only` | `false` (nunca) | `false` (nunca) | Solo Paso 1 (Gatillo 1, Take Profit), ambas cuentas |
| `repository_dispatch` (cronjob.org) | `full` | `true` | `true` (por cuenta, solo si esa cuenta no está en demo) | Paso 1 completo (TP + SL) + Pasos 2-14 (SHORT y LONG) + barrido de balance (por cuenta) |
| `workflow_dispatch` (manual) | `full` | `false` (nunca) | `false` (nunca) | Paso 1 (solo Gatillo 1) + Pasos 2-14 (SHORT y LONG) |

Si quieres cambiar la frecuencia del cron interno, ajusta la línea `cron: "0 * * * *"` en `.github/workflows/bingx-bot.yml`.

## 📊 El Paso 9 (volumen 24h) siempre usa el mercado REAL

El filtro de volumen del Paso 9 (`MIN_VOLUME_24H_USDT = 2,000,000`) consulta **siempre el volumen real del mercado**, en ambas direcciones — este es un caso particular de la regla general explicada arriba ("El bot SIEMPRE analiza el mercado REAL").

## 📐 Apalancamiento (Paso 13): la distancia depende del tipo de entrada

La fórmula sigue siendo **80% ÷ distancia%** en ambas direcciones, pero la **distancia** usada cambia según cómo termine ejecutándose la entrada (Paso 14):

- **SHORT** — Orden STOP: distancia = máximo → SMA20(5m). Entrada a MERCADO: distancia = máximo → precio actual.
- **LONG** — Orden STOP: distancia = SMA20(5m) → mínimo. Entrada a MERCADO: distancia = precio actual → mínimo.

Por esto, el cálculo de apalancamiento y margen (Paso 13) no se hace durante el escaneo, sino justo antes de ejecutar la orden (Paso 14, en `bingx_bot.execute_entry()` para short y `bingx_bot.execute_entry_long()` para long): solo ahí se conoce el precio en vivo necesario para decidir qué tipo de entrada corresponde.

## 🐛 Bug corregido: entradas en pares de forex y acciones sintéticas (NCFX / NCSK)

Se corrigió generalizando el filtro con dos señales independientes: (1) el prefijo del id se amplió a `NC`, cubriendo cualquier subtipo de este producto; y (2) se descarta cualquier símbolo cuya moneda base siga el patrón `SUBYACENTE2REFERENCIA` (ej. `EUR2USD`, `APP2USD`) o sea directamente una divisa fiat. Esta función (`_looks_like_forex_or_cfd`) es **una sola**, compartida por `bingx_bot.py`, y se usa para construir el universo de símbolos del bot completo (ambas direcciones), además de una segunda verificación justo antes de ejecutar cada orden.

## ⚠️ Advertencias importantes

- Este bot abre posiciones apalancadas (en corto Y en largo) de forma 100% automática, sin confirmación manual. Revisa cuidadosamente la lógica antes de usarlo con capital real.
- El API key debe tener permisos de lectura, trading de futuros, y **transferencia entre cuentas** (necesario para el barrido de balance), pero nunca de retiro de fondos (withdraw) hacia fuera de BingX.
- El bot fuerza automáticamente el **Hedge Mode** en CADA cuenta (short y long) al arrancar. Si tienes posiciones abiertas al momento de correrlo por primera vez, ese cambio puede no aplicarse hasta que las cierres.
- BingX exige el parámetro `side` (LONG/SHORT/BOTH) en `set_leverage()`; el bot ya lo envía correctamente según la dirección.
- El tipo de orden correcto para la entrada condicional (Paso 14) es `"STOP"` en la API de BingX (no `"STOP_LIMIT"`, que el exchange rechaza) — para ambas direcciones (sell en short, buy en long).
- En Hedge Mode, BingX rechaza el campo `reduceOnly` en las órdenes de cierre (error 109400) — no debe enviarse; `positionSide` ya es suficiente.
- El barrido de balance transfiere fondos DENTRO de la misma cuenta de BingX (de futuros a fondos) — nunca retira dinero fuera del exchange. Revisa el historial de transferencias en BingX después de las primeras corridas.
- Recuerda que el Stop Loss (Gatillo 2 del Paso 1) solo se coloca cuando corres el bot vía tu disparador externo (cronjob.org); si dependes únicamente del cron interno de GitHub o de ejecuciones manuales, tus posiciones abiertas (short o long) solo tendrán Take Profit hasta que cronjob.org corra.
- Prueba primero en cuenta demo/testnet o con montos pequeños, y revisa tanto "Posiciones" como el "Historial de órdenes" en BingX — una orden STOP puede quedar "colocada" en el log sin llegar a ejecutarse si el precio nunca la cruza.
- El trading con apalancamiento conlleva riesgo real de pérdida total del capital, especialmente con una estrategia con tantas variables y ejecución automática.
- Como SHORT y LONG usan las mismas API keys, si ambas están en modo real comparten el mismo margen de cuenta — vigila que el 10% de margen por operación (Paso 13) de cada dirección no se solape de forma imprevista con la otra.
