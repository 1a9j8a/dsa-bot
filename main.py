import os
import logging
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import httpx

# -----------------------------------------------------------------------------
# CONFIGURAÇÃO BÁSICA
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  |  %(levelname)5s  | %(message)s",
)

load_dotenv()

ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN")  # opcional, mas sua instância está pedindo
CATALOG_URL = os.getenv(
    "CATALOG_URL",
    # coloque aqui o link final do catálogo, se quiser fixo no código:
    # "https://www.dropbox.com/scl/fi/2yezy6v6fo89t0z00c2vn/LINHA-MOVELEIRA-REZYMOL-3-2.pdf?dl=1"
)

if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
    logging.warning("⚠️ ZAPI_INSTANCE_ID ou ZAPI_TOKEN não configurados. Verifique o .env / Render.")

BASE_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}"

app = FastAPI(title="DSA Bot", version="1.0.0")


# -----------------------------------------------------------------------------
# FUNÇÕES AUXILIARES Z-API
# -----------------------------------------------------------------------------
def _zapi_headers() -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
    }
    if ZAPI_CLIENT_TOKEN:
        headers["Client-Token"] = ZAPI_CLIENT_TOKEN
    else:
        logging.warning("⚠️ ZAPI_CLIENT_TOKEN não definido. "
                        "Se a sua instância exige Client Token, a Z-API retornará erro.")
    return headers


async def send_whatsapp_text(phone: str, message: str) -> None:
    """
    Envia uma mensagem de texto simples via endpoint oficial /send-text.
    NÃO tenta outros endpoints.
    """
    message = (message or "").strip()
    if not message:
        logging.error("Tentativa de enviar mensagem vazia para %s. Abortando envio.", phone)
        return

    if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
        logging.error("ZAPI_INSTANCE_ID ou ZAPI_TOKEN não configurados. Não é possível enviar mensagem.")
        return

    url = f"{BASE_URL}/send-text"
    payload = {"phone": phone, "message": message}

    logging.info('=> Enviando texto para %s via /send-text: %r', phone, message)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=_zapi_headers())
        logging.info("<= Resposta Z-API /send-text: %s | %s", resp.status_code, resp.text)
    except Exception as e:
        logging.exception("Erro ao enviar mensagem para %s: %s", phone, e)


# -----------------------------------------------------------------------------
# LÓGICA DE ATENDIMENTO
# -----------------------------------------------------------------------------
def montar_menu_principal() -> str:
    return (
        "Olá! 👋\n"
        "Sou o assistente da *DSA Cristal Química / Rezymol*.\n\n"
        "Escolha uma opção:\n"
        "1️⃣ Falar com um atendente\n"
        "2️⃣ Receber o *Catálogo Promocional Linha Moveleira*\n\n"
        "Ou envie a palavra *promoção* para receber o catálogo diretamente. 😉"
    )


def montar_msg_catalogo() -> str:
    link = CATALOG_URL or "https://www.dropbox.com/scl/fi/2yezy6v6fo89t0z00c2vn/LINHA-MOVELEIRA-REZYMOL-3-2.pdf?dl=1"
    return (
        "📘 *Catálogo Promocional - Linha Moveleira Rezymol*\n\n"
        "Aqui está o link para você acessar o catálogo completo em PDF:\n"
        f"{link}\n\n"
        "Se precisar de um atendimento personalizado, responda com *1* "
        "que um atendente da DSA Cristal Química vai falar com você. 🙂"
    )


# -----------------------------------------------------------------------------
# ROTAS
# -----------------------------------------------------------------------------
@app.get("/")
async def root() -> Dict[str, Any]:
    return {"status": "ok", "message": "DSA Bot rodando."}


@app.post("/api/webhook/receber")
async def receber_webhook(request: Request) -> JSONResponse:
    """
    Endpoint chamado pela Z-API.
    """
    body = await request.json()
    logging.info("CORPO BRUTO : %s", body)

    try:
        phone = body.get("phone")
        text_obj = body.get("text") or {}
        message_text = (text_obj.get("message") or "").strip()
    except Exception as e:
        logging.exception("Erro ao extrair dados do webhook: %s", e)
        raise HTTPException(status_code=400, detail="Payload inválido")

    if not phone:
        logging.error("Webhook recebido sem 'phone'. Payload: %s", body)
        raise HTTPException(status_code=400, detail="Campo 'phone' ausente")

    # Normaliza texto
    msg_lower = message_text.lower()

    # Se não veio texto, manda um aviso simples
    if not msg_lower:
        await send_whatsapp_text(
            phone,
            "Olá! Recebi sua mensagem, mas não entendi o conteúdo. "
            "Envie um texto para que eu possa te ajudar. 🙂",
        )
        return JSONResponse({"ok": True})

    # ---- REGRAS SIMPLES DE ATENDIMENTO ----
    if "promo" in msg_lower or msg_lower == "2":
        # Envia link do catálogo
        await send_whatsapp_text(phone, montar_msg_catalogo())

    elif msg_lower == "1":
        # Falar com atendente
        await send_whatsapp_text(
            phone,
            "Perfeito! 🙌\n"
            "Vou encaminhar seu contato para um atendente da *DSA Cristal Química*.\n"
            "Em breve alguém vai falar com você. 😊",
        )

    else:
        # Qualquer outra coisa → mostra menu
        await send_whatsapp_text(phone, montar_menu_principal())

    return JSONResponse({"ok": True})
