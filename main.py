import os
import csv
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
CATALOG_PITTY_URL = os.getenv("CATALOG_PITTY_URL", "")

app = FastAPI(title="DSA Bot")

# ==============================
# VARIÁVEIS GLOBAIS
# ==============================
SESSIONS = {}
LEADS_CSV = Path("leads.csv")

# ==============================
# FUNÇÃO: enviar texto via Z-API
# ==============================
async def send_text_via_zapi(phone: str, message: str):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-text"
    payload = {"phone": phone, "message": message}
    headers = {"Client-Token": CLIENT_TOKEN}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload, headers=headers)
    return r.status_code, r.text

# ==============================
# FUNÇÃO: enviar arquivo via Z-API
# ==============================
async def send_file_via_zapi(phone: str, file_url: str, file_name: str = "", caption: str = ""):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-file"
    payload = {"phone": phone, "file": file_url, "fileName": file_name, "caption": caption}
    headers = {"Client-Token": CLIENT_TOKEN}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=headers)
    return r.status_code, r.text

# ==============================
# MENSAGEM DE MENU PRINCIPAL
# ==============================
WELCOME = (
    "👋 Olá! Sou o Assistente da *DSA Cristal Química*.\n"
    "Como posso ajudar hoje?\n\n"
    "1️⃣ *Produtos Rezymol* (moveleiro)\n"
    "2️⃣ *Linha Pitty* (biossegurança)\n"
    "3️⃣ *Falar com um atendente*\n\n"
    "Você pode digitar o número da opção ou escrever sua dúvida."
)

# ==============================
# CAPTURA DE LEAD
# ==============================
def start_lead_capture(phone: str):
    SESSIONS[phone] = {"stage": "ask_name", "data": {}}
    return "📞 Vamos agilizar seu atendimento humano. Qual é o seu *nome*?"

def continue_lead_capture(phone: str, text: str):
    session = SESSIONS.get(phone, {})
    stage = session.get("stage")

    if stage == "ask_name":
        session["data"]["nome"] = text.strip()
        session["stage"] = "ask_company"
        return f"Ótimo, *{session['data']['nome']}*! Qual é o nome da *empresa*?"

    if stage == "ask_company":
        session["data"]["empresa"] = text.strip()
        session["stage"] = "ask_city"
        return "Perfeito. De qual *cidade* você fala?"

    if stage == "ask_city":
        session["data"]["cidade"] = text.strip()
        save_lead(session["data"], phone)
        SESSIONS.pop(phone, None)
        return (
            "✅ Dados recebidos! Em instantes um atendente DSA falará com você.\n"
            f"Resumo: *{session['data']['nome']}*, *{session['data']['empresa']}*, *{session['data']['cidade']}*."
        )

    return "Pode repetir? Vamos começar com seu *nome*."

def save_lead(data: dict, phone: str):
    file_exists = LEADS_CSV.exists()
    with LEADS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["telefone", "nome", "empresa", "cidade"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "telefone": phone,
            "nome": data.get("nome", ""),
            "empresa": data.get("empresa", ""),
            "cidade": data.get("cidade", "")
        })

# ==============================
# LÓGICA DE ROTEAMENTO
# ==============================
def route_message(phone: str, text: str) -> str:
    t = (text or "").strip()
    tl = t.lower()

    if phone in SESSIONS:
        return continue_lead_capture(phone, t)

    if tl in ("oi", "olá", "ola", "menu", "inicio", "start", "hi"):
        return WELCOME

    if tl.startswith("1") or "rezymol" in tl:
        return ("🟢 *Rezymol – Setor moveleiro*\n"
                "- 1250 BSC (Limpa chapas / remoção de cola)\n"
                "- 982 NI | 983 FI | 984 RD | 985 AT\n\n"
                "Quer receber *catálogo/preços* ou saber *qual usar* no seu caso?\n"
                "Responda: *catálogo rezymol* ou *qual usar rezymol*.")

    if tl.startswith("2") or "pitty" in tl:
        return ("🟣 *Pitty – Biossegurança / Higiene industrial*\n"
                "- BSC 1100, Desincrustante 890, protocolos de limpeza.\n\n"
                "Digite *catálogo pitty* ou sua dúvida específica.")

    if tl.startswith("3") or "atendente" in tl or "humano" in tl:
        return start_lead_capture(phone)

    if "catálogo" in tl or "catalogo" in tl:
        if "rezymol" in tl:
            return "__SEND_CATALOG_REZYMOL__"
        if "pitty" in tl:
            return "__SEND_CATALOG_PITTY__"
        return "📄 Catálogo de qual linha? *Rezymol* ou *Pitty*?"

    return "👍 Entendi. Para começar, digite *menu* ou escolha:\n" + WELCOME

# ==============================
# ENDPOINTS
# ==============================
@app.get("/api/webhook/receber")
async def receber_get():
    return {"ok": True, "hint": "Use POST para eventos. GET existe só para validação do painel."}

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

    text = ""
    for k in ("message", "body", "text", "content"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
        if isinstance(v, dict):
            for kk in ("text", "body", "message", "caption"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    text = vv.strip()
                    break
            if text:
                break

    print("==> MSG DE:", phone, "| TEXTO:", text)

    if not phone or not text:
        return JSONResponse({"ok": True, "ignored": True})

    reply = route_message(phone, text)

    # ---- Envio de catálogos ----
    if reply == "__SEND_CATALOG_REZYMOL__":
        if CATALOG_REZYMOL_URL:
            status, resp = await send_file_via_zapi(phone, CATALOG_REZYMOL_URL, "Catalogo-Rezymol.pdf", "📄 Catálogo Rezymol")
            print("<== RESPOSTA (Rezymol):", status, resp)
            if status >= 300:
                await send_text_via_zapi(phone, f"📄 Catálogo Rezymol: {CATALOG_REZYMOL_URL}")
        else:
            await send_text_via_zapi(phone, "📄 Link do catálogo Rezymol não configurado.")
        return JSONResponse({"ok": True})

    if reply == "__SEND_CATALOG_PITTY__":
        if CATALOG_PITTY_URL:
            status, resp = await send_file_via_zapi(phone, CATALOG_PITTY_URL, "Catalogo-Pitty.pdf", "📄 Catálogo Pitty")
            print("<== RESPOSTA (Pitty):", status, resp)
            if status >= 300:
                await send_text_via_zapi(phone, f"📄 Catálogo Pitty: {CATALOG_PITTY_URL}")
        else:
            await send_text_via_zapi(phone, "📄 Link do catálogo Pitty não configurado.")
        return JSONResponse({"ok": True})

    # ---- Resposta padrão (texto) ----
    status, resp = await send_text_via_zapi(phone, reply)
    print("<== RESPOSTA:", status, resp)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok"}
