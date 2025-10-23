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

app = FastAPI(title="DSA Bot - Spark")

# ==============================
# VARIÁVEIS GLOBAIS
# ==============================
SESSIONS = {}
LEADS_CSV = Path("leads.csv")

# ==============================
# FUNÇÕES DE ENVIO VIA Z-API
# ==============================
async def send_text_via_zapi(phone: str, message: str):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-text"
    payload = {"phone": phone, "message": message}
    headers = {"Client-Token": CLIENT_TOKEN}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload, headers=headers)
    return r.status_code, r.text


async def send_file_via_zapi(phone: str, file_url: str, file_name: str = "", caption: str = ""):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-file"
    payload = {"phone": phone, "file": file_url, "fileName": file_name, "caption": caption}
    headers = {"Client-Token": CLIENT_TOKEN}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=headers)
    return r.status_code, r.text

# ==============================
# MENSAGEM DE BOAS-VINDAS
# ==============================
WELCOME = (
    "⚡ Olá! Sou o *Spark*, assistente virtual da *DSA Cristal Química*.\n"
    "Seja muito bem-vindo(a)! 👋\n\n"
    "Como posso te ajudar hoje?\n\n"
    "1️⃣ *Produtos Rezymol* (linha moveleira)\n"
    "2️⃣ *Linha Pitty* (biossegurança e higienização industrial)\n"
    "3️⃣ *Falar com um atendente humano*\n\n"
    "Digite o número da opção desejada."
)

# ==============================
# CAPTURA DE LEADS E COMPRAS
# ==============================
def start_lead_capture(phone: str, mode: str = "atendimento"):
    if mode == "compra":
        SESSIONS[phone] = {"stage": "ask_name", "mode": "compra", "data": {}}
        return "🛒 Vamos registrar seu pedido! Qual é o seu *nome*?"
    else:
        SESSIONS[phone] = {"stage": "ask_name", "mode": "atendimento", "data": {}}
        return "📞 Vamos agilizar seu atendimento humano. Qual é o seu *nome*?"


def continue_lead_capture(phone: str, text: str):
    session = SESSIONS.get(phone, {})
    stage = session.get("stage")
    mode = session.get("mode", "atendimento")

    if stage == "ask_name":
        session["data"]["nome"] = text.strip()
        session["stage"] = "ask_company"
        return f"Ótimo, *{session['data']['nome']}*! Qual é o nome da *empresa*?"

    if stage == "ask_company":
        session["data"]["empresa"] = text.strip()
        session["stage"] = "ask_cnpj"
        return "Perfeito. Qual é o *CNPJ* da empresa?"

    if stage == "ask_cnpj":
        session["data"]["cnpj"] = text.strip()
        session["stage"] = "ask_city"
        return "Informe agora a *cidade* de onde está falando."

    if stage == "ask_city":
        session["data"]["cidade"] = text.strip()
        if mode == "compra":
            session["stage"] = "ask_cep"
            return "Informe também o *CEP* da sua região."
        else:
            session["stage"] = "done"
            save_lead(session["data"], phone, mode)
            SESSIONS.pop(phone, None)
            return (
                "✅ Dados recebidos! Em instantes um atendente da DSA falará com você.\n"
                f"Resumo: *{session['data']['nome']}*, *{session['data']['empresa']}*, *{session['data']['cidade']}*."
            )

    if stage == "ask_cep":
        session["data"]["cep"] = text.strip()
        session["stage"] = "ask_email"
        return "Por fim, poderia me informar seu *e-mail* de contato?"

    if stage == "ask_email":
        session["data"]["email"] = text.strip()
        session["stage"] = "done"
        save_lead(session["data"], phone, mode)
        SESSIONS.pop(phone, None)
        return (
            "🧾 Pedido registrado com sucesso! Um atendente entrará em contato para confirmar os detalhes.\n"
            f"Resumo: *{session['data']['nome']}*, *{session['data']['empresa']}*, *{session['data']['cidade']}*, *{session['data']['cep']}*, *{session['data']['email']}*."
        )

    return "Pode repetir, por favor? Vamos começar com seu *nome*."


def save_lead(data: dict, phone: str, mode: str = "atendimento"):
    file_exists = LEADS_CSV.exists()
    with LEADS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["telefone", "nome", "empresa", "cnpj", "cidade", "cep", "email", "modo"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "telefone": phone,
            "nome": data.get("nome", ""),
            "empresa": data.get("empresa", ""),
            "cnpj": data.get("cnpj", ""),
            "cidade": data.get("cidade", ""),
            "cep": data.get("cep", ""),
            "email": data.get("email", ""),
            "modo": mode
        })

# ==============================
# ROTEAMENTO DE MENSAGENS
# ==============================
def route_message(phone: str, text: str) -> str:
    t = (text or "").strip()
    tl = t.lower()

    # se já está em um fluxo
    if phone in SESSIONS:
        return continue_lead_capture(phone, t)

    # comandos básicos
    if tl in ("oi", "olá", "ola", "menu", "inicio", "start", "spark"):
        return WELCOME

    # produtos Rezymol
    if tl.startswith("1") or "rezymol" in tl:
        return (
            "🟢 *Linha Rezymol – Setor Moveleiro*\n"
            "• 982 NI – Fluido Antiaderente (coladeiras de borda)\n"
            "• 983 FI – Fluido Finalizador (coladeiras de borda)\n"
            "• 984 RD – Fluido Resfriador (coladeiras de borda)\n"
            "• 985 AT – Fluido Antiestático (coladeiras de borda)\n"
            "• 1250 BSC – Limpa chapas e remoção de cola\n"
            "• 1100 BSC – Limpa chapas e peças\n"
            "• Limpa Coleiros | Desengraxantes | Removedores de resina e tinta anilox\n\n"
            "Para continuar:\n"
            "✳️ Digite *catálogo rezymol* para ver o catálogo\n"
            "🛒 Digite *compra rezymol* para registrar um pedido"
        )

    # linha Pitty
    if tl.startswith("2") or "pitty" in tl:
        return (
            "🟣 *Linha Pitty – Biossegurança e Higiene Industrial*\n"
            "• BSC 1100 – Limpeza pesada e sanitização\n"
            "• 890 – Desincrustante industrial\n"
            "• Protocolos de limpeza e higienização para frigoríficos e indústrias.\n\n"
            "Para continuar:\n"
            "✳️ Digite *catálogo pitty* ou *compra pitty*."
        )

    # atendente humano
    if tl.startswith("3") or "atendente" in tl or "humano" in tl:
        return start_lead_capture(phone, "atendimento")

    # catálogos
    if "catálogo" in tl or "catalogo" in tl:
        if "rezymol" in tl:
            return "__SEND_CATALOG_REZYMOL__"
        if "pitty" in tl:
            return "__SEND_CATALOG_PITTY__"
        return "📄 De qual linha você deseja o catálogo? *Rezymol* ou *Pitty*?"

    # compras
    if "compra" in tl:
        return start_lead_capture(phone, "compra")

    return "⚡ Digite *menu* para ver as opções novamente."


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

    text = ""
    for k in ("message", "body", "text", "content"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break

    print("==> MSG DE:", phone, "| TEXTO:", text)

    if not phone or not text:
        return JSONResponse({"ok": True, "ignored": True})

    reply = route_message(phone, text)

    # envio de catálogo
    if reply == "__SEND_CATALOG_REZYMOL__":
        if CATALOG_REZYMOL_URL:
            status, resp = await send_file_via_zapi(phone, CATALOG_REZYMOL_URL, "Catalogo-Rezymol.pdf", "📄 Catálogo Rezymol")
            if status >= 300:
                await send_text_via_zapi(phone, f"📄 Catálogo Rezymol: {CATALOG_REZYMOL_URL}")
        else:
            await send_text_via_zapi(phone, "📄 Catálogo Rezymol ainda não configurado.")
        return JSONResponse({"ok": True})

    if reply == "__SEND_CATALOG_PITTY__":
        if CATALOG_PITTY_URL:
            status, resp = await send_file_via_zapi(phone, CATALOG_PITTY_URL, "Catalogo-Pitty.pdf", "📄 Catálogo Pitty")
            if status >= 300:
                await send_text_via_zapi(phone, f"📄 Catálogo Pitty: {CATALOG_PITTY_URL}")
        else:
            await send_text_via_zapi(phone, "📄 Catálogo Pitty ainda não configurado.")
        return JSONResponse({"ok": True})

    # resposta normal
    await send_text_via_zapi(phone, reply)
    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    return {"status": "ok"}


# ==============================
# RODAR LOCALMENTE
# ==============================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
