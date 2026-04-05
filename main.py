import os
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import hmac
import hashlib

# ─────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET", "")  # Seguridad opcional

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ─────────────────────────────────────────
# EMOJIS Y MAPEOS
# ─────────────────────────────────────────
KZ_EMOJIS = {
    "asia":   "🌏 Asia",
    "london": "🌍 London",
    "ny":     "🗽 New York",
    "nueva york": "🗽 New York",
}

DIRECTION_CONFIG = {
    "compra": {"emoji": "🟢", "label": "COMPRA",  "color": "🔼"},
    "venta":  {"emoji": "🔴", "label": "VENTA",   "color": "🔽"},
    "buy":    {"emoji": "🟢", "label": "COMPRA",  "color": "🔼"},
    "sell":   {"emoji": "🔴", "label": "VENTA",   "color": "🔽"},
}

SIGNAL_EMOJIS = {
    "fvg":        "📦 FVG H1",
    "liquidez":   "💧 Toma de Liquidez",
    "liquidity":  "💧 Toma de Liquidez",
    "fvg+liq":    "📦💧 FVG + Liquidez",
}

# ─────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────
def send_telegram(message: str) -> bool:
    """Envía mensaje a Telegram con manejo de errores."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Variables de entorno TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no definidas.")
        return False
    try:
        response = requests.post(TELEGRAM_URL, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        response.raise_for_status()
        logger.info("Mensaje enviado a Telegram correctamente.")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error al enviar mensaje a Telegram: {e}")
        return False


def format_signal_message(data: dict) -> str:
    """Formatea el mensaje de señal para Telegram."""
    par        = data.get("par", "N/A").upper()
    direccion  = data.get("direccion", "").lower()
    kz_raw     = data.get("kz", "").lower()
    tipo_raw   = data.get("tipo", "").lower()
    condiciones = int(data.get("condiciones", 0))
    sl         = data.get("sl", None)
    tp         = data.get("tp", None)
    timeframe  = data.get("timeframe", "H1")
    now        = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Mapeos
    dir_config  = DIRECTION_CONFIG.get(direccion, {"emoji": "⚪", "label": direccion.upper(), "color": "➡️"})
    kz_label    = KZ_EMOJIS.get(kz_raw, f"🕐 {kz_raw.upper()}")
    signal_type = SIGNAL_EMOJIS.get(tipo_raw, f"📊 {tipo_raw.upper()}")

    # Barra de condiciones
    total_condiciones = 4
    barra = "✅" * condiciones + "⬜" * (total_condiciones - condiciones)
    fuerza = "FUERTE 🔥" if condiciones == 4 else "MODERADA ⚡" if condiciones == 3 else "DÉBIL ⚠️"

    # Construcción del mensaje
    msg = f"""
🎯 <b>SETUP DETECTADO — SmartFlow</b>
━━━━━━━━━━━━━━━━━━━━━
📌 <b>Par:</b> {par}
{dir_config['emoji']} <b>Dirección:</b> {dir_config['color']} {dir_config['label']}
🕐 <b>Kill Zone:</b> {kz_label}
📊 <b>Tipo:</b> {signal_type}
⏱ <b>Timeframe:</b> {timeframe}
━━━━━━━━━━━━━━━━━━━━━
📶 <b>Confluencias:</b> {barra} ({condiciones}/{total_condiciones})
💪 <b>Fuerza:</b> {fuerza}"""

    if sl:
        msg += f"\n🛑 <b>Stop Loss:</b> {sl}"
    if tp:
        msg += f"\n🎯 <b>Take Profit:</b> {tp}"

    msg += f"""
━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>Valida tu checklist antes de entrar</b>
🕒 {now}
"""
    return msg.strip()


def validate_secret(req) -> bool:
    """Valida el secret del webhook si está configurado."""
    if not WEBHOOK_SECRET:
        return True
    secret_header = req.headers.get("X-Webhook-Secret", "")
    return hmac.compare_digest(secret_header, WEBHOOK_SECRET)


# ─────────────────────────────────────────
# RUTAS
# ─────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe la señal de TradingView y la reenvía a Telegram."""

    # Validación de seguridad
    if not validate_secret(request):
        logger.warning("Intento de acceso con secret inválido.")
        return jsonify({"error": "Unauthorized"}), 401

    # Parseo del payload
    data = request.get_json(silent=True)
    if not data:
        # TradingView a veces manda texto plano
        try:
            import json
            data = json.loads(request.data.decode("utf-8"))
        except Exception:
            logger.error("Payload inválido recibido.")
            return jsonify({"error": "Invalid payload"}), 400

    logger.info(f"Señal recibida: {data}")

    # Validación mínima de campos requeridos
    required = ["par", "direccion", "kz", "tipo"]
    missing = [f for f in required if f not in data]
    if missing:
        logger.warning(f"Campos faltantes en payload: {missing}")
        return jsonify({"error": f"Campos requeridos faltantes: {missing}"}), 400

    # Formatear y enviar
    message = format_signal_message(data)
    success = send_telegram(message)

    if success:
        return jsonify({"status": "ok", "message": "Señal enviada a Telegram"}), 200
    else:
        return jsonify({"status": "error", "message": "Error al enviar a Telegram"}), 500


@app.route("/health", methods=["GET"])
def health():
    """Endpoint de salud para verificar que el servidor está activo."""
    return jsonify({
        "status": "online",
        "service": "SmartFlow Signals",
        "timestamp": datetime.utcnow().isoformat()
    }), 200


@app.route("/test", methods=["GET"])
def test():
    """Envía un mensaje de prueba a Telegram."""
    msg = """
🔔 <b>SmartFlow Signals — Prueba de conexión</b>
━━━━━━━━━━━━━━━━━━━━━
✅ El servidor está activo y conectado correctamente.
🤖 Las alertas de TradingView llegarán aquí.
━━━━━━━━━━━━━━━━━━━━━
    """.strip()
    success = send_telegram(msg)
    if success:
        return jsonify({"status": "ok", "message": "Mensaje de prueba enviado"}), 200
    else:
        return jsonify({"status": "error", "message": "Fallo al enviar prueba"}), 500


# ─────────────────────────────────────────
# INICIO
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"SmartFlow Signals iniciando en puerto {port}...")
    app.run(host="0.0.0.0", port=port)
