import os
import logging
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from langdetect import detect
from langdetect import DetectorFactory
DetectorFactory.seed = 0

# ============================================
# CONFIGURAÇÃO DO AMBIENTE
# ============================================
TELEGRAM_TOKEN = "8900982200:AAF6gjwDxyNGOK3smhg4Lr2h5XeHqeHCQPI"
GROQ_KEY = os.environ.get("GROQ_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID", "8407367527")  

# Caminho para dados persistentes (Render monta um disco em /data)
DATA_DIR = "/data" if os.path.exists("/data") else "."
USER_DATA_FILE = os.path.join(DATA_DIR, "user_data.json")
LOG_FILE = os.path.join(DATA_DIR, "meu_caderninho.log")

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

# Inicia o servidor em uma thread separada
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
# PROMPT E HISTÓRICO
# ============================================
user_histories = {}

SYSTEM_PROMPT = """Você é o Orçamentista IA, consultor de preços para autônomos brasileiros. Fale como colega de feira: direto, use "você", "a gente", "na real".

REGRAS OBRIGATÓRIAS:
- Se o usuário falar de um produto/serviço, peça a unidade (horas, kg, metros, peças). Só depois dê um chute de preço.
- Faça UMA pergunta por vez. Nunca mais que uma.
- Se o usuário der custo e tempo, CALCULE uma estimativa imediatamente.
- Exemplo: se custa R$30 e leva 30min, com margem de 50% dá ~R$60. Dê esse número.
- Só mostre os três caminhos (Seguro, Agressivo, Valor Agregado) se o usuário pedir "mais detalhes".
- Proibido: inventar preço de mercado, dar conselho fiscal, usar linguagem de coach.
- Se o usuário já deu informações, NÃO repita a pergunta. Use o que ele já falou."""

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
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + user_histories[user_id]
    if len(messages) > 11:
        messages = [messages[0]] + messages[-10:]

    try:
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "max_tokens": 600,
            "temperature": 0.7
        }
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        reply = response.json()["choices"][0]["message"]["content"]
        user_histories[user_id].append({"role": "assistant", "content": reply})
    except Exception as e:
        logger.error(f"Erro: {e}")
        reply = "Deu ruim na minha conexão. Tenta de novo."

    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot rodando 24/7 no Render...")
    app.run_polling()

if __name__ == "__main__":
    main()