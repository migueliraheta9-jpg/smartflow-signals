# CLAUDE.md — smartflow-signals

Contexto operativo para Claude Code. Leer antes de tocar nada.
Este archivo es estable: comandos, convenciones y patrones. NO contiene estado de fases
(eso vive en los handoffs de sesión y se pudre rápido si se documenta acá).

## Qué es esto

Backend del bot SmartFlow: el "cerebro" de un sistema de trading semi-manual.
Pine (TradingView) → webhook → este Flask → Postgres (buzón) → Telegram.
Repo separado de Quantum Terminal (el "brazo": ejecutor MT5 + journal QuestDB).
Los dos repos NO se fusionan; se comunican vía la Postgres. Este bot es dueño del esquema.

## Stack

- Flask 3.0.3 + gunicorn 22.0.0 (2 workers) en Railway.
- `psycopg[binary]==3.2.3` (psycopg3, NO psycopg2). psycopg2-binary FALLA en Railway+Py3.13
  (falta libpq.so.5). API psycopg3: `import psycopg` + `from psycopg.rows import dict_row`;
  `row_factory=dict_row` en el cursor, no `cursor_factory`.
- Python 3.13.7, pineado vía `.python-version`.
- Repo plano: `main.py`, `Procfile`, `requirements.txt`, jobs/ para crons.
- Pine Script v3.5 (Buy/Sell en H1). `alert.freq_once_per_bar_close` siempre.

## Comandos

- Compilar antes de mostrar cualquier cambio: `python -m py_compile <archivo>`
  (en Windows es `python`, NO `python3` — no existe).
- Windows + cp1252: para leer archivos con UTF-8 (emojis/box chars), forzar
  `encoding='utf-8'` al abrir, o el script crashea con UnicodeDecodeError.
- Recon de funciones sin volcar todo el archivo: parsear con `ast` (inventario de
  funciones top-level + statements a nivel módulo + cuerpos por nombre).

## Reglas de cambio (no negociable)

- Todo cambio = `str_replace` targeted con diff completo en pantalla. NUNCA reescritura
  de archivo completo. Code tiende a DUPLICAR líneas en rewrites — escanear duplicados
  siempre antes de mostrar (numeración de líneas monótona; si un número se repite es
  artefacto del visor, no del archivo — confirmar con grep/findstr).
- Nada se commitea sin OK explícito de Miguel. Commit y push son pasos separados:
  commitear local, mostrar `git log -1 --stat`, esperar OK, después push.
- Cambios estrictamente ADITIVOS: no renombrar, borrar ni retipar columnas o lógica validada.
- Recon contra la fuente REAL antes de escribir: código verbatim del repo (no paráfrasis,
  no resúmenes). Si se devuelve un resumen en vez del verbatim, pedir el crudo antes de escribir.
  Las paráfrasis meten errores silenciosos (nombres de columna mal, firmas mal).

## Migraciones de esquema

- Todas via `init_db()` en main.py + Railway auto-deploy. NO ejecución manual de SQL.
- `init_db()` corre a nivel módulo (no detrás de `if __name__`), es idempotente
  (ALTERs en try/except), seguro re-correr. Cualquier `import main` lo dispara.
- Miguel corre las queries de verificación en el SQL Editor de Railway (CLI no instalada).
  Code NO toca la DB viva ni el Pine en TradingView.

## Patrón de crons (jobs/)

- Servicio Railway SEPARADO, mismo repo, start command `python jobs/<nombre>.py`
  (NO `python -m` — evita dependencia de `__init__.py`/package).
- El job arranca con CWD en jobs/, NO en la raíz. Para `import main`, insertar la raíz
  en sys.path al tope del script:
  `sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`
- El service necesita sus propias env vars (DATABASE_URL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID).
  Las constantes en main.py usan `os.environ.get()` → si faltan, resuelven None y el job
  corre MUDO (no crashea). Verificar vars seteadas antes de confiar en un "nada pendiente".
- Restart Policy: Never (un cron sale con código 0; reiniciar lo correría infinito).
- Serverless: OFF (incompatible con cron schedule de todos modos).
- Reclamo atómico de filas: `SELECT ... FOR UPDATE SKIP LOCKED` para evitar duplicados
  por solape de invocaciones.

## Zona horaria

- Todo UTC con conversión a NY centralizada y explícita vía `ZoneInfo("America/New_York")`
  (requiere `tzdata` en requirements). Cierre forzado 16:55 NY, deadline 19:30 NY,
  día sintético anclado 17:00 NY. Railway lee los cron schedules en UTC.

## Frozen spec de trading (no re-litigar)

3% riesgo diario sobre base escalonada (+10%/-5% reevaluada 17:00 NY). RR 1:2 bracket puro.
Máx 4 ciclos/día + máx 2 por cohorte + 1 trade por KZ. SL = low candle liquidez −5 pips
(FVG-puro: piso FVG −5 pips). Cierre forzado 16:55 NY. Aprobación de ciclo vía Telegram,
default-reject 19:30 NY. Dirección lockeada en aprobación, viaja en la señal (TradingView
única fuente de verdad). Gold excluido. FVG Daily: cuerpo D-1 cubre mín 50% del rango FVG.
