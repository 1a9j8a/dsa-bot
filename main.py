import os
import csv
import re
import time
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

# Configs de nudge
IDLE_NUDGE_SECONDS = 600                 # 10min (quando em fluxo, ao receber uma nova msg, cutuca)
NUDGE_10M = 10 * 60
NUDGE_1H  = 60 * 60
NUDGE_24H = 24 * 60 * 60

# Palavras-chave e intenções que disparam saudação/menu
GREET_KEYWORDS = {
    "oi", "olá", "ola", "oie", "hey", "hi", "hello", "bom dia", "boa tarde", "boa noite",
    "quero mais informações", "quero informações", "quero saber da promoção",
    "promoção", "promocao", "tenho interesse", "gostaria de saber", "preciso de ajuda"
}
COMMAND_TOKENS = {"menu", "início", "inicio", "start", "help", "ajuda"}
CANCEL_TOKENS = {"cancelar", "cancelar fluxo", "sair", "parar", "encerrar"}

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
        "📘 *Para solicitar Catálogo*, digite *3* ou *catálogo*.\n"
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
    """
    Tenta enviar arquivo por diferentes endpoints da Z-API, pois variam por plano/versão:
      1) /send-file
      2) /send-file-from-url
      3) /send-document
    Usa o primeiro que funcionar (status < 300). Loga a resposta de cada tentativa.
    """
    headers = {"Client-Token": CLIENT_TOKEN} if CLIENT_TOKEN else {}
    base = zapi_base_url()

    payload = {"phone": phone, "file": file_url}
    if file_name:
        payload["fileName"] = file_name
    if caption:
        payload["caption"] = caption

    endpoints = [
        "send-file",
        "send-file-from-url",
        "send-document",
    ]

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
# AUXILIARES
# ==============================
def ensure_session(phone: str):
    SESSIONS.setdefault(phone, {
        "stage": None,
        "mode": None,
        "data": {},
        "last": time.time(),
        "nudge_flags": {"10m": False, "1h": False, "24h": False},
        "last_outbound": 0.0
    })
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

# -------- Extração robusta do texto recebido --------
def extract_incoming_text(body: dict) -> str:
    """
    Extrai o texto da mensagem de forma robusta:
    - Se 'texto' for dict: usa 'mensagem'
    - Se 'texto' for string parecida com JSON/dict: tenta regex para campo 'mensagem'
    - Fallbacks: 'message', 'text', 'body', 'content', 'msg'
    """
    raw = body.get("texto")

    if isinstance(raw, dict):
        v = raw.get("mensagem")
        if isinstance(v, (str, int, float)):
            return str(v).strip()

    if isinstance(raw, str):
        m = re.search(r"'mensagem'\s*:\s*'([^']*)'", raw)
        if m:
            return m.group(1).strip()
        m = re.search(r'"mensagem"\s*:\s*"([^"]*)"', raw)
        if m:
            return m.group(1).strip()
        if raw.strip():
            return raw.strip()

    for key in ("message", "text", "body", "content", "msg"):
        if body.get(key):
            return str(body.get(key)).strip()

    # Áudio, imagem, template etc. sem texto -> vazio
    return ""

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
# FLUXOS
# ==============================
def start_flow(phone: str, mode: str):
    ensure_session(phone)
    if SESSIONS[phone].get("stage") not in (None, "done"):
        return "Você já está em um fluxo. Pode continuar de onde parou. 😊"

    SESSIONS[phone] = {
        "mode": mode,
        "stage": "ask_name",
        "data": {"cart": []},
        "last": time.time(),
        "nudge_flags": {"10m": False, "1h": False, "24h": False},
        "last_outbound": 0.0
    }
    if mode == "compra":
        return "🛒 Vamos registrar seu pedido! Qual é o seu *Nome*?"
    if mode == "catalogo":
        return "📄 Para enviar o catálogo, preciso de alguns dados. Qual é o seu *Nome*?"
    return "📞 Vamos agilizar seu atendimento humano. Qual é o seu *Nome*?"

def continue_flow(phone: str, text: str) -> str:
    ensure_session(phone)
    sess = SESSIONS[phone]
    data = sess["data"]
    mode = sess["mode"]
    tl = (text or "").lower().strip()

    nudge = maybe_idle_nudge(phone)
    prefix = f"{nudge}\n\n" if nudge else ""

    # COMUM
    if sess["stage"] == "ask_name":
        data["nome"] = text.strip().title()
        sess["stage"] = "ask_phone"
        return prefix + "Por favor, informe seu *Telefone* com DDD."

    if sess["stage"] == "ask_phone":
        data["telefone_cliente"] = re.sub(r"\D", "", text)
        sess["stage"] = "ask_profile"
        return prefix + (
            "Qual é o seu *Perfil*?\n"
            "1) Cliente\n"
            "2) Distribuidor\n"
            "3) Representante\n"
            "4) Fornecedor de Produtos - Matéria Prima"
        )

    if sess["stage"] == "ask_profile":
        perfis = {
            "1": "Cliente",
            "2": "Distribuidor",
            "3": "Representante",
            "4": "Fornecedor de Produtos - Matéria Prima",
        }
        # aceita número 1-4 ou texto
        data["perfil"] = perfis.get(tl, text.strip().title())
        sess["stage"] = "ask_company"
        return prefix + "Qual é o nome da *Empresa*?"

    if sess["stage"] == "ask_company":
        data["empresa"] = text.strip().title()
        sess["stage"] = "ask_cnpj"
        return prefix + "Perfeito. Qual é o *CNPJ* da empresa? (Somente números)"

    if sess["stage"] == "ask_cnpj":
        m = re.search(r"\b\d{14}\b", text)
        data["cnpj"] = (m.group(0) if m else re.sub(r"\D", "", text))
        sess["stage"] = "ask_endereco"
        label = (
            "Informe o *Endereço Comercial* (Rua, número, bairro, cidade, UF, CEP)."
            if str(data.get("perfil","")).lower().startswith("represent")
            else "Informe o *Endereço* (Rua, número, bairro, cidade, UF, CEP)."
        )
        return prefix + label

    if sess["stage"] == "ask_endereco":
        data["endereco"] = text.strip().title()
        if mode == "catalogo":
            sess["stage"] = "ask_email_catalogo"
            return prefix + "Por fim, seu *E-mail* para registro (opcional)."
        sess["stage"] = "ask_email"
        return prefix + "Por fim, seu *E-mail* de contato (opcional)."

    # ==============================
    # CATÁLOGO
    # ==============================
    if mode == "catalogo":
        if sess["stage"] == "ask_email_catalogo":
            data["email"] = text.strip()
            sess["stage"] = "done"
            save_lead(data, phone, "catalogo")

            resumo = (
                "✅ Dados recebidos! Estou enviando agora o *Catálogo Rezymol* diretamente por aqui. 📲\n\n"
                f"👤 *Nome:* {data.get('nome','')}\n"
                f"🏢 *Empresa:* {data.get('empresa','')}\n"
                f"🆔 *CNPJ:* {data.get('cnpj','')}\n"
                "Se precisar de ajuda com algum produto ou cotação, é só me avisar! 💬"
            )
            return f"{resumo}\n__SEND_CATALOG_AFTER_LEAD__:rezymol"

    # ==============================
    # COMPRA
    # ==============================
    if mode == "compra":
        if sess["stage"] == "ask_email":
            data["email"] = text.strip()
            sess["stage"] = "ask_items"
            return prefix + (
                "Perfeito! Agora me diga *Produtos e Quantidades*.\n\n"
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
                return resumo

            parsed = parse_items_free_text(text)
            if parsed:
                data.setdefault("cart", []).extend(parsed)
                added = "\n".join([f"• {i['desc']} x{i['qty']}" for i in parsed])
                return (
                    prefix
                    + f"Adicionei ao carrinho:\n{added}\n\nSe quiser, envie mais itens. Para encerrar, digite *finalizar*."
                )
            else:
                return prefix + (
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
            save_lead(data, phone, "atendimento")
            return prefix + (
                "✅ Dados recebidos! Em instantes um atendente da DSA falará com você.\n"
                f"Resumo: *{data.get('nome','')}*, *{data.get('empresa','')}*, *{data.get('endereco','')}*."
            )

    return prefix + "Pode repetir, por favor? Digite *menu* para ver as opções."

# ==============================
# ROTAS
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

    # Campos comuns da Z-API (variam conforme idioma/plano)
    phone = str(body.get("phone") or "")
    from_me = bool(body.get("fromMe"))
    _ = body.get("status", "")

    texto = extract_incoming_text(body)
    sender_name = body.get("senderName") or body.get("chatName") or body.get("nomeChat") or ""
    first_name = first_name_from_sender(sender_name)
    if first_name:
        KNOWN_NAMES[phone] = first_name

    if from_me:
        return JSONResponse({"ok": True, "ignored": "fromMe"})

    print(f"==> MSG DE: {phone} | TEXTO PARS: {texto!r}")

    async def reply(msg: str):
        SESSIONS.setdefault(phone, {})
        SESSIONS[phone]["last_outbound"] = time.time()
        return await send_text_via_zapi(phone, msg)

    # Garante sessão
    ensure_session(phone)
    sess = SESSIONS.get(phone) or {}
    in_flow = sess.get("stage") not in (None, "done")

    msg_lower = (texto or "").strip().lower()

    # ====== Comandos de controle em fluxo ======
    if in_flow and msg_lower in CANCEL_TOKENS:
        SESSIONS[phone]["stage"] = "done"
        await reply("Fluxo cancelado. Se quiser recomeçar, digite *menu*.")
        return JSONResponse({"ok": True})

    # Se ESTÁ em fluxo, prioriza continuar o fluxo e NÃO deixar números 1/2/3/4 dispararem o menu
    if in_flow:
        sess["last"] = time.time()
        resposta = continue_flow(phone, texto)
        clean_resp = resposta.replace("__SEND_CATALOG_AFTER_LEAD__:rezymol", "").strip()
        if clean_resp:
            await reply(clean_resp)
        if "__SEND_CATALOG_AFTER_LEAD__:rezymol" in resposta and CATALOG_REZYMOL_URL:
            caption = "📘 *Catálogo Rezymol* — DSA Cristal Química\nSe preferir, salve este arquivo para consultar quando quiser."
            status_code, _ = await send_file_via_zapi(
                phone, CATALOG_REZYMOL_URL, file_name="Catalogo-Rezymol.pdf", caption=caption
            )
            if status_code >= 300:
                await reply("Tive um problema ao enviar o catálogo. Pode me confirmar se recebeu? Se não, tento reenviar.")
        return JSONResponse({"ok": True})

    # ====== Fora de fluxo: saudações/atalhos/menu ======
    contains_greet = any(k in msg_lower for k in GREET_KEYWORDS)
    is_quick_symbol = (len(msg_lower) <= 2 and msg_lower in {"?", "ok", "oi", "hi", "yo", "👍", "👋"})
    numeric_option = msg_lower in {"1", "2", "3", "4"}
    direct_token = msg_lower in COMMAND_TOKENS or msg_lower.startswith("spark")

    if contains_greet or is_quick_symbol or direct_token:
        await reply(welcome_text(KNOWN_NAMES.get(phone)))
        return JSONResponse({"ok": True})

    # Comandos diretos (fora de fluxo)
    if msg_lower in {"menu", "início", "inicio", "help", "ajuda"}:
        await reply(welcome_text(KNOWN_NAMES.get(phone)))
        return JSONResponse({"ok": True})

    if numeric_option or msg_lower in {"produtos", "produto", "linha", "rezymol"}:
        if msg_lower == "1" or msg_lower in {"produtos", "produto", "linha", "rezymol"}:
            await reply(produtos_menu_text())
            return JSONResponse({"ok": True})
        if msg_lower == "2":
            out = start_flow(phone, "compra")
            await reply(out)
            return JSONResponse({"ok": True})
        if msg_lower == "3":
            out = start_flow(phone, "catalogo")
            await reply(out)
            return JSONResponse({"ok": True})
        if msg_lower == "4":
            out = start_flow(phone, "atendimento")
            await reply(out)
            return JSONResponse({"ok": True})

    # Não reconhecido fora de fluxo → ajuda
    await reply("Não entendi. Digite *menu* para ver as opções ou me diga o que precisa. 😊")
    return JSONResponse({"ok": True})

# ==============================
# CRON PARA NUDGES PROATIVOS (10m / 1h / 24h)
# ==============================
@app.get("/cron/tick")
async def cron_tick():
    now = time.time()
    results = []

    for phone, sess in list(SESSIONS.items()):
        try:
            stage = sess.get("stage")
            mode = sess.get("mode")
            last = float(sess.get("last", now))
            flags = sess.setdefault("nudge_flags", {"10m": False, "1h": False, "24h": False})
            last_out = float(sess.get("last_outbound", 0.0))

            if stage in (None, "done"):
                continue

            elapsed = now - last
            if now - last_out < 60:
                continue

            if elapsed >= NUDGE_10M and not flags.get("10m", False):
                msg = "Percebi que ficou um tempinho sem responder. Posso ajudar em algo ou ficou alguma dúvida? 🙂"
                await send_text_via_zapi(phone, msg)
                sess["last_outbound"] = now
                flags["10m"] = True
                results.append((phone, "nudge_10m"))
                continue

            if mode == "compra" and elapsed >= NUDGE_1H and not flags.get("1h", False):
                msg = "Conseguiu verificar a proposta/itens? Se precisar, reviso os detalhes ou ajusto o pedido. 👌"
                await send_text_via_zapi(phone, msg)
                sess["last_outbound"] = now
                flags["1h"] = True
                results.append((phone, "nudge_1h"))
                continue

            if elapsed >= NUDGE_24H and not flags.get("24h", False):
                msg = "Continuo à disposição para te ajudar quando quiser. É só me chamar por aqui. 🤝"
                await send_text_via_zapi(phone, msg)
                sess["last_outbound"] = now
                flags["24h"] = True
                results.append((phone, "nudge_24h"))

        except Exception as e:
            results.append((phone, f"error: {repr(e)}"))

    return JSONResponse({"ok": True, "nudges": results})
