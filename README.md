# SmartFlow Signals 🎯

Servidor webhook para recibir señales de TradingView y enviarlas a Telegram.

## Variables de entorno requeridas en Railway

| Variable | Descripción |
|---|---|
| `TELEGRAM_TOKEN` | Token de tu bot de Telegram (@BotFather) |
| `TELEGRAM_CHAT_ID` | Tu Chat ID de Telegram |
| `WEBHOOK_SECRET` | (Opcional) Clave secreta para mayor seguridad |

## Endpoints

| Ruta | Método | Descripción |
|---|---|---|
| `/webhook` | POST | Recibe señales de TradingView |
| `/health` | GET | Verifica que el servidor está activo |
| `/test` | GET | Envía mensaje de prueba a Telegram |

## Formato del payload (TradingView → servidor)

```json
{
  "par": "EURUSD",
  "direccion": "compra",
  "kz": "london",
  "tipo": "fvg",
  "condiciones": 4,
  "sl": "1.0820",
  "tp": "1.0920",
  "timeframe": "H1"
}
```

## Valores válidos

- **direccion:** `compra` / `venta` / `buy` / `sell`
- **kz:** `asia` / `london` / `ny`
- **tipo:** `fvg` / `liquidez` / `fvg+liq`
- **condiciones:** número del 1 al 4
