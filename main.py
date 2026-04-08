import os
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import hmac

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "")
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

@app.route("/webhook", methods=["POST"])
def webhook():
    if not validate_secret(request):
        logger.warning("Secret inválido.")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        try:
            import json
            data = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid payload"}), 400

    logger.info(f"Payload recibido: {data}")
    tipo = data.get("tipo_msg", "signal")

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"SmartFlow v3.0 iniciando en puerto {port}...")
    app.run(host="0.0.0.0", port=port)
