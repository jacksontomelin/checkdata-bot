import os
import httpx
import asyncio
import logging
from datetime import datetime, date
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
WEBHOOK_URL     = os.getenv("WEBHOOK_URL", "")
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

AGUARDANDO_VALOR = 1

# ─── Stats em memória ─────────────────────────────────────
stats = {
    "total":        0,
    "sucesso":      0,
    "erro":         0,
    "por_tipo":     defaultdict(int),
    "por_dia":      defaultdict(int),
    "por_hora":     defaultdict(int),
    "por_usuario":  defaultdict(int),
    "historico":    [],   # últimas 100 consultas
}

def registrar_consulta(tipo: str, query: str, usuario: str, sucesso: bool, erro: str = None):
    agora = datetime.now()
    stats["total"] += 1
    if sucesso:
        stats["sucesso"] += 1
    else:
        stats["erro"] += 1
    stats["por_tipo"][tipo] += 1
    stats["por_dia"][str(agora.date())] += 1
    stats["por_hora"][agora.strftime("%H:00")] += 1
    stats["por_usuario"][usuario] += 1
    entrada = {
        "id":        stats["total"],
        "tipo":      tipo,
        "label":     LABELS.get(tipo, tipo),
        "query":     query[:30] + "..." if len(query) > 30 else query,
        "usuario":   usuario,
        "sucesso":   sucesso,
        "erro":      erro,
        "timestamp": agora.strftime("%d/%m/%Y %H:%M:%S"),
    }
    stats["historico"].insert(0, entrada)
    if len(stats["historico"]) > 100:
        stats["historico"].pop()

def rodape() -> str:
    return (
        "\n\n─────────────────\n"
        "🦅 <b>Unicontroller</b>\n"
        "👨‍💻 <b>Developer:</b> Jackson Tomelin"
    )

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
        for i, item in enumerate(data[:5]):
            partes.append(f"{'  '*profundidade}[{i+1}] {formatar_resultado(item, profundidade+1)}")
        if len(data) > 5:
            partes.append(f"{'  '*profundidade}... (+{len(data)-5} itens)")
        return "\n".join(partes)
    else:
        return str(data)

# ─── FastAPI ──────────────────────────────────────────────
bot_app: Application = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    if TELEGRAM_TOKEN:
        bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", cmd_start))
        bot_app.add_handler(CommandHandler("help", cmd_help))
        bot_app.add_handler(CommandHandler("menu", cmd_menu))
        bot_app.add_handler(CommandHandler("stats", cmd_stats))
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
        else:
            asyncio.create_task(bot_app.run_polling())
    yield
    if bot_app:
        await bot_app.shutdown()

api = FastAPI(title="Unicontroller API", version="1.0.0", lifespan=lifespan)
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def verificar_chave(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Chave de API inválida.")

# ─── Rotas REST ───────────────────────────────────────────
@api.get("/")
async def root():
    return {"sistema": "Unicontroller", "developer": "Jackson Tomelin", "endpoints": list(ENDPOINTS.keys())}

@api.get("/consulta/{tipo}")
async def consulta(tipo: str, query: str, _=Depends(verificar_chave)):
    try:
        result = await consultar_checkdata(tipo, query)
        registrar_consulta(tipo, query, "api", True)
        return result
    except Exception as e:
        registrar_consulta(tipo, query, "api", False, str(e))
        raise

@api.get("/consulta/{tipo}/raw")
async def consulta_raw(tipo: str, query: str):
    try:
        result = await consultar_checkdata(tipo, query)
        registrar_consulta(tipo, query, "api-raw", True)
        return result
    except Exception as e:
        registrar_consulta(tipo, query, "api-raw", False, str(e))
        raise

@api.post("/webhook")
async def telegram_webhook(update: dict):
    if bot_app:
        await bot_app.process_update(Update.de_json(update, bot_app.bot))
    return {"ok": True}

@api.get("/api/stats")
async def get_stats():
    taxa = round((stats["sucesso"] / stats["total"] * 100), 1) if stats["total"] > 0 else 0
    top_tipos = sorted(stats["por_tipo"].items(), key=lambda x: x[1], reverse=True)
    top_usuarios = sorted(stats["por_usuario"].items(), key=lambda x: x[1], reverse=True)[:10]
    hoje = str(date.today())
    return {
        "total":        stats["total"],
        "sucesso":      stats["sucesso"],
        "erro":         stats["erro"],
        "taxa_sucesso": taxa,
        "hoje":         stats["por_dia"].get(hoje, 0),
        "por_tipo":     dict(top_tipos),
        "por_dia":      dict(sorted(stats["por_dia"].items())[-7:]),
        "por_hora":     dict(sorted(stats["por_hora"].items())),
        "top_usuarios": dict(top_usuarios),
        "historico":    stats["historico"][:50],
    }

@api.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

# ─── Handlers Telegram ────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🦅 <b>Unicontroller</b>\n"
        "Sistema de Consultas\n\n"
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
        "📊 <code>/stats</code> — Ver estatísticas\n"
        "Ou use /menu para botões interativos."
        + rodape()
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    taxa = round((stats["sucesso"] / stats["total"] * 100), 1) if stats["total"] > 0 else 0
    hoje = str(date.today())
    hoje_total = stats["por_dia"].get(hoje, 0)
    top = sorted(stats["por_tipo"].items(), key=lambda x: x[1], reverse=True)[:5]
    top_txt = "\n".join([f"  {LABELS.get(t,'?')} — <b>{n}</b>" for t, n in top]) or "  Nenhuma ainda"
    await update.message.reply_html(
        "📊 <b>Estatísticas — Unicontroller</b>\n\n"
        f"🔢 <b>Total de consultas:</b> {stats['total']}\n"
        f"✅ <b>Sucesso:</b> {stats['sucesso']}\n"
        f"❌ <b>Erros:</b> {stats['erro']}\n"
        f"📈 <b>Taxa de sucesso:</b> {taxa}%\n"
        f"📅 <b>Hoje:</b> {hoje_total}\n\n"
        f"🏆 <b>Top consultas:</b>\n{top_txt}"
        + rodape()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = "\n".join([f"<code>/{k} &lt;valor&gt;</code> — {v}" for k, v in LABELS.items()])
    await update.message.reply_html(
        "🦅 <b>Unicontroller</b> — Ajuda\n\n"
        f"📋 <b>Todos os comandos:</b>\n\n{cmds}\n\n"
        "Ou use /menu para botões interativos."
        + rodape()
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipos = list(LABELS.items())
    keyboard = []
    for i in range(0, len(tipos), 2):
        row = [InlineKeyboardButton(tipos[i][1], callback_data=f"tipo:{tipos[i][0]}")]
        if i + 1 < len(tipos):
            row.append(InlineKeyboardButton(tipos[i+1][1], callback_data=f"tipo:{tipos[i+1][0]}"))
        keyboard.append(row)
    await update.message.reply_html(
        "🦅 <b>Unicontroller</b>\n\n🔍 Escolha o tipo de consulta:" + rodape(),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_consulta_direta(update: Update, context: ContextTypes.DEFAULT_TYPE, tipo: str):
    usuario = str(update.effective_user.id) if update.effective_user else "unknown"
    username = update.effective_user.username or update.effective_user.first_name or usuario
    if not context.args:
        await update.message.reply_html(
            f"ℹ️ Use: <code>/{tipo} &lt;valor&gt;</code>\n"
            f"Exemplo: <code>/{tipo} {_exemplos(tipo)}</code>" + rodape()
        )
        return
    query = " ".join(context.args)
    msg = await update.message.reply_text("⏳ Consultando...")
    try:
        resultado = await consultar_checkdata(tipo, query)
        registrar_consulta(tipo, query, username, True)
        texto = (
            "🦅 <b>Unicontroller</b>\n\n"
            f"✅ <b>{LABELS[tipo]}</b>\n"
            f"🔎 <code>{query}</code>\n\n"
            f"{formatar_resultado(resultado)}" + rodape()
        )
        if len(texto) > 4000:
            texto = texto[:3900] + "\n\n<i>... resultado truncado</i>" + rodape()
        await msg.edit_text(texto, parse_mode="HTML")
    except Exception as e:
        registrar_consulta(tipo, query, username, False, str(e))
        await msg.edit_text(f"❌ Erro: {str(e)}" + rodape(), parse_mode="HTML")

async def cb_tipo_selecionado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tipo = query.data.split(":")[1]
    context.user_data["tipo"] = tipo
    await query.message.reply_html(
        f"✏️ Você escolheu <b>{LABELS[tipo]}</b>\n\n"
        f"Digite o valor para consultar:\n"
        f"<i>Exemplo: {_exemplos(tipo)}</i>" + rodape()
    )
    return AGUARDANDO_VALOR

async def cb_receber_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo = context.user_data.get("tipo")
    if not tipo:
        return ConversationHandler.END
    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)
    query_val = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Consultando...")
    try:
        resultado = await consultar_checkdata(tipo, query_val)
        registrar_consulta(tipo, query_val, usuario, True)
        texto = (
            "🦅 <b>Unicontroller</b>\n\n"
            f"✅ <b>{LABELS[tipo]}</b>\n"
            f"🔎 <code>{query_val}</code>\n\n"
            f"{formatar_resultado(resultado)}" + rodape()
        )
        if len(texto) > 4000:
            texto = texto[:3900] + "\n\n<i>... resultado truncado</i>" + rodape()
        await msg.edit_text(texto, parse_mode="HTML")
    except Exception as e:
        registrar_consulta(tipo, query_val, usuario, False, str(e))
        await msg.edit_text(f"❌ Erro: {str(e)}" + rodape(), parse_mode="HTML")
    return ConversationHandler.END

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("❌ Consulta cancelada." + rodape())
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

# ─── Dashboard HTML ───────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Unicontroller — Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
  *{margin:0;padding:0;box-sizing:border-box}
  :root{
    --bg:#050d1a;--surface:#0a1628;--border:#0f2a4a;--accent:#00d4ff;
    --green:#00e676;--red:#ff4757;--yellow:#ffd32a;--text:#e8f4ff;--muted:#4a7a9b;
  }
  body{background:var(--bg);color:var(--text);font-family:'Space Grotesk',sans-serif;min-height:100vh}
  header{
    padding:20px 32px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;justify-content:space-between;
    background:rgba(10,22,40,0.95);position:sticky;top:0;z-index:100;backdrop-filter:blur(10px);
  }
  .logo{display:flex;align-items:center;gap:12px}
  .logo-icon{font-size:28px}
  .logo-text{font-size:18px;font-weight:700;letter-spacing:1px}
  .logo-text span{color:var(--accent)}
  .logo-sub{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace}
  .badge{padding:5px 14px;border-radius:20px;font-size:11px;font-family:'JetBrains Mono',monospace;
    background:rgba(0,230,118,0.1);border:1px solid rgba(0,230,118,0.3);color:var(--green);letter-spacing:1px}
  .last-update{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace}
  main{padding:28px 32px;max-width:1400px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:28px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;position:relative;overflow:hidden;transition:border-color .2s}
  .card:hover{border-color:var(--accent)}
  .card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
  .card.total::before{background:var(--accent)}
  .card.sucesso::before{background:var(--green)}
  .card.erro::before{background:var(--red)}
  .card.hoje::before{background:var(--yellow)}
  .card.taxa::before{background:#bf5af2}
  .card-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:2px;margin-bottom:10px;font-family:'JetBrains Mono',monospace}
  .card-value{font-size:38px;font-weight:700;line-height:1;font-family:'JetBrains Mono',monospace}
  .card.total .card-value{color:var(--accent)}
  .card.sucesso .card-value{color:var(--green)}
  .card.erro .card-value{color:var(--red)}
  .card.hoje .card-value{color:var(--yellow)}
  .card.taxa .card-value{color:#bf5af2}
  .card-icon{position:absolute;right:18px;top:50%;transform:translateY(-50%);font-size:36px;opacity:.15}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
  .grid3{display:grid;grid-template-columns:2fr 1fr;gap:20px;margin-bottom:20px}
  .panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px}
  .panel-title{font-size:12px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);margin-bottom:18px;font-family:'JetBrains Mono',monospace;display:flex;align-items:center;gap:8px}
  .panel-title::before{content:'';width:3px;height:14px;background:var(--accent);border-radius:2px}
  canvas{max-height:220px}
  .bar-item{display:flex;align-items:center;gap:10px;margin-bottom:10px}
  .bar-label{font-size:12px;min-width:130px;color:var(--text)}
  .bar-track{flex:1;background:rgba(255,255,255,0.05);border-radius:4px;height:8px;overflow:hidden}
  .bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),#0099bb);border-radius:4px;transition:width .6s ease}
  .bar-count{font-size:12px;color:var(--accent);min-width:32px;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:10px 14px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:500;border-bottom:1px solid var(--border)}
  td{padding:10px 14px;border-bottom:1px solid rgba(15,42,74,0.5);font-family:'JetBrains Mono',monospace;font-size:12px}
  tr:hover td{background:rgba(0,212,255,0.03)}
  .tag{padding:3px 10px;border-radius:20px;font-size:10px;font-weight:600;letter-spacing:1px}
  .tag.ok{background:rgba(0,230,118,0.1);color:var(--green);border:1px solid rgba(0,230,118,0.2)}
  .tag.fail{background:rgba(255,71,87,0.1);color:var(--red);border:1px solid rgba(255,71,87,0.2)}
  .refresh-btn{background:rgba(0,212,255,0.1);border:1px solid rgba(0,212,255,0.3);color:var(--accent);
    padding:7px 16px;border-radius:6px;cursor:pointer;font-size:12px;font-family:'JetBrains Mono',monospace;
    letter-spacing:1px;transition:all .2s}
  .refresh-btn:hover{background:rgba(0,212,255,0.2)}
  .empty{text-align:center;padding:40px;color:var(--muted);font-size:13px}
  @media(max-width:900px){.grid2,.grid3{grid-template-columns:1fr}}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
  .live{animation:pulse 2s infinite}
</style>
</head>
<body>
<header>
  <div class="logo">
    <div class="logo-icon">🦅</div>
    <div>
      <div class="logo-text">Uni<span>controller</span></div>
      <div class="logo-sub">Developer: Jackson Tomelin</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="last-update" id="lastUpdate">Atualizando...</span>
    <div class="badge live">● LIVE</div>
    <button class="refresh-btn" onclick="loadStats()">↻ ATUALIZAR</button>
  </div>
</header>

<main>
  <div class="cards">
    <div class="card total"><div class="card-label">Total de Consultas</div><div class="card-value" id="cTotal">—</div><div class="card-icon">🔢</div></div>
    <div class="card sucesso"><div class="card-label">Sucesso</div><div class="card-value" id="cSucesso">—</div><div class="card-icon">✅</div></div>
    <div class="card erro"><div class="card-label">Erros</div><div class="card-value" id="cErro">—</div><div class="card-icon">❌</div></div>
    <div class="card hoje"><div class="card-label">Hoje</div><div class="card-value" id="cHoje">—</div><div class="card-icon">📅</div></div>
    <div class="card taxa"><div class="card-label">Taxa de Sucesso</div><div class="card-value" id="cTaxa">—</div><div class="card-icon">📈</div></div>
  </div>

  <div class="grid2">
    <div class="panel">
      <div class="panel-title">Consultas por Dia (últimos 7 dias)</div>
      <canvas id="chartDia"></canvas>
    </div>
    <div class="panel">
      <div class="panel-title">Consultas por Hora</div>
      <canvas id="chartHora"></canvas>
    </div>
  </div>

  <div class="grid3">
    <div class="panel">
      <div class="panel-title">Histórico Recente</div>
      <div id="historicoContainer">
        <div class="empty">Nenhuma consulta ainda</div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-title">Ranking por Tipo</div>
      <div id="rankingTipo"></div>
    </div>
  </div>
</main>

<script>
let chartDia = null, chartHora = null;
const cfg = (labels, data, color) => ({
  type:'line',
  data:{labels,datasets:[{data,borderColor:color,backgroundColor:color+'22',fill:true,tension:.4,pointRadius:4,pointBackgroundColor:color,borderWidth:2}]},
  options:{responsive:true,plugins:{legend:{display:false}},scales:{
    x:{grid:{color:'#0f2a4a'},ticks:{color:'#4a7a9b',font:{size:10,family:'JetBrains Mono'}}},
    y:{grid:{color:'#0f2a4a'},ticks:{color:'#4a7a9b',font:{size:10,family:'JetBrains Mono'},stepSize:1}}
  }}
});

async function loadStats(){
  try{
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('cTotal').textContent = d.total;
    document.getElementById('cSucesso').textContent = d.sucesso;
    document.getElementById('cErro').textContent = d.erro;
    document.getElementById('cHoje').textContent = d.hoje;
    document.getElementById('cTaxa').textContent = d.taxa_sucesso + '%';
    document.getElementById('lastUpdate').textContent = 'Atualizado: ' + new Date().toLocaleTimeString('pt-BR');

    // Chart dia
    const diasL = Object.keys(d.por_dia);
    const diasV = Object.values(d.por_dia);
    if(chartDia) chartDia.destroy();
    chartDia = new Chart(document.getElementById('chartDia'), cfg(diasL, diasV, '#00d4ff'));

    // Chart hora
    const horasL = Object.keys(d.por_hora);
    const horasV = Object.values(d.por_hora);
    if(chartHora) chartHora.destroy();
    chartHora = new Chart(document.getElementById('chartHora'), cfg(horasL, horasV, '#00e676'));

    // Ranking tipo
    const tipos = Object.entries(d.por_tipo);
    const max = tipos.length ? tipos[0][1] : 1;
    const labels = {
      cpf:'👤 CPF',cns:'🏥 CNS',cep:'📍 CEP',cnpj:'🏢 CNPJ',nome:'🔤 Nome',
      email:'✉️ E-mail',telefone:'📞 Telefone',vizinhos:'🏘️ Vizinhos',
      placa:'🚗 Veículo',proprietario:'🔑 Proprietário',mae:'👩 Mãe',pai:'👨 Pai',titulo:'🗳️ Título'
    };
    document.getElementById('rankingTipo').innerHTML = tipos.length
      ? tipos.map(([t,n])=>`
        <div class="bar-item">
          <div class="bar-label">${labels[t]||t}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.round(n/max*100)}%"></div></div>
          <div class="bar-count">${n}</div>
        </div>`).join('')
      : '<div class="empty">Nenhuma ainda</div>';

    // Histórico
    const hist = d.historico;
    document.getElementById('historicoContainer').innerHTML = hist.length ? `
      <table>
        <thead><tr><th>#</th><th>Tipo</th><th>Query</th><th>Usuário</th><th>Status</th><th>Horário</th></tr></thead>
        <tbody>${hist.map(h=>`
          <tr>
            <td style="color:var(--muted)">${h.id}</td>
            <td>${h.label}</td>
            <td style="color:var(--accent)">${h.query}</td>
            <td style="color:var(--text)">${h.usuario}</td>
            <td><span class="tag ${h.sucesso?'ok':'fail'}">${h.sucesso?'OK':'ERRO'}</span></td>
            <td style="color:var(--muted)">${h.timestamp}</td>
          </tr>`).join('')}
        </tbody>
      </table>` : '<div class="empty">Nenhuma consulta ainda</div>';
  }catch(e){console.error(e)}
}
loadStats();
setInterval(loadStats, 10000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
