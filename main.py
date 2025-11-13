# main.py
import os
import re
import json
import logging
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Configuração e utilidades
# -----------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

ZAPI_BASE = os.getenv("ZAPI_BASE", "https://api.z-api.io").rstrip("/")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID", "").strip()
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "").strip()
CATALOG_URL_ORIG = os.getenv("CATALOG_URL", "").strip()

app = FastAPI(title="DSA Bot - Webhook")

# Memória de sessão em RAM (simples)
SESSIONS: Dict[str, Dict[str, Any]] = {}

MENU_TXT = (
    "Olá! 😊 Tudo bem? Prazer em te conhecer.\n\n"
    "⚡ Eu sou o Spark, assistente virtual da *DSA Cristal Química*.\n"
    "Como posso te ajudar hoje?\n\n"
    "1 - *Produtos Rezymol*\n"
    "2 - *Compras*\n"
    "3 - *Catálogo Rezymol*\n"
    "4 - *Falar com um atendente/especialista*\n\n"
    "Você pode digitar o número da opção ou escrever sua dúvida.\n"
    "Comandos rápidos: *compra, catálogo, produtos, menu*."
)

PRODUCTS_TXT = (
    "Conheça nossa *Linha Rezymol – Setor Moveleiro* 🪵\n\n"
    "• Fluido Antiaderente (coladeiras de borda)\n"
    "• Fluido Resfriador (coladeiras de borda)\n"
    "• Fluido Antiestático (coladeiras de borda)\n"
    "• Fluido Finalizador (coladeiras de borda)\n"
    "• Limpa Chapas / Remoção de Colas\n"
    "• Limpa Chapas / Peças / Finalizador\n"
    "• Limpa Coleiros\n"
    "• Desengraxantes Protetivo e Mãos\n"
    "• Removedor de Resinas\n"
    "• Removedor de Tintas Anilox\n\n"
    "📘 *Para solicitar catálogo*, digite *3* ou *catálogo*.\n"
    "🛒 *Para comprar agora*, digite *2* ou *compra*."
)

PROFILE_TXT = (
    "Qual é o seu *Perfil*?\n"
    "1) *Cliente*\n"
    "2) *Distribuidor*\n"
    "3) *Representante*\n"
    "4) *Fornecedor de Produtos - Matéria Prima*"
)

# -----------------------------------------------------------------------------
# Funções auxiliares
# -----------------------------------------------------------------------------
def dropbox_to_direct(url: str) -> str:
    """
    Converte links do Dropbox (qualquer variação de domínio/parâmetro) em link
    de download direto (dl.dropboxusercontent.com) e remove querystring.
    """
    if not url:
        return url
    parsed = urlparse(url)
    host = parsed.netloc
    # Normaliza domínio
    if "dropbox" in host:
        new_netloc = "dl.dropboxusercontent.com"
        # Remove query string
        new_url = urlunparse((
            parsed.scheme or "https",
            new_netloc,
            parsed.path,
            "", "", ""   # no params / query / fragment
        ))
        return new_url
    return url

CATALOG_URL_DIRECT = dropbox_to_direct(CATALOG_URL_ORIG)

def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return s.strip()

def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def get_session(chat_id: str) -> Dict[str, Any]:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {
            "flow": None,
            "step": None,
            "data": {},
        }
    return SESSIONS[chat_id]

def reset_session(chat_id: str):
    if chat_id in SESSIONS:
        SESSIONS.pop(chat_id, None)

# -----------------------------------------------------------------------------
# Integração Z-API
# -----------------------------------------------------------------------------
def zapi_base_url() -> str:
    """
    Monta a URL base de API da Z-API.
    """
    if not ZAPI_INSTANCE_ID or not ZAPI_TOKEN:
        logging.warning("Z-API credenciais ausentes (ZAPI_INSTANCE_ID/ZAPI_TOKEN).")
    # Formato comum:
    # https://api.z-api.io/instances/{instance}/token/{token}
    return f"{ZAPI_BASE}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}"

async def send_text(phone: str, text: str):
    """
    Envia texto via Z-API. Tenta diferentes endpoints compatíveis.
    """
    url_candidates = [
        "/send-text", "/sendText", "/messages", "/send-message", "/sendMessage"
    ]
    payload_candidates = [
        {"phone": phone, "message": text},
        {"phone": phone, "text": text},
        {"phone": phone, "message": text, "delayMessage": 0},
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        for path in url_candidates:
            for body in payload_candidates:
                try:
                    url = zapi_base_url() + path
                    resp = await client.post(url, json=body)
                    logging.info(f"<== Z-API TRY send-text {path} STATUS: {resp.status_code} | RESP: {resp.text}")
                    if resp.status_code < 300 and "error" not in (resp.text or "").lower():
                        return
                except Exception as e:
                    logging.warning(f"send_text exception ({path}): {e}")
    # Se nada deu certo, seguimos adiante sem levantar erro.

async def send_file_by_url(phone: str, file_url: str, filename: str, caption: str):
    """
    Envia arquivo (PDF) por URL via Z-API. Tenta variações comuns de endpoint/corpo.
    Se falhar, o chamador decide enviar o link como fallback.
    """
    url_candidates = [
        "/send-file", "/sendFile", "/send-file-from-url", "/sendFileFromUrl",
        "/send-file-url", "/sendFileUrl", "/messages/file"
    ]
    payload_candidates = [
        {"phone": phone, "fileUrl": file_url, "caption": caption, "fileName": filename},
        {"phone": phone, "url": file_url, "caption": caption, "fileName": filename},
        {"phone": phone, "path": file_url, "caption": caption, "fileName": filename},
        {"phone": phone, "fileUrl": file_url, "caption": caption},
    ]
    async with httpx.AsyncClient(timeout=20) as client:
        for path in url_candidates:
            for body in payload_candidates:
                try:
                    url = zapi_base_url() + path
                    resp = await client.post(url, json=body)
                    logging.info(f"<== Z-API TRY send-file {path} STATUS: {resp.status_code} | RESP: {resp.text}")
                    if resp.status_code < 300 and "error" not in (resp.text or "").lower():
                        return True
                except Exception as e:
                    logging.warning(f"send_file_by_url exception ({path}): {e}")
    return False

# -----------------------------------------------------------------------------
# Envio do catálogo com confirmação e fallback
# -----------------------------------------------------------------------------
async def send_catalog_bundle(phone: str, data: Dict[str, Any]):
    """
    Envia confirmação dos dados + catálogo (arquivo via URL). Se falhar, envia link.
    """
    nome = data.get("nome") or data.get("name") or ""
    empresa = data.get("empresa") or data.get("company") or ""
    cnpj = data.get("cnpj") or ""
    perfil = data.get("perfil_desc") or data.get("perfil") or ""

    confirm = (
        "✅ *Dados recebidos!* Estou enviando agora o *Catálogo Rezymol* diretamente por aqui. 📄\n\n"
        f"👤 *Nome:* {nome}\n"
        f"🏢 *Empresa:* {empresa}\n"
        f"🧾 *CNPJ:* {cnpj}\n"
        f"🧭 *Perfil:* {perfil}\n\n"
        "Se precisar de ajuda com algum produto ou cotação, é só me avisar! 💬"
    )
    await send_text(phone, confirm)

    if not CATALOG_URL_DIRECT:
        # Sem URL configurada: envia aviso
        await send_text(phone, "⚠️ O catálogo não está configurado no sistema. Por favor, solicite ao suporte.")
        return

    # Tenta enviar o arquivo
    ok = await send_file_by_url(
        phone=phone,
        file_url=CATALOG_URL_DIRECT,
        filename="Catalogo_Rezymol.pdf",
        caption="📘 Catálogo Rezymol – Linha Moveleira (PDF)"
    )

    # Fallback: envia link direto
    if not ok:
        link_msg = (
            "⚠️ Não consegui anexar o PDF agora, mas você pode acessar pelo link direto:\n"
            f"{CATALOG_URL_DIRECT}\n\n"
            "Se preferir, posso tentar reenviar o arquivo em alguns instantes."
        )
        await send_text(phone, link_msg)

# -----------------------------------------------------------------------------
# Fluxo de atendimento
# -----------------------------------------------------------------------------
def text_matches(opt: str, txt: str) -> bool:
    t = txt.lower()
    opt = opt.lower()
    return t == opt or opt in t

def extract_text(payload: Dict[str, Any]) -> str:
    """
    Aceita variações do Z-API (texto.mensagem / text.message etc).
    """
    # Português
    txt = payload.get("texto", {}) or payload.get("text", {})
    if isinstance(txt, dict):
        msg = txt.get("mensagem") or txt.get("message") or ""
    else:
        msg = ""

    if not msg:
        # Algumas integrações usam 'text' no topo
        msg = payload.get("text") or ""
        if isinstance(msg, dict):
            msg = msg.get("message") or ""

    # Às vezes vem como string vazia quando é áudio/imagem
    return normalize_text(msg)

def get_phone(payload: Dict[str, Any]) -> str:
    return only_digits(payload.get("phone") or "")

def get_chat_id(payload: Dict[str, Any]) -> str:
    # Use 'phone' como identificador de sessão
    return get_phone(payload) or (payload.get("chatLid") or "")

async def handle_menu(phone: str):
    await send_text(phone, MENU_TXT)

async def handle_products(phone: str):
    await send_text(phone, PRODUCTS_TXT)

async def start_catalog_flow(sess: Dict[str, Any], phone: str):
    sess["flow"] = "catalog"
    sess["step"] = "nome"
    sess["data"] = {}
    await send_text(phone, "📝 Vamos registrar seus dados para enviar o *Catálogo Rezymol*.\nQual é o seu *Nome*?")

async def step_catalog_flow(sess: Dict[str, Any], phone: str, msg: str):
    data = sess["data"]
    step = sess.get("step")

    if step == "nome":
        data["nome"] = msg.strip().title()
        sess["step"] = "telefone"
        await send_text(phone, "Por favor, informe seu *Telefone* com DDD:")
        return

    if step == "telefone":
        data["telefone"] = only_digits(msg)
        sess["step"] = "perfil"
        await send_text(phone, PROFILE_TXT)
        return

    if step == "perfil":
        choice = only_digits(msg)
        perfis = {
            "1": "Cliente",
            "2": "Distribuidor",
            "3": "Representante",
            "4": "Fornecedor de Produtos - Matéria Prima"
        }
        if choice in perfis:
            data["perfil"] = choice
            data["perfil_desc"] = perfis[choice]
            sess["step"] = "empresa"
            await send_text(phone, "Qual é o nome da sua *Empresa*?")
        else:
            await send_text(phone, "Não entendi. Informe *1, 2, 3* ou *4* para o seu *Perfil*.\n\n" + PROFILE_TXT)
        return

    if step == "empresa":
        data["empresa"] = msg.strip().title()
        sess["step"] = "cnpj"
        await send_text(phone, "Informe o *CNPJ* (opcional). Se não tiver agora, pode digitar *pular*:")
        return

    if step == "cnpj":
        if msg.lower().strip() != "pular":
            data["cnpj"] = only_digits(msg)
        else:
            data["cnpj"] = ""
        sess["step"] = "endereco"
        await send_text(phone, "Informe seu *Endereço* + *CEP* (ex.: Rua X, 123 - Centro - 00000-000):")
        return

    if step == "endereco":
        data["endereco"] = msg.strip()
        sess["step"] = "email"
        await send_text(phone, "Informe seu *E-mail* (opcional). Se quiser pular, digite *pular*:")
        return

    if step == "email":
        if msg.lower().strip() != "pular":
            data["email"] = msg.strip()
        else:
            data["email"] = ""

        # FIM DO FLUXO -> envia catálogo + confirma
        await send_catalog_bundle(phone, data)
        # encerra sessão do fluxo para evitar repetição
        sess["flow"] = None
        sess["step"] = None
        return

# -----------------------------------------------------------------------------
# Rotas
# -----------------------------------------------------------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "DSA Bot - OK"

@app.post("/api/webhook/receber")
async def receber(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    logging.info(f"CORPO BRUTO : {json.dumps(payload, ensure_ascii=False)}")

    phone = get_phone(payload)
    chat_id = get_chat_id(payload)
    msg = extract_text(payload)

    # Apenas confirma 200 se não houver telefone
    if not phone:
        return JSONResponse({"ok": True})

    sess = get_session(chat_id)

    # Normalizadores rápidos
    mlow = msg.lower().strip()

    # Comandos universais
    if mlow in {"menu", "inicio", "start"}:
        await handle_menu(phone)
        sess["flow"] = None
        sess["step"] = None
        return JSONResponse({"ok": True})

    if mlow in {"cancelar", "sair", "parar"}:
        reset_session(chat_id)
        await send_text(phone, "Fluxo cancelado. Se quiser recomeçar, digite *menu*.")
        return JSONResponse({"ok": True})

    # Se estiver em fluxo de catálogo, prioriza continuidade
    if sess.get("flow") == "catalog" and sess.get("step"):
        await step_catalog_flow(sess, phone, msg)
        return JSONResponse({"ok": True})

    # Entrada de menu por número ou palavra-chave
    if mlow in {"1", "produtos", "produto"}:
        await handle_products(phone)
        return JSONResponse({"ok": True})

    if mlow in {"2", "compras", "compra"}:
        # Aqui apenas um placeholder; pode acoplar seu fluxo de compras
        await send_text(phone, "🛒 Perfeito! Me diga qual produto deseja e a quantidade. Posso te ajudar com a cotação.")
        return JSONResponse({"ok": True})

    if mlow in {"3", "catalogo", "catálogo"}:
        await start_catalog_flow(sess, phone)
        return JSONResponse({"ok": True})

    if mlow in {"4", "atendente", "especialista", "suporte", "vendedor"}:
        await send_text(phone, "👨‍💼 Já encaminhei sua solicitação. Um atendente entrará em contato em breve.")
        return JSONResponse({"ok": True})

    # Não entendeu: oferece menu
    await send_text(phone, "Não entendi. Digite *menu* para ver as opções ou me diga o que precisa. 🙂")
    return JSONResponse({"ok": True})
