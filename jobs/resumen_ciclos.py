"""
Cron de resumen de ciclos — servicio Railway separado (mismo repo, distinto start command).
Lee ciclos sin notificar, manda UN reporte consolidado con teclado, marca notificado_at.
Reusa helpers validados de main.py. import main dispara init_db() idempotente (L350) — OK.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import requests                       # alinear con tg_edit_message/send_telegram
from psycopg.rows import dict_row

import main                           # trae helpers + constantes; init_db idempotente

_NY = ZoneInfo("America/New_York")
SETTLE_SECONDS = 60                   # asentamiento: no mandar reporte parcial mid-insert

def _ny_now():
    return datetime.now(_NY)

def _send_with_keyboard(text, reply_markup):
    """
    ENVÍO INICIAL del reporte (sendMessage + reply_markup).
    Mecánica alineada VERBATIM a send_telegram / tg_edit_message de main.py:
    lib requests, json=payload, reply_markup como dict (NO json.dumps),
    parse_mode="HTML", timeout=10. Token/chat tomados de las CONSTANTES de main.
    DIVERGENCIA INTENCIONAL: NO swallowea el error (a diferencia de send_telegram,
    que captura RequestException y retorna False). Acá raise_for_status() propaga,
    para que run() haga rollback y deje notificado_at en NULL (semántica at-least-once:
    si el envío falla, no marcamos → reintento en el próximo cron).
    """
    token = main.TELEGRAM_TOKEN       # CHANGED: constante de main.py (L16), no os.environ
    chat_id = main.TELEGRAM_CHAT_ID   # CHANGED: constante de main.py (L17), no os.environ
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",         # mismo parse_mode que send_telegram y que emite _render_reporte
        "reply_markup": reply_markup, # dict directo, igual que tg_edit_message (json= serializa)
    }
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

def run():
    # Gate día sintético: solo después de 17:00 NY. La ventana cron UTC 21-23 cubre
    # EDT (21:00 UTC=17 NY) y EST (22:00 UTC=17 NY); el gate no-opea la hora previa.
    if _ny_now().hour < 17:
        print(f"[resumen] {_ny_now():%H:%M NY} — antes de 17:00 NY, skip.")
        return

    conn = main.get_db_connection()
    if conn is None:
        print("[resumen] sin conexión DB, skip.")
        return

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # Reclamo atómico: solapes (Telegram lento) skipean lo lockeado → cero duplicado.
            # Sin filtro de fecha (solo notificado_at IS NULL) → ninguna fila queda huérfana.
            # age vía NOW()-created_at en Postgres: evita mismatch naive/aware en Python.
            cur.execute("""
                SELECT id, par, direccion, fecha_ciclo, tipo_daily,
                       liq_high, liq_low, proyeccion, estado,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_s
                FROM ciclos
                WHERE notificado_at IS NULL
                ORDER BY fecha_ciclo, id
                FOR UPDATE SKIP LOCKED
            """)
            rows = cur.fetchall()

            if not rows:
                print("[resumen] nada pendiente.")
                conn.rollback()
                return

            # El ciclo más NUEVO debe tener > SETTLE_SECONDS (los 4 llegan mismo segundo).
            youngest = min(r["age_s"] for r in rows)
            if youngest < SETTLE_SECONDS:
                print(f"[resumen] asentando ({youngest:.0f}s < {SETTLE_SECONDS}s), skip.")
                conn.rollback()
                return

            ids = [r["id"] for r in rows]
            texto = main._render_reporte(rows)
            teclado = main._build_ciclo_keyboard(rows)

            # Orden send → mark (at-least-once): primero mando, después marco.
            _send_with_keyboard(texto, teclado)
            cur.execute(
                "UPDATE ciclos SET notificado_at = NOW() WHERE id = ANY(%s)",
                (ids,),
            )
            conn.commit()
            print(f"[resumen] reporte enviado, {len(ids)} ciclos marcados: {ids}")
    except Exception as e:
        conn.rollback()
        print(f"[resumen] ERROR: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    run()
