import os
import logging
import json
import re
import time
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from langdetect import detect
from langdetect import DetectorFactory
DetectorFactory.seed = 0

# ============================================
# CONFIGURAÇÃO DO AMBIENTE
# ============================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID", "8407367527")

DATA_DIR = "/data" if os.path.exists("/data") else "."
USER_DATA_FILE = os.path.join(DATA_DIR, "user_data.json")
LOG_FILE = os.path.join(DATA_DIR, "meu_caderninho.log")

# ============================================
# REGRAS DE PRECIFICAÇÃO
# ============================================
MARGEM_PADRAO = 0.50  # 50%

# ============================================
# SERVIDOR DE SAÚDE
# ============================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()

thread = threading.Thread(target=run_health_server, daemon=True)
thread.start()

# ============================================
# LOG
# ============================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================
# PERSISTÊNCIA COM TRAVA ASSÍNCRONA
# ============================================
file_lock = asyncio.Lock()

def load_user_data_sync():
    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

async def load_user_data():
    async with file_lock:
        if os.path.exists(USER_DATA_FILE):
            with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

async def save_user_data(data):
    async with file_lock:
        with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

user_histories = {}
user_state = {}
user_language = {}

def carregar_estados_iniciais():
    data = load_user_data_sync()
    for uid, uinfo in data.items():
        lang = uinfo.get("language")
        if lang:
            user_language[uid] = lang
        state = uinfo.get("state")
        if state:
            user_state[uid] = state

carregar_estados_iniciais()

async def atualizar_dados_usuario(user_id: str, **kwargs):
    data = await load_user_data()
    if user_id not in data:
        data[user_id] = {}
    for key, value in kwargs.items():
        if value is not None:
            data[user_id][key] = value
        else:
            data[user_id].pop(key, None)
    await save_user_data(data)

# ============================================
# CÁLCULO TRANSPARENTE
# ============================================
def calcular_preco(
    custo: float,
    horas: float,
    valor_hora: float,
    margem_pct: float = MARGEM_PADRAO
) -> dict:
    custo_tempo = horas * valor_hora
    custo_total = custo + custo_tempo
    margem_valor = custo_total * margem_pct
    preco_seguro = custo_total + margem_valor
    preco_agressivo = preco_seguro * 0.85
    preco_valor_agregado = preco_seguro * 1.25

    return {
        "custo_material": round(custo, 2),
        "horas": horas,
        "valor_hora": valor_hora,
        "custo_tempo": round(custo_tempo, 2),
        "custo_total": round(custo_total, 2),
        "margem_pct": margem_pct,
        "margem_valor": round(margem_valor, 2),
        "preco_seguro": round(preco_seguro, 2),
        "preco_agressivo": round(preco_agressivo, 2),
        "preco_valor_agregado": round(preco_valor_agregado, 2),
    }

MOEDAS = {'pt': 'R$', 'en': '$', 'es': '€'}

def formatar_resposta_preco(dados: dict, resultado: dict, lang: str = 'pt') -> str:
    moeda = MOEDAS.get(lang, 'R$')
    unidade = dados.get("unidade")
    ref = f" ({unidade})" if unidade else ""
    return (
        f"Fechei a conta{ref}:\n\n"
        f"• Custo dos materiais: {moeda}{resultado['custo_material']:.2f}\n"
        f"• Seu tempo ({resultado['horas']:g}h × {moeda}{resultado['valor_hora']:.2f}/h): "
        f"{moeda}{resultado['custo_tempo']:.2f}\n"
        f"• Custo total: {moeda}{resultado['custo_total']:.2f}\n"
        f"• Margem de {int(resultado['margem_pct']*100)}%: {moeda}{resultado['margem_valor']:.2f}\n\n"
        f"→ *Preço Seguro: {moeda}{resultado['preco_seguro']:.2f}*\n\n"
        f"Quer ver as outras opções (Agressivo e Valor Agregado)?"
    )

def formatar_detalhes(resultado: dict, lang: str = 'pt') -> str:
    moeda = MOEDAS.get(lang, 'R$')
    return (
        f"Aqui estão as 3 opções, em cima da mesma conta:\n\n"
        f"*Seguro*: {moeda}{resultado['preco_seguro']:.2f}\n"
        f"*Agressivo*: {moeda}{resultado['preco_agressivo']:.2f} (–15%)\n"
        f"*Valor Agregado*: {moeda}{resultado['preco_valor_agregado']:.2f} (+25%)\n\n"
        f"Qual deles combina mais com o seu momento? (responda 'Seguro', 'Agressivo' ou 'Valor Agregado')"
    )

# ============================================
# PROMPT UNIVERSAL (ASSISTENTE + EXTRAÇÃO)
# ============================================
UNIVERSAL_PROMPT = {
    "pt": """Você é o Meu Caderninho, um assistente amigável que ajuda empreendedores a precificar seus produtos.

**Sua tarefa:**
1. Responda à mensagem do usuário de forma direta, útil e em poucas frases.
2. Se o usuário estiver fornecendo informações para precificação (custo em reais, horas de trabalho, valor que quer ganhar por hora), extraia esses dados e devolva **APENAS** um JSON com os campos: "custo" (float), "horas" (float), "valor_hora" (float), "unidade" (string ou null), "margem_pct" (float ou null) e "quer_detalhes" (booleano).
3. Caso contrário, apenas responda à pergunta e, ao final, pergunte se o usuário quer precificar algo.

**IMPORTANTE:**
- Se a mensagem NÃO contiver números de custo/horas/valor, responda com texto puro (não use JSON).
- Se houver informações de precificação, retorne SOMENTE o JSON, sem texto adicional.

**Exemplos:**
Usuário: "Qual a capital da França?"
Resposta: "Paris é a capital da França! 🇫🇷 Agora, me conta: você tem algum produto para precificar?"

Usuário: "Custo 30 reais, leva 2 horas, quero ganhar 20 por hora"
Resposta: {"custo": 30, "horas": 2, "valor_hora": 20, "unidade": null, "margem_pct": null, "quer_detalhes": false}""",

    "en": """You are Pricing Pal, a friendly assistant that helps entrepreneurs price their products.

**Your task:**
1. Respond to the user's message directly, helpfully, and in a few sentences.
2. If the user is providing pricing information (cost in dollars, hours of work, desired hourly rate), extract that data and return **ONLY** a JSON with the fields: "custo" (float), "horas" (float), "valor_hora" (float), "unidade" (string or null), "margem_pct" (float or null) and "quer_detalhes" (boolean).
3. Otherwise, just answer the question and at the end ask if the user wants to price something.

**IMPORTANT:**
- If the message does NOT contain cost/hours/rate numbers, respond with plain text (no JSON).
- If there is pricing information, return ONLY the JSON, with no extra text.

**Examples:**
User: "What's the capital of France?"
Response: "Paris is the capital of France! 🇫🇷 Now, tell me: do you have a product to price?"

User: "Cost $30, takes 2 hours, I want to earn $20 per hour"
Response: {"custo": 30, "horas": 2, "valor_hora": 20, "unidade": null, "margem_pct": null, "quer_detalhes": false}""",

    "es": """Eres Mi Cuaderno, un asistente amigable que ayuda a emprendedores a fijar precios.

**Tu tarea:**
1. Responde al mensaje del usuario de forma directa, útil y en pocas frases.
2. Si el usuario está proporcionando información para fijar precios (costo en euros, horas de trabajo, cuánto quiere ganar por hora), extrae esos datos y devuelve **SOLO** un JSON con los campos: "custo" (float), "horas" (float), "valor_hora" (float), "unidade" (string o null), "margem_pct" (float o null) y "quer_detalhes" (booleano).
3. De lo contrario, solo responde a la pregunta y al final pregunta si el usuario quiere fijar el precio de algo.

**IMPORTANTE:**
- Si el mensaje NO contiene números de costo/horas/tarifa, responde con texto plano (no uses JSON).
- Si hay información de precios, devuelve SOLO el JSON, sin texto adicional.

**Ejemplos:**
Usuario: "¿Cuál es la capital de Francia?"
Respuesta: "París es la capital de Francia! 🇫🇷 Ahora dime, ¿tienes algún producto para fijar precio?"

Usuario: "Costo 30 euros, 2 horas, quiero ganar 20 por hora"
Respuesta: {"custo": 30, "horas": 2, "valor_hora": 20, "unidade": null, "margem_pct": null, "quer_detalhes": false}"""
}

# ============================================
# CONSULTORIA (mantida separada)
# ============================================
CONSULTING_PROMPTS = {
    "pt": """Você é um consultor de negócios simpático e direto, focado em ajudar pequenos empreendedores a vender mais.
O usuário vende {unidade}. O preço seguro calculado foi {moeda}{preco_seguro:.2f}.
O público-alvo que ele atende é: {audiencia}.

Com base nisso, escreva uma resposta amigável (máximo 4 parágrafos curtos) contendo:
1. Uma sugestão de combo ou upsell (com nome e valor sugerido, usando a moeda {moeda}).
2. Um lembrete para reajustar os preços em 2 meses, mencionando a inflação como motivo.

IMPORTANTE: NÃO mencione preços da concorrência, pois você não tem dados reais sobre eles.
Não use JSON, apenas o texto final.""",

    "en": """You are a friendly, straight-to-the-point business consultant helping small entrepreneurs sell more.
The user sells {unidade}. The calculated safe price is {moeda}{preco_seguro:.2f}.
Their target audience is: {audiencia}.

Based on that, write a friendly response (max 4 short paragraphs) containing:
1. A combo or upsell suggestion (with name and suggested price, using {moeda}).
2. A reminder to adjust prices in 2 months, mentioning inflation as the reason.

IMPORTANT: Do NOT mention competitors' prices, as you have no real data on them.
Do not use JSON, just the final text.""",

    "es": """Eres un consultor de negocios simpático y directo, enfocado en ayudar a pequeños emprendedores a vender más.
El usuario vende {unidade}. El precio seguro calculado es {moeda}{preco_seguro:.2f}.
Su público objetivo es: {audiencia}.

Con base en eso, escribe una respuesta amigable (máximo 4 párrafos cortos) que contenga:
1. Una sugerencia de combo o venta adicional (con nombre y precio sugerido, usando {moeda}).
2. Un recordatorio para ajustar los precios en 2 meses, mencionando la inflación como motivo.

IMPORTANTE: NO menciones precios de la competencia, ya que no tienes datos reales sobre ellos.
No uses JSON, solo el texto final."""
}

# ============================================
# FUNÇÕES AUXILIARES
# ============================================
def extrair_json(resposta: str) -> dict:
    """Tenta extrair um objeto JSON de uma string, com tratamento específico de erro."""
    inicio = resposta.find('{')
    fim = resposta.rfind('}')
    if inicio != -1 and fim != -1 and fim > inicio:
        candidato = resposta[inicio:fim+1]
        try:
            return json.loads(candidato)
        except json.JSONDecodeError:
            pass
    # Última tentativa com a resposta inteira; se falhar, levanta exceção (capturada no handler)
    return json.loads(resposta)

# ============================================
# CHAMADAS À GROQ
# ============================================
ultimo_tempo_groq = 0
MIN_INTERVALO_GROQ = 3.0
groq_bloqueado_ate = 0

async def chamar_groq_async(messages, headers, temperature=0):
    global ultimo_tempo_groq, groq_bloqueado_ate

    agora = time.time()
    if agora < groq_bloqueado_ate:
        raise Exception("Groq temporariamente indisponível. Aguarde alguns minutos.")

    diff = agora - ultimo_tempo_groq
    if diff < MIN_INTERVALO_GROQ:
        await asyncio.sleep(MIN_INTERVALO_GROQ - diff)

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 600,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        
        if response.status_code == 429:
            groq_bloqueado_ate = time.time() + 60
            logger.warning("Limite da Groq excedido. Bloqueando novas chamadas por 1 minuto.")
            raise Exception("Limite de requisições excedido. Tente novamente em 1 minuto.")
        
        ultimo_tempo_groq = time.time()
        response.raise_for_status()
        return response

# ============================================
# CONSULTORIA ASSÍNCRONA
# ============================================
async def gerar_consultoria(user_id: str, audiencia: str) -> str:
    lang = user_language.get(user_id, 'pt')
    moeda = MOEDAS.get(lang, 'R$')
    dados = user_state[user_id]["dados"]
    resultado = user_state[user_id]["resultado"]
    unidade = dados.get("unidade", "seu produto/serviço")
    preco_seguro = resultado["preco_seguro"]

    prompt_template = CONSULTING_PROMPTS.get(lang, CONSULTING_PROMPTS['pt'])
    system_prompt = prompt_template.format(
        unidade=unidade,
        moeda=moeda,
        preco_seguro=preco_seguro,
        audiencia=audiencia
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Público: {audiencia}"}
    ]
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    response = await chamar_groq_async(messages, headers, temperature=0.7)
    return response.json()["choices"][0]["message"]["content"].strip()

# ============================================
# FALLBACK
# ============================================
FALLBACKS = {
    'pt': "😅 *Poxa, estou com muitas pessoas usando o bot agora!*\n\n"
          "Minha capacidade de pensar (a inteligência artificial) atingiu o limite do plano gratuito. "
          "Mas calma, daqui a pouquinho eu volto ao normal.\n\n"
          "Enquanto isso, você pode me falar:\n"
          "• Quanto custa o material\n"
          "• Quantas horas (ou minutos) leva para fazer\n"
          "• Quanto você quer ganhar por hora\n\n"
          "Que eu já vou anotando. Quando voltar, calculo rapidinho!",

    'en': "😅 *Wow, I'm overwhelmed right now!*\n\n"
          "My AI brain hit the free plan limit. But hang tight, I'll be back to normal in a bit.\n\n"
          "In the meantime, you can tell me:\n"
          "• How much the materials cost\n"
          "• How many hours (or minutes) it takes to make\n"
          "• How much you want to earn per hour\n\n"
          "And I'll take notes. As soon as I'm back, I'll crunch the numbers!",

    'es': "😅 *¡Vaya, tengo mucha gente ahora!*\n\n"
          "Mi capacidad de IA alcanzó el límite del plan gratuito. Pero tranquilo, en un ratito vuelvo a la normalidad.\n\n"
          "Mientras tanto, puedes decirme:\n"
          "• Cuánto cuestan los materiales\n"
          "• Cuántas horas (o minutos) te lleva hacerlo\n"
          "• Cuánto quieres ganar por hora\n\n"
          "Que voy tomando nota. ¡En cuanto vuelva, te hago el cálculo!"
}

def gerar_resposta_fallback(user_id: str) -> str:
    lang = user_language.get(user_id, 'pt')
    return FALLBACKS.get(lang, FALLBACKS['en'])

# ============================================
# HANDLER PRINCIPAL
# ============================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_msg = update.message.text
    if not user_msg:
        return

    msg_lower = user_msg.strip().lower()

    # --- SAUDAÇÕES (força idioma) ---
    saudacoes = {
        "oi": "pt", "olá": "pt", "ola": "pt", "oie": "pt", "oiee": "pt", "e aí": "pt", "eai": "pt",
        "opa": "pt", "tudo bem": "pt", "bom dia": "pt", "boa tarde": "pt", "boa noite": "pt",
        "hi": "en", "hello": "en", "hey": "en", "good morning": "en", "good afternoon": "en",
        "good evening": "en", "how are you": "en", "what's up": "en", "yo": "en",
        "hola": "es", "holi": "es", "buenos días": "es", "buenas tardes": "es",
        "buenas noches": "es", "qué tal": "es", "cómo estás": "es",
    }

    if msg_lower in saudacoes:
        lang = saudacoes[msg_lower]
        user_language[user_id] = lang
        user_histories[user_id] = []
        user_state.pop(user_id, None)

        boas_vindas = {
            "pt": "Oi! Sou o Meu Caderninho. Me fala o que você quer precificar.",
            "en": "Hi! I'm Pricing Pal. Tell me what you want to price.",
            "es": "¡Hola! Soy Mi Cuaderno. Dime qué quieres precificar.",
        }
        await update.message.reply_text(boas_vindas[lang])
        await atualizar_dados_usuario(user_id, language=lang, state=None)
        return

    # --- DETECÇÃO DE IDIOMA COM PERSISTÊNCIA ---
    if user_id not in user_language:
        # Primeira mensagem: detecta e define
        try:
            detected = detect(user_msg)
            if detected in ['pt', 'en', 'es']:
                user_language[user_id] = detected
            else:
                user_language[user_id] = 'en'
        except:
            user_language[user_id] = 'pt'
    else:
        # Já tem idioma: só atualiza se mensagem longa (≥4 palavras) ou contiver "?"
        if len(user_msg.split()) >= 4 or "?" in user_msg:
            try:
                detected = detect(user_msg)
                if detected in ['pt', 'en', 'es']:
                    user_language[user_id] = detected
            except:
                pass
        # Se curta, mantém o idioma atual

    lang = user_language[user_id]
    moeda = MOEDAS.get(lang, 'R$')

    # --- ESTATÍSTICAS E PERSISTÊNCIA ---
    data = await load_user_data()
    if user_id not in data:
        data[user_id] = {"first_seen": datetime.now().isoformat()}
    data[user_id]["last_seen"] = datetime.now().isoformat()
    data[user_id]["messages"] = data[user_id].get("messages", 0) + 1
    data[user_id]["language"] = lang
    await save_user_data(data)

    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "content": user_msg})

    # --- CAPTURAR NOME DO USUÁRIO ---
    padroes_nome = [
        r'meu nome é\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'chamo\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'sou o\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'sou a\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'me chamo\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
    ]
    nome_encontrado = False
    for padrao in padroes_nome:
        match = re.search(padrao, user_msg, re.IGNORECASE)
        if match:
            nome = match.group(1)
            if user_id not in user_state:
                user_state[user_id] = {}
            user_state[user_id]["nome"] = nome
            await atualizar_dados_usuario(user_id, nome=nome)
            saudacoes_nome = {
                'pt': f"Olá {nome}! Agora, me conta: você tem algum produto para precificar?",
                'en': f"Hello {nome}! Now, tell me: do you have a product to price?",
                'es': f"¡Hola {nome}! Ahora, dime: ¿tienes algún producto para fijar precio?"
            }
            await update.message.reply_text(saudacoes_nome.get(lang, saudacoes_nome['pt']))
            nome_encontrado = True
            break
    if nome_encontrado:
        return

    # --- PERGUNTA SOBRE O NOME ---
    if any(p in msg_lower for p in ["qual meu nome", "meu nome", "quem sou eu"]):
        nome = user_state.get(user_id, {}).get("nome")
        if nome:
            replies = {
                'pt': f"Seu nome é {nome}!",
                'en': f"Your name is {nome}!",
                'es': f"¡Tu nombre es {nome}!"
            }
            reply = replies.get(lang, replies['pt'])
        else:
            replies = {
                'pt': "Ainda não sei seu nome. Pode me dizer? (ex: 'meu nome é Caio')",
                'en': "I don't know your name yet. Can you tell me? (e.g., 'my name is John')",
                'es': "Aún no sé tu nombre. ¿Puedes decírmelo? (ej.: 'mi nombre es Carlos')"
            }
            reply = replies.get(lang, replies['pt'])
        await update.message.reply_text(reply)
        return

    # --- CONSULTORIA (estágio especial) ---
    if user_id in user_state and user_state[user_id].get("stage") == "awaiting_audience":
        audiencia = user_msg.strip()
        try:
            consulta = await gerar_consultoria(user_id, audiencia)
        except Exception as e:
            logger.error(f"Erro na consultoria: {e}")
            consulta = {
                'pt': "🐌 Ops, deu uma travada aqui. Mas me conta: você costuma vender pra que tipo de cliente?",
                'en': "🐌 Oops, got a bit stuck. Tell me, who do you usually sell to?",
                'es': "🐌 ¡Ups! Me trabé un poco. Cuéntame, ¿a quién le sueles vender?"
            }.get(lang, "Quem compra seu produto?")

        user_state[user_id]["stage"] = "consulting_done"
        await update.message.reply_text(consulta, parse_mode="Markdown")
        await atualizar_dados_usuario(user_id, state=user_state[user_id])
        return

    # --- CHAMADA AO ASSISTENTE UNIVERSAL ---
    try:
        system_prompt = UNIVERSAL_PROMPT.get(lang, UNIVERSAL_PROMPT['pt'])
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ]
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
        response = await chamar_groq_async(messages, headers, temperature=0.7)
        raw_reply = response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Erro no assistente universal: {e}")
        reply = gerar_resposta_fallback(user_id)
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    # Tentar interpretar como JSON de precificação
    try:
        dados = extrair_json(raw_reply)
        if (dados.get("custo") is not None and 
            dados.get("horas") is not None and 
            dados.get("valor_hora") is not None):
            
            try:
                resultado = calcular_preco(
                    custo=float(dados["custo"]),
                    horas=float(dados["horas"]),
                    valor_hora=float(dados["valor_hora"]),
                    margem_pct=float(dados.get("margem_pct")) if dados.get("margem_pct") is not None else MARGEM_PADRAO
                )
            except (ValueError, TypeError) as e:
                logger.error(f"Erro ao converter valores: {e}")
                await update.message.reply_text("Não consegui entender algum número. Pode repetir?")
                return

            user_state[user_id] = {"dados": dados, "resultado": resultado}

            if dados.get("quer_detalhes"):
                reply = formatar_detalhes(resultado, lang)
            else:
                reply = formatar_resposta_preco(dados, resultado, lang)

            user_histories[user_id].append({"role": "assistant", "content": reply})
            await update.message.reply_text(reply, parse_mode="Markdown")
            await atualizar_dados_usuario(user_id, state=user_state[user_id], language=lang)

            if not dados.get("quer_detalhes"):
                await iniciar_consultoria(update, user_id, lang)
            return
    except:
        pass

    # Resposta normal (texto)
    user_histories[user_id].append({"role": "assistant", "content": raw_reply})
    await update.message.reply_text(raw_reply, parse_mode="Markdown")

async def iniciar_consultoria(update: Update, user_id: str, lang: str):
    perguntas = {
        'pt': "Agora, deixa eu te ajudar a vender melhor! 👇\nQuem costuma comprar seu produto? (ex: festas de aniversário, happy hour, empresas...)",
        'en': "Now let me help you sell better! 👇\nWho usually buys your product? (e.g., birthday parties, happy hour, companies...)",
        'es': "¡Ahora déjame ayudarte a vender mejor! 👇\n¿Quién suele comprar tu producto? (ej.: fiestas de cumpleaños, happy hour, empresas...)"
    }
    pergunta = perguntas.get(lang, perguntas['pt'])
    user_state[user_id]["stage"] = "awaiting_audience"
    await atualizar_dados_usuario(user_id, state=user_state[user_id])
    await update.message.reply_text(pergunta)

# ============================================
# COMANDOS
# ============================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await load_user_data()
    if user_id not in data:
        data[user_id] = {"first_seen": datetime.now().isoformat()}
    data[user_id]["last_seen"] = datetime.now().isoformat()
    data[user_id]["messages"] = data[user_id].get("messages", 0) + 1
    await save_user_data(data)

    user_histories[user_id] = []
    user_state.pop(user_id, None)
    await atualizar_dados_usuario(user_id, state=None)
    await update.message.reply_text("Oi! Sou o Meu Caderninho. Me fala o que você quer precificar.")

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_ID:
        await update.message.reply_text("Você não tem permissão.")
        return

    data = await load_user_data()
    total_users = len(data)
    total_messages = sum(u.get("messages", 0) for u in data.values())
    last_users = sorted(data.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)[:5]
    last_users_text = "\n".join([f"- {u[0]}: {u[1].get('last_seen', 'Nunca')[:16]}" for u in last_users])

    reply = (
        f"📊 *Meu Caderninho*\n"
        f"👥 Usuários: {total_users}\n"
        f"💬 Mensagens: {total_messages}\n\n"
        f"🕒 Últimos:\n{last_users_text}"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

# ============================================
# INICIALIZAÇÃO
# ============================================
if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não configurado nas variáveis de ambiente.")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot rodando 24/7 no Render...")
    app.run_polling(drop_pending_updates=True)
