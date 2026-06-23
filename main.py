import os
import logging
import json
import requests
import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, jsonify
from datetime import datetime, timezone
import hmac

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
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

# ═══════════════════════════════════════════════════════════
#  ENTRADA LOCAL (gunicorn no ejecuta este bloque en Railway)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"SmartFlow Backend v{VERSION_BACKEND} iniciando en puerto {port}...")
    app.run(host="0.0.0.0", port=port)
