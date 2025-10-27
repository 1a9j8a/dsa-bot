import os
import csv
import re
import time
import smtplib
from email.message import EmailMessage
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
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN") or os.getenv("ZAPI_CLIENT_TOKEN")

CATALOG_REZYMOL_URL = os.getenv("CATALOG_REZYMOL_URL", "")

# SMTP (opcional p/ envio por e-mail do catálogo)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")

app = FastAPI(title="DSA Bot - Spark")

# ==============================
# VARIÁVEIS GLOBAIS
# ==============================
SESSIONS: dict[str, dict] = {}            # estado por telefone
LEADS_CSV = Path("leads.csv")
KNOWN_NAMES: dict[str, str] = {}          # primeiro nome por telefone
LAST_LEAD_BY_PHONE: dict[str, dict] = {}  # último lead salvo (p/ catálogo/e-mail)
IDLE_NUDGE_SECONDS = 600                  # 10min

# ==============================
# PRODUTOS (VISUAL)
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
        "📘 *Para solicitar catálogo*, digite *3* ou *catálogo*.\n"
        "🛒 *Para comprar agora*, digite *2* ou *compra*."
    )

# ==============================
# MENSAGEM DE BOAS-VINDAS (MENU)
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
        "3 - *Catálogo Rezymol*\n"
        "4 - *Falar com um atendente/especialista*\n\n"
        "Você pode digitar o número da opção ou escrever sua dúvida.\n"
        "Comandos rápidos: *compra*, *catálogo*, *produtos*."
    )

# ==============================
# ENVIO VIA Z-API
# ==============================
async def send_text_via_zapi(phone: str, message: str):
    url = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}/send-text"
    headers = {"Client-Token": CLIENT_TOKEN} if CLIENT_TOKEN else {}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload, headers=headers)
    print(f"<== Z-API SEND-TEXT STATUS: {r.status_code} | RESP: {r.text}")
    return r.status_code, r.text

async def send_file_via_zapi(phone: str, file_url: str, file_name: str = "", caption: str = ""):
    """
    Tenta enviar arquivo por diferentes endpoints da Z-API, pois variam por plano/versão:
      1) /send-file
      2) /send-file-from-url
      3) /send-document
    Usa o primeiro que funcionar (status < 300). Loga a resposta de cada tentativa.
    """
    headers = {"Client-Token": CLIENT_TOKEN} if CLIENT_TOKEN else {}
    base = f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}"

    payload = {"phone": phone, "file": file_url}
    if file_name:
        payload["fileName"] = file_name
    if caption:
        payload["caption"] = caption

    endpoints = ["send-file", "send-file-from-url", "send-document"]

    async with httpx.AsyncClient(timeout=40) as client:
        last_status, last_text = None, None
        for ep in endpoints:
            url = f"{base}/{ep}"
            try:
                r = await client.post(url, json=payload, headers=headers)
                print(f"<== Z-API TRY {ep} STATUS: {r.status_code} | RESP: {r.text}")
                if r.status_code < 300:
                    return r.status_code, r.text
                last_status, last_text = r.status_code, r.text
            except Exception as e:
                print(f"<== Z-API TRY {ep} EXC: {repr(e)}")
                last_status, last_text = 599, repr(e)
        return last_status or 500, last_text or "Falha ao enviar arquivo"

# ==============================
# ENVIO POR E-MAIL (OPCIONAL)
# ==============================
def send_catalog_email(to_email: str, subject: str, body: str, attachment_url: str | None = None):
    """
    Envia e-mail simples com link do catálogo (ou anexo se futuramente baixar).
    Só executa se SMTP_* estiver configurado. Caso contrário, não faz nada.
    """
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM and to_email):
        print("[EMAIL] SMTP não configurado ou e-mail de destino vazio. Pulando envio por e-mail.")
        return False

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    texto = body
    if attachment_url:
        texto += f"\n\nLink do catálogo: {attachment_url}"
    msg.set_content(texto)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print("[EMAIL] Enviado com sucesso para", to_email)
        return True
    except Exception as e:
        print("[EMAIL] Falha ao enviar:", repr(e))
        return False

# ==============================
# AUXILIARES
# ==============================
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

def first_name_from_sender(sender: str | None) -> str | None:
    if not sender:
        return None
    s = sender.strip()
    s = re.split(r"[^\wÀ-ÖØ-öø-ÿ'-]+", s)[0]
    return s if s else None

def save_lead(data: dict, phone: str, mode: str = "atendimento"):
    file_exists = LEADS_CSV.exists()
    fields = ["telefone", "nome", "telefone_cliente", "perfil", "empresa", "cnpj",
              "endereco", "email", "modo", "itens"]
    row = {
        "telefone": phone,
        "nome": data.get("nome", ""),
        "telefone_cliente": data.get("telefone_cliente", ""),
        "perfil": data.get("perfil", ""),
        "empresa": data.get("empresa", ""),
        "cnpj": data.get("cnpj", ""),
        "endereco": data.get("endereco", ""),
        "email": data.get("email", ""),
        "modo": mode,
        "itens": "; ".join([f"{i['desc']} x{i['qty']}" for i in data.get("cart", [])]) if data.get("cart") else "",
    }
    with LEADS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    # mantém na memória para uso imediato (envio catálogo/e-mail)
    LAST_LEAD_BY_PHONE[phone] = {**data, "modo": mode}

def generate_order_code(phone: str) -> str:
    date_str = datetime.now().strftime("%Y%m%d")
    short_phone = phone[-4:] if phone else "0000"
    # contador simples por tamanho da sessão atual
    return f"PED-{short_phone}-{date_str}-{str(len(SESSIONS) + 1).zfill(3)}"

# ==============================
# PARSE DE ITENS (livre: “produto x2” etc.)
# ==============================
CATALOG_KEYWORDS = [
    ("Fluido Antiaderente", "Fluido Antiaderente"),
    ("Fluido Resfriador", "Fluido Resfriador"),
    ("Fluido Antiestático", "Fluido Antiestático"),
    ("Fluido Finalizador", "Fluido Finalizador"),
    ("Limpa Chapas / Remoção de Colas", "Limpa Chapas / Remoção de Colas"),
    ("Limpa Chapas / Peças / Finalizador", "Limpa Chapas / Peças / Finalizador"),
    ("Limpa Coleiros", "Limpa Coleiros"),
    ("Desengraxantes Protetivo e Mãos", "Desengraxantes Protetivo e Mãos"),
    ("Removedor de Resinas", "Removedor de Resinas"),
    ("Removedor de Tintas Anilox", "Removedor de Tintas Anilox"),
]

def parse_items_free_text(line: str) -> list[dict]:
    """
    Tenta pegar padrões como:
      - "Fluido Antiaderente x2"
      - "Removedor de Resinas x 3"
      - múltiplos separados por vírgula/ponto e vírgula
    Retorna: [{"desc": <produto>, "qty": <int>}]
    """
    out = []
    parts = re.split(r"[;,]\s*", line)
    for part in parts:
        if not part.strip():
            continue
        m = re.search(r"x\s*(\d{1,3})", part, re.IGNORECASE)
        qty = int(m.group(1)) if m else 1
        found = None
        for key, desc in CATALOG_KEYWORDS:
            if key.lower() in part.lower():
                found = desc
                break
        if found:
            out.append({"desc": found, "qty": qty})
    return out

# ==============================
# FLUXOS
# ==============================
def start_flow(phone: str, mode: str):
    ensure_session(phone)
    # NÃO reinicia se já está em fluxo
    if SESSIONS[phone].get("stage") not in (None, "done"):
        return "Você já está em um fluxo. Pode continuar de onde parou. 😊"

    SESSIONS[phone] = {"mode": mode, "stage": "ask_name", "data": {"cart": []}, "last": time.time()}
    if mode == "compra":
        return "🛒 Vamos registrar seu pedido! Qual é o seu *nome*?"
    if mode == "catalogo":
        return "📄 Para enviar o catálogo, preciso de alguns dados. Qual é o seu *nome*?"
    return "📞 Vamos agilizar seu atendimento humano. Qual é o seu *nome*?"

def continue_flow(phone: str, text: str) -> str:
    ensure_session(phone)
    sess = SESSIONS[phone]
    data = sess["data"]
    mode = sess["mode"]
    tl = text.lower().strip()

    # lembrete de inatividade
    nudge = maybe_idle_nudge(phone)
    prefix = f"{nudge}\n\n" if nudge else ""

    # --------- COMUM ---------
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
        sess["stage"] = "ask_endereco"
        label = (
            "Informe o *endereço comercial* (Rua, número, bairro, cidade, UF, CEP)."
            if data.get("perfil", "").lower().startswith("represent")
            else "Informe o *endereço* (Rua, número, bairro, cidade, UF, CEP)."
        )
        return prefix + label

    if sess["stage"] == "ask_endereco":
        data["endereco"] = text.strip()
        if mode == "catalogo":
            sess["stage"] = "ask_email_catalogo"
            return prefix + "Por fim, seu *e-mail* para envio do catálogo."
        # compra e atendimento seguem para e-mail
        sess["stage"] = "ask_email"
        return prefix + "Por fim, seu *e-mail* de contato."

    # --------- CATÁLOGO ---------
    if mode == "catalogo":
        if sess["stage"] == "ask_email_catalogo":
            data["email"] = text.strip()
            sess["stage"] = "done"
            save_lead(data, phone, "catalogo")

            resumo = (
                "✅ Dados recebidos! Enviarei o *Catálogo Rezymol* agora.\n"
                f"Resumo: *{data.get('nome','')}*, *{data.get('empresa','')}*, *{data.get('cnpj','')}*.\n"
                "Você também receberá por e-mail (se informado)."
            )
            # marcador p/ o endpoint enviar WhatsApp + e-mail
            return f"{resumo}\n__SEND_CATALOG_AFTER_LEAD__:rezymol"

    # --------- COMPRA ---------
    if mode == "compra":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            sess["stage"] = "ask_items"
            return prefix + (
                "Perfeito! Agora me diga *produtos e quantidades*.\n\n"
                "Exemplos:\n"
                "• Fluido Antiaderente x2\n"
                "• Removedor de Resinas x1; Desengraxantes Protetivo e Mãos x3\n\n"
                "Quando terminar, digite *finalizar*."
            )

        if sess["stage"] == "ask_items":
            if tl == "finalizar":
                order_code = generate_order_code(phone)
                sess["stage"] = "done"
                save_lead(data, phone, "compra")

                itens_str = (
                    "\n".join([f"• {i['desc']} x{i['qty']}" for i in data.get("cart", [])])
                    if data.get("cart") else "—"
                )

                resumo = (
                    f"🧾 *Pedido registrado com sucesso!* Código: *{order_code}*\n\n"
                    f"👤 *Nome:* {data.get('nome','')}\n"
                    f"🏢 *Empresa:* {data.get('empresa','')}\n"
                    f"🆔 *CNPJ:* {data.get('cnpj','')}\n"
                    f"📞 *Telefone:* {data.get('telefone_cliente','')}\n"
                    f"📦 *Endereço:* {data.get('endereco','')}\n"
                    f"✉️ *E-mail:* {data.get('email','')}\n"
                    f"🧺 *Itens:*\n{itens_str}\n\n"
                    "✅ Obrigado por confiar na *DSA Cristal Química*!\n"
                    "Em instantes, um atendente entrará em contato para confirmar os detalhes do seu pedido. 🙌"
                )
                return resumo

            parsed = parse_items_free_text(text)
            if parsed:
                data.setdefault("cart", []).extend(parsed)
                added = "\n".join([f"• {i['desc']} x{i['qty']}" for i in parsed])
                return prefix + f"Adicionei ao carrinho:\n{added}\n\nSe quiser, envie mais itens. Para encerrar, digite *finalizar*."
            else:
                return prefix + (
                    "Não consegui identificar itens nessa mensagem.\n"
                    "Envie no formato: *Produto x2* (separando por vírgulas ou ponto e vírgula)."
                )

    # --------- ATENDIMENTO ---------
    if mode == "atendimento":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            sess["stage"] = "done"
            save_lead(data, phone, "atendimento")
            return prefix + (
                "✅ Dados recebidos! Em instantes um atendente da DSA falará com você.\n"
                f"Resumo: *{data.get('nome','')}*, *{data.get('empresa','')}*, *{data.get('endereco','')}*."
            )

    # fallback se nada casou
    return prefix + "Pode repetir, por favor? Digite *menu* para ver as opções."

# ==============================
# ROTEAMENTO DE MENSAGENS
# ==============================
def greeting_match(tl: str) -> bool:
    # aciona o menu para "oi", saudações, ou qualquer primeira mensagem fora de fluxo
    greetings = ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "menu", "inicio", "start", "spark", "help", "?"]
    return any(g in tl for g in greetings)

def route_message(phone: str, text: str) -> str:
    ensure_session(phone)
    t = (text or "").strip()
    tl = t.lower()

    # Se já está em um fluxo, não processa atalhos/menus para evitar duplicar etapas
    if SESSIONS.get(phone, {}).get("stage") not in (None, "done"):
        return continue_flow(phone, t)

    # Saudações / primeira mensagem -> sempre mostra menu
    if greeting_match(tl) or t:
        first = KNOWN_NAMES.get(phone)
        menu = welcome_text(first)

        # atalhos diretos
        if tl.startswith("1") or "produtos" in tl or "rezymol" in tl:
            return produtos_menu_text()
        if tl.startswith("2") or "compra" in tl:
            return start_flow(phone, "compra")
        if tl.startswith("3") or "catálogo" in tl or "catalogo" in tl:
            return start_flow(phone, "catalogo")
        if tl.startswith("4") or "atendente" in tl or "humano" in tl or "ajuda" in tl:
            return start_flow(phone, "atendimento")

        # caso apenas "oi" ou texto livre sem atalho: devolve o menu
        return menu

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

    # Guarda primeiro nome se vier do payload
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

    # Se o reply contém marcador de catálogo, enviar o arquivo e depois a mensagem; também enviar e-mail
    if isinstance(reply, str) and "__SEND_CATALOG_AFTER_LEAD__" in reply:
        # envia catálogo no WhatsApp
        if CATALOG_REZYMOL_URL:
            status, resp = await send_file_via_zapi(
                phone, CATALOG_REZYMOL_URL, "Catalogo-Rezymol.pdf", "📄 Catálogo Rezymol"
            )
            if status >= 300:
                # fallback: envia o link como texto
                await send_text_via_zapi(phone, f"📄 Catálogo Rezymol: {CATALOG_REZYMOL_URL}")
        else:
            await send_text_via_zapi(phone, "📄 Catálogo Rezymol não configurado no servidor.")

        # tenta enviar por e-mail se disponível no último lead
        lead = LAST_LEAD_BY_PHONE.get(phone, {})
        email = (lead.get("email") or "").strip()
        if email:
            send_catalog_email(
                to_email=email,
                subject="Catálogo Rezymol – DSA Cristal Química",
                body="Conforme solicitado, segue o catálogo Rezymol. Qualquer dúvida, estou à disposição.",
                attachment_url=CATALOG_REZYMOL_URL if CATALOG_REZYMOL_URL else None,
            )

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
