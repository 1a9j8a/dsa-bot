import os
import csv
import re
import time
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
from dotenv import load_dotenv

# ==============================
# CARREGAR VARIÁVEIS DO .ENV
# ==============================
load_dotenv()

ZAPI_BASE = os.getenv("ZAPI_BASE", "https://api.z-api.io")
INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID", "")
TOKEN = os.getenv("ZAPI_TOKEN", "")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN", "") or os.getenv("ZAPI_CLIENT_TOKEN", "")

# Link do catálogo (PDF/arquivo público acessível)
CATALOG_REZYMOL_URL = os.getenv("CATALOG_REZYMOL_URL", "")

app = FastAPI(title="DSA Bot - Spark")

# ==============================
# VARIÁVEIS GLOBAIS
# ==============================
SESSIONS: dict[str, dict] = {}           # estado por telefone
LEADS_CSV = Path("leads.csv")
KNOWN_NAMES: dict[str, str] = {}         # primeiro nome por telefone

# tempos (segundos)
IDLE_10_MIN = 10 * 60
FOLLOWUP_1H = 60 * 60
FOLLOWUP_24H = 24 * 60 * 60

# Palavras-chave que disparam saudação/menu (reinício)
GREET_TOKENS = {
    "oi", "olá", "ola", "oie", "hey", "hi", "hello",
    "bom dia", "boa tarde", "boa noite",
    "menu", "início", "inicio", "começar", "comecar", "start", "help", "ajuda",
    "quero mais informações", "quero saber da promoção", "quero saber da promocao",
    "promoção", "promocao", "informações", "informacao", "informacoes", "info"
}

# ==============================
# TEXTOS PRONTOS
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
def zapi_base_url() -> str:
    return f"{ZAPI_BASE}/instances/{INSTANCE_ID}/token/{TOKEN}"

async def send_text_via_zapi(phone: str, message: str):
    url = f"{zapi_base_url()}/send-text"
    headers = {"Client-Token": CLIENT_TOKEN} if CLIENT_TOKEN else {}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"<== STATUS DE ENVIO DE TEXTO Z-API : {r.status_code} | RESP: {r.text}")
        return r.status_code, r.text

async def send_file_via_zapi(phone: str, file_url: str, file_name: str = "", caption: str = ""):
    headers = {"Client-Token": CLIENT_TOKEN} if CLIENT_TOKEN else {}
    base = zapi_base_url()
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
# AUXILIARES DE SESSÃO
# ==============================
def reset_session(phone: str):
    SESSIONS[phone] = {
        "stage": None,
        "mode": None,
        "data": {},
        "last_user": time.time(),
        "last_bot": 0.0,
        "followups": {
            "idle10_sent": False,
            "hour_sent": False,
            "day_sent": False
        },
        "proposal_time": 0.0,     # quando o pedido é finalizado
        "flow_complete": False     # fluxo finalizado
    }

def ensure_session(phone: str):
    if phone not in SESSIONS:
        reset_session(phone)

def mark_user_activity(phone: str):
    ensure_session(phone)
    SESSIONS[phone]["last_user"] = time.time()

def mark_bot_activity(phone: str):
    ensure_session(phone)
    SESSIONS[phone]["last_bot"] = time.time()

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
    with LEADS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
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
        })

def generate_order_code(phone: str) -> str:
    date_str = datetime.now().strftime("%Y%m%d")
    short_phone = phone[-4:] if phone else "0000"
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
    out = []
    parts = re.split(r"[;,]\s*", line)
    for part in parts:
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
# FOLLOW-UPS PROATIVOS (tarefas agendadas em memória)
# ==============================
async def schedule_idle_10min_followup(phone: str):
    # Espera 10 min; se ninguém falou desde então, envia lembrete (uma única vez)
    start = SESSIONS[phone]["last_user"]
    await asyncio.sleep(IDLE_10_MIN)
    sess = SESSIONS.get(phone)
    if not sess:
        return
    if sess["followups"]["idle10_sent"]:
        return
    # Se houve atividade depois do agendamento, não envia
    if sess["last_user"] > start or sess["flow_complete"]:
        return
    sess["followups"]["idle10_sent"] = True
    msg = "Só confirmando: ficou alguma dúvida? Posso te ajudar e seguimos de onde paramos. 🙂"
    await send_text_via_zapi(phone, msg)
    mark_bot_activity(phone)

async def schedule_proposal_1h_followup(phone: str):
    # 1h após proposta/pedido finalizado
    base_time = SESSIONS[phone]["proposal_time"]
    await asyncio.sleep(FOLLOWUP_1H)
    sess = SESSIONS.get(phone)
    if not sess:
        return
    if sess["followups"]["hour_sent"]:
        return
    # Se o usuário respondeu depois da proposta, não insiste
    if sess["last_user"] > base_time:
        return
    sess["followups"]["hour_sent"] = True
    msg = "Sobre a proposta que enviamos há pouco: surgiu alguma dúvida? Estou por aqui para ajudar! 💬"
    await send_text_via_zapi(phone, msg)
    mark_bot_activity(phone)

async def schedule_day_24h_followup(phone: str):
    base_time = max(SESSIONS[phone]["last_user"], SESSIONS[phone]["last_bot"])
    await asyncio.sleep(FOLLOWUP_24H)
    sess = SESSIONS.get(phone)
    if not sess:
        return
    if sess["followups"]["day_sent"]:
        return
    if max(sess["last_user"], sess["last_bot"]) > base_time:
        return
    sess["followups"]["day_sent"] = True
    msg = "Passando para dizer que seguimos à disposição para te atender quando quiser. 🤝"
    await send_text_via_zapi(phone, msg)
    mark_bot_activity(phone)

def start_idle_10min_timer(phone: str):
    # dispara checagem de 10min sempre que o usuário manda algo no meio do fluxo
    ensure_session(phone)
    SESSIONS[phone]["followups"]["idle10_sent"] = False
    asyncio.create_task(schedule_idle_10min_followup(phone))

def start_day_24h_timer(phone: str):
    ensure_session(phone)
    # não reseta o flag se já enviado; apenas agenda a partir do momento atual
    asyncio.create_task(schedule_day_24h_followup(phone))

# ==============================
# FLUXOS
# ==============================
def start_flow(phone: str, mode: str):
    ensure_session(phone)
    # reinicia fluxo do zero
    SESSIONS[phone].update({
        "mode": mode,
        "stage": "ask_name",
        "data": {"cart": []},
        "flow_complete": False
    })
    start_idle_10min_timer(phone)   # começa a vigiar 10min durante o fluxo
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
    tl = (text or "").strip().lower()

    # COMUM
    if sess["stage"] == "ask_name":
        data["nome"] = text.strip()
        sess["stage"] = "ask_phone"
        return "Por favor, informe seu *telefone* com DDD."

    if sess["stage"] == "ask_phone":
        data["telefone_cliente"] = re.sub(r"\D", "", text)
        sess["stage"] = "ask_profile"
        return (
            "Qual é o seu *perfil*?\n"
            "1) Representante\n"
            "2) Cliente\n"
            "3) Distribuidor\n"
            "4) Fornecedor de Produtos - Matéria Prima"
        )

    if sess["stage"] == "ask_profile":
        perfis = {
            "1": "Representante",
            "2": "Cliente",
            "3": "Distribuidor",
            "4": "Fornecedor de Produtos - Matéria Prima",
        }
        data["perfil"] = perfis.get(tl, text.strip())
        sess["stage"] = "ask_company"
        return "Qual é o nome da *empresa*?"

    if sess["stage"] == "ask_company":
        data["empresa"] = text.strip()
        sess["stage"] = "ask_cnpj"
        return "Perfeito. Qual é o *CNPJ* da empresa? (somente números)"

    if sess["stage"] == "ask_cnpj":
        m = re.search(r"\b\d{14}\b", text)
        data["cnpj"] = (m.group(0) if m else re.sub(r"\D", "", text))
        sess["stage"] = "ask_endereco"
        label = (
            "Informe o *endereço comercial* (Rua, número, bairro, cidade, UF, CEP)."
            if data.get("perfil", "").lower().startswith("represent")
            else "Informe o *endereço* (Rua, número, bairro, cidade, UF, CEP)."
        )
        return label

    if sess["stage"] == "ask_endereco":
        data["endereco"] = text.strip()
        if mode == "catalogo":
            sess["stage"] = "ask_email_catalogo"
            return "Por fim, seu *e-mail* para registro (opcional)."
        sess["stage"] = "ask_email"
        return "Por fim, seu *e-mail* de contato (opcional)."

    # ==============================
    # CATÁLOGO (envio SÓ via WhatsApp)
    # ==============================
    if mode == "catalogo":
        if sess["stage"] == "ask_email_catalogo":
            data["email"] = text.strip()  # apenas registro/CSV
            sess["stage"] = "done"
            sess["flow_complete"] = True
            save_lead(data, phone, "catalogo")

            resumo = (
                "✅ Dados recebidos! Estou enviando agora o *Catálogo Rezymol* diretamente por aqui. 📲\n\n"
                f"👤 *Nome:* {data.get('nome','')}\n"
                f"🏢 *Empresa:* {data.get('empresa','')}\n"
                f"🆔 *CNPJ:* {data.get('cnpj','')}\n"
                "Se precisar de ajuda com algum produto ou cotação, é só me avisar! 💬"
            )
            # Agenda lembrete de 24h a partir de agora
            start_day_24h_timer(phone)
            # Flag para o webhook enviar o arquivo via WhatsApp com send_file_via_zapi
            return f"{resumo}\n__SEND_CATALOG_AFTER_LEAD__:rezymol"

    # ==============================
    # COMPRA
    # ==============================
    if mode == "compra":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            sess["stage"] = "ask_items"
            return (
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
                sess["flow_complete"] = True
                save_lead(data, phone, "compra")

                itens_str = (
                    "\n".join([f"• {i['desc']} x{i['qty']}" for i in data.get("cart", [])])
                    if data.get("cart")
                    else "—"
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
                # registra tempo da “proposta/pedido” para follow-up de 1h
                SESSIONS[phone]["proposal_time"] = time.time()
                SESSIONS[phone]["followups"]["hour_sent"] = False
                asyncio.create_task(schedule_proposal_1h_followup(phone))
                # agenda lembrete de 24h
                start_day_24h_timer(phone)
                return resumo

            # tentar adicionar itens da linha
            parsed = parse_items_free_text(text)
            if parsed:
                data.setdefault("cart", []).extend(parsed)
                added = "\n".join([f"• {i['desc']} x{i['qty']}" for i in parsed])
                return (
                    f"Adicionei ao carrinho:\n{added}\n\nSe quiser, envie mais itens. Para encerrar, digite *finalizar*."
                )
            else:
                return (
                    "Não consegui identificar itens nessa mensagem.\n"
                    "Envie no formato: *Produto x2* (separando por vírgulas ou ponto e vírgula)."
                )

    # ==============================
    # ATENDIMENTO
    # ==============================
    if mode == "atendimento":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            sess["stage"] = "done"
            sess["flow_complete"] = True
            save_lead(data, phone, "atendimento")
            # agenda lembrete de 24h
            start_day_24h_timer(phone)
            return (
                "✅ Dados recebidos! Em instantes um atendente da DSA falará com você.\n"
                f"Resumo: *{data.get('nome','')}*, *{data.get('empresa','')}*, *{data.get('endereco','')}*."
            )

    # fallback
    return "Pode repetir, por favor? Digite *menu* para ver as opções."

# ==============================
# ROUTES
# ==============================
@app.get("/")
async def root():
    return PlainTextResponse("DSA Bot - Spark ativo. Use POST /api/webhook/receber.")

@app.get("/health")
async def health():
    return PlainTextResponse("ok")

@app.post("/api/webhook/receber")
async def receber(request: Request):
    body = await request.json()
    print("CORPO BRUTO :", body)

    # Z-API formatos comuns
    phone = str(body.get("phone") or "")
    from_me = bool(body.get("fromMe"))
    status = body.get("status", "")
    # normaliza texto
    texto = ""
    if isinstance(body.get("texto"), dict):
        texto = str(body["texto"].get("mensagem") or "")
    else:
        texto = str(body.get("message") or body.get("text") or "")

    sender_name = body.get("senderName") or body.get("chatName") or ""
    first_name = first_name_from_sender(sender_name)
    if first_name:
        KNOWN_NAMES[phone] = first_name

    # Ignorar mensagens que EU enviei (para não entrar em loop)
    if from_me:
        return JSONResponse({"ok": True, "ignored": "fromMe"})

    print(f"==> MSG DE: {phone} | TEXTO: {texto}")
    mark_user_activity(phone)

    # função para responder
    async def reply(msg: str):
        status, _ = await send_text_via_zapi(phone, msg)
        mark_bot_activity(phone)
        return status

    # Saudações/atalhos SEMPRE reiniciam (sua opção A)
    msg_lower = (texto or "").strip().lower()
    def is_greeting_token(s: str) -> bool:
        s = re.sub(r"[!,.?;:]+", "", s).strip()
        return s in GREET_TOKENS

    if is_greeting_token(msg_lower):
        reset_session(phone)
        await reply(welcome_text(KNOWN_NAMES.get(phone)))
        return JSONResponse({"ok": True, "reset": True})

    # Comandos diretos equivalentes
    if msg_lower in {"menu", "início", "inicio", "help", "ajuda"}:
        reset_session(phone)
        await reply(welcome_text(KNOWN_NAMES.get(phone)))
        return JSONResponse({"ok": True})

    if msg_lower in {"1", "produtos", "produto", "linha", "rezymol"}:
        ensure_session(phone)
        await reply(produtos_menu_text())
        start_day_24h_timer(phone)  # mesmo fora de fluxo, agenda 24h
        return JSONResponse({"ok": True})

    if msg_lower in {"2", "compra", "comprar"}:
        out = start_flow(phone, "compra")
        await reply(out)
        return JSONResponse({"ok": True})

    if msg_lower in {"3", "catalogo", "catálogo", "catalogue"}:
        out = start_flow(phone, "catalogo")
        await reply(out)
        return JSONResponse({"ok": True})

    if msg_lower in {"4", "atendente", "especialista", "humano", "suporte"}:
        out = start_flow(phone, "atendimento")
        await reply(out)
        return JSONResponse({"ok": True})

    # Se já estiver em fluxo, continuar
    sess = SESSIONS.get(phone)
    if sess and sess.get("stage") not in (None, "done"):
        # a cada mensagem do usuário no fluxo, (re)agenda o lembrete de 10min
        start_idle_10min_timer(phone)

        resposta = continue_flow(phone, texto)

        # Envia texto da resposta (sem a flag)
        clean_resp = resposta.replace("__SEND_CATALOG_AFTER_LEAD__:rezymol", "").strip()
        if clean_resp:
            await reply(clean_resp)

        # Se houver a flag de envio do catálogo, dispara o arquivo via WhatsApp
        if "__SEND_CATALOG_AFTER_LEAD__:rezymol" in resposta and CATALOG_REZYMOL_URL:
            caption = "📘 *Catálogo Rezymol* — DSA Cristal Química\nSe preferir, salve este arquivo para consultar quando quiser."
            status_code, resp_text = await send_file_via_zapi(
                phone, CATALOG_REZYMOL_URL, file_name="Catalogo-Rezymol.pdf", caption=caption
            )
            if status_code >= 300:
                await reply("Tive um problema ao enviar o catálogo. Pode me confirmar se recebeu? Se não, tento reenviar.")
        return JSONResponse({"ok": True})

    # Fora de fluxo, sem comando reconhecido → ajuda + agenda 24h
    await reply("Não entendi. Digite *menu* para ver as opções ou me diga o que precisa. 😊")
    start_day_24h_timer(phone)
    return JSONResponse({"ok": True})
