import os
import logging
import json
import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from datetime import datetime
import hmac

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "")
DATABASE_URL     = os.environ.get("DATABASE_URL", "")
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

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

# ═══════════════════════════════════════════════════════════
#  DATABASE — PostgreSQL (nuevo en Fase 1)
# ═══════════════════════════════════════════════════════════

def get_db_connection():
    """Abre conexión a PostgreSQL con timeout de 10s. Retorna None si falla."""
    if not DATABASE_URL:
        return None
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:
        logger.error(f"Error conectando a DB: {e}")
        return None

def init_db():
    """Crea la tabla signals si no existe. Idempotente — seguro de ejecutar múltiples veces."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no configurada — persistencia deshabilitada, bot funcionará igual.")
        return
    conn = get_db_connection()
    if not conn:
        logger.error("No se pudo inicializar DB (conexión falló) — bot funcionará sin persistencia.")
        return
    try:
        with conn.cursor() as cur:
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
            conn.commit()
            logger.info("Base de datos inicializada — tabla 'signals' lista.")
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

def save_event_to_db(data: dict, tipo_msg: str) -> bool:
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
                    sl, dist_obj, precio, raw_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
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
                json.dumps(data),
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
# Se ejecutará una vez por worker. CREATE TABLE IF NOT EXISTS es idempotente.
init_db()

# ═══════════════════════════════════════════════════════════
#  TELEGRAM — sin cambios respecto a la versión anterior
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
    ops_a      = data.get("ops_asia", "0")
    ops_l      = data.get("ops_lon", "0")
    ops_n      = data.get("ops_ny", "0")
    pend       = data.get("pendientes", "0")
    sl         = data.get("sl", "N/A")
    dist       = data.get("dist_obj", "No definido")
    precio     = data.get("precio", "N/A")
    fvg_src    = data.get("fvg_src", "N/A")
    rr_ok      = data.get("rr_ok", "no") == "si"
    now        = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    dir_cfg  = DIR_CONFIG.get(dir_raw, {"emoji": "⚪", "label": dir_raw.upper(), "arrow": "➡️"})
    kz_label = KZ_EMOJIS.get(kz_raw, f"🕐 {kz_raw.upper()}")
    tipo_lbl = TIPO_EMOJIS.get(tipo_raw, f"📊 {tipo_raw.upper()}")

    cal_emoji = "⭐⭐⭐" if calidad == "ALTA" else "⭐⭐" if calidad == "MEDIA" else "⭐"
    total_ops = int(ops_a) + int(ops_l) + int(ops_n)

    msg = f"""
🎯 <b>SETUP DETECTADO — SmartFlow v3.0</b>
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
📏 <b>Dist. objetivo:</b> {("✅ " if rr_ok else "⚠️ ") + dist}
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
    ops_a  = data.get("ops_asia", "0")
    ops_l  = data.get("ops_lon", "0")
    ops_n  = data.get("ops_ny", "0")
    asia   = data.get("asia", "N/A")
    london = data.get("london", "N/A")
    ny     = data.get("ny", "N/A")
    total  = int(ops_a) + int(ops_l) + int(ops_n)
    now    = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

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

    # ─── Guardado en DB ANTES de Telegram ──────────────────
    # Doble try/except — la DB nunca puede romper el envío a Telegram
    try:
        save_event_to_db(data, tipo)
    except Exception as e:
        logger.error(f"Error inesperado al guardar en DB: {e}")
    # ───────────────────────────────────────────────────────

    if tipo == "fin_dia":
        message = format_fin_dia(data)
    else:
        required = ["par", "direccion", "kz", "tipo"]
        missing = [f for f in required if f not in data]
        if missing:
            return jsonify({"error": f"Campos faltantes: {missing}"}), 400
        message = format_signal(data)

    success = send_telegram(message)
    return (jsonify({"status": "ok"}), 200) if success else (jsonify({"status": "error"}), 500)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "service": "SmartFlow v3.0", "timestamp": datetime.utcnow().isoformat()}), 200

@app.route("/test", methods=["GET"])
def test():
    msg = "🔔 <b>SmartFlow v3.0 — Prueba de conexión</b>\n━━━━━━━━━━━━━━━━━━━━━\n✅ Servidor activo y conectado.\n🤖 Las alertas de TradingView llegarán aquí."
    success = send_telegram(msg)
    return (jsonify({"status": "ok", "message": "Prueba enviada"}), 200) if success else (jsonify({"status": "error"}), 500)

# ─── NUEVO: endpoint para consultar señales guardadas ────
@app.route("/signals", methods=["GET"])
def list_signals():
    """Retorna las últimas 100 señales guardadas en formato JSON."""
    if not DATABASE_URL:
        return jsonify({"error": "Database not configured"}), 503
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    id, received_at, tipo_msg, par, direccion, ciclo, kz, tipo,
                    fvg_src, calidad, cierre_fuerte, vela_comp, vic_robusta, daily,
                    ops_asia, ops_lon, ops_ny, pendientes, sl, dist_obj, precio
                FROM signals
                ORDER BY received_at DESC
                LIMIT 100
            """)
            rows = cur.fetchall()
            for row in rows:
                if row.get("received_at"):
                    row["received_at"] = row["received_at"].isoformat()
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
    logger.info(f"SmartFlow v3.0 iniciando en puerto {port}...")
    app.run(host="0.0.0.0", port=port)
