import os
import httpx
import asyncio
import logging
import secrets
import json
from datetime import datetime, date
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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
ADMIN_SECRET    = os.getenv("API_SECRET", "minha-chave-secreta")
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

# ─── Gerenciamento de API Keys ────────────────────────────
# Estrutura: { "uc_xxx": { "nome": str, "ativo": bool, "limite": int|-1, "usado": int, "criado": str, "ultimo_uso": str, "projetos": [] } }
api_keys: dict = {}

def gerar_key(nome: str, limite: int = -1, projetos: list = []) -> dict:
    key = "uc_" + secrets.token_hex(16)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    api_keys[key] = {
        "nome":       nome,
        "ativo":      True,
        "limite":     limite,   # -1 = ilimitado
        "usado":      0,
        "criado":     agora,
        "ultimo_uso": None,
        "projetos":   projetos,
    }
    return {"key": key, **api_keys[key]}

def verificar_api_key(key: str) -> dict:
    if key not in api_keys:
        raise HTTPException(status_code=401, detail="API Key inválida.")
    k = api_keys[key]
    if not k["ativo"]:
        raise HTTPException(status_code=403, detail="API Key desativada.")
    if k["limite"] != -1 and k["usado"] >= k["limite"]:
        raise HTTPException(status_code=429, detail=f"Limite de {k['limite']} consultas atingido.")
    return k

def consumir_key(key: str):
    if key in api_keys:
        api_keys[key]["usado"] += 1
        api_keys[key]["ultimo_uso"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

# ─── Stats ────────────────────────────────────────────────
stats = {
    "total":       0,
    "sucesso":     0,
    "erro":        0,
    "por_tipo":    defaultdict(int),
    "por_dia":     defaultdict(int),
    "por_hora":    defaultdict(int),
    "por_usuario": defaultdict(int),
    "por_key":     defaultdict(int),
    "historico":   [],
}

def registrar_consulta(tipo: str, query: str, usuario: str, sucesso: bool, erro: str = None, key: str = None):
    agora = datetime.now()
    stats["total"] += 1
    if sucesso: stats["sucesso"] += 1
    else:       stats["erro"] += 1
    stats["por_tipo"][tipo] += 1
    stats["por_dia"][str(agora.date())] += 1
    stats["por_hora"][agora.strftime("%H:00")] += 1
    stats["por_usuario"][usuario] += 1
    if key: stats["por_key"][key[:12] + "..."] += 1
    stats["historico"].insert(0, {
        "id":        stats["total"],
        "tipo":      tipo,
        "label":     LABELS.get(tipo, tipo),
        "query":     query[:30] + "..." if len(query) > 30 else query,
        "usuario":   usuario,
        "sucesso":   sucesso,
        "erro":      erro,
        "key":       (key[:12] + "...") if key else "bot",
        "timestamp": agora.strftime("%d/%m/%Y %H:%M:%S"),
    })
    if len(stats["historico"]) > 100:
        stats["historico"].pop()

def rodape() -> str:
    return "\n\n─────────────────\n🦅 <b>Unicontroller</b>\n👨‍💻 <b>Developer:</b> Jackson Tomelin"

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
            if k.lower() in CAMPOS_IGNORADOS: continue
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
    return str(data)

# ─── FastAPI ──────────────────────────────────────────────
bot_app: Application = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    # Criar key padrão para uso interno/admin
    if not any(v["nome"] == "admin" for v in api_keys.values()):
        gerar_key("admin", limite=-1, projetos=["railway", "github", "internal"])
    if TELEGRAM_TOKEN:
        bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
        bot_app.add_handler(CommandHandler("start",  cmd_start))
        bot_app.add_handler(CommandHandler("help",   cmd_help))
        bot_app.add_handler(CommandHandler("menu",   cmd_menu))
        bot_app.add_handler(CommandHandler("stats",  cmd_stats))
        bot_app.add_handler(CommandHandler("keys",   cmd_keys))
        bot_app.add_handler(CommandHandler("newkey", cmd_newkey))
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
    if bot_app: await bot_app.shutdown()

api = FastAPI(title="Unicontroller API", version="2.0.0", lifespan=lifespan)
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def admin_auth(x_api_key: str = Header(...)):
    if x_api_key != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Acesso negado.")

def key_auth(x_api_key: str = Header(...)):
    k = verificar_api_key(x_api_key)
    return x_api_key

# ─── Rotas Públicas ───────────────────────────────────────
@api.get("/")
async def root():
    return {
        "sistema":   "Unicontroller",
        "developer": "Jackson Tomelin",
        "versao":    "2.0.0",
        "docs":      "/docs",
        "dashboard": "/dashboard",
    }

@api.get("/ping")
async def ping():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

# ─── Rotas de Consulta (com API Key) ──────────────────────
@api.get("/v1/consulta/{tipo}")
async def consulta_v1(tipo: str, query: str, x_api_key: str = Header(...)):
    """
    Consulta autenticada por API Key.
    Header: X-API-Key: uc_sua_chave_aqui
    """
    k = verificar_api_key(x_api_key)
    try:
        result = await consultar_checkdata(tipo, query)
        consumir_key(x_api_key)
        registrar_consulta(tipo, query, k["nome"], True, key=x_api_key)
        return {
            "ok":      True,
            "tipo":    tipo,
            "query":   query,
            "data":    result,
            "projeto": k["projetos"],
        }
    except HTTPException:
        raise
    except Exception as e:
        registrar_consulta(tipo, query, k["nome"], False, str(e), key=x_api_key)
        raise HTTPException(status_code=500, detail=str(e))

@api.get("/v1/consulta/{tipo}/raw")
async def consulta_v1_raw(tipo: str, query: str, x_api_key: str = Header(...)):
    """Retorna apenas o JSON bruto da consulta."""
    k = verificar_api_key(x_api_key)
    try:
        result = await consultar_checkdata(tipo, query)
        consumir_key(x_api_key)
        registrar_consulta(tipo, query, k["nome"], True, key=x_api_key)
        return result
    except Exception as e:
        registrar_consulta(tipo, query, k["nome"], False, str(e), key=x_api_key)
        raise HTTPException(status_code=500, detail=str(e))

# ─── Gerenciamento de Keys (admin) ────────────────────────
@api.get("/admin/keys", dependencies=[Depends(admin_auth)])
async def listar_keys():
    return {"keys": [{"key": k, **v} for k, v in api_keys.items()]}

@api.post("/admin/keys", dependencies=[Depends(admin_auth)])
async def criar_key(nome: str, limite: int = -1, projetos: str = ""):
    """
    Cria uma nova API Key.
    - nome: nome do projeto/cliente
    - limite: máximo de consultas (-1 = ilimitado)
    - projetos: lista separada por vírgula (ex: railway,github,meusite)
    """
    proj = [p.strip() for p in projetos.split(",") if p.strip()] if projetos else []
    result = gerar_key(nome, limite, proj)
    return {"ok": True, "key": result["key"], "nome": nome, "limite": limite, "projetos": proj}

@api.delete("/admin/keys/{key}", dependencies=[Depends(admin_auth)])
async def revogar_key(key: str):
    if key not in api_keys:
        raise HTTPException(status_code=404, detail="Key não encontrada.")
    api_keys[key]["ativo"] = False
    return {"ok": True, "mensagem": f"Key {key[:12]}... desativada."}

@api.patch("/admin/keys/{key}/reativar", dependencies=[Depends(admin_auth)])
async def reativar_key(key: str):
    if key not in api_keys:
        raise HTTPException(status_code=404, detail="Key não encontrada.")
    api_keys[key]["ativo"] = True
    return {"ok": True, "mensagem": f"Key {key[:12]}... reativada."}

@api.get("/admin/keys/{key}/info", dependencies=[Depends(admin_auth)])
async def info_key(key: str):
    if key not in api_keys:
        raise HTTPException(status_code=404, detail="Key não encontrada.")
    return {"key": key, **api_keys[key]}

# ─── Stats ────────────────────────────────────────────────
@api.get("/api/stats")
async def get_stats():
    taxa = round((stats["sucesso"] / stats["total"] * 100), 1) if stats["total"] > 0 else 0
    hoje = str(date.today())
    return {
        "total":        stats["total"],
        "sucesso":      stats["sucesso"],
        "erro":         stats["erro"],
        "taxa_sucesso": taxa,
        "hoje":         stats["por_dia"].get(hoje, 0),
        "por_tipo":     dict(sorted(stats["por_tipo"].items(), key=lambda x: x[1], reverse=True)),
        "por_dia":      dict(sorted(stats["por_dia"].items())[-7:]),
        "por_hora":     dict(sorted(stats["por_hora"].items())),
        "por_key":      dict(stats["por_key"]),
        "total_keys":   len(api_keys),
        "keys_ativas":  sum(1 for v in api_keys.values() if v["ativo"]),
        "historico":    stats["historico"][:50],
    }

@api.get("/api/keys/stats")
async def keys_stats():
    return {
        "keys": [
            {
                "key":       k[:12] + "...",
                "nome":      v["nome"],
                "ativo":     v["ativo"],
                "usado":     v["usado"],
                "limite":    v["limite"],
                "restante":  (v["limite"] - v["usado"]) if v["limite"] != -1 else "∞",
                "projetos":  v["projetos"],
                "criado":    v["criado"],
                "ultimo_uso":v["ultimo_uso"],
            }
            for k, v in api_keys.items()
        ]
    }

@api.post("/webhook")
async def telegram_webhook(update: dict):
    if bot_app:
        await bot_app.process_update(Update.de_json(update, bot_app.bot))
    return {"ok": True}

@api.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

# ─── Handlers Telegram ────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🦅 <b>Unicontroller</b>\nSistema de Consultas\n\n"
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
        "📊 <code>/stats</code> — Estatísticas\n"
        "🔑 <code>/keys</code> — Listar API Keys\n"
        "➕ <code>/newkey nome limite</code> — Criar Key\n\n"
        "Ou use /menu para botões interativos." + rodape()
    )

async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not api_keys:
        await update.message.reply_html("🔑 Nenhuma API Key criada ainda." + rodape())
        return
    txt = "🔑 <b>API Keys ativas:</b>\n\n"
    for k, v in api_keys.items():
        status = "✅" if v["ativo"] else "❌"
        limite = f"{v['usado']}/{v['limite']}" if v["limite"] != -1 else f"{v['usado']}/∞"
        txt += (
            f"{status} <b>{v['nome']}</b>\n"
            f"   <code>{k[:20]}...</code>\n"
            f"   Uso: {limite} | Projetos: {', '.join(v['projetos']) or 'nenhum'}\n\n"
        )
    await update.message.reply_html(txt + rodape())

async def cmd_newkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_html(
            "➕ <b>Criar nova API Key:</b>\n\n"
            "Use: <code>/newkey nome limite projetos</code>\n\n"
            "Exemplos:\n"
            "<code>/newkey meu-site</code> — ilimitada\n"
            "<code>/newkey railway-app 1000</code> — 1000 consultas\n"
            "<code>/newkey github-actions 500 github,ci</code>" + rodape()
        )
        return
    nome    = args[0]
    limite  = int(args[1]) if len(args) > 1 and args[1].isdigit() else -1
    projetos= [p.strip() for p in args[2].split(",")] if len(args) > 2 else []
    result  = gerar_key(nome, limite, projetos)
    lim_txt = str(limite) if limite != -1 else "ilimitado"
    await update.message.reply_html(
        f"✅ <b>Nova API Key criada!</b>\n\n"
        f"🏷️ Nome: <b>{nome}</b>\n"
        f"🔑 Key: <code>{result['key']}</code>\n"
        f"📊 Limite: {lim_txt}\n"
        f"📁 Projetos: {', '.join(projetos) or 'nenhum'}\n\n"
        f"<b>Como usar:</b>\n"
        f"<code>GET /v1/consulta/cpf?query=000.000.000-00</code>\n"
        f"Header: <code>X-API-Key: {result['key']}</code>" + rodape()
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    taxa   = round((stats["sucesso"] / stats["total"] * 100), 1) if stats["total"] > 0 else 0
    hoje   = stats["por_dia"].get(str(date.today()), 0)
    top    = sorted(stats["por_tipo"].items(), key=lambda x: x[1], reverse=True)[:5]
    top_txt= "\n".join([f"  {LABELS.get(t,'?')} — <b>{n}</b>" for t, n in top]) or "  Nenhuma ainda"
    keys_a = sum(1 for v in api_keys.values() if v["ativo"])
    await update.message.reply_html(
        "📊 <b>Estatísticas — Unicontroller</b>\n\n"
        f"🔢 Total: <b>{stats['total']}</b>\n"
        f"✅ Sucesso: <b>{stats['sucesso']}</b>\n"
        f"❌ Erros: <b>{stats['erro']}</b>\n"
        f"📈 Taxa de sucesso: <b>{taxa}%</b>\n"
        f"📅 Hoje: <b>{hoje}</b>\n"
        f"🔑 API Keys ativas: <b>{keys_a}</b>\n\n"
        f"🏆 <b>Top consultas:</b>\n{top_txt}" + rodape()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = "\n".join([f"<code>/{k} &lt;valor&gt;</code> — {v}" for k, v in LABELS.items()])
    await update.message.reply_html(
        "🦅 <b>Unicontroller</b> — Ajuda\n\n"
        f"📋 <b>Consultas:</b>\n{cmds}\n\n"
        "📊 <code>/stats</code> — Estatísticas\n"
        "🔑 <code>/keys</code> — Listar keys\n"
        "➕ <code>/newkey nome [limite]</code> — Criar key\n"
        "Ou use /menu para botões interativos." + rodape()
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipos    = list(LABELS.items())
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
    usuario = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)
    if not context.args:
        await update.message.reply_html(
            f"ℹ️ Use: <code>/{tipo} &lt;valor&gt;</code>\n"
            f"Exemplo: <code>/{tipo} {_exemplos(tipo)}</code>" + rodape()
        )
        return
    query = " ".join(context.args)
    msg   = await update.message.reply_text("⏳ Consultando...")
    try:
        resultado = await consultar_checkdata(tipo, query)
        registrar_consulta(tipo, query, usuario, True)
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
        registrar_consulta(tipo, query, usuario, False, str(e))
        await msg.edit_text(f"❌ Erro: {str(e)}" + rodape(), parse_mode="HTML")

async def cb_tipo_selecionado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tipo  = query.data.split(":")[1]
    context.user_data["tipo"] = tipo
    await query.message.reply_html(
        f"✏️ Você escolheu <b>{LABELS[tipo]}</b>\n\n"
        f"Digite o valor para consultar:\n"
        f"<i>Exemplo: {_exemplos(tipo)}</i>" + rodape()
    )
    return AGUARDANDO_VALOR

async def cb_receber_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo    = context.user_data.get("tipo")
    if not tipo: return ConversationHandler.END
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
        "cpf":"123.456.789-00","cns":"123456789012345","cep":"01310-100",
        "cnpj":"00.000.000/0001-00","nome":"João Silva","email":"joao@email.com",
        "telefone":"11999999999","vizinhos":"01310-100","placa":"ABC1234",
        "proprietario":"123.456.789-00","mae":"Maria Silva","pai":"José Silva","titulo":"000000000000",
    }
    return ex.get(tipo, "valor")

# ─── Dashboard HTML ───────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unicontroller — Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#050d1a;--surface:#0a1628;--border:#0f2a4a;--accent:#00d4ff;--green:#00e676;--red:#ff4757;--yellow:#ffd32a;--purple:#bf5af2;--text:#e8f4ff;--muted:#4a7a9b}
body{background:var(--bg);color:var(--text);font-family:'Space Grotesk',sans-serif;min-height:100vh}
header{padding:18px 32px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:rgba(10,22,40,0.97);position:sticky;top:0;z-index:100;backdrop-filter:blur(10px)}
.logo{display:flex;align-items:center;gap:12px}
.logo-text{font-size:18px;font-weight:700;letter-spacing:1px}.logo-text span{color:var(--accent)}
.logo-sub{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace}
.tabs{display:flex;gap:4px;background:rgba(0,0,0,0.3);padding:4px;border-radius:8px}
.tab{padding:7px 18px;border-radius:6px;cursor:pointer;font-size:12px;letter-spacing:1px;border:none;background:transparent;color:var(--muted);font-family:'Space Grotesk',sans-serif;transition:all .2s}
.tab.active{background:var(--accent);color:#000;font-weight:600}
.badge{padding:5px 12px;border-radius:20px;font-size:11px;font-family:'JetBrains Mono',monospace;background:rgba(0,230,118,0.1);border:1px solid rgba(0,230,118,0.3);color:var(--green)}
main{padding:24px 32px;max-width:1400px;margin:0 auto}
.page{display:none}.page.active{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;position:relative;overflow:hidden;transition:border-color .2s}
.card:hover{border-color:var(--accent)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.card.c1::before{background:var(--accent)}.card.c2::before{background:var(--green)}
.card.c3::before{background:var(--red)}.card.c4::before{background:var(--yellow)}
.card.c5::before{background:var(--purple)}.card.c6::before{background:#ff9f43}
.card-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;font-family:'JetBrains Mono',monospace}
.card-value{font-size:34px;font-weight:700;font-family:'JetBrains Mono',monospace}
.card.c1 .card-value{color:var(--accent)}.card.c2 .card-value{color:var(--green)}
.card.c3 .card-value{color:var(--red)}.card.c4 .card-value{color:var(--yellow)}
.card.c5 .card-value{color:var(--purple)}.card.c6 .card-value{color:#ff9f43}
.card-icon{position:absolute;right:16px;top:50%;transform:translateY(-50%);font-size:32px;opacity:.12}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
.grid3{display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-bottom:18px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px}
.panel-title{font-size:11px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);margin-bottom:16px;font-family:'JetBrains Mono',monospace;display:flex;align-items:center;gap:8px}
.panel-title::before{content:'';width:3px;height:12px;background:var(--accent);border-radius:2px}
canvas{max-height:200px}
.bar-item{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.bar-label{font-size:12px;min-width:120px;color:var(--text)}
.bar-track{flex:1;background:rgba(255,255,255,0.05);border-radius:4px;height:7px;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),#0099bb);border-radius:4px;transition:width .6s ease}
.bar-count{font-size:12px;color:var(--accent);min-width:28px;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:9px 12px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid var(--border)}
td{padding:9px 12px;border-bottom:1px solid rgba(15,42,74,0.5);font-family:'JetBrains Mono',monospace}
tr:hover td{background:rgba(0,212,255,0.03)}
.tag{padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;letter-spacing:1px}
.tag.ok{background:rgba(0,230,118,0.1);color:var(--green);border:1px solid rgba(0,230,118,0.2)}
.tag.fail{background:rgba(255,71,87,0.1);color:var(--red);border:1px solid rgba(255,71,87,0.2)}
.tag.ativo{background:rgba(0,212,255,0.1);color:var(--accent);border:1px solid rgba(0,212,255,0.2)}
.tag.inativo{background:rgba(255,71,87,0.07);color:var(--red);border:1px solid rgba(255,71,87,0.15)}
.btn{padding:7px 16px;border-radius:6px;cursor:pointer;font-size:12px;font-family:'JetBrains Mono',monospace;letter-spacing:1px;transition:all .2s;border:none}
.btn-accent{background:rgba(0,212,255,0.1);border:1px solid rgba(0,212,255,0.3);color:var(--accent)}
.btn-accent:hover{background:rgba(0,212,255,0.2)}
.btn-green{background:rgba(0,230,118,0.1);border:1px solid rgba(0,230,118,0.3);color:var(--green)}
.btn-green:hover{background:rgba(0,230,118,0.2)}
.btn-red{background:rgba(255,71,87,0.1);border:1px solid rgba(255,71,87,0.3);color:var(--red)}
.btn-red:hover{background:rgba(255,71,87,0.2)}
.form-row{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap}
input,select{background:#060e1a;border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:12px;outline:none;transition:border-color .2s}
input:focus,select:focus{border-color:var(--accent)}
.key-box{background:#030810;border:1px solid var(--border);border-radius:8px;padding:14px 16px;font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--accent);word-break:break-all;cursor:pointer;transition:border-color .2s}
.key-box:hover{border-color:var(--accent)}
.copied{color:var(--green) !important;border-color:var(--green) !important}
.progress-bar{background:rgba(255,255,255,0.05);border-radius:4px;height:5px;overflow:hidden;margin-top:4px}
.progress-fill{height:100%;border-radius:4px;transition:width .4s ease}
.empty{text-align:center;padding:40px;color:var(--muted);font-size:13px}
@media(max-width:900px){.grid2,.grid3{grid-template-columns:1fr}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.live{animation:pulse 2s infinite}
.separator{border:none;border-top:1px solid var(--border);margin:20px 0}
</style>
</head>
<body>
<header>
  <div class="logo">
    <div style="font-size:26px">🦅</div>
    <div><div class="logo-text">Uni<span>controller</span></div><div class="logo-sub">Developer: Jackson Tomelin</div></div>
  </div>
  <div class="tabs">
    <button class="tab active" onclick="switchTab('stats')">📊 Stats</button>
    <button class="tab" onclick="switchTab('keys')">🔑 API Keys</button>
    <button class="tab" onclick="switchTab('docs')">📄 Docs</button>
  </div>
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace" id="lastUpdate"></span>
    <div class="badge live">● LIVE</div>
    <button class="btn btn-accent" onclick="loadAll()">↻</button>
  </div>
</header>

<main>
<!-- ── STATS ── -->
<div class="page active" id="page-stats">
  <div class="cards">
    <div class="card c1"><div class="card-label">Total</div><div class="card-value" id="cTotal">—</div><div class="card-icon">🔢</div></div>
    <div class="card c2"><div class="card-label">Sucesso</div><div class="card-value" id="cSucesso">—</div><div class="card-icon">✅</div></div>
    <div class="card c3"><div class="card-label">Erros</div><div class="card-value" id="cErro">—</div><div class="card-icon">❌</div></div>
    <div class="card c4"><div class="card-label">Hoje</div><div class="card-value" id="cHoje">—</div><div class="card-icon">📅</div></div>
    <div class="card c5"><div class="card-label">Taxa Sucesso</div><div class="card-value" id="cTaxa">—</div><div class="card-icon">📈</div></div>
    <div class="card c6"><div class="card-label">Keys Ativas</div><div class="card-value" id="cKeys">—</div><div class="card-icon">🔑</div></div>
  </div>
  <div class="grid2">
    <div class="panel"><div class="panel-title">Consultas por Dia</div><canvas id="chartDia"></canvas></div>
    <div class="panel"><div class="panel-title">Consultas por Hora</div><canvas id="chartHora"></canvas></div>
  </div>
  <div class="grid3">
    <div class="panel">
      <div class="panel-title">Histórico Recente</div>
      <div id="historicoContainer"><div class="empty">Nenhuma consulta ainda</div></div>
    </div>
    <div class="panel">
      <div class="panel-title">Ranking por Tipo</div>
      <div id="rankingTipo"><div class="empty">—</div></div>
    </div>
  </div>
</div>

<!-- ── API KEYS ── -->
<div class="page" id="page-keys">
  <div class="panel" style="margin-bottom:18px">
    <div class="panel-title">Nova API Key</div>
    <div class="form-row">
      <input id="kNome" placeholder="Nome do projeto (ex: meu-site)" style="flex:2;min-width:180px">
      <input id="kLimite" placeholder="Limite (-1 = ilimitado)" type="number" value="-1" style="width:180px">
      <input id="kProjetos" placeholder="Tags: railway,github,site" style="flex:2;min-width:180px">
      <button class="btn btn-green" onclick="criarKey()">➕ CRIAR KEY</button>
    </div>
    <div id="novaKeyBox" style="display:none">
      <div style="font-size:11px;color:var(--green);margin-bottom:6px;letter-spacing:1px">✅ KEY CRIADA — Clique para copiar:</div>
      <div class="key-box" id="novaKeyValor" onclick="copiarKey(this)"></div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-title">Keys Cadastradas</div>
    <div id="keysContainer"><div class="empty">Carregando...</div></div>
  </div>
</div>

<!-- ── DOCS ── -->
<div class="page" id="page-docs">
  <div class="panel" style="margin-bottom:18px">
    <div class="panel-title">Como usar a API externamente</div>
    <div style="font-size:13px;line-height:2;color:#b0c8e0">
      <p>Qualquer projeto externo (Railway, GitHub Actions, site, app) pode usar a Unicontroller API com uma API Key.</p>
      <hr class="separator">
      <p style="font-family:'JetBrains Mono',monospace;color:var(--accent);margin-bottom:8px">1. AUTENTICAÇÃO — Header obrigatório:</p>
      <div class="key-box" style="margin-bottom:14px">X-API-Key: uc_sua_chave_aqui</div>
      <p style="font-family:'JetBrains Mono',monospace;color:var(--accent);margin-bottom:8px">2. ENDPOINT BASE:</p>
      <div class="key-box" style="margin-bottom:14px" id="baseUrl">https://api-consultas-unicontroller-production.up.railway.app</div>
      <p style="font-family:'JetBrains Mono',monospace;color:var(--accent);margin-bottom:8px">3. ROTAS DISPONÍVEIS:</p>
    </div>
    <table style="margin-bottom:18px">
      <thead><tr><th>Método</th><th>Rota</th><th>Descrição</th></tr></thead>
      <tbody>
        <tr><td><span class="tag ok">GET</span></td><td>/v1/consulta/{tipo}?query=valor</td><td>Retorna dados + metadados</td></tr>
        <tr><td><span class="tag ok">GET</span></td><td>/v1/consulta/{tipo}/raw?query=valor</td><td>Retorna só o JSON bruto</td></tr>
        <tr><td><span class="tag ok">GET</span></td><td>/ping</td><td>Health check</td></tr>
        <tr><td><span class="tag ok">GET</span></td><td>/api/stats</td><td>Estatísticas gerais</td></tr>
      </tbody>
    </table>
    <p style="font-family:'JetBrains Mono',monospace;color:var(--accent);margin-bottom:8px">4. TIPOS SUPORTADOS:</p>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px" id="tiposDoc"></div>
    <p style="font-family:'JetBrains Mono',monospace;color:var(--accent);margin-bottom:8px">5. EXEMPLOS:</p>
    <div id="exemplosDoc"></div>
  </div>
</div>
</main>

<script>
const TIPOS = {cpf:'👤 CPF',cns:'🏥 CNS',cep:'📍 CEP',cnpj:'🏢 CNPJ',nome:'🔤 Nome',email:'✉️ E-mail',telefone:'📞 Telefone',vizinhos:'🏘️ Vizinhos',placa:'🚗 Veículo',proprietario:'🔑 Proprietário',mae:'👩 Mãe',pai:'👨 Pai',titulo:'🗳️ Título'};
const BASE = window.location.origin;
let chartDia=null, chartHora=null;

function switchTab(t){
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('page-'+t).classList.add('active');
  if(t==='keys') loadKeys();
  if(t==='docs') buildDocs();
}

function buildDocs(){
  document.getElementById('baseUrl').textContent = BASE;
  const tipos = document.getElementById('tiposDoc');
  tipos.innerHTML = Object.keys(TIPOS).map(t=>`<span class="tag ativo" style="font-size:11px">${t}</span>`).join('');
  const exemplos = [
    {lang:'cURL',code:`curl -H "X-API-Key: uc_sua_chave" "${BASE}/v1/consulta/cpf?query=123.456.789-00"`},
    {lang:'JavaScript (fetch)',code:`const res = await fetch('${BASE}/v1/consulta/cep?query=01310-100', {\\n  headers: { 'X-API-Key': 'uc_sua_chave' }\\n});\\nconst data = await res.json();`},
    {lang:'Python',code:`import requests\\nresp = requests.get(\\n  '${BASE}/v1/consulta/cnpj',\\n  params={'query': '00.000.000/0001-00'},\\n  headers={'X-API-Key': 'uc_sua_chave'}\\n)\\nprint(resp.json())`},
    {lang:'Node.js',code:`const axios = require('axios');\\nconst { data } = await axios.get('${BASE}/v1/consulta/placa', {\\n  params: { query: 'ABC1234' },\\n  headers: { 'X-API-Key': 'uc_sua_chave' }\\n});`},
    {lang:'GitHub Actions',code:`- name: Consulta CPF\\n  run: |\\n    curl -H "X-API-Key: \${{ secrets.UNICONTROLLER_KEY }}" \\\\\\n    "${BASE}/v1/consulta/cpf?query=\${{ inputs.cpf }}"`},
  ];
  document.getElementById('exemplosDoc').innerHTML = exemplos.map(e=>`
    <div style="margin-bottom:14px">
      <div style="font-size:11px;color:var(--muted);margin-bottom:5px;letter-spacing:1px">${e.lang}</div>
      <div class="key-box" style="font-size:11px;white-space:pre;overflow-x:auto;cursor:default;color:#b0c8e0">${e.code}</div>
    </div>`).join('');
}

const chartCfg = (labels, data, color) => ({
  type:'line',
  data:{labels,datasets:[{data,borderColor:color,backgroundColor:color+'18',fill:true,tension:.4,pointRadius:3,pointBackgroundColor:color,borderWidth:2}]},
  options:{responsive:true,plugins:{legend:{display:false}},scales:{
    x:{grid:{color:'#0f2a4a'},ticks:{color:'#4a7a9b',font:{size:9,family:'JetBrains Mono'}}},
    y:{grid:{color:'#0f2a4a'},ticks:{color:'#4a7a9b',font:{size:9,family:'JetBrains Mono'},stepSize:1}}
  }}
});

async function loadStats(){
  try{
    const d = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('cTotal').textContent = d.total;
    document.getElementById('cSucesso').textContent = d.sucesso;
    document.getElementById('cErro').textContent = d.erro;
    document.getElementById('cHoje').textContent = d.hoje;
    document.getElementById('cTaxa').textContent = d.taxa_sucesso+'%';
    document.getElementById('cKeys').textContent = d.keys_ativas;
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('pt-BR');
    if(chartDia) chartDia.destroy();
    if(chartHora) chartHora.destroy();
    chartDia  = new Chart(document.getElementById('chartDia'),  chartCfg(Object.keys(d.por_dia),  Object.values(d.por_dia),  '#00d4ff'));
    chartHora = new Chart(document.getElementById('chartHora'), chartCfg(Object.keys(d.por_hora), Object.values(d.por_hora), '#00e676'));
    const tipos = Object.entries(d.por_tipo), max = tipos.length?tipos[0][1]:1;
    document.getElementById('rankingTipo').innerHTML = tipos.length
      ? tipos.map(([t,n])=>`<div class="bar-item"><div class="bar-label">${TIPOS[t]||t}</div><div class="bar-track"><div class="bar-fill" style="width:${Math.round(n/max*100)}%"></div></div><div class="bar-count">${n}</div></div>`).join('')
      : '<div class="empty">—</div>';
    document.getElementById('historicoContainer').innerHTML = d.historico.length
      ? `<table><thead><tr><th>#</th><th>Tipo</th><th>Query</th><th>Origem</th><th>Status</th><th>Horário</th></tr></thead><tbody>${d.historico.map(h=>`<tr><td style="color:var(--muted)">${h.id}</td><td>${h.label}</td><td style="color:var(--accent)">${h.query}</td><td>${h.key}</td><td><span class="tag ${h.sucesso?'ok':'fail'}">${h.sucesso?'OK':'ERRO'}</span></td><td style="color:var(--muted)">${h.timestamp}</td></tr>`).join('')}</tbody></table>`
      : '<div class="empty">Nenhuma consulta ainda</div>';
  }catch(e){console.error(e)}
}

async function loadKeys(){
  try{
    const d = await fetch('/api/keys/stats').then(r=>r.json());
    document.getElementById('keysContainer').innerHTML = d.keys.length ? `
      <table>
        <thead><tr><th>Nome</th><th>Key (parcial)</th><th>Uso</th><th>Limite</th><th>Projetos</th><th>Último uso</th><th>Status</th><th>Ações</th></tr></thead>
        <tbody>${d.keys.map(k=>{
          const pct = k.limite===-1?0:Math.round(k.usado/k.limite*100);
          const barColor = pct>80?'var(--red)':pct>50?'var(--yellow)':'var(--green)';
          return `<tr>
            <td style="color:var(--text);font-weight:600">${k.nome}</td>
            <td><span class="tag ativo" style="cursor:pointer" onclick="alert('Gerencie suas keys pelo bot com /keys')">${k.key}</span></td>
            <td>${k.usado}<div class="progress-bar"><div class="progress-fill" style="width:${pct}%;background:${barColor}"></div></div></td>
            <td>${k.limite===-1?'∞':k.limite}</td>
            <td>${k.projetos.length?k.projetos.map(p=>`<span class="tag ativo" style="margin-right:3px">${p}</span>`).join(''):'—'}</td>
            <td style="color:var(--muted)">${k.ultimo_uso||'nunca'}</td>
            <td><span class="tag ${k.ativo?'ativo':'inativo'}">${k.ativo?'ATIVA':'INATIVA'}</span></td>
            <td><button class="btn btn-accent" style="font-size:10px;padding:4px 10px" onclick="alert('Use /keys e /newkey no bot do Telegram para gerenciar.')">⚙️</button></td>
          </tr>`;}).join('')}</tbody>
      </table>` : '<div class="empty">Nenhuma key criada ainda</div>';
  }catch(e){console.error(e)}
}

async function criarKey(){
  const nome = document.getElementById('kNome').value.trim();
  const limite = parseInt(document.getElementById('kLimite').value)||(-1);
  const projetos = document.getElementById('kProjetos').value.trim();
  if(!nome){alert('Informe um nome para a key!');return;}
  const url = `/admin/keys?nome=${encodeURIComponent(nome)}&limite=${limite}&projetos=${encodeURIComponent(projetos)}`;
  const adminKey = prompt('Informe sua API_SECRET (chave admin):');
  if(!adminKey) return;
  try{
    const d = await fetch(url,{method:'POST',headers:{'X-API-Key':adminKey}}).then(r=>r.json());
    if(d.ok){
      document.getElementById('novaKeyBox').style.display='block';
      document.getElementById('novaKeyValor').textContent = d.key;
      document.getElementById('novaKeyValor').classList.remove('copied');
      loadKeys();
    } else { alert(d.detail||'Erro ao criar key'); }
  }catch(e){alert('Erro: '+e)}
}

function copiarKey(el){
  navigator.clipboard.writeText(el.textContent);
  el.classList.add('copied');
  el.textContent = '✅ Copiado! ' + el.textContent;
  setTimeout(()=>{el.classList.remove('copied');loadKeys();},2000);
}

function loadAll(){ loadStats(); if(document.getElementById('page-keys').classList.contains('active')) loadKeys(); }
loadStats();
setInterval(loadStats, 10000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=False)
