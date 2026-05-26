import os
import httpx
import asyncio
import logging
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
import uvicorn
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────
CHECKDATA_TOKEN = os.getenv("CHECKDATA_TOKEN", "unicontroller-api-completa")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_SECRET      = os.getenv("API_SECRET", "minha-chave-secreta")
WEBHOOK_URL     = os.getenv("WEBHOOK_URL", "")  # ex: https://seu-app.railway.app

CHECKDATA_BASE  = "https://checkdata.vip/api/consultas"

ENDPOINTS = {
    "cpf":          "cpf_basico",
    "cns":          "cns",
    "cep":          "cep",
    "cnpj":         "cnpj_online",
    "nome":         "nome_online",
    "email":        "email_abreviado",
    "telefone":     "telefone_endereco",
    "vizinhos":     "vizinhos-online",
    "placa":        "placa_completa",
    "proprietario": "frota_cpf",
    "mae":          "mae",
    "pai":          "pai",
    "titulo":       "titulo",
}

LABELS = {
    "cpf":          "👤 CPF",
    "cns":          "🏥 CNS",
    "cep":          "📍 CEP",
    "cnpj":         "🏢 CNPJ",
    "nome":         "🔤 Nome",
    "email":        "✉️ E-mail",
    "telefone":     "📞 Telefone",
    "vizinhos":     "🏘️ Vizinhos",
    "placa":        "🚗 Veículo",
    "proprietario": "🔑 Proprietário",
    "mae":          "👩 Nome da Mãe",
    "pai":          "👨 Nome do Pai",
    "titulo":       "🗳️ Título Eleitor",
}

AGUARDANDO_VALOR = 1  # estado da conversa

# ─── CheckData helper ─────────────────────────────────────
async def consultar_checkdata(tipo: str, query: str) -> dict:
    endpoint = ENDPOINTS.get(tipo)
    if not endpoint:
        raise HTTPException(status_code=400, detail=f"Tipo '{tipo}' inválido.")
    url = f"{CHECKDATA_BASE}/{endpoint}?query={query}&token={CHECKDATA_TOKEN}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

CAMPOS_IGNORADOS = {"status", "developer", "dev", "api", "version", "via", "source", "powered_by"}

def formatar_resultado(data, profundidade=0) -> str:
    """Converte JSON em texto legível para o Telegram."""
    if isinstance(data, dict):
        linhas = []
        for k, v in data.items():
            if k.lower() in CAMPOS_IGNORADOS:
                continue
            if isinstance(v, (dict, list)):
                linhas.append(f"{'  '*profundidade}<b>{k}:</b>")
                linhas.append(formatar_resultado(v, profundidade + 1))
            else:
                linhas.append(f"{'  '*profundidade}<b>{k}:</b> {v}")
        return "\n".join(linhas)
    elif isinstance(data, list):
        partes = []
        for i, item in enumerate(data[:5]):  # max 5 itens
            partes.append(f"{'  '*profundidade}[{i+1}] {formatar_resultado(item, profundidade+1)}")
        if len(data) > 5:
            partes.append(f"{'  '*profundidade}... (+{len(data)-5} itens)")
        return "\n".join(partes)
    else:
        return str(data)

# ─── FastAPI app ──────────────────────────────────────────
bot_app: Application = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    if TELEGRAM_TOKEN:
        bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", cmd_start))
        bot_app.add_handler(CommandHandler("help", cmd_help))
        bot_app.add_handler(CommandHandler("menu", cmd_menu))
        for tipo in ENDPOINTS:
            bot_app.add_handler(CommandHandler(tipo, lambda u, c, t=tipo: cmd_consulta_direta(u, c, t)))
        bot_app.add_handler(ConversationHandler(
            entry_points=[CallbackQueryHandler(cb_tipo_selecionado, pattern="^tipo:")],
            states={AGUARDANDO_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_receber_valor)]},
            fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
        ))
        await bot_app.initialize()
        if WEBHOOK_URL:
            await bot_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
            logger.info(f"Webhook configurado: {WEBHOOK_URL}/webhook")
        else:
            asyncio.create_task(bot_app.run_polling())
            logger.info("Bot rodando em polling mode")
    yield
    if bot_app:
        await bot_app.shutdown()

api = FastAPI(title="CheckData API", version="1.0.0", lifespan=lifespan)
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Auth ─────────────────────────────────────────────────
def verificar_chave(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Chave de API inválida.")

# ─── Rotas REST ───────────────────────────────────────────
@api.get("/")
async def root():
    return {"status": "ok", "endpoints": list(ENDPOINTS.keys())}

@api.get("/consulta/{tipo}")
async def consulta(tipo: str, query: str, _=Depends(verificar_chave)):
    """
    Consulta qualquer tipo de dado.
    - **tipo**: cpf | cns | cep | cnpj | nome | email | telefone | vizinhos | placa | proprietario | mae | pai | titulo
    - **query**: valor a consultar
    - **Header X-API-Key**: sua chave secreta
    """
    return await consultar_checkdata(tipo, query)

@api.get("/consulta/{tipo}/raw")
async def consulta_raw(tipo: str, query: str):
    """Rota pública sem autenticação (use com cuidado)."""
    return await consultar_checkdata(tipo, query)

@api.post("/webhook")
async def telegram_webhook(update: dict):
    """Recebe updates do Telegram via webhook."""
    if bot_app:
        await bot_app.process_update(Update.de_json(update, bot_app.bot))
    return {"ok": True}

@api.get("/endpoints")
async def listar_endpoints():
    return {"endpoints": [{"tipo": k, "label": v, "endpoint": ENDPOINTS[k]} for k, v in LABELS.items()]}

# ─── Handlers do Telegram ─────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🔍 <b>CheckData Bot</b>\n\n"
        "Consulte dados completos direto pelo Telegram!\n\n"
        "📋 <b>Comandos disponíveis:</b>\n\n"
        "👤 <code>/cpf 000.000.000-00</code>\n"
        "🏢 <code>/cnpj 00.000.000/0001-00</code>\n"
        "📍 <code>/cep 00000-000</code>\n"
        "🏥 <code>/cns 000000000000000</code>\n"
        "🔤 <code>/nome João Silva</code>\n"
        "✉️ <code>/email joao@email.com</code>\n"
        "📞 <code>/telefone 11999999999</code>\n"
        "🏘️ <code>/vizinhos 00000-000</code>\n"
        "🚗 <code>/placa ABC1234</code>\n"
        "🔑 <code>/proprietario 000.000.000-00</code>\n"
        "👩 <code>/mae Nome da Mãe</code>\n"
        "👨 <code>/pai Nome do Pai</code>\n"
        "🗳️ <code>/titulo 000000000000</code>\n\n"
        "Ou use /menu para botões interativos."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = "\n".join([f"<code>/{k} &lt;valor&gt;</code> — {v}" for k, v in LABELS.items()])
    await update.message.reply_html(
        f"📋 <b>Comandos disponíveis:</b>\n\n{cmds}\n\n"
        "Ou use /menu para botões interativos."
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipos = list(LABELS.items())
    keyboard = []
    for i in range(0, len(tipos), 2):
        row = [InlineKeyboardButton(tipos[i][1], callback_data=f"tipo:{tipos[i][0]}")]
        if i + 1 < len(tipos):
            row.append(InlineKeyboardButton(tipos[i+1][1], callback_data=f"tipo:{tipos[i+1][0]}"))
        keyboard.append(row)
    await update.message.reply_text(
        "🔍 Escolha o tipo de consulta:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_consulta_direta(update: Update, context: ContextTypes.DEFAULT_TYPE, tipo: str):
    if not context.args:
        await update.message.reply_html(
            f"ℹ️ Use: <code>/{tipo} &lt;valor&gt;</code>\n"
            f"Exemplo: <code>/{tipo} {_exemplos(tipo)}</code>"
        )
        return
    query = " ".join(context.args)
    msg = await update.message.reply_text("⏳ Consultando...")
    try:
        resultado = await consultar_checkdata(tipo, query)
        texto = f"✅ <b>{LABELS[tipo]}</b>\n<code>{query}</code>\n\n{formatar_resultado(resultado)}"
        if len(texto) > 4000:
            texto = texto[:3900] + "\n\n<i>... resultado truncado</i>"
        await msg.edit_text(texto, parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Erro: {str(e)}")

async def cb_tipo_selecionado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tipo = query.data.split(":")[1]
    context.user_data["tipo"] = tipo
    await query.message.reply_html(
        f"✏️ Você escolheu <b>{LABELS[tipo]}</b>\n\n"
        f"Digite o valor para consultar:\n"
        f"<i>Exemplo: {_exemplos(tipo)}</i>"
    )
    return AGUARDANDO_VALOR

async def cb_receber_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo = context.user_data.get("tipo")
    if not tipo:
        return ConversationHandler.END
    query_val = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Consultando...")
    try:
        resultado = await consultar_checkdata(tipo, query_val)
        texto = f"✅ <b>{LABELS[tipo]}</b>\n<code>{query_val}</code>\n\n{formatar_resultado(resultado)}"
        if len(texto) > 4000:
            texto = texto[:3900] + "\n\n<i>... resultado truncado</i>"
        await msg.edit_text(texto, parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Erro: {str(e)}")
    return ConversationHandler.END

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Consulta cancelada.")
    return ConversationHandler.END

def _exemplos(tipo: str) -> str:
    ex = {
        "cpf": "123.456.789-00", "cns": "123456789012345",
        "cep": "01310-100", "cnpj": "00.000.000/0001-00",
        "nome": "João Silva", "email": "joao@email.com",
        "telefone": "11999999999", "vizinhos": "01310-100",
        "placa": "ABC1234", "proprietario": "123.456.789-00",
        "mae": "Maria Silva", "pai": "José Silva", "titulo": "000000000000",
    }
    return ex.get(tipo, "valor")

if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
