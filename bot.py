import os
import logging
import json
import re
import time
import asyncio
import csv
import io
from datetime import datetime, timedelta
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
MARGEM_PADRAO = 0.50
IMPOSTO_PADRAO_MEI = 0.03   # 3%

# Taxas por canal de venda
TAXA_CANAL = {
    "direto": 1.0,
    "ifood": 1.15,
    "shopee": 1.12,
    "lojas": 0.8
}

# Mapeamento idioma → país para inflação (World Bank code)
IDIOMA_PAIS = {
    "pt": "BRA",
    "en": "USA",
    "es": "MEX"
}

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
# PROMPT UNIVERSAL
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
# CONSULTORIA (agora com teste A/B)
# ============================================
CONSULTING_PROMPTS = {
    "pt": """Você é um consultor de negócios simpático e direto, focado em ajudar pequenos empreendedores a vender mais.
O usuário vende {unidade}. O preço seguro calculado foi {moeda}{preco_seguro:.2f}.
O público-alvo que ele atende é: {audiencia}.

Com base nisso, escreva uma resposta amigável (máximo 4 parágrafos curtos) contendo:
1. Uma sugestão de combo ou upsell (com nome e valor sugerido, usando a moeda {moeda}).
2. Um lembrete para reajustar os preços em 2 meses, mencionando a inflação como motivo.
3. Uma dica para testar o novo preço com uma pequena amostra de clientes (ex: 10%) antes de aplicar a todos.

IMPORTANTE: NÃO mencione preços da concorrência, pois você não tem dados reais sobre eles.
Não use JSON, apenas o texto final.""",

    "en": """You are a friendly, straight-to-the-point business consultant helping small entrepreneurs sell more.
The user sells {unidade}. The calculated safe price is {moeda}{preco_seguro:.2f}.
Their target audience is: {audiencia}.

Based on that, write a friendly response (max 4 short paragraphs) containing:
1. A combo or upsell suggestion (with name and suggested price, using {moeda}).
2. A reminder to adjust prices in 2 months, mentioning inflation as the reason.
3. A tip to test the new price with a small sample of customers (e.g., 10%) before applying to everyone.

IMPORTANT: Do NOT mention competitors' prices, as you have no real data on them.
Do not use JSON, just the final text.""",

    "es": """Eres un consultor de negocios simpático y directo, enfocado en ayudar a pequeños emprendedores a vender más.
El usuario vende {unidade}. El precio seguro calculado es {moeda}{preco_seguro:.2f}.
Su público objetivo es: {audiencia}.

Con base en eso, escribe una respuesta amigable (máximo 4 párrafos cortos) que contenga:
1. Una sugerencia de combo o venta adicional (con nombre y precio sugerido, usando {moeda}).
2. Un recordatorio para ajustar los precios en 2 meses, mencionando la inflación como motivo.
3. Un consejo para probar el nuevo precio con una pequeña muestra de clientes (ej.: 10%) antes de aplicarlo a todos.

IMPORTANTE: NO menciones precios de la competencia, ya que no tienes datos reales sobre ellos.
No uses JSON, solo el texto final."""
}

# --- Fallback local para consultoria ---
FALLBACK_CONSULTING = {
    "pt": "💡 *Dica rápida:* que tal oferecer um combo com outro produto relacionado? Por exemplo, {unidade} + bebida por um valor especial.\n\n"
          "📈 Lembre-se de revisar seu preço a cada 2 meses para acompanhar a inflação.\n\n"
          "🧪 Antes de mudar o preço para todos, teste com alguns clientes e veja a reação!",
    "en": "💡 *Quick tip:* how about offering a combo with another related product? For example, {unidade} + drink for a special price.\n\n"
          "📈 Remember to review your price every 2 months to keep up with inflation.\n\n"
          "🧪 Before changing the price for everyone, test it with a few customers and see how they react!",
    "es": "💡 *Consejo rápido:* ¿qué tal ofrecer un combo con otro producto relacionado? Por ejemplo, {unidade} + bebida por un precio especial.\n\n"
          "📈 Recuerda revisar tu precio cada 2 meses para seguir la inflación.\n\n"
          "🧪 ¡Antes de cambiar el precio para todos, pruébalo con algunos clientes y observa la reacción!"
}

# ============================================
# FUNÇÕES AUXILIARES
# ============================================
def extrair_json(resposta: str) -> dict:
    inicio = resposta.find('{')
    fim = resposta.rfind('}')
    if inicio != -1 and fim != -1 and fim > inicio:
        candidato = resposta[inicio:fim+1]
        try:
            return json.loads(candidato)
        except json.JSONDecodeError:
            pass
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
# INFLAÇÃO (World Bank API – por país)
# ============================================
async def get_inflacao_pais(country_code: str) -> float:
    """Retorna a inflação acumulada anual mais recente para o país (variação %)."""
    if country_code == "BRA":
        # Mantém IPCA rápido para Brasil (Banco Central)
        return await get_ipca_mensal()
    # Para outros, tenta World Bank
    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/FP.CPI.TOTL.ZG?format=json&per_page=1&mrv=1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dados = resp.json()
            if len(dados) >= 2 and dados[1]:
                valor = dados[1][0].get("value")
                if valor is not None:
                    return float(valor)
    except Exception as e:
        logger.error(f"Erro ao obter inflação para {country_code}: {e}")
    return 0.0

async def get_ipca_mensal() -> float:
    """Mantida para Brasil (IPCA mensal, Banco Central)."""
    url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dados = resp.json()
            if dados:
                return float(dados[-1]["valor"])
    except:
        pass
    return 0.0

async def obter_inflacao_para_usuario(user_id: str) -> float:
    lang = user_language.get(user_id, 'pt')
    country = IDIOMA_PAIS.get(lang, "USA")  # fallback
    return await get_inflacao_pais(country)

# ============================================
# HISTÓRICO E EXPORTAÇÃO
# ============================================
async def salvar_historico(user_id, resultado, preco_final, custo_fixo, vendas, canal):
    data = await load_user_data()
    if user_id not in data:
        data[user_id] = {"first_seen": datetime.now().isoformat()}
    if "historico" not in data[user_id]:
        data[user_id]["historico"] = []
    produto = user_state[user_id].get("dados", {}).get("unidade") or "produto"
    entry = {
        "data": datetime.now().isoformat(),
        "produto": produto,
        "preco_final": preco_final,
        "custo_fixo_mensal": custo_fixo,
        "vendas_mes": vendas,
        "canal": canal
    }
    data[user_id]["historico"].append(entry)
    # Mantém os últimos 50 registros
    data[user_id]["historico"] = data[user_id]["historico"][-50:]
    await save_user_data(data)

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

async def historico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await load_user_data()
    hist = data.get(user_id, {}).get("historico", [])
    if not hist:
        await update.message.reply_text("Você ainda não registrou nenhum preço. Que tal começar?")
        return
    lang = user_language.get(user_id, 'pt')
    moeda = MOEDAS.get(lang, 'R$')
    reply = "📋 *Seu histórico de preços (últimos 10):*\n\n"
    for i, entry in enumerate(hist[-10:], 1):
        reply += f"{i}. {entry['produto']} — {moeda}{entry['preco_final']:.2f} ({entry['data'][:10]})\n"
    await update.message.reply_text(reply, parse_mode="Markdown")

async def revisar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await load_user_data()
    hist = data.get(user_id, {}).get("historico", [])
    if len(hist) < 2:
        await update.message.reply_text("Você precisa de pelo menos dois preços registrados para comparar.")
        return
    lang = user_language.get(user_id, 'pt')
    moeda = MOEDAS.get(lang, 'R$')
    # Últimos 5 (ou menos)
    ultimos = hist[-5:]
    reply = "📈 *Tendência de preços (últimos 5):*\n\n"
    for i in range(1, len(ultimos)):
        atual = ultimos[i]
        anterior = ultimos[i-1]
        variacao = ((atual["preco_final"] - anterior["preco_final"]) / anterior["preco_final"]) * 100
        sinal = "+" if variacao > 0 else ""
        reply += f"{anterior['data'][:10]} → {atual['data'][:10]}: {moeda}{anterior['preco_final']:.2f} → {moeda}{atual['preco_final']:.2f} ({sinal}{variacao:.1f}%)\n"
    await update.message.reply_text(reply, parse_mode="Markdown")

async def exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await load_user_data()
    hist = data.get(user_id, {}).get("historico", [])
    if not hist:
        await update.message.reply_text("Nenhum dado para exportar.")
        return
    # Gera CSV em memória
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["data", "produto", "preco_final", "custo_fixo_mensal", "vendas_mes", "canal"])
    writer.writeheader()
    for entry in hist:
        writer.writerow(entry)
    csv_bytes = output.getvalue().encode("utf-8")
    # Envia como documento
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename="historico_precos.csv",
        caption="📎 Seu histórico exportado."
    )

async def alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await load_user_data()
    alerts = data.get(user_id, {}).get("alerts_enabled", False)
    novo_status = not alerts
    await atualizar_dados_usuario(user_id, alerts_enabled=novo_status)
    lang = user_language.get(user_id, 'pt')
    msgs = {
        'pt': "Alertas de inflação ativados! Você receberá avisos quando precisar reajustar seus preços.",
        'en': "Inflation alerts enabled! You'll be notified when prices need adjustment.",
        'es': "¡Alertas de inflación activados! Recibirás avisos cuando necesites ajustar tus precios."
    }
    off_msgs = {
        'pt': "Alertas de inflação desativados.",
        'en': "Inflation alerts disabled.",
        'es': "Alertas de inflación desactivados."
    }
    text = msgs.get(lang, msgs['pt']) if novo_status else off_msgs.get(lang, off_msgs['pt'])
    await update.message.reply_text(text)

async def reajuste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await load_user_data()
    hist = data.get(user_id, {}).get("historico", [])
    if not hist:
        await update.message.reply_text("Você ainda não tem preço registrado. Faça um cálculo primeiro!")
        return
    lang = user_language.get(user_id, 'pt')
    moeda = MOEDAS.get(lang, 'R$')
    ultimo = hist[-1]
    inflacao = await obter_inflacao_para_usuario(user_id)
    if inflacao == 0:
        await update.message.reply_text("Não consegui obter a inflação agora. Tente mais tarde.")
        return
    novo_preco = round(ultimo["preco_final"] * (1 + inflacao/100), 2)
    respostas = {
        'pt': f"A inflação (último período) foi de *{inflacao}%*.\n"
              f"Seu último preço ({ultimo['produto']}) era *{moeda}{ultimo['preco_final']:.2f}*.\n"
              f"Para manter o poder de compra, o novo preço sugerido é: *{moeda}{novo_preco:.2f}*",
        'en': f"The latest inflation was *{inflacao}%*.\n"
              f"Your last price ({ultimo['produto']}) was *{moeda}{ultimo['preco_final']:.2f}*.\n"
              f"To keep the same purchasing power, the suggested new price is: *{moeda}{novo_preco:.2f}*",
        'es': f"La inflación (último período) fue del *{inflacao}%*.\n"
              f"Tu último precio ({ultimo['produto']}) era *{moeda}{ultimo['preco_final']:.2f}*.\n"
              f"Para mantener el poder adquisitivo, el nuevo precio sugerido es: *{moeda}{novo_preco:.2f}*"
    }
    await update.message.reply_text(respostas.get(lang, respostas['pt']), parse_mode="Markdown")

# ============================================
# HANDLER PRINCIPAL
# ============================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_msg = update.message.text
    if not user_msg:
        return

    msg_lower = user_msg.strip().lower()

    # --- SAUDAÇÕES ---
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

    # --- IDIOMA PERSISTENTE ---
    if user_id not in user_language:
        try:
            detected = detect(user_msg)
            if detected in ['pt', 'en', 'es']:
                user_language[user_id] = detected
            else:
                user_language[user_id] = 'en'
        except:
            user_language[user_id] = 'pt'
    else:
        if len(user_msg.split()) >= 4 or "?" in user_msg:
            try:
                detected = detect(user_msg)
                if detected in ['pt', 'en', 'es']:
                    user_language[user_id] = detected
            except:
                pass

    lang = user_language[user_id]
    moeda = MOEDAS.get(lang, 'R$')

    # --- ALERTA AUTOMÁTICO DE INFLAÇÃO (apenas se alertas ativados) ---
    data = await load_user_data()
    if data.get(user_id, {}).get("alerts_enabled") and data[user_id].get("historico"):
        ultimo_hist = data[user_id]["historico"][-1]
        ultima_data = datetime.fromisoformat(ultimo_hist["data"])
        # Alerta se passaram mais de 30 dias e ainda não alertamos hoje
        if (datetime.now() - ultima_data > timedelta(days=30) and
            data[user_id].get("last_alert_date") != datetime.now().strftime("%Y-%m-%d")):
            inflacao = await obter_inflacao_para_usuario(user_id)
            if inflacao > 0:
                novo_preco = round(ultimo_hist["preco_final"] * (1 + inflacao/100), 2)
                alerta = {
                    'pt': f"🔔 Já faz 30 dias desde seu último cálculo. A inflação foi de {inflacao}%. Que tal reajustar seu preço?\n"
                          f"Último: {moeda}{ultimo_hist['preco_final']:.2f} → Sugestão: {moeda}{novo_preco:.2f}",
                    'en': f"🔔 It's been 30 days since your last calculation. Inflation was {inflacao}%. How about adjusting your price?\n"
                          f"Last: {moeda}{ultimo_hist['preco_final']:.2f} → Suggested: {moeda}{novo_preco:.2f}",
                    'es': f"🔔 Han pasado 30 días desde tu último cálculo. La inflación fue del {inflacao}%. ¿Qué tal ajustar tu precio?\n"
                          f"Último: {moeda}{ultimo_hist['preco_final']:.2f} → Sugerido: {moeda}{novo_preco:.2f}"
                }
                await update.message.reply_text(alerta.get(lang, alerta['pt']), parse_mode="Markdown")
                await atualizar_dados_usuario(user_id, last_alert_date=datetime.now().strftime("%Y-%m-%d"))

    # --- ESTATÍSTICAS ---
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

    # --- CAPTURA DE NOME ---
    padroes_nome = [
        r'meu nome é\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'chamo\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'sou o\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'sou a\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
        r'me chamo\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)',
    ]
    for padrao in padroes_nome:
        match = re.search(padrao, user_msg, re.IGNORECASE)
        if match:
            nome = match.group(1)
            user_state[user_id] = user_state.get(user_id, {})
            user_state[user_id]["nome"] = nome
            await atualizar_dados_usuario(user_id, nome=nome)
            saudacoes_nome = {
                'pt': f"Olá {nome}! Agora, me conta: você tem algum produto para precificar?",
                'en': f"Hello {nome}! Now, tell me: do you have a product to price?",
                'es': f"¡Hola {nome}! Ahora, dime: ¿tienes algún producto para fijar precio?"
            }
            await update.message.reply_text(saudacoes_nome.get(lang, saudacoes_nome['pt']))
            return

    if any(p in msg_lower for p in ["qual meu nome", "meu nome", "quem sou eu"]):
        nome = user_state.get(user_id, {}).get("nome")
        if nome:
            replies = {'pt': f"Seu nome é {nome}!", 'en': f"Your name is {nome}!", 'es': f"¡Tu nombre es {nome}!"}
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

    # --- ESTÁGIOS DE CONVERSA ---
    stage = user_state.get(user_id, {}).get("stage")

    if stage == "awaiting_audience":
        audiencia = user_msg.strip()
        try:
            consulta = await gerar_consultoria(user_id, audiencia)
        except:
            # Fallback local
            unidade = user_state[user_id].get("dados", {}).get("unidade", "seu produto")
            consulta = FALLBACK_CONSULTING.get(lang, FALLBACK_CONSULTING["pt"]).format(unidade=unidade)
        user_state[user_id]["stage"] = "consulting_done"
        await update.message.reply_text(consulta, parse_mode="Markdown")
        await atualizar_dados_usuario(user_id, state=user_state[user_id])
        return

    # (restante dos estágios de custos fixos, vendas, canal mantidos iguais)
    if stage == "awaiting_fixed_costs":
        try:
            custo_fixo = float(user_msg.strip().replace(",", "."))
            user_state[user_id]["custo_fixo_total"] = custo_fixo
            perguntas = {
                'pt': "Quantos produtos você vende (ou espera vender) por mês, em média?",
                'en': "How many products do you sell (or expect to sell) per month on average?",
                'es': "¿Cuántos productos vendes (o esperas vender) al mes en promedio?"
            }
            await update.message.reply_text(perguntas.get(lang))
            user_state[user_id]["stage"] = "awaiting_monthly_sales"
            return
        except:
            await update.message.reply_text("Não entendi o valor. Pode me enviar apenas o número (ex: 350)?")
            return

    if stage == "awaiting_monthly_sales":
        try:
            vendas_mes = int(user_msg.strip())
            user_state[user_id]["vendas_mes"] = vendas_mes
            resultado = user_state[user_id]["resultado"]
            custo_fixo = user_state[user_id].get("custo_fixo_total", 0)
            vendas = max(vendas_mes, 1)
            custo_fixo_unitario = custo_fixo / vendas
            preco_com_fixo = resultado["preco_seguro"] + custo_fixo_unitario
            preco_final = preco_com_fixo * (1 + IMPOSTO_PADRAO_MEI)

            lucro_unitario = preco_final - resultado["custo_total"] - custo_fixo_unitario
            ponto_equilibrio = round(custo_fixo / lucro_unitario) if lucro_unitario > 0 else "∞"

            user_state[user_id]["preco_final_sem_canal"] = preco_final
            user_state[user_id]["custo_fixo_unitario"] = custo_fixo_unitario

            reply = (
                f"🏠 *Análise financeira realista*\n\n"
                f"• Custo fixo mensal: {moeda}{custo_fixo:.2f}\n"
                f"• Vendas por mês: {vendas}\n"
                f"• Custo fixo por unidade: {moeda}{custo_fixo_unitario:.2f}\n"
                f"• Preço antes do imposto: {moeda}{preco_com_fixo:.2f}\n"
                f"• Imposto estimado (3% MEI): {moeda}{preco_com_fixo * IMPOSTO_PADRAO_MEI:.2f}\n"
                f"→ *Preço final (sem canal): {moeda}{preco_final:.2f}*\n\n"
                f"💰 *Projeção mensal:*\n"
                f"• Receita esperada: {moeda}{preco_final * vendas:.2f}\n"
                f"• Lucro líquido: {moeda}{lucro_unitario * vendas:.2f}\n"
                f"• Ponto de equilíbrio: vender *{ponto_equilibrio}* unid./mês\n\n"
            )

            perguntas_canal = {
                'pt': "Agora, para finalizar: você vende direto para o cliente, por aplicativo (iFood, Shopee) ou para lojas/revendedores?\n"
                      "Responda: direto, ifood, shopee ou lojas.",
                'en': "Now, to finalize: do you sell directly to customers, through apps (iFood, Shopee) or to stores/resellers?\n"
                      "Answer: direct, ifood, shopee or stores.",
                'es': "Ahora, para finalizar: ¿vendes directo al cliente, por aplicación (iFood, Shopee) o a tiendas/revendedores?\n"
                      "Responde: directo, ifood, shopee o tiendas."
            }
            reply += perguntas_canal.get(lang, perguntas_canal['pt'])
            await update.message.reply_text(reply, parse_mode="Markdown")
            user_state[user_id]["stage"] = "awaiting_channel"
            return
        except:
            await update.message.reply_text("Não entendi. Quantos produtos você vende por mês? (ex: 30)")
            return

    if stage == "awaiting_channel":
        canal = msg_lower.strip()
        canal_map = {
            "direto": "direto", "direct": "direto", "directo": "direto",
            "ifood": "ifood", "shopee": "shopee", "lojas": "lojas", "stores": "lojas", "tiendas": "lojas"
        }
        canal = canal_map.get(canal, "direto")
        fator = TAXA_CANAL.get(canal, 1.0)

        preco_final = user_state[user_id]["preco_final_sem_canal"] * fator
        resultado = user_state[user_id]["resultado"]
        custo_fixo = user_state[user_id].get("custo_fixo_total", 0)
        vendas = max(user_state[user_id].get("vendas_mes", 1), 1)
        custo_fixo_unitario = user_state[user_id]["custo_fixo_unitario"]
        lucro_unitario = preco_final - resultado["custo_total"] - custo_fixo_unitario

        nome_canal = {"direto": "Venda direta", "ifood": "iFood", "shopee": "Shopee", "lojas": "Lojas/revendedores"}.get(canal, canal.capitalize())
        reply = (
            f"✅ *Preço ajustado para {nome_canal}*\n\n"
            f"• Fator de canal: {fator*100:.0f}%\n"
            f"→ *Preço final: {moeda}{round(preco_final,2):.2f}*\n\n"
            f"Lucro líquido estimado por unidade: {moeda}{round(lucro_unitario,2):.2f}\n"
            f"Lucro mensal projetado: {moeda}{round(lucro_unitario * vendas,2):.2f}\n\n"
        )

        await salvar_historico(user_id, resultado, preco_final, custo_fixo, vendas, canal)
        await update.message.reply_text(reply, parse_mode="Markdown")

        user_state[user_id]["stage"] = "done"
        await atualizar_dados_usuario(user_id, state=user_state[user_id])
        return

    # --- ASSISTENTE UNIVERSAL (para qualquer outra mensagem) ---
    try:
        system_prompt = UNIVERSAL_PROMPT.get(lang, UNIVERSAL_PROMPT['pt'])
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ]
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
        response = await chamar_groq_async(messages, headers, temperature=0.7)
        raw_reply = response.json()["choices"][0]["message"]["content"].strip()
    except:
        reply = gerar_resposta_fallback(user_id)
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    try:
        dados = extrair_json(raw_reply)
        if dados.get("custo") is not None and dados.get("horas") is not None and dados.get("valor_hora") is not None:
            resultado = calcular_preco(
                custo=float(dados["custo"]),
                horas=float(dados["horas"]),
                valor_hora=float(dados["valor_hora"]),
                margem_pct=float(dados.get("margem_pct", MARGEM_PADRAO))
            )
            user_state[user_id] = {"dados": dados, "resultado": resultado}
            reply = formatar_resposta_preco(dados, resultado, lang) if not dados.get("quer_detalhes") else formatar_detalhes(resultado, lang)
            await update.message.reply_text(reply, parse_mode="Markdown")
            await atualizar_dados_usuario(user_id, state=user_state[user_id], language=lang)
            await iniciar_custos_fixos(update, user_id, lang)
            return
    except:
        pass

    # Resposta normal
    user_histories[user_id].append({"role": "assistant", "content": raw_reply})
    await update.message.reply_text(raw_reply, parse_mode="Markdown")

async def iniciar_custos_fixos(update: Update, user_id: str, lang: str):
    perguntas = {
        'pt': "Agora vamos colocar os custos reais do seu negócio! 🏠\n"
              "Você tem custos fixos mensais (luz, aluguel, internet, maquininha, transporte)?\n"
              "Se sim, me diga o valor total por mês (ex: 300).",
        'en': "Let's add your real business costs! 🏠\n"
              "Do you have fixed monthly costs (electricity, rent, internet, card machine, delivery)?\n"
              "If yes, tell me the total per month (e.g., 200).",
        'es': "¡Agregemos tus costos reales! 🏠\n"
              "¿Tienes costos fijos mensuales (luz, alquiler, internet, terminal de tarjeta, envío)?\n"
              "Si es así, dime el total al mes (ej.: 200)."
    }
    pergunta = perguntas.get(lang, perguntas['pt'])
    user_state[user_id]["stage"] = "awaiting_fixed_costs"
    await atualizar_dados_usuario(user_id, state=user_state[user_id])
    await update.message.reply_text(pergunta)

async def gerar_consultoria(user_id: str, audiencia: str) -> str:
    lang = user_language.get(user_id, 'pt')
    moeda = MOEDAS.get(lang, 'R$')
    dados = user_state[user_id]["dados"]
    resultado = user_state[user_id]["resultado"]
    unidade = dados.get("unidade", "seu produto/serviço")
    preco_seguro = resultado["preco_seguro"]
    prompt_template = CONSULTING_PROMPTS.get(lang, CONSULTING_PROMPTS['pt'])
    system_prompt = prompt_template.format(unidade=unidade, moeda=moeda, preco_seguro=preco_seguro, audiencia=audiencia)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Público: {audiencia}"}]
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    response = await chamar_groq_async(messages, headers, temperature=0.7)
    return response.json()["choices"][0]["message"]["content"].strip()

FALLBACKS = {
    'pt': "😅 *Poxa, estou com muitas pessoas usando o bot agora!* ...",
    'en': "😅 *Wow, I'm overwhelmed right now!* ...",
    'es': "😅 *¡Vaya, tengo mucha gente ahora!* ..."
}

def gerar_resposta_fallback(user_id: str) -> str:
    lang = user_language.get(user_id, 'pt')
    return FALLBACKS.get(lang, FALLBACKS['en'])

# ============================================
# INICIALIZAÇÃO
# ============================================
if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não configurado nas variáveis de ambiente.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("historico", historico))
    app.add_handler(CommandHandler("revisar", revisar))
    app.add_handler(CommandHandler("exportar", exportar))
    app.add_handler(CommandHandler("alertas", alertas))
    app.add_handler(CommandHandler("reajuste", reajuste))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot rodando 24/7 no Render...")
    app.run_polling(drop_pending_updates=True)
