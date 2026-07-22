import os
import logging
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================
# CONFIGURAÇÃO DO AMBIENTE
# ============================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID", "8407367527")

# Caminho para dados persistentes (Render monta um disco em /data)
DATA_DIR = "/data" if os.path.exists("/data") else "."
USER_DATA_FILE = os.path.join(DATA_DIR, "user_data.json")
LOG_FILE = os.path.join(DATA_DIR, "meu_caderninho.log")

# ============================================
# REGRAS DE PRECIFICAÇÃO — decisão de negócio, não do modelo.
# Mude aqui, não no prompt, e o comportamento muda pra todo mundo de uma vez.
# ============================================
MARGEM_PADRAO = 0.5        # 50% — usada quando o usuário não diz a margem que quer
VALOR_HORA_PADRAO = 20.0   # R$/hora — quanto o tempo do dono vale, embutido no preço.
                            # Coloque None se preferir que o tempo do dono NÃO entre na conta.

# ============================================
# SERVIDOR DE SAÚDE (para manter o Render acordado)
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
# CONFIGURAÇÃO DE LOG
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
# DADOS DOS USUÁRIOS (persistência em JSON)
# ============================================
def load_user_data():
    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_user_data(data):
    with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ============================================
# HISTÓRICO E ESTADO POR USUÁRIO (em memória)
# ============================================
user_histories = {}
user_state = {}  # último cálculo de cada usuário — usado quando ele só pede "mais detalhes"

# ============================================
# CÁLCULO DE PREÇO — determinístico. O modelo NUNCA faz essa conta.
# ============================================
def calcular_preco(custo: float, horas: float = None, margem_pct: float = None, valor_hora: float = None) -> dict:
    """
    Única fonte de verdade do preço. Mesma entrada -> sempre a mesma saída.

    custo: custo de insumos/materiais em R$ (obrigatório)
    horas: tempo de produção em horas decimais (opcional)
    margem_pct: margem desejada, 0.5 = 50% (usa MARGEM_PADRAO se None)
    valor_hora: R$/hora do trabalho do dono (usa VALOR_HORA_PADRAO se None)
    """
    margem_pct = MARGEM_PADRAO if margem_pct is None else margem_pct
    valor_hora = VALOR_HORA_PADRAO if valor_hora is None else valor_hora

    margem_valor = custo * margem_pct
    custo_tempo = (horas * valor_hora) if (horas and valor_hora) else 0.0

    preco_seguro = custo + margem_valor + custo_tempo
    preco_agressivo = preco_seguro * 0.75
    preco_valor_agregado = preco_seguro * 1.35

    return {
        "custo": round(custo, 2),
        "horas": horas,
        "margem_pct": margem_pct,
        "margem_valor": round(margem_valor, 2),
        "valor_hora": valor_hora,
        "custo_tempo": round(custo_tempo, 2),
        "preco_seguro": round(preco_seguro, 2),
        "preco_agressivo": round(preco_agressivo, 2),
        "preco_valor_agregado": round(preco_valor_agregado, 2),
    }

# ============================================
# TEMPLATES DE RESPOSTA — texto fixo, só os números (já calculados) entram.
# O modelo nunca reescreve nem recalcula esses valores.
# ============================================
def formatar_resposta_preco(dados: dict, resultado: dict) -> str:
    unidade = dados.get("unidade")
    ref = f" ({unidade})" if unidade else ""
    if resultado["custo_tempo"]:
        h_fmt = f"{resultado['horas']:g}"
        linha_tempo = f" + R${resultado['custo_tempo']:.2f} pelo seu tempo ({h_fmt}h)"
    else:
        linha_tempo = ""
    return (
        f"Fechei a conta{ref}: R${resultado['custo']:.2f} de custo "
        f"+ R${resultado['margem_valor']:.2f} de margem ({int(resultado['margem_pct']*100)}%)"
        f"{linha_tempo} = *R${resultado['preco_seguro']:.2f}*.\n\n"
        f"Quer ver os 3 caminhos de preço (Seguro, Agressivo, Valor Agregado)? É só pedir \"mais detalhes\"."
    )

def formatar_detalhes(resultado: dict) -> str:
    return (
        f"Aqui vão as 3 opções, todas em cima dos mesmos números:\n\n"
        f"*Seguro*: R${resultado['preco_seguro']:.2f} — sua margem normal, risco baixo.\n"
        f"*Agressivo*: R${resultado['preco_agressivo']:.2f} — pra atrair cliente rápido, margem mais apertada.\n"
        f"*Valor Agregado*: R${resultado['preco_valor_agregado']:.2f} — se seu diferencial justifica cobrar mais.\n\n"
        f"Qual combina mais com o seu momento agora?"
    )

# ============================================
# PROMPT — só extrai dados e formula a próxima pergunta. NUNCA calcula.
# ============================================
EXTRACTION_PROMPT = """Você é o motor de extração do Orçamentista IA, bot de precificação para autônomos brasileiros.
Você NUNCA calcula preço — isso é feito por um sistema separado, fora do seu controle.
Sua única função é ler a conversa inteira e devolver um objeto JSON.

Devolva SOMENTE um objeto JSON válido, sem nenhum texto fora dele, neste formato:

{
  "unidade": <string ou null — ex: "por kg", "por bolo de 5kg", "por hora">,
  "custo": <número ou null — custo em reais, sem "R$">,
  "horas": <número ou null — tempo de produção EM HORAS DECIMAIS. 30 minutos = 0.5>,
  "margem_pct": <número entre 0 e 1, ou null — 50% = 0.5>,
  "quer_detalhes": <true/false — o usuário pediu pra ver mais opções, os 3 caminhos, "mais detalhes"?>,
  "ready": <true SE "custo" E "horas" já são conhecidos, mesmo que tenham vindo de mensagens anteriores; senão false>,
  "proxima_pergunta": <string ou null — SE ready=false, a ÚNICA próxima pergunta pra conseguir o dado que falta; SE ready=true, null>
}

Regras para "proxima_pergunta":
- Tom "colega de feira": direto, use "você", "a gente", "na real".
- UMA pergunta só, nunca mais que uma.
- Nunca repita uma pergunta cuja resposta já apareceu na conversa.
- Se o produto/serviço ainda não tem unidade definida (kg, hora, peça, metro), pergunte a unidade antes de custo/tempo.
- Proibido: inventar preço de mercado, dar conselho fiscal, usar linguagem de coach.

Responda em JSON puro. Nada de markdown, nada de texto antes ou depois do objeto."""

# ============================================
# COMANDOS
# ============================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_user_data()
    if user_id not in data:
        data[user_id] = {"first_seen": datetime.now().isoformat(), "last_seen": None, "messages": 0}
    data[user_id]["last_seen"] = datetime.now().isoformat()
    data[user_id]["messages"] = data[user_id].get("messages", 0) + 1
    save_user_data(data)
    user_histories[user_id] = []
    user_state.pop(user_id, None)
    await update.message.reply_text("Oi! Sou o Meu Caderninho. Me fala o que você quer precificar.")

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != ADMIN_ID:
        await update.message.reply_text("Você não tem permissão.")
        return
    data = load_user_data()
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
# EXTRAÇÃO — única chamada à Groq por turno, sempre em modo JSON
# ============================================
async def extrair_dados(user_id: str) -> dict:
    messages = [{"role": "system", "content": EXTRACTION_PROMPT}] + user_histories[user_id]
    if len(messages) > 11:
        messages = [messages[0]] + messages[-10:]

    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 400,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()
    # Defensivo: alguns modelos ainda embrulham em ```json mesmo em JSON mode
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    return json.loads(content)

# ============================================
# MENSAGENS
# ============================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_msg = update.message.text
    if not user_msg:
        return

    data = load_user_data()
    if user_id not in data:
        data[user_id] = {"first_seen": datetime.now().isoformat(), "last_seen": None, "messages": 0}
    data[user_id]["last_seen"] = datetime.now().isoformat()
    data[user_id]["messages"] = data[user_id].get("messages", 0) + 1
    save_user_data(data)

    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "content": user_msg})

    try:
        dados = await extrair_dados(user_id)
    except Exception as e:
        logger.error(f"Erro na extração: {e}")
        reply = "Deu ruim na minha conexão. Tenta de novo."
        user_histories[user_id].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)
        return

    # Caso 1: já temos custo + horas (novos ou repetidos) -> calcula de novo, sempre
    if dados.get("ready") and dados.get("custo") is not None and dados.get("horas") is not None:
        resultado = calcular_preco(
            custo=float(dados["custo"]),
            horas=float(dados["horas"]),
            margem_pct=dados.get("margem_pct"),
        )
        user_state[user_id] = {"dados": dados, "resultado": resultado}
        if dados.get("quer_detalhes"):
            reply = formatar_detalhes(resultado)
        else:
            reply = formatar_resposta_preco(dados, resultado)

    # Caso 2: pediu detalhes mas os números saíram da janela de histórico -> reusa o último cálculo salvo
    elif dados.get("quer_detalhes") and user_id in user_state:
        reply = formatar_detalhes(user_state[user_id]["resultado"])

    # Caso 3: ainda faltam dados -> só pergunta, nunca calcula
    else:
        reply = dados.get("proxima_pergunta") or "Me conta mais sobre o que você quer precificar?"

    user_histories[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply, parse_mode="Markdown")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não configurado nas variáveis de ambiente.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot rodando 24/7 no Render...")
    app.run_polling()

if __name__ == "__main__":
    main()
