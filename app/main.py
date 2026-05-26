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
from app.dashboard import DASH

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
    "placa":        "placacompleta",
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

# ─── Planos de Revenda ────────────────────────────────────
PLANOS = {
    "basico":   {"nome": "Básico",   "limite": 500,   "preco": "R$ 29,90"},
    "pro":      {"nome": "Pro",      "limite": 2000,  "preco": "R$ 79,90"},
    "premium":  {"nome": "Premium",  "limite": 10000, "preco": "R$ 199,90"},
    "ilimitado":{"nome": "Ilimitado","limite": -1,    "preco": "R$ 399,90"},
}

# ─── Gerenciamento de API Keys ────────────────────────────
# Estrutura: { "uc_xxx": { "nome": str, "ativo": bool, "limite": int|-1, "usado": int, "criado": str, "ultimo_uso": str, "projetos": [], "plano": str, "cliente": str } }
api_keys: dict = {}

def gerar_key(nome: str, limite: int = -1, projetos: list = [], plano: str = "custom", cliente: str = "") -> dict:
    key = "uc_" + secrets.token_hex(16)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    api_keys[key] = {
        "nome":       nome,
        "ativo":      True,
        "limite":     limite,
        "usado":      0,
        "criado":     agora,
        "ultimo_uso": None,
        "projetos":   projetos,
        "plano":      plano,
        "cliente":    cliente or nome,
    }
    return {"key": key, **api_keys[key]}

def gerar_key_plano(cliente: str, plano_id: str, projetos: list = []) -> dict:
    plano = PLANOS.get(plano_id)
    if not plano:
        raise HTTPException(status_code=400, detail=f"Plano '{plano_id}' inválido.")
    return gerar_key(cliente, plano["limite"], projetos, plano_id, cliente)

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
            per_message=False,
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
# ─── Exportação JSON para implantação externa ─────────────
@api.get("/admin/export/key/{key}", dependencies=[Depends(admin_auth)])
async def exportar_key(key: str, request: Request):
    """Exporta configuração completa de uma key para implantação."""
    if key not in api_keys:
        raise HTTPException(status_code=404, detail="Key não encontrada.")
    v = api_keys[key]
    base = str(request.base_url).rstrip("/")
    return {
        "unicontroller": {
            "api_key":    key,
            "cliente":    v.get("cliente", v["nome"]),
            "plano":      v.get("plano", "custom"),
            "limite":     v["limite"],
            "usado":      v["usado"],
            "restante":   (v["limite"] - v["usado"]) if v["limite"] != -1 else -1,
            "ativo":      v["ativo"],
        },
        "endpoints": {
            "base_url":   base,
            "consulta":   f"{base}/v1/consulta/{{tipo}}?query={{valor}}",
            "tipos":      list(ENDPOINTS.keys()),
        },
        "integracao": {
            "header":     "X-API-Key",
            "header_value": key,
            "exemplo_curl": f'curl -H "X-API-Key: {key}" "{base}/v1/consulta/cpf?query=000.000.000-00"',
            "exemplo_js": {
                "url":     f"{base}/v1/consulta/{{tipo}}?query={{valor}}",
                "headers": {"X-API-Key": key},
            },
            "exemplo_python": {
                "url":     f"{base}/v1/consulta/{{tipo}}",
                "headers": {"X-API-Key": key},
                "params":  {"query": "{valor}"},
            },
            "exemplo_env": f"UNICONTROLLER_KEY={key}\nUNICONTROLLER_URL={base}/v1/consulta",
        },
        "railway": {
            "env_vars": {
                "UNICONTROLLER_KEY": key,
                "UNICONTROLLER_URL": f"{base}/v1/consulta",
            }
        },
        "github_actions": {
            "secret_name": "UNICONTROLLER_KEY",
            "secret_value": key,
            "uso_no_workflow": f'curl -H "X-API-Key: ${{{{ secrets.UNICONTROLLER_KEY }}}}" "{base}/v1/consulta/cpf?query=${{{{ inputs.cpf }}}}"',
        },
    }

@api.get("/admin/export/all", dependencies=[Depends(admin_auth)])
async def exportar_todas_keys(request: Request):
    """Exporta todas as keys ativas com configuração completa."""
    base = str(request.base_url).rstrip("/")
    resultado = []
    for k, v in api_keys.items():
        if not v["ativo"]:
            continue
        resultado.append({
            "api_key":      k,
            "cliente":      v.get("cliente", v["nome"]),
            "plano":        v.get("plano", "custom"),
            "limite":       v["limite"],
            "usado":        v["usado"],
            "restante":     (v["limite"] - v["usado"]) if v["limite"] != -1 else -1,
            "base_url":     f"{base}/v1/consulta",
            "header":       {"X-API-Key": k},
            "env":          f"UNICONTROLLER_KEY={k}",
            "criado":       v["criado"],
            "ultimo_uso":   v["ultimo_uso"],
        })
    return {"total": len(resultado), "keys": resultado}

@api.get("/v1/me")
async def minha_key(x_api_key: str = Header(...)):
    """Retorna informações da própria key (para sistemas externos verificarem)."""
    k = verificar_api_key(x_api_key)
    key_data = api_keys[x_api_key]
    return {
        "ok":       True,
        "cliente":  key_data.get("cliente", key_data["nome"]),
        "plano":    key_data.get("plano", "custom"),
        "limite":   key_data["limite"],
        "usado":    key_data["usado"],
        "restante": (key_data["limite"] - key_data["usado"]) if key_data["limite"] != -1 else -1,
        "ativo":    key_data["ativo"],
        "tipos_disponiveis": list(ENDPOINTS.keys()),
    }

@api.post("/admin/keys/plano", dependencies=[Depends(admin_auth)])
async def criar_key_por_plano(cliente: str, plano: str, projetos: str = ""):
    """
    Cria key por plano pré-definido.
    - plano: basico | pro | premium | ilimitado
    - cliente: nome do cliente/empresa
    """
    proj = [p.strip() for p in projetos.split(",") if p.strip()] if projetos else []
    result = gerar_key_plano(cliente, plano, proj)
    p = PLANOS[plano]
    return {
        "ok":      True,
        "key":     result["key"],
        "cliente": cliente,
        "plano":   p["nome"],
        "limite":  p["limite"],
        "preco":   p["preco"],
        "projetos":proj,
    }

@api.get("/admin/planos", dependencies=[Depends(admin_auth)])
async def listar_planos():
    return {"planos": PLANOS}

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
                "key":       k[:20] + "...",
                "key_full":  k,
                "nome":      v["nome"],
                "cliente":   v.get("cliente", v["nome"]),
                "plano":     v.get("plano", "custom"),
                "ativo":     v["ativo"],
                "usado":     v["usado"],
                "limite":    v["limite"],
                "restante":  (v["limite"] - v["usado"]) if v["limite"] != -1 else "∞",
                "pct":       round(v["usado"] / v["limite"] * 100, 1) if v["limite"] > 0 else 0,
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
        planos_txt = "\n".join([f"  <code>{k}</code> — {v['nome']} | {v['preco']} | {v['limite'] if v['limite']!=-1 else '∞'} consultas" for k,v in PLANOS.items()])
        await update.message.reply_html(
            "➕ <b>Criar nova API Key:</b>\n\n"
            "<b>Por plano:</b>\n"
            "<code>/newkey cliente plano</code>\n"
            "Ex: <code>/newkey joao-silva pro</code>\n\n"
            "<b>Personalizada:</b>\n"
            "<code>/newkey cliente custom 1500</code>\n"
            "Ex: <code>/newkey empresa-x custom 5000</code>\n\n"
            f"📦 <b>Planos disponíveis:</b>\n{planos_txt}" + rodape()
        )
        return

    cliente = args[0]

    # por plano
    if len(args) >= 2 and args[1] in PLANOS:
        plano_id = args[1]
        projetos = [p.strip() for p in args[2].split(",")] if len(args) > 2 else []
        result   = gerar_key_plano(cliente, plano_id, projetos)
        p        = PLANOS[plano_id]
        await update.message.reply_html(
            f"✅ <b>Key criada — Plano {p['nome']}</b>\n\n"
            f"👤 Cliente: <b>{cliente}</b>\n"
            f"📦 Plano: <b>{p['nome']}</b> ({p['preco']})\n"
            f"📊 Limite: <b>{p['limite'] if p['limite']!=-1 else 'Ilimitado'}</b> consultas\n"
            f"🔑 Key:\n<code>{result['key']}</code>\n\n"
            f"<b>Instruções para o cliente:</b>\n"
            f"URL: <code>https://api-consultas-unicontroller-production.up.railway.app/v1/consulta/{{tipo}}?query={{valor}}</code>\n"
            f"Header: <code>X-API-Key: {result['key']}</code>" + rodape()
        )
        return

    # custom
    limite   = int(args[2]) if len(args) > 2 and args[2].isdigit() else -1
    projetos = [p.strip() for p in args[3].split(",")] if len(args) > 3 else []
    result   = gerar_key(cliente, limite, projetos, "custom", cliente)
    lim_txt  = str(limite) if limite != -1 else "Ilimitado"
    await update.message.reply_html(
        f"✅ <b>Key personalizada criada!</b>\n\n"
        f"👤 Cliente: <b>{cliente}</b>\n"
        f"📊 Limite: <b>{lim_txt}</b> consultas\n"
        f"🔑 Key:\n<code>{result['key']}</code>\n\n"
        f"<b>Instruções para o cliente:</b>\n"
        f"URL: <code>https://api-consultas-unicontroller-production.up.railway.app/v1/consulta/{{tipo}}?query={{valor}}</code>\n"
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
DASHBOARD_HTML = DASH


if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=False)
