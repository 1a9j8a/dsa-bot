import os
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from dotenv import load_dotenv

# ==============================
# CARREGAR VARIÁVEIS DO .ENV
# ==============================
load_dotenv()

ZAPI_BASE = os.getenv("ZAPI_BASE", "https://api.z-api.io")
INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN")
CATALOG_REZYMOL_URL = os.getenv("CATALOG_REZYMOL_URL", "")

app = FastAPI(title="DSA Bot - Spark")

# ==============================
# VARIÁVEIS GLOBAIS
# ==============================
SESSIONS: dict[str, dict] = {}   # estado por telefone
LEADS_CSV = Path("leads.csv")
KNOWN_NAMES: dict[str, str] = {} # armazena primeiro nome por telefone (quando vier do senderName)
IDLE_NUDGE_SECONDS = 600         # 10min

# ==============================
# HELPERS
# ==============================
def generate_order_code(phone: str) -> str:
    """Gera um código único para o pedido com base no telefone e data."""
    date_str = datetime.now().strftime("%Y%m%d")
    short_phone = (re.sub(r"\D", "", phone)[-4:] if phone else "0000")
    seq = str(int(time.time()) % 1000).zfill(3)
    return f"PED-{short_phone}-{date_str}-{seq}"

def first_name_from_sender(sender: str | None) -> str | None:
    if not sender:
        return None
    s = sender.strip()
    # pega a primeira "palavra" antes de emoji/separadores
    s = re.split(r"[^\wÀ-ÖØ-öø-ÿ'-]+", s)[0]
    return s if s else None

def greeting_match(tl: str) -> bool:
    return any(kw in tl for kw in (
        "oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "menu", "inicio", "start", "spark"
    ))

def ensure_session(phone: str):
    SESSIONS.setdefault(phone, {"stage": None, "mode": None, "data": {}, "last": time.time()})
    SESSIONS[phone]["last"] = time.time()

def maybe_idle_nudge(phone: str) -> str | None:
    sess = SESSIONS.get(phone)
    if not sess:
        return None
    last = sess.get("last", time.time())
    if time.time() - last > IDLE_NUDGE_SECONDS and sess.get("stage") not in (None, "done"):
        SESSIONS[phone]["last"] = time.time()
        return "Entendi! Pode me contar qual é a sua dúvida? Estou aqui pra te ajudar 👍"
    return None

def save_lead(data: dict, phone: str, mode: str = "atendimento"):
    file_exists = LEADS_CSV.exists()
    with LEADS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "telefone", "nome", "telefone_cliente", "perfil", "empresa", "cnpj",
                "cidade", "rua", "bairro", "cep", "email", "modo", "auxilio_tecnico"
            ]
        )
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "telefone": phone,
            "nome": data.get("nome", ""),
            "telefone_cliente": data.get("telefone_cliente", ""),
            "perfil": data.get("perfil", ""),
            "empresa": data.get("empresa", ""),
            "cnpj": data.get("cnpj", ""),
            "cidade": data.get("cidade", ""),
            "rua": data.get("rua", ""),
            "bairro": data.get("bairro", ""),
            "cep": data.get("cep", ""),
            "email": data.get("email", ""),
            "modo": mode,
            "auxilio_tecnico": data.get("auxilio_tecnico", ""),
        })

# ==============================
# ENVIO VIA Z-API
# ==============================
async def send_text_via_zapi(phone: str, message: str):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-text"
    payload = {"phone": phone, "message": message}
    headers = {"Client-Token": CLIENT_TOKEN}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload, headers=headers)
    print(f"<== Z-API SEND-TEXT STATUS: {r.status_code} | RESP: {r.text}")
    return r.status_code, r.text

async def send_file_via_zapi(phone: str, file_url: str, file_name: str = "", caption: str = ""):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-file"
    payload = {"phone": phone, "file": file_url, "fileName": file_name, "caption": caption}
    headers = {"Client-Token": CLIENT_TOKEN}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=headers)
    print(f"<== Z-API SEND-FILE STATUS: {r.status_code} | RESP: {r.text}")
    return r.status_code, r.text

# ==============================
# MÓDULO 1 — BOAS-VINDAS / MENU
# ==============================
def welcome_text(first_name: str | None = None) -> str:
    saudacao = "Olá! 😊 Tudo bem?"
    prazer = f" Prazer em te conhecer, {first_name}!" if first_name else ""
    return (
        f"{saudacao}{prazer}\n\n"
        "⚡ Eu sou o *Spark*, assistente virtual da *DSA Cristal Química*.\n"
        "Como posso te ajudar hoje?\n\n"
        "1 - *Produtos Rezymol*\n"
        "2 - *Compras*\n"
        "3 - *Representantes*\n"
        "4 - *Fornecedores - MP*\n"
        "5 - *Falar com um atendente/especialista*\n\n"
        "Você pode digitar o número da opção ou escrever sua dúvida.\n"
        "Comandos rápidos: *compra*, *catálogo*, *produtos*."
    )

# ==============================
# MÓDULO 2 — LINHA DE PRODUTOS REZYMOL
# ==============================
def produtos_menu_text() -> str:
    return (
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
        "📘 *Para solicitar catálogo*, digite *catálogo* ou *3*.\n\n"
        "🛒 *Para realizar um pedido*, digite *compra* ou *2*."
    )

# ==============================
# FLUXOS
# ==============================
def start_flow(phone: str, mode: str):
    ensure_session(phone)
    SESSIONS[phone].update({"mode": mode, "stage": "ask_name", "data": {}})
    if mode == "compra":
        return "🛒 Vamos registrar seu pedido! Qual é o seu *nome*?"
    if mode == "catalogo":
        return "📄 Para enviar o catálogo, preciso de alguns dados. Qual é o seu *nome*?"
    # atendimento
    return "📞 Vamos agilizar seu atendimento humano. Qual é o seu *nome*?"

def continue_flow(phone: str, text: str) -> str:
    ensure_session(phone)
    sess = SESSIONS[phone]
    data = sess["data"]
    mode = sess["mode"]
    tl = text.lower().strip()

    # Nudge se ficou parado
    nudge = maybe_idle_nudge(phone)
    prefix = f"{nudge}\n\n" if nudge else ""

    # ========== ETAPAS COMUNS ==========
     if sess["stage"] == "ask_name":
        data["nome"] = text.strip()
        sess["stage"] = "ask_phone"
        return prefix + "Por favor, informe seu *telefone* com DDD."

    if sess["stage"] == "ask_phone":
        data["telefone_cliente"] = re.sub(r"\D", "", text)
        sess["stage"] = "ask_profile"
        return prefix + (
            "Qual é o seu *perfil*?\n"
            "1) Representante\n"
            "2) Cliente\n"
            "3) Distribuidor\n"
            "4) Fornecedor de Produtos - Matéria Prima"
        )

    if sess["stage"] == "ask_profile":
        perfis = {"1": "Representante", "2": "Cliente", "3": "Distribuidor", "4": "Fornecedor de Produtos - Matéria Prima"}
        data["perfil"] = perfis.get(tl, text.strip())
        sess["stage"] = "ask_company"
        return prefix + "Qual é o nome da *empresa*?"

    if sess["stage"] == "ask_company":
        data["empresa"] = text.strip()
        sess["stage"] = "ask_cnpj"
        return prefix + "Perfeito. Qual é o *CNPJ* da empresa? (somente números)"

    if sess["stage"] == "ask_cnpj":
        m = re.search(r"\b\d{14}\b", text)
        data["cnpj"] = (m.group(0) if m else re.sub(r"\D", "", text))
        sess["stage"] = "ask_city"
        return prefix + "Informe a *cidade*."

    if sess["stage"] == "ask_city":
        data["cidade"] = text.strip()
        sess["stage"] = "ask_rua"

        # Se o perfil for Representante, muda o texto
        if data.get("perfil", "").lower().startswith("represent"):
            return prefix + "Informe o *endereço comercial* (Rua/Av)."
        else:
            return prefix + "Endereço de entrega — informe a *Rua/Av*."

    if sess["stage"] == "ask_rua":
        data["rua"] = text.strip()
        sess["stage"] = "ask_bairro"
        return prefix + "Agora o *Bairro*."

    if sess["stage"] == "ask_bairro":
        data["bairro"] = text.strip()
        sess["stage"] = "ask_cep"
        return prefix + "Informe o *CEP* (somente números)."

    if sess["stage"] == "ask_cep":
        m = re.search(r"\b\d{8}\b", text)
        data["cep"] = (m.group(0) if m else re.sub(r"\D", "", text))
        if mode == "catalogo":
            sess["stage"] = "ask_email_catalogo"
            return prefix + "Por fim, seu *e-mail* para envio do catálogo."
        # compra / atendimento seguem para e-mail
        sess["stage"] = "ask_email"
        return prefix + "Por fim, seu *e-mail* de contato."


    # ========== CATÁLOGO ==========
    if mode == "catalogo":
        if sess["stage"] == "ask_email_catalogo":
            data["email"] = text.strip()
            sess["stage"] = "done"
            save_lead(data, phone, "catalogo")
            resumo = (
                "✅ Dados recebidos! Enviarei o *Catálogo Rezymol* em seguida.\n"
                f"Resumo: *{data.get('nome','')}*, *{data.get('empresa','')}*, *{data.get('cnpj','')}*, "
                f"*{data.get('cidade','')}*."
            )
            # marcador para o endpoint enviar o arquivo
            return f"{resumo}\n__SEND_CATALOG_AFTER_LEAD__:rezymol"

    # ========== COMPRA ==========
    if mode == "compra":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            order_code = generate_order_code(phone)
            sess["stage"] = "done"
            save_lead(data, phone, "compra")
            SESSIONS.pop(phone, None)

            resumo = (
                f"🧾 *Pedido registrado com sucesso!* Código: *{order_code}*\n\n"
                f"👤 *Nome:* {data.get('nome','')}\n"
                f"🏢 *Empresa:* {data.get('empresa','')}\n"
                f"🆔 *CNPJ:* {data.get('cnpj','')}\n"
                f"📍 *Cidade:* {data.get('cidade','')}\n"
                f"📞 *Telefone:* {data.get('telefone_cliente','')}\n"
                f"📦 *Endereço de entrega:* {data.get('rua','')} - {data.get('bairro','')} - CEP {data.get('cep','')}\n"
                f"✉️ *E-mail:* {data.get('email','')}\n\n"
                "✅ Obrigado por confiar na *DSA Cristal Química*!\n"
                "Em instantes, um atendente entrará em contato para confirmar os detalhes do seu pedido. 🙌"
            )
            return resumo

    # ========== ATENDIMENTO ==========
    if mode == "atendimento":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            sess["stage"] = "done"
            save_lead(data, phone, "atendimento")
            SESSIONS.pop(phone, None)
            return prefix + (
                "✅ Dados recebidos! Em instantes um atendente da DSA falará com você.\n"
                f"Resumo: *{data.get('nome','')}*, *{data.get('empresa','')}*, *{data.get('cidade','')}*."
            )

    # fallback
    return prefix + "Pode repetir, por favor?"

# ==============================
# ROTEAMENTO DE MENSAGENS
# ==============================
def route_message(phone: str, text: str) -> str:
    ensure_session(phone)
    t = (text or "").strip()
    tl = t.lower()

    # Se já está em um fluxo
    if SESSIONS.get(phone, {}).get("stage") not in (None, "done"):
        return continue_flow(phone, t)

    # Saudações / menu
    if greeting_match(tl):
        first = KNOWN_NAMES.get(phone)
        return welcome_text(first)

    # Números diretos
    if tl.startswith("1"):
        return produtos_menu_text()
    if tl.startswith("2") or "compra" in tl:
        return start_flow(phone, "compra")
    if tl.startswith("3") or "representante" in tl:
        return "Nossa equipe comercial entrará em contato. Para agilizar, digite *5* para falar com um atendente."
    if tl.startswith("4") or "fornecedor" in tl or "matéria prima" in tl or "materia prima" in tl:
        return "Se você é fornecedor de matéria-prima, deixe seu *nome, empresa, e e-mail* e o setor de compras retornará."
    if tl.startswith("5") or "atendente" in tl or "humano" in tl or "especialista" in tl:
        return start_flow(phone, "atendimento")

    # Produtos / Rezymol palavras-chave
    if "rezymol" in tl or "produtos" in tl:
        return produtos_menu_text()

    # Catálogo por palavra-chave
    if "catálogo" in tl or "catalogo" in tl:
        return start_flow(phone, "catalogo")

    return "⚡ Digite *menu* para ver as opções ou *compra* para iniciar seu pedido."

# ==============================
# ENDPOINTS
# ==============================
@app.get("/api/webhook/receber")
async def receber_get():
    return {"ok": True, "hint": "Use POST para eventos. GET existe só para validação."}

@app.post("/api/webhook/receber")
async def receber(request: Request):
    body = await request.json()
    print("RAW BODY:", body)

    data = body.get("data") or body

    phone = (
        str(data.get("phone") or data.get("from") or data.get("chatId") or "")
        .replace("@c.us", "")
        .replace("@s.whatsapp.net", "")
        .strip()
    )

    # Guarda primeiro nome se veio do payload (personalização)
    sender_name = data.get("senderName") or data.get("chatName")
    first = first_name_from_sender(sender_name)
    if phone and first:
        KNOWN_NAMES[phone] = first

    # Extração robusta do texto
    text = ""
    for k in ("message", "body", "text", "content", "texto"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
        if isinstance(v, dict):
            for kk in ("mensagem", "text", "body", "message", "caption"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    text = vv.strip()
                    break
            if text:
                break
    if not text:
        md = data.get("messageData") or {}
        if isinstance(md, dict):
            tmd = md.get("textMessageData") or md.get("extendedTextMessageData") or {}
            if isinstance(tmd, dict):
                for kk in ("textMessage", "text", "caption", "body"):
                    vv = tmd.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        text = vv.strip()
                        break
    if not text:
        msgs = data.get("messages")
        if isinstance(msgs, list) and msgs:
            m0 = msgs[0]
            if isinstance(m0, dict):
                for kk in ("text", "body", "message", "content", "caption"):
                    vv = m0.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        text = vv.strip()
                        break

    print("==> MSG DE:", phone, "| TEXTO:", text)

    if not phone or not text:
        return JSONResponse({"ok": True, "ignored": True})

    reply = route_message(phone, text)

    # Se o reply contém marcador de catálogo, enviar o arquivo e depois a mensagem
    if isinstance(reply, str) and "__SEND_CATALOG_AFTER_LEAD__" in reply:
        if CATALOG_REZYMOL_URL:
            status, resp = await send_file_via_zapi(
                phone, CATALOG_REZYMOL_URL, "Catalogo-Rezymol.pdf", "📄 Catálogo Rezymol"
            )
            if status >= 300:
                await send_text_via_zapi(phone, f"📄 Catálogo Rezymol: {CATALOG_REZYMOL_URL}")
        else:
            await send_text_via_zapi(phone, "📄 Catálogo Rezymol não configurado no servidor.")
        clean_reply = reply.replace("__SEND_CATALOG_AFTER_LEAD__:rezymol", "").strip()
        await send_text_via_zapi(phone, clean_reply)
        return JSONResponse({"ok": True})

    # resposta normal
    await send_text_via_zapi(phone, reply)
    return JSONResponse({"ok": True})

# ==============================
# HEALTHCHECK
# ==============================
@app.get("/health")
async def health():
    """
    Endpoint de verificação usado pelo Render para monitorar a aplicação.
    Retorna status 200 e JSON {"status": "ok"} quando o servidor está ativo.
    """
    return {"status": "ok"}

# ==============================
# RODAR LOCALMENTE
# ==============================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
