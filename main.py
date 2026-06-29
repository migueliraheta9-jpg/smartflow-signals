import os
import logging
import json
import requests
import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, jsonify
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
import hmac

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "")
DATABASE_URL     = os.environ.get("DATABASE_URL", "")
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

VERSION_BACKEND  = "4.0"   # versión de infraestructura (este archivo)
VERSION_SISTEMA  = "3.4"   # versión del sistema SmartFlow (Pine v3.4)

KZ_EMOJIS = {
    "asia":     "🌏 Asia",
    "london":   "🌍 London",
    "new york": "🗽 New York",
    "ny":       "🗽 New York",
}

DIR_CONFIG = {
    "compra": {"emoji": "🟢", "label": "COMPRA", "arrow": "🔼"},
    "venta":  {"emoji": "🔴", "label": "VENTA",  "arrow": "🔽"},
}

TIPO_EMOJIS = {
    "liq+fvg":  "📦💧 Liq + FVG",
    "fvg":      "📦 FVG H1",
    "liquidez": "💧 Toma de Liquidez",
}

def utc_now():
    """Reemplazo moderno de datetime.utcnow() (deprecado en Python 3.12+)."""
    return datetime.now(timezone.utc)

# ═══════════════════════════════════════════════════════════
#  DATABASE — PostgreSQL (psycopg3)
# ═══════════════════════════════════════════════════════════

def get_db_connection():
    """Abre conexión a PostgreSQL con timeout de 10s. Retorna None si falla."""
    if not DATABASE_URL:
        return None
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:
        logger.error(f"Error conectando a DB: {e}")
        return None

# ─── Valores semilla de configuración de riesgo ────────────
# Solo se insertan si la clave NO existe (ON CONFLICT DO NOTHING).
# Cambiarlos aquí después del primer deploy NO sobreescribe la DB:
# la fuente de verdad es la tabla risk_config.
RISK_CONFIG_DEFAULTS = {
    "riesgo_diario_pct":   "3",      # % de la base — pérdida máxima diaria
    "rr":                  "2",      # Risk/Reward fijo (1:2)
    "max_ciclos_dia":      "4",      # ciclos aprobables por día
    "max_por_cohorte":     "2",      # advertencia de concentración
    "base_equity":         "",       # se fija con /base (vacío = sin definir)
    "umbral_subida_pct":   "10",     # base escalonada: sube al superar +10%
    "umbral_bajada_pct":   "5",      # base escalonada: baja al caer -5%
    "buffer_pips_default": "5",      # buffer del SL si el símbolo no define uno
    "hora_cierre_forzado": "16:55",  # hora NY — cierre de toda posición
    "deadline_pase":       "19:30",  # hora NY — ciclos sin pase se rechazan
    "modo_operacion":      "senal",  # senal | gate | semiauto | auto
}

def init_db():
    """Crea/actualiza el esquema completo. Idempotente — seguro de ejecutar
    múltiples veces. 100% aditivo: nunca borra ni modifica datos existentes."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no configurada — persistencia deshabilitada, bot funcionará igual.")
        return
    conn = get_db_connection()
    if not conn:
        logger.error("No se pudo inicializar DB (conexión falló) — bot funcionará sin persistencia.")
        return
    try:
        with conn.cursor() as cur:
            # ── Tabla original (sin cambios) ──────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id              SERIAL PRIMARY KEY,
                    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    tipo_msg        TEXT NOT NULL DEFAULT 'signal',
                    par             TEXT,
                    direccion       TEXT,
                    ciclo           TEXT,
                    kz              TEXT,
                    tipo            TEXT,
                    fvg_src         TEXT,
                    calidad         TEXT,
                    cierre_fuerte   TEXT,
                    vela_comp       TEXT,
                    vic_robusta     TEXT,
                    daily           TEXT,
                    ops_asia        INTEGER,
                    ops_lon         INTEGER,
                    ops_ny          INTEGER,
                    pendientes      INTEGER,
                    sl              TEXT,
                    dist_obj        TEXT,
                    precio          TEXT,
                    raw_payload     JSONB
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_received_at ON signals(received_at DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_par ON signals(par);")

            # ── v4.0 — Extensión de signals (columnas nuevas, todas nullable) ──
            # Las filas existentes quedan intactas; las columnas nacen vacías.
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS ciclo_id      INTEGER;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS gate_decision TEXT;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS outcome       TEXT;")      # tp | sl | tiempo | manual
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pnl_r         NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pnl_dinero    NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS mfe_r         NUMERIC;")   # máx excursión a favor (en R)
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS mae_r         NUMERIC;")   # máx excursión en contra (en R)
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS cierre_ts     TIMESTAMPTZ;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS low_vela      TEXT;")      # low de la vela de señal (Pine)

            # ── v4.1 — Extensión de signals (estado de ciclo de vida + MT5 + sizing) ──
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS estado           TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','aprobada','en_ejecucion','abierta','cerrada','rechazada_gate','rechazada_sizing','error'));")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS mt5_position_id  BIGINT;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS r_dinero         NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS base_equity_snap NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS reclamo_ts       TIMESTAMPTZ;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS fill_price       NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS fill_ts          TIMESTAMPTZ;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl_real          NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS lote_real        NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS precio_cierre    NUMERIC;")
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS error_msg        TEXT;")

            # ── v4.0 — Tabla ciclos (flujo de aprobación manual) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ciclos (
                    id            SERIAL PRIMARY KEY,
                    fecha_ciclo   DATE NOT NULL,
                    par           TEXT NOT NULL,
                    direccion     TEXT,
                    tipo_daily    TEXT,
                    estado        TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente|aprobado|rechazado|expirado
                    modo_riesgo   TEXT,                               -- tercio_kz | todo_primera
                    noticia_alta  BOOLEAN DEFAULT FALSE,
                    razon_rechazo TEXT,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    decided_at    TIMESTAMPTZ,
                    UNIQUE (par, fecha_ciclo)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ciclos_fecha ON ciclos(fecha_ciclo DESC);")

            # --- Migración aditiva: CHECKs sobre ciclos (idempotente) ---
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ciclos_estado_check'
                          AND conrelid = 'ciclos'::regclass
                    ) THEN
                        ALTER TABLE ciclos ADD CONSTRAINT ciclos_estado_check
                            CHECK (estado IN ('pendiente','aprobado','rechazado','expirado'));
                    END IF;
                END $$;
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ciclos_direccion_check'
                          AND conrelid = 'ciclos'::regclass
                    ) THEN
                        ALTER TABLE ciclos ADD CONSTRAINT ciclos_direccion_check
                            CHECK (direccion IN ('compra','venta'));
                    END IF;
                END $$;
            """)
            cur.execute("ALTER TABLE ciclos ALTER COLUMN direccion SET NOT NULL;")

            # ── v4.2 — Campos de registro del ciclo (aditivo, nullable) ──
            # Datos que manda el Pine v3.5 en la alerta de ciclo. Sin lógica
            # asociada todavía: puro registro para análisis futuro
            # (min/max semanal, invalidación por proyección).
            cur.execute("ALTER TABLE ciclos ADD COLUMN IF NOT EXISTS liq_high   NUMERIC;")
            cur.execute("ALTER TABLE ciclos ADD COLUMN IF NOT EXISTS liq_low    NUMERIC;")
            cur.execute("ALTER TABLE ciclos ADD COLUMN IF NOT EXISTS proyeccion NUMERIC;")
            cur.execute("ALTER TABLE ciclos ADD COLUMN IF NOT EXISTS notificado_at TIMESTAMPTZ;")

            # ── v4.0 — Tabla simbolos (registro de activos y cohortes) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS simbolos (
                    simbolo        TEXT PRIMARY KEY,
                    cohorte        TEXT,
                    buffer_pips    NUMERIC DEFAULT 5,
                    objetivo_macro NUMERIC,
                    estado         TEXT NOT NULL DEFAULT 'activo',    -- activo | agotado
                    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

            # ── v4.1 — Extensión de simbolos (datos MT5 para sizing) ──
            cur.execute("ALTER TABLE simbolos ADD COLUMN IF NOT EXISTS mt5_symbol  TEXT;")
            cur.execute("ALTER TABLE simbolos ADD COLUMN IF NOT EXISTS valor_punto NUMERIC;")
            cur.execute("ALTER TABLE simbolos ADD COLUMN IF NOT EXISTS lote_min    NUMERIC;")
            cur.execute("ALTER TABLE simbolos ADD COLUMN IF NOT EXISTS lote_step   NUMERIC;")
            cur.execute("ALTER TABLE simbolos ADD COLUMN IF NOT EXISTS digits      INTEGER;")

            # ── v4.0 — Tabla risk_config (clave/valor, editable sin deploy) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS risk_config (
                    clave      TEXT PRIMARY KEY,
                    valor      TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            for clave, valor in RISK_CONFIG_DEFAULTS.items():
                cur.execute("""
                    INSERT INTO risk_config (clave, valor)
                    VALUES (%s, %s)
                    ON CONFLICT (clave) DO NOTHING;
                """, (clave, valor))

            # ── v4.0 — Tabla risk_decisions (auditoría del gate) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS risk_decisions (
                    id        SERIAL PRIMARY KEY,
                    ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    signal_id INTEGER,
                    ciclo_id  INTEGER,
                    decision  TEXT,        -- aprobada | rechazada
                    regla     TEXT,        -- regla que determinó la decisión
                    detalle   JSONB        -- contexto numérico del momento
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_risk_decisions_ts ON risk_decisions(ts DESC);")

            conn.commit()
            logger.info("Base de datos inicializada — esquema v4.0 listo (signals + ciclos + simbolos + risk_config + risk_decisions).")
    except Exception as e:
        logger.error(f"Error inicializando DB: {e}")
    finally:
        conn.close()

def safe_int(val, default=0):
    """Convierte un valor a int de forma segura. Retorna default si falla."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

# ─────────────────────────────────────────────────────────────
# Normalizadores canónicos (Fase 0.1 / 0.2). None-safe, reusables
# (los reusará el endpoint de ciclos en Fase 1.3).
#   dir → compra / venta
#   kz  → asia / london / new york   (alias "ny" → "new york")
#   par → UPPER  (consistencia de UNIQUE(par,fecha_ciclo) y del match)
# Valores desconocidos: se devuelven trimmed/lowercased SIN coerción,
# para que el CHECK de ciclos o el gate los rechacen explícitamente.
# ─────────────────────────────────────────────────────────────
_KZ_CANON = {
    "ny": "new york", "newyork": "new york", "new york": "new york",
    "asia": "asia", "london": "london", "londres": "london",
}

def _norm_dir(v):
    return str(v).strip().lower() if v is not None else None

def _norm_kz(v):
    if v is None:
        return None
    k = str(v).strip().lower()
    return _KZ_CANON.get(k, k)

def _norm_par(v):
    return str(v).strip().upper() if v is not None else None


def save_event_to_db(data: dict, tipo_msg: str, raw_json: str = None) -> bool:
    """Inserta el evento recibido en la tabla signals.
    Retorna True si tuvo éxito, False si falló.
    NUNCA lanza excepción al caller — el bot debe seguir funcionando aunque la DB falle."""
    if not DATABASE_URL:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signals (
                    tipo_msg, par, direccion, ciclo, kz, tipo, fvg_src, calidad,
                    cierre_fuerte, vela_comp, vic_robusta, daily,
                    ops_asia, ops_lon, ops_ny, pendientes,
                    sl, dist_obj, precio, low_vela, raw_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
            """, (
                tipo_msg,
                data.get("par"),
                data.get("direccion"),
                data.get("ciclo"),
                data.get("kz"),
                data.get("tipo"),
                data.get("fvg_src"),
                data.get("calidad"),
                data.get("cierre_fuerte"),
                data.get("vela_comp"),
                data.get("vic_robusta"),
                data.get("daily"),
                safe_int(data.get("ops_asia")),
                safe_int(data.get("ops_lon")),
                safe_int(data.get("ops_ny")),
                safe_int(data.get("pendientes")),
                data.get("sl"),
                data.get("dist_obj"),
                data.get("precio"),
                data.get("low_vela"),   # llegará cuando el Pine se actualice; mientras tanto queda NULL
                raw_json if raw_json is not None else json.dumps(data),
            ))
            conn.commit()
            logger.info(f"Evento '{tipo_msg}' guardado en DB.")
            return True
    except Exception as e:
        logger.error(f"Error guardando en DB: {e}")
        return False
    finally:
        conn.close()

# Inicializar DB al cargar el módulo (compatible con gunicorn).
# Se ejecutará una vez por worker. Todas las operaciones son idempotentes.
init_db()

# ═══════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Variables TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no definidas.")
        return False
    try:
        r = requests.post(TELEGRAM_URL, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        r.raise_for_status()
        logger.info("Mensaje enviado a Telegram.")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error Telegram: {e}")
        return False

def validate_secret(req) -> bool:
    if not WEBHOOK_SECRET:
        return True
    return hmac.compare_digest(req.headers.get("X-Webhook-Secret", ""), WEBHOOK_SECRET)

# ─────────────────────────────────────────────────────────────
# FASE 1.4 — Telegram inbound: helpers de callbacks de ciclos
# ─────────────────────────────────────────────────────────────

def tg_answer_callback(callback_query_id, text="") -> bool:
    """Responde un callback_query (toast efímero). Estilo idéntico a send_telegram."""
    if not TELEGRAM_TOKEN:
        logger.error("tg_answer_callback: TELEGRAM_TOKEN no definido.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error answerCallbackQuery: {e}")
        return False


def tg_edit_message(chat_id, message_id, text, reply_markup) -> bool:
    """Edita un mensaje existente (editMessageText, parse_mode=HTML)."""
    if not TELEGRAM_TOKEN:
        logger.error("tg_edit_message: TELEGRAM_TOKEN no definido.")
        return False
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error editMessageText: {e}")
        return False


def _build_ciclo_keyboard(rows):
    """InlineKeyboardMarkup: una fila [✅ N][❌ N] por ciclo pendiente.
    Los ciclos ya decididos no llevan botones. rows: dicts con id+estado."""
    keyboard = []
    for r in rows:
        if r.get("estado") != "pendiente":
            continue
        cid = r["id"]
        par = r["par"]
        keyboard.append([
            {"text": f"✅ {par}", "callback_data": f"ciclo:{cid}:ap"},
            {"text": f"❌ {par}", "callback_data": f"ciclo:{cid}:rc"},
        ])
    return {"inline_keyboard": keyboard}


def _render_reporte(ciclos_rows):
    """Texto HTML del reporte de ciclos del día. Marca ✅/❌ los decididos."""
    hoy = utc_now().astimezone(_NY_TZ).date()
    aprobados  = sum(1 for r in ciclos_rows if r.get("estado") == "aprobado")
    pendientes = sum(1 for r in ciclos_rows if r.get("estado") == "pendiente")
    lineas = [
        f"📋 <b>CICLOS — {hoy}</b>",
        f"Aprobados: {aprobados}/4 | Pendientes: {pendientes}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    for i, r in enumerate(ciclos_rows, start=1):
        estado = r.get("estado")
        marca = "✅" if estado == "aprobado" else "❌" if estado == "rechazado" else "•"
        par = r.get("par", "N/A")
        direccion = (r.get("direccion") or "").upper()
        tipo_daily = r.get("tipo_daily") or "—"
        liq_high = r.get("liq_high")
        liq_low = r.get("liq_low")
        proy = r.get("proyeccion")
        lineas.append(
            f"{marca} <b>{i}. {par}</b> {direccion} | {tipo_daily}\n"
            f"     liq: {liq_low}–{liq_high} | proy: {proy}"
        )
    return "\n".join(lineas)

def format_signal(data: dict) -> str:
    par        = data.get("par", "N/A").upper()
    dir_raw    = data.get("direccion", "").lower()
    kz_raw     = data.get("kz", "").lower()
    tipo_raw   = data.get("tipo", "").lower()
    calidad    = data.get("calidad", "ESTANDAR")
    cf         = data.get("cierre_fuerte", "no") == "si"
    v_comp     = data.get("vela_comp", "no") == "si"
    v_rob      = data.get("vic_robusta", "no") == "si"
    daily      = data.get("daily", "N/A")
    ops_a      = safe_int(data.get("ops_asia"))
    ops_l      = safe_int(data.get("ops_lon"))
    ops_n      = safe_int(data.get("ops_ny"))
    pend       = data.get("pendientes", "0")
    sl         = data.get("sl", "N/A")
    dist       = data.get("dist_obj", "No definido")
    precio     = data.get("precio", "N/A")
    fvg_src    = data.get("fvg_src", "N/A")
    now        = utc_now().strftime("%Y-%m-%d %H:%M UTC")

    dir_cfg  = DIR_CONFIG.get(dir_raw, {"emoji": "⚪", "label": dir_raw.upper(), "arrow": "➡️"})
    kz_label = KZ_EMOJIS.get(kz_raw, f"🕐 {kz_raw.upper()}")
    tipo_lbl = TIPO_EMOJIS.get(tipo_raw, f"📊 {tipo_raw.upper()}")

    cal_emoji = "⭐⭐⭐" if calidad == "ALTA" else "⭐⭐" if calidad == "MEDIA" else "⭐"
    total_ops = ops_a + ops_l + ops_n

    msg = f"""
🎯 <b>SETUP DETECTADO — SmartFlow v{VERSION_SISTEMA}</b>
━━━━━━━━━━━━━━━━━━━━━
📌 <b>Par:</b> {par}
{dir_cfg['emoji']} <b>Dirección:</b> {dir_cfg['arrow']} {dir_cfg['label']}
🕐 <b>Kill Zone:</b> {kz_label}
📊 <b>Tipo:</b> {tipo_lbl}
📈 <b>Daily:</b> {daily}
💰 <b>Precio:</b> {precio}
🛑 <b>SL:</b> {sl}
━━━━━━━━━━━━━━━━━━━━━
💡 <b>CONTEXTO</b>
{cal_emoji} <b>Calidad:</b> {calidad}
📦 <b>FVG:</b> {fvg_src}
🕯️ <b>Cierre:</b> {"Fuerte ✅" if cf else "Débil ⚠️"}
🔬 <b>Vela tomadora:</b> {"Comprimida ✅" if v_comp else "Normal"}
🔬 <b>Vela víctima:</b> {"Robusta ✅" if v_rob else "Normal"}
📏 <b>Dist. objetivo:</b> {dist}
━━━━━━━━━━━━━━━━━━━━━
📊 <b>KZs del ciclo:</b> 🌏{ops_a} 🌍{ops_l} 🗽{ops_n} | Total: {total_ops} | Pend: {pend}
━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>Valida tu checklist antes de entrar</b>
🕒 {now}
""".strip()
    return msg

def format_fin_dia(data: dict) -> str:
    par    = data.get("par", "N/A").upper()
    daily  = data.get("daily", "N/A")
    ops_a  = safe_int(data.get("ops_asia"))
    ops_l  = safe_int(data.get("ops_lon"))
    ops_n  = safe_int(data.get("ops_ny"))
    asia   = data.get("asia", "N/A")
    london = data.get("london", "N/A")
    ny     = data.get("ny", "N/A")
    total  = ops_a + ops_l + ops_n
    now    = utc_now().strftime("%Y-%m-%d %H:%M UTC")

    msg = f"""
⏰ <b>FIN DEL CICLO DE INVERSIÓN</b>
━━━━━━━━━━━━━━━━━━━━━
📌 <b>Par:</b> {par}
📈 <b>Confirmación daily:</b> {daily}
━━━━━━━━━━━━━━━━━━━━━
📊 <b>Resumen del ciclo:</b>
🌏 Asia: {ops_a} entrada(s) — {asia}
🌍 London: {ops_l} entrada(s) — {london}
🗽 NY: {ops_n} entrada(s) — {ny}
📈 <b>Total operaciones:</b> {total}
━━━━━━━━━━━━━━━━━━━━━
🕐 Inicio KZ Asia en curso
🕒 {now}
""".strip()
    return msg

# ═══════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────
# FASE 1.3 — Cálculo de fecha_ciclo y handler de ciclo
# ─────────────────────────────────────────────────────────────
_NY_TZ = ZoneInfo("America/New_York")

def fecha_ciclo(bar_time_ms) -> date:
    """Calcula el DATE del día de trading sobre el bar_time del Pine.
    Ventana del día sintético: [17:00 NY, +24h). Una barra con hora NY >= 17
    pertenece al día siguiente; < 17 al mismo día. DST lo maneja ZoneInfo.
    bar_time_ms: epoch UTC en milisegundos (string o int desde el Pine)."""
    ms = int(bar_time_ms)
    dt_utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    dt_ny = dt_utc.astimezone(_NY_TZ)
    base = dt_ny.date()
    return base + timedelta(days=1) if dt_ny.hour >= 17 else base


def _to_num(v):
    """Castea texto del Pine a float; None si no parsea o es placeholder."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.upper() in ("N/A", "NA"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def handle_ciclo(data: dict):
    """Procesa una alerta de ciclo (tipo_msg='ciclo') del Pine v3.5.
    Inserta/actualiza en ciclos. Idempotente vía UNIQUE(par,fecha_ciclo):
    si el ciclo ya existe y sigue 'pendiente', actualiza; si ya fue decidido,
    el reenvío se ignora (protege la decisión manual de Telegram).
    NUNCA lanza al caller — el webhook responde 200 igual."""
    par = _norm_par(data.get("par"))
    direccion = _norm_dir(data.get("direccion"))

    # Validación no-null (mismo criterio que la rama signal)
    if not par or not direccion:
        logger.warning(f"Ciclo rechazado (par/direccion nulos): par={par} dir={direccion}")
        return jsonify({"error": "Ciclo: par/direccion invalidos"}), 400
    if direccion not in ("compra", "venta"):
        logger.warning(f"Ciclo rechazado (direccion fuera de CHECK): {direccion}")
        return jsonify({"error": "Ciclo: direccion debe ser compra/venta"}), 400

    bar_time = data.get("bar_time")
    if not bar_time:
        logger.warning("Ciclo rechazado (sin bar_time)")
        return jsonify({"error": "Ciclo: falta bar_time"}), 400
    try:
        fc = fecha_ciclo(bar_time)
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Ciclo rechazado (bar_time invalido '{bar_time}'): {e}")
        return jsonify({"error": "Ciclo: bar_time invalido"}), 400

    tipo_daily = data.get("criterio")
    liq_high   = _to_num(data.get("liq_high"))
    liq_low    = _to_num(data.get("liq_low"))
    proyeccion = _to_num(data.get("proyeccion"))

    if not DATABASE_URL:
        logger.warning("Ciclo no guardado: DATABASE_URL ausente.")
        return jsonify({"status": "ok", "warning": "sin DB"}), 200
    conn = get_db_connection()
    if not conn:
        logger.error("Ciclo no guardado: conexión DB falló.")
        return jsonify({"status": "ok", "warning": "DB no disponible"}), 200
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ciclos (par, direccion, fecha_ciclo, tipo_daily,
                                    liq_high, liq_low, proyeccion, estado)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendiente')
                ON CONFLICT (par, fecha_ciclo) DO UPDATE SET
                    direccion  = EXCLUDED.direccion,
                    tipo_daily = EXCLUDED.tipo_daily,
                    liq_high   = EXCLUDED.liq_high,
                    liq_low    = EXCLUDED.liq_low,
                    proyeccion = EXCLUDED.proyeccion
                WHERE ciclos.estado = 'pendiente';
            """, (par, direccion, fc, tipo_daily, liq_high, liq_low, proyeccion))
            conn.commit()
            logger.info(f"Ciclo guardado: {par} {direccion} {fc} ({tipo_daily})")
            return jsonify({"status": "ok", "ciclo": f"{par} {direccion} {fc}"}), 200
    except Exception as e:
        logger.error(f"Error guardando ciclo: {e}")
        return jsonify({"status": "error"}), 500
    finally:
        conn.close()


@app.route("/webhook", methods=["POST"])
def webhook():
    if not validate_secret(request):
        logger.warning("Secret inválido.")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        try:
            data = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid payload"}), 400

    logger.info(f"Payload recibido: {data}")
    tipo = data.get("tipo_msg", "signal")

    # ─── FASE 1.3 — Rama de ciclo (corta temprano: no toca signals ni la
    # validación 0.3, que exige kz/tipo que el ciclo no manda) ───
    if tipo == "ciclo":
        return handle_ciclo(data)

    # ─── 0.3 Validación NO-NULL ANTES del INSERT ───
    # {"par": null} ya no llega al INSERT ni a los formatters: 400 acá.
    # Mata el AttributeError → 500 → reintento de TV → fila duplicada.
    if tipo != "fin_dia":
        required = ["par", "direccion", "kz", "tipo"]
        bad = [f for f in required if not str(data.get(f) or "").strip()]
        if bad:
            logger.warning(f"Senal rechazada (campos nulos/vacios): {bad}")
            return jsonify({"error": f"Campos invalidos: {bad}"}), 400

    # raw_payload CRUDO: serializado ANTES de normalizar (valor forense:
    # preserva el casing exacto que mandó Pine).
    raw_json = json.dumps(data)

    # ─── 0.1/0.2 Normalización canónica AL RECIBIR (dir / kz / par) ───
    if tipo != "fin_dia":
        data["par"]       = _norm_par(data.get("par"))
        data["direccion"] = _norm_dir(data.get("direccion"))
        data["kz"]        = _norm_kz(data.get("kz"))

    # ─── Guardado en DB ANTES de Telegram ──────────────────
    # Doble try/except — la DB nunca puede romper el envío a Telegram
    try:
        save_event_to_db(data, tipo, raw_json=raw_json)
    except Exception as e:
        logger.error(f"Error inesperado al guardar en DB: {e}")
    # ───────────────────────────────────────────────────────

    if tipo == "fin_dia":
        message = format_fin_dia(data)
    else:
        message = format_signal(data)

    success = send_telegram(message)
    return (jsonify({"status": "ok"}), 200) if success else (jsonify({"status": "error"}), 500)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "online",
        "service": f"SmartFlow Backend v{VERSION_BACKEND} (sistema v{VERSION_SISTEMA})",
        "timestamp": utc_now().isoformat()
    }), 200

@app.route("/test", methods=["GET"])
def test():
    msg = (f"🔔 <b>SmartFlow v{VERSION_SISTEMA} — Prueba de conexión</b>\n"
           "━━━━━━━━━━━━━━━━━━━━━\n"
           f"✅ Servidor activo y conectado (backend v{VERSION_BACKEND}).\n"
           "🤖 Las alertas de TradingView llegarán aquí.")
    success = send_telegram(msg)
    return (jsonify({"status": "ok", "message": "Prueba enviada"}), 200) if success else (jsonify({"status": "error"}), 500)

# ─── Endpoint para consultar señales guardadas ────────────
@app.route("/signals", methods=["GET"])
def list_signals():
    """Retorna las últimas 100 señales guardadas en formato JSON."""
    if not DATABASE_URL:
        return jsonify({"error": "Database not configured"}), 503
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 503
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                    id, received_at, tipo_msg, par, direccion, ciclo, kz, tipo,
                    fvg_src, calidad, cierre_fuerte, vela_comp, vic_robusta, daily,
                    ops_asia, ops_lon, ops_ny, pendientes, sl, dist_obj, precio,
                    ciclo_id, gate_decision, outcome, pnl_r, mfe_r, mae_r, low_vela
                FROM signals
                ORDER BY received_at DESC
                LIMIT 100
            """)
            rows = cur.fetchall()
            for row in rows:
                if row.get("received_at"):
                    row["received_at"] = row["received_at"].isoformat()
                for k in ("pnl_r", "pnl_dinero", "mfe_r", "mae_r"):
                    if row.get(k) is not None:
                        row[k] = float(row[k])
            return jsonify({"count": len(rows), "signals": rows}), 200
    except Exception as e:
        logger.error(f"Error consultando signals: {e}")
        return jsonify({"error": "Query failed"}), 500
    finally:
        conn.close()

# ─────────────────────────────────────────────────────────────
# FASE 1.4 — Telegram inbound: receptor de callbacks de aprobación
# ─────────────────────────────────────────────────────────────
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    # (a) Validación FAIL-CLOSED del secret token de Telegram.
    #     A diferencia de /webhook, si no hay secret configurado se RECHAZA.
    if not TELEGRAM_WEBHOOK_SECRET:
        logger.warning("/telegram: TELEGRAM_WEBHOOK_SECRET no configurado — rechazo (fail-closed).")
        return jsonify({"error": "Unauthorized"}), 401
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token, TELEGRAM_WEBHOOK_SECRET):
        logger.warning("/telegram: secret token inválido.")
        return jsonify({"error": "Unauthorized"}), 401

    update = request.get_json(silent=True) or {}
    cq = update.get("callback_query")
    if not cq:
        return ("", 200)  # otros updates (mensajes, etc.): ignorar

    # (i) Todo el cuerpo en try/except que loguee y devuelva 200:
    #     Telegram reintenta ante !=200; un bug de formato no debe crear loops.
    try:
        # (b) Filtro por chat autorizado.
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            logger.warning(f"/telegram: chat no autorizado: {chat_id}")
            return ("", 200)

        # (c) Parseo de callback_data "ciclo:<id>:<ap|rc>".
        parts = (cq.get("data") or "").split(":")
        if len(parts) != 3 or parts[0] != "ciclo":
            tg_answer_callback(cq.get("id"), "Callback no reconocido")
            return ("", 200)
        ciclo_id = safe_int(parts[1], 0)
        accion = parts[2]
        if ciclo_id <= 0 or accion not in ("ap", "rc"):
            tg_answer_callback(cq.get("id"), "Callback inválido")
            return ("", 200)

        message_id = msg.get("message_id")

        if not DATABASE_URL:
            tg_answer_callback(cq.get("id"), "Sin DB")
            return ("", 200)
        conn = get_db_connection()
        if not conn:
            tg_answer_callback(cq.get("id"), "DB no disponible")
            return ("", 200)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # (d) Leer created_at + fecha_ciclo del ciclo para las guardias.
                cur.execute("SELECT id, created_at, fecha_ciclo, estado FROM ciclos WHERE id = %s", (ciclo_id,))
                ciclo = cur.fetchone()
                if not ciclo:
                    tg_answer_callback(cq.get("id"), "Ciclo inexistente")
                    return ("", 200)
                fc = ciclo["fecha_ciclo"]

                # (e) GUARDIA deadline: 19:30 NY del día en que LLEGÓ el ciclo (created_at).
                ahora_ny = utc_now().astimezone(_NY_TZ)
                llegada_ny = ciclo["created_at"].astimezone(_NY_TZ)
                deadline = datetime(llegada_ny.year, llegada_ny.month, llegada_ny.day, 19, 30, tzinfo=_NY_TZ)
                if ahora_ny > deadline:
                    tg_answer_callback(cq.get("id"), "Expirado, pasó el deadline 19:30 NY")
                    return ("", 200)

                # (f) GUARDIA max-4 aprobados del día (solo en 'ap').
                if accion == "ap":
                    cur.execute(
                        "SELECT count(*) AS n FROM ciclos WHERE fecha_ciclo = %s AND estado = 'aprobado'",
                        (fc,),
                    )
                    if cur.fetchone()["n"] >= 4:
                        tg_answer_callback(cq.get("id"), "Ya tenés 4 ciclos aprobados")
                        return ("", 200)

                # (g) UPDATE atómico: solo transiciona si sigue 'pendiente'.
                nuevo_estado = "aprobado" if accion == "ap" else "rechazado"
                cur.execute(
                    "UPDATE ciclos SET estado = %s, decided_at = %s WHERE id = %s AND estado = 'pendiente'",
                    (nuevo_estado, utc_now(), ciclo_id),
                )
                if cur.rowcount == 0:
                    conn.commit()
                    tg_answer_callback(cq.get("id"), "Ese ciclo ya estaba decidido")
                    return ("", 200)
                conn.commit()

                # (h) Toast de confirmación + re-render del mensaje.
                tg_answer_callback(cq.get("id"), "Aprobado ✅" if accion == "ap" else "Rechazado ❌")

                # IDs de ciclo presentes en ESTE mensaje (de su reply_markup).
                ids = []
                for fila in (msg.get("reply_markup") or {}).get("inline_keyboard", []):
                    for btn in fila:
                        bp = (btn.get("callback_data") or "").split(":")
                        if len(bp) == 3 and bp[0] == "ciclo":
                            bid = safe_int(bp[1], 0)
                            if bid > 0 and bid not in ids:
                                ids.append(bid)
                if ciclo_id not in ids:
                    ids.append(ciclo_id)

                cur.execute(
                    """SELECT id, par, direccion, tipo_daily, liq_high, liq_low, proyeccion, estado
                       FROM ciclos WHERE id = ANY(%s) ORDER BY created_at""",
                    (ids,),
                )
                rows = cur.fetchall()
                tg_edit_message(chat_id, message_id, _render_reporte(rows), _build_ciclo_keyboard(rows))
                return ("", 200)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"/telegram error: {e}")
        return ("", 200)

# ═══════════════════════════════════════════════════════════
#  ENTRADA LOCAL (gunicorn no ejecuta este bloque en Railway)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"SmartFlow Backend v{VERSION_BACKEND} iniciando en puerto {port}...")
    app.run(host="0.0.0.0", port=port)
