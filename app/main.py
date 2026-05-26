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
    "cpf":           "cpf_basico",
    "cns":           "cns",
    "cep":           "cep",
    "cnpj":          "cnpj_online",
    "nome":          "nome_online",
    "email":         "email_abreviado",
    "telefone":      "telefone_endereco",
    "vizinhos":      "vizinhos-online",
    "placa":         "placacompleta",
    "proprietario":  "frota_cpf",
    "mae":           "mae",
    "pai":           "pai",
    "titulo":        "titulo",
}

# Endpoints externos (outras APIs)
ENDPOINTS_EXTERNOS = {
    "laudo_veicular": {
        "url":    "https://api.fetchbrasil.pro/",
        "token":  "FB-4EF4-BA2D-353A-BEFC",
        "api":    "laudo_veicular",
        "tipo_validacao": "placa",
        "exemplo": "ABC1234",
        "label":  "📋 Laudo Veicular",
    },
}

LABELS = {
    "cpf":           "👤 CPF",
    "cns":           "🏥 CNS",
    "cep":           "📍 CEP",
    "cnpj":          "🏢 CNPJ",
    "nome":          "🔤 Nome",
    "email":         "✉️ E-mail",
    "telefone":      "📞 Telefone",
    "vizinhos":      "🏘️ Vizinhos",
    "placa":         "🚗 Veículo",
    "proprietario":  "🔑 Proprietário",
    "mae":           "👩 Nome da Mãe",
    "pai":           "👨 Nome do Pai",
    "titulo":        "🗳️ Título Eleitor",
    "laudo_veicular":"📋 Laudo Veicular",
}

AGUARDANDO_VALOR = 1

# ─── Planos de Revenda ────────────────────────────────────
PLANOS = {
    "basico":   {"nome": "Básico",   "limite": 500,   "preco": "R$ 29,90"},
    "pro":      {"nome": "Pro",      "limite": 2000,  "preco": "R$ 79,90"},
    "premium":  {"nome": "Premium",  "limite": 10000, "preco": "R$ 199,90"},
    "ilimitado":{"nome": "Ilimitado","limite": -1,    "preco": "R$ 399,90"},
}

# ─── Banco de dados SQLite ────────────────────────────────
import sqlite3, json as _json

DB_PATH = os.getenv("DB_PATH", "/app/data/unicontroller.db")

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key        TEXT PRIMARY KEY,
            nome       TEXT NOT NULL,
            cliente    TEXT NOT NULL,
            plano      TEXT DEFAULT 'custom',
            ativo      INTEGER DEFAULT 1,
            limite     INTEGER DEFAULT -1,
            usado      INTEGER DEFAULT 0,
            projetos   TEXT DEFAULT '[]',
            criado     TEXT NOT NULL,
            ultimo_uso TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"✅ Banco inicializado: {DB_PATH}")

# ─── Gerenciamento de API Keys ────────────────────────────
api_keys: dict = {}  # cache em memória

def _row_to_dict(row) -> dict:
    if row is None: return None
    d = dict(row)
    d["projetos"] = _json.loads(d.get("projetos", "[]"))
    d["ativo"]    = bool(d["ativo"])
    return d

def _load_keys_cache():
    """Carrega todas as keys do banco para o cache em memória."""
    global api_keys
    conn = get_db()
    rows = conn.execute("SELECT * FROM api_keys").fetchall()
    conn.close()
    api_keys = {r["key"]: _row_to_dict(r) for r in rows}
    logger.info(f"✅ {len(api_keys)} API Keys carregadas do banco")

def gerar_key(nome: str, limite: int = -1, projetos: list = [], plano: str = "custom", cliente: str = "") -> dict:
    key   = "uc_" + secrets.token_hex(16)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    cli   = cliente or nome
    proj  = _json.dumps(projetos)
    # Salva no banco
    conn = get_db()
    conn.execute(
        "INSERT INTO api_keys (key,nome,cliente,plano,ativo,limite,usado,projetos,criado) VALUES (?,?,?,?,1,?,0,?,?)",
        (key, nome, cli, plano, limite, proj, agora)
    )
    conn.commit()
    conn.close()
    # Atualiza cache
    api_keys[key] = {
        "nome": nome, "cliente": cli, "plano": plano,
        "ativo": True, "limite": limite, "usado": 0,
        "projetos": projetos, "criado": agora, "ultimo_uso": None,
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
        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        api_keys[key]["ultimo_uso"] = agora
        # Persiste no banco
        conn = get_db()
        conn.execute("UPDATE api_keys SET usado=usado+1, ultimo_uso=? WHERE key=?", (agora, key))
        conn.commit()
        conn.close()

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

async def consultar_externo(tipo: str, query: str) -> dict:
    """Consulta APIs externas (ex: fetchbrasil)."""
    cfg = ENDPOINTS_EXTERNOS.get(tipo)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"Endpoint externo '{tipo}' não encontrado.")
    params = {"token": cfg["token"], "api": cfg["api"], "query": query}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(cfg["url"], params=params)
        r.raise_for_status()
        return r.json()

async def consultar_qualquer(tipo: str, query: str) -> dict:
    """Consulta checkdata ou API externa dependendo do tipo."""
    if tipo in ENDPOINTS:
        return await consultar_checkdata(tipo, query)
    elif tipo in ENDPOINTS_EXTERNOS:
        return await consultar_externo(tipo, query)
    else:
        raise HTTPException(status_code=400, detail=f"Tipo '{tipo}' inválido.")

CAMPOS_IGNORADOS = {"status", "developer", "dev", "api", "version", "via", "source", "powered_by"}

def _limpar_valor(v) -> str:
    """Limpa e formata um valor para exibição."""
    if v is None or v == "None" or v == "":
        return "—"
    s = str(v).strip()
    # Remove links tel: gerados automaticamente
    import re
    s = re.sub(r'\[([^\]]+)\]\(tel:[^)]+\)', r'\1', s)
    return s if s and s != "None" else "—"

def formatar_resultado(data, profundidade=0, max_prof=3) -> str:
    if profundidade > max_prof:
        return ""
    if isinstance(data, dict):
        linhas = []
        for k, v in data.items():
            if k.lower() in CAMPOS_IGNORADOS: continue
            if v is None or v == "" or v == "None": continue
            if isinstance(v, dict):
                # Só mostra seções não vazias
                sub = formatar_resultado(v, profundidade+1, max_prof)
                if sub.strip():
                    pad = "  " * profundidade
                    linhas.append(f"{pad}<b>▸ {k.upper().replace('_',' ')}:</b>")
                    linhas.append(sub)
            elif isinstance(v, list):
                if not v: continue
                sub = formatar_resultado(v, profundidade+1, max_prof)
                if sub.strip():
                    pad = "  " * profundidade
                    linhas.append(f"{pad}<b>▸ {k.upper().replace('_',' ')}:</b>")
                    linhas.append(sub)
            else:
                val = _limpar_valor(v)
                if val == "—": continue
                pad = "  " * profundidade
                chave = k.replace("_", " ").title()
                linhas.append(f"{pad}<b>{chave}:</b> {val}")
        return "\n".join(filter(None, linhas))
    elif isinstance(data, list):
        partes = []
        for i, item in enumerate(data[:5]):
            pad = "  " * profundidade
            if isinstance(item, dict):
                sub = formatar_resultado(item, profundidade, max_prof)
                if sub.strip():
                    partes.append(f"{pad}┌ <b>#{i+1}</b>")
                    partes.append(sub)
                    partes.append(f"{pad}└")
            else:
                val = _limpar_valor(item)
                if val != "—":
                    partes.append(f"{pad}• {val}")
        if len(data) > 5:
            partes.append(f"{'  '*profundidade}<i>... +{len(data)-5} itens</i>")
        return "\n".join(filter(None, partes))
    return _limpar_valor(data)

# ─── FastAPI ──────────────────────────────────────────────
bot_app: Application = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    # Inicializa banco e carrega keys
    init_db()
    _load_keys_cache()
    # Criar key admin se não existir
    if not any(v["nome"] == "admin" for v in api_keys.values()):
        gerar_key("admin", limite=-1, projetos=["railway", "github", "internal"])
        logger.info("✅ Key admin criada")
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
        for tipo in ENDPOINTS_EXTERNOS:
            bot_app.add_handler(CommandHandler(tipo, lambda u, c, t=tipo: cmd_consulta_direta(u, c, t)))
        # Alias curtos
        bot_app.add_handler(CommandHandler("laudo", lambda u, c: cmd_consulta_direta(u, c, "laudo_veicular")))
        bot_app.add_handler(ConversationHandler(
            entry_points=[CallbackQueryHandler(cb_tipo_selecionado, pattern="^tipo:")],
            states={AGUARDANDO_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_receber_valor)]},
            fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
            per_message=False,
        ))
        await bot_app.initialize()
        if WEBHOOK_URL:
            # Limpa webhook antigo antes de registrar novo
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
            await bot_app.bot.set_webhook(
                url=f"{WEBHOOK_URL}/webhook",
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info(f"✅ Webhook registrado: {WEBHOOK_URL}/webhook")
        else:
            logger.info("🔄 Iniciando polling mode")
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

# ─── Rotas individuais estilo checkdata ───────────────────
# GET /consultas/cpf?query=valor&token=uc_xxx

async def _consulta_token(tipo: str, query: str, token: str):
    k = verificar_api_key(token)
    try:
        resultado = await consultar_qualquer(tipo, query)
        consumir_key(token)
        registrar_consulta(tipo, query, k["nome"], True, key=token)
        return resultado
    except HTTPException:
        raise
    except Exception as e:
        registrar_consulta(tipo, query, k["nome"], False, str(e), key=token)
        raise HTTPException(status_code=500, detail=str(e))

@api.get("/consultas/cpf")
async def rota_cpf(query: str, token: str):
    return await _consulta_token("cpf", query, token)

@api.get("/consultas/cns")
async def rota_cns(query: str, token: str):
    return await _consulta_token("cns", query, token)

@api.get("/consultas/cep")
async def rota_cep(query: str, token: str):
    return await _consulta_token("cep", query, token)

@api.get("/consultas/cnpj")
async def rota_cnpj(query: str, token: str):
    return await _consulta_token("cnpj", query, token)

@api.get("/consultas/nome")
async def rota_nome(query: str, token: str):
    return await _consulta_token("nome", query, token)

@api.get("/consultas/email")
async def rota_email(query: str, token: str):
    return await _consulta_token("email", query, token)

@api.get("/consultas/telefone")
async def rota_telefone(query: str, token: str):
    return await _consulta_token("telefone", query, token)

@api.get("/consultas/vizinhos")
async def rota_vizinhos(query: str, token: str):
    return await _consulta_token("vizinhos", query, token)

@api.get("/consultas/placa")
async def rota_placa(query: str, token: str):
    return await _consulta_token("placa", query, token)

@api.get("/consultas/proprietario")
async def rota_proprietario(query: str, token: str):
    return await _consulta_token("proprietario", query, token)

@api.get("/consultas/mae")
async def rota_mae(query: str, token: str):
    return await _consulta_token("mae", query, token)

@api.get("/consultas/pai")
async def rota_pai(query: str, token: str):
    return await _consulta_token("pai", query, token)

@api.get("/consultas/titulo")
async def rota_titulo(query: str, token: str):
    return await _consulta_token("titulo", query, token)

@api.get("/consultas/laudo_veicular")
async def rota_laudo(query: str, token: str):
    """Laudo veicular completo por placa. Ex: query=ABC1234"""
    return await _consulta_token("laudo_veicular", query, token)

# ─── Rota pública estilo fetchbrasil ──────────────────────
# GET /?token=uc_xxx&api=cpf&query=valor
@api.get("/api")
async def consulta_publica(token: str, api: str, query: str):
    """
    Rota no estilo fetchbrasil:
    GET /api?token=uc_xxx&api=cpf&query=valor
    """
    # Valida token como API Key
    k = verificar_api_key(token)
    # Valida tipo
    todos = list(ENDPOINTS.keys()) + list(ENDPOINTS_EXTERNOS.keys())
    if api not in todos:
        return {
            "status": "erro",
            "code": 400,
            "mensagem": f"API '{api}' invalida. Tipos: {', '.join(todos)}",
            "developer": "Jackson Tomelin — Unicontroller",
        }
    try:
        resultado = await consultar_qualquer(api, query)
        consumir_key(token)
        registrar_consulta(api, query, k["nome"], True, key=token)
        return {
            "status": "ok",
            "api": api,
            "query": query,
            "data": resultado,
            "developer": "Jackson Tomelin — Unicontroller",
        }
    except HTTPException as e:
        registrar_consulta(api, query, k["nome"], False, e.detail, key=token)
        return {"status": "erro", "code": e.status_code, "mensagem": e.detail, "developer": "Jackson Tomelin — Unicontroller"}
    except Exception as e:
        registrar_consulta(api, query, k["nome"], False, str(e), key=token)
        return {"status": "erro", "code": 500, "mensagem": str(e), "developer": "Jackson Tomelin — Unicontroller"}

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
    conn = get_db()
    conn.execute("UPDATE api_keys SET ativo=0 WHERE key=?", (key,))
    conn.commit(); conn.close()
    return {"ok": True, "mensagem": f"Key {key[:12]}... desativada."}

@api.patch("/admin/keys/{key}/reativar", dependencies=[Depends(admin_auth)])
async def reativar_key(key: str):
    if key not in api_keys:
        raise HTTPException(status_code=404, detail="Key não encontrada.")
    api_keys[key]["ativo"] = True
    conn = get_db()
    conn.execute("UPDATE api_keys SET ativo=1 WHERE key=?", (key,))
    conn.commit(); conn.close()
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
    import os
    html_path = os.path.join(os.path.dirname(__file__), "dash.html")
    with open(html_path) as f:
        return HTMLResponse(f.read())

# ─── Handlers Telegram ────────────────────────────────────
def formatar_laudo(data: dict) -> str:
    """Formata laudo veicular de forma limpa e legível."""
    lines = []
    def v(val):
        if val is None or str(val).strip() in ("None","","null","False"): return "—"
        import re
        s = re.sub(r'\[([^\]]+)\]\(tel:[^)]+\)', r'\1', str(val).strip())
        return s or "—"

    # Resumo geral
    if "placa" in data: lines.append(f"🚗 <b>Placa:</b> {v(data.get('placa'))}")
    if "total_registros" in data: lines.append(f"📋 <b>Total laudos:</b> {v(data.get('total_registros'))}")

    laudo = data.get("laudo_recente", {})
    detalhes = laudo.get("detalhes", {}) if laudo else {}
    veiculo  = detalhes.get("veiculo", {}) if detalhes else {}
    geral    = detalhes.get("geral", {}) if detalhes else {}
    prop     = detalhes.get("proprietario", {}) if detalhes else {}
    ecv      = detalhes.get("ecv", {}) if detalhes else {}
    vistoria = data.get("laudo_recente", {}).get("dados_laudo", {}) if data.get("laudo_recente") else {}
    dvistoria= detalhes.get("dados_vistoria", {}) if detalhes else {}

    if geral:
        lines.append("")
        lines.append("📅 <b>VISTORIA</b>")
        if v(geral.get("data_vistoria")) != "—":     lines.append(f"  Data: {v(geral.get('data_vistoria'))}")
        if v(geral.get("data_hora_emissao")) != "—": lines.append(f"  Emissão: {v(geral.get('data_hora_emissao'))}")
        if v(geral.get("validade_vistoria")) != "—": lines.append(f"  Validade: {v(geral.get('validade_vistoria'))}")

    if veiculo and any(v(val) != "—" for val in veiculo.values()):
        lines.append("")
        lines.append("🚘 <b>VEÍCULO</b>")
        campos_veiculo = [
            ("marca_modelo","Modelo"), ("tipo_veiculo","Tipo"),
            ("cor","Cor"), ("ano_fabricacao","Ano Fab."),
            ("ano_modelo","Ano Mod."), ("combustivel","Combustível"),
            ("chassi","Chassi"), ("motor","Motor"),
            ("renavam","RENAVAM"), ("especie","Espécie"),
            ("potencia","Potência"), ("cilindrada","Cilindrada"),
        ]
        for campo, label in campos_veiculo:
            val = v(veiculo.get(campo))
            if val != "—": lines.append(f"  {label}: {val}")

    if prop and any(v(val) != "—" for val in prop.values()):
        lines.append("")
        lines.append("👤 <b>PROPRIETÁRIO</b>")
        if v(prop.get("proprietario_nome")) != "—":      lines.append(f"  Nome: {v(prop.get('proprietario_nome'))}")
        if v(prop.get("proprietario_cpf_cnpj")) != "—":  lines.append(f"  CPF/CNPJ: {v(prop.get('proprietario_cpf_cnpj'))}")
        if v(prop.get("proprietario_municipio")) != "—": lines.append(f"  Município: {v(prop.get('proprietario_municipio'))}")
        if v(prop.get("proprietario_uf")) != "—":        lines.append(f"  UF: {v(prop.get('proprietario_uf'))}")

    if ecv and any(v(val) != "—" for val in ecv.values()):
        lines.append("")
        lines.append("🏢 <b>ECV (VISTORIADORA)</b>")
        if v(ecv.get("ecv_razao_social")) != "—": lines.append(f"  Nome: {v(ecv.get('ecv_razao_social'))}")
        if v(ecv.get("ecv_cnpj")) != "—":         lines.append(f"  CNPJ: {v(ecv.get('ecv_cnpj'))}")
        if v(ecv.get("ecv_municipio")) != "—":    lines.append(f"  Cidade: {v(ecv.get('ecv_municipio'))}")
        if v(ecv.get("ecv_uf")) != "—":           lines.append(f"  UF: {v(ecv.get('ecv_uf'))}")
        if v(ecv.get("ecv_telefone")) != "—":     lines.append(f"  Tel: {v(ecv.get('ecv_telefone'))}")
        if v(ecv.get("ecv_validade_portaria")) != "—": lines.append(f"  Validade portaria: {v(ecv.get('ecv_validade_portaria'))}")

    if dvistoria:
        lines.append("")
        lines.append("🔍 <b>DADOS VISTORIA</b>")
        if v(dvistoria.get("km")) != "—":             lines.append(f"  KM: {v(dvistoria.get('km'))}")
        if v(dvistoria.get("numero_chassi")) != "—":  lines.append(f"  Chassi: {v(dvistoria.get('numero_chassi'))}")
        if v(dvistoria.get("numero_motor")) != "—":   lines.append(f"  Motor: {v(dvistoria.get('numero_motor'))}")
        if v(dvistoria.get("origem_motor")) != "—":   lines.append(f"  Origem motor: {v(dvistoria.get('origem_motor'))}")
        if v(dvistoria.get("origem_chassi")) != "—":  lines.append(f"  Origem chassi: {v(dvistoria.get('origem_chassi'))}")
        if v(dvistoria.get("numero_lacre")) != "—":   lines.append(f"  Lacre: {v(dvistoria.get('numero_lacre'))}")

    # Histórico de laudos
    laudos = data.get("laudos", [])
    if laudos:
        lines.append("")
        lines.append(f"📜 <b>HISTÓRICO ({len(laudos)} laudos)</b>")
        for i, l in enumerate(laudos[:5], 1):
            lines.append(f"  {i}. {v(l.get('data_str'))} — {v(l.get('ecv'))}")

    return "\n".join(lines) if lines else formatar_resultado(data)

def _extrair_fotos(data, _fotos=None) -> list:
    """
    Percorre o JSON recursivamente procurando campos base64 de imagens.
    Remove os campos de foto do dict para não poluir o texto.
    Retorna lista de strings base64.
    """
    import re as _re
    if _fotos is None:
        _fotos = []

    FOTO_CAMPOS = {
        "foto", "fotos", "photo", "photos", "imagem", "imagens",
        "image", "images", "foto_base64", "imagem_base64", "base64",
        "foto1", "foto2", "foto3", "foto4", "foto5",
        "url_foto", "thumbnail", "picture", "pictures",
        "img", "imgs", "arquivo", "arquivos", "anexo", "anexos",
    }

    B64_PATTERN = _re.compile(r'^[A-Za-z0-9+/]{100,}={0,2}$')

    def _is_base64(v):
        if not isinstance(v, str) or len(v) < 100:
            return False
        # Remove data:image prefix if present
        s = v.split(',')[-1] if ',' in v else v
        return bool(B64_PATTERN.match(s.replace('\n','').replace('\r','')))

    def _clean_b64(v):
        """Remove data:image/jpeg;base64, prefix if present."""
        if ',' in v:
            return v.split(',', 1)[1]
        return v

    if isinstance(data, dict):
        keys_to_remove = []
        for k, v in list(data.items()):
            kl = k.lower()
            if kl in FOTO_CAMPOS or 'foto' in kl or 'imag' in kl or 'photo' in kl or 'picture' in kl:
                if _is_base64(v):
                    _fotos.append(_clean_b64(v))
                    keys_to_remove.append(k)
                elif isinstance(v, list):
                    has_b64 = False
                    for item in v:
                        if _is_base64(item):
                            _fotos.append(_clean_b64(item))
                            has_b64 = True
                    if has_b64:
                        keys_to_remove.append(k)
                    else:
                        _extrair_fotos(v, _fotos)
                else:
                    _extrair_fotos(v, _fotos)
            else:
                _extrair_fotos(v, _fotos)
        for k in keys_to_remove:
            del data[k]
    elif isinstance(data, list):
        for item in data:
            _extrair_fotos(item, _fotos)

    return _fotos

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
        "🗳️ <code>/titulo 000000000000</code>\n"
        "📋 <code>/laudo ABC1234</code> ou <code>/laudo_veicular ABC1234</code>\n\n"
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
        resultado = await consultar_qualquer(tipo, query)
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
        resultado = await consultar_qualquer(tipo, query_val)
        registrar_consulta(tipo, query_val, usuario, True)

        fotos_b64 = _extrair_fotos(resultado)

        if tipo in ("laudo_veicular", "laudo"):
            corpo = formatar_laudo(resultado)
        else:
            corpo = formatar_resultado(resultado)

        texto = (
            "🦅 <b>Unicontroller</b>\n\n"
            f"✅ <b>{LABELS.get(tipo, tipo)}</b>\n"
            f"🔎 <code>{query_val}</code>\n\n"
            f"{corpo}" + rodape()
        )
        if len(texto) > 4000:
            texto = texto[:3900] + "\n\n<i>... resultado truncado</i>" + rodape()
        await msg.edit_text(texto, parse_mode="HTML")

        if fotos_b64:
            await update.message.reply_text(f"📸 Enviando {len(fotos_b64)} foto(s)...")
            for i, b64 in enumerate(fotos_b64[:10], 1):
                try:
                    import base64, io
                    img_bytes = base64.b64decode(b64)
                    bio = io.BytesIO(img_bytes)
                    bio.name = f"foto_{i}.jpg"
                    await update.message.reply_photo(
                        photo=bio,
                        caption=f"📸 Foto {i}/{len(fotos_b64)} — {LABELS.get(tipo,tipo)}: {query_val}" + rodape(),
                        parse_mode="HTML"
                    )
                except Exception as ef:
                    logger.warning(f"Erro ao enviar foto {i}: {ef}")

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
        "proprietario":"123.456.789-00","mae":"Maria Silva","pai":"José Silva",
        "titulo":"000000000000","laudo_veicular":"ABC1234","laudo":"ABC1234",
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
:root{
  --bg:#050d1a;--surface:#0a1628;--surface2:#0d1e38;--border:#0f2a4a;
  --accent:#00d4ff;--green:#00e676;--red:#ff4757;--yellow:#ffd32a;
  --purple:#bf5af2;--orange:#ff9f43;--text:#e8f4ff;--muted:#4a7a9b;
}
body{background:var(--bg);color:var(--text);font-family:'Space Grotesk',sans-serif;min-height:100vh}
a{color:inherit;text-decoration:none}
/* ─── Header ─── */
header{padding:16px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:rgba(10,22,40,0.98);position:sticky;top:0;z-index:200;backdrop-filter:blur(12px)}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{font-size:26px}
.logo-text{font-size:17px;font-weight:700;letter-spacing:1px}
.logo-text span{color:var(--accent)}
.logo-sub{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:1px}
.tabs{display:flex;gap:3px;background:rgba(0,0,0,0.4);padding:4px;border-radius:8px}
.tab{padding:7px 16px;border-radius:5px;cursor:pointer;font-size:11px;letter-spacing:1px;border:none;background:transparent;color:var(--muted);font-family:'Space Grotesk',sans-serif;transition:all .18s;white-space:nowrap}
.tab.active{background:var(--accent);color:#000;font-weight:700}
.header-right{display:flex;align-items:center;gap:10px}
.live-badge{padding:4px 12px;border-radius:20px;font-size:10px;font-family:'JetBrains Mono',monospace;background:rgba(0,230,118,0.1);border:1px solid rgba(0,230,118,0.3);color:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
/* ─── Layout ─── */
main{padding:22px 28px;max-width:1400px;margin:0 auto}
.page{display:none}.page.active{display:block}
/* ─── Cards ─── */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;position:relative;overflow:hidden;transition:border-color .2s,transform .2s}
.card:hover{border-color:var(--accent);transform:translateY(-2px)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.c1::before{background:var(--accent)}.c2::before{background:var(--green)}
.c3::before{background:var(--red)}.c4::before{background:var(--yellow)}
.c5::before{background:var(--purple)}.c6::before{background:var(--orange)}
.card-label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:2px;margin-bottom:7px;font-family:'JetBrains Mono',monospace}
.card-value{font-size:32px;font-weight:700;font-family:'JetBrains Mono',monospace;line-height:1}
.c1 .card-value{color:var(--accent)}.c2 .card-value{color:var(--green)}
.c3 .card-value{color:var(--red)}.c4 .card-value{color:var(--yellow)}
.c5 .card-value{color:var(--purple)}.c6 .card-value{color:var(--orange)}
.card-icon{position:absolute;right:14px;top:50%;transform:translateY(-50%);font-size:30px;opacity:.1}
/* ─── Panels ─── */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.grid3{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px}
.panel-title{font-size:10px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);margin-bottom:14px;font-family:'JetBrains Mono',monospace;display:flex;align-items:center;justify-content:space-between}
.panel-title-left{display:flex;align-items:center;gap:8px}
.panel-title-left::before{content:'';width:3px;height:12px;background:var(--accent);border-radius:2px;display:inline-block}
canvas{max-height:190px}
/* ─── Tables ─── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:8px 10px;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid rgba(15,42,74,0.5);font-family:'JetBrains Mono',monospace;vertical-align:middle}
tr:hover td{background:rgba(0,212,255,0.03)}
/* ─── Tags ─── */
.tag{padding:2px 8px;border-radius:20px;font-size:9px;font-weight:700;letter-spacing:1px;white-space:nowrap}
.tag-ok{background:rgba(0,230,118,0.12);color:var(--green);border:1px solid rgba(0,230,118,0.25)}
.tag-fail{background:rgba(255,71,87,0.12);color:var(--red);border:1px solid rgba(255,71,87,0.25)}
.tag-on{background:rgba(0,212,255,0.12);color:var(--accent);border:1px solid rgba(0,212,255,0.25)}
.tag-off{background:rgba(255,71,87,0.07);color:var(--red);border:1px solid rgba(255,71,87,0.15)}
/* ─── Buttons ─── */
.btn{padding:7px 14px;border-radius:6px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;letter-spacing:1px;transition:all .18s;border:1px solid transparent;white-space:nowrap}
.btn-a{background:rgba(0,212,255,0.1);border-color:rgba(0,212,255,0.3);color:var(--accent)}.btn-a:hover{background:rgba(0,212,255,0.22)}
.btn-g{background:rgba(0,230,118,0.1);border-color:rgba(0,230,118,0.3);color:var(--green)}.btn-g:hover{background:rgba(0,230,118,0.22)}
.btn-r{background:rgba(255,71,87,0.1);border-color:rgba(255,71,87,0.3);color:var(--red)}.btn-r:hover{background:rgba(255,71,87,0.22)}
.btn-y{background:rgba(255,211,42,0.1);border-color:rgba(255,211,42,0.3);color:var(--yellow)}.btn-y:hover{background:rgba(255,211,42,0.22)}
.btn-sm{padding:4px 9px;font-size:10px}
/* ─── Forms ─── */
.form-row{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;align-items:flex-end}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-group label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;font-family:'JetBrains Mono',monospace}
input,select,textarea{background:#060e1a;border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:12px;outline:none;transition:border-color .2s}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
textarea{resize:vertical;min-height:80px}
/* ─── Key box ─── */
.key-box{background:#030810;border:1px solid var(--border);border-radius:8px;padding:12px 14px;font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--accent);word-break:break-all;cursor:pointer;transition:border-color .2s,color .2s}
.key-box:hover{border-color:var(--accent)}
/* ─── Bars ─── */
.bar-item{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.bar-label{font-size:11px;min-width:110px;color:var(--text)}
.bar-track{flex:1;background:rgba(255,255,255,0.05);border-radius:4px;height:6px;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),#0099bb);border-radius:4px;transition:width .6s}
.bar-count{font-size:11px;color:var(--accent);min-width:26px;text-align:right;font-family:'JetBrains Mono',monospace;font-weight:700}
/* ─── Progress ─── */
.prog{background:rgba(255,255,255,0.05);border-radius:4px;height:4px;overflow:hidden;margin-top:3px}
.prog-fill{height:100%;border-radius:4px;transition:width .4s}
/* ─── Consulta panel ─── */
.consulta-input{display:flex;gap:0;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.tipo-badge{background:#0a1827;padding:0 14px;display:flex;align-items:center;border-right:1px solid var(--border);font-size:11px;color:var(--accent);letter-spacing:1px;white-space:nowrap}
.consulta-input input{flex:1;background:#060e1a;border:none;outline:none;color:var(--text);padding:12px 16px;font-family:'JetBrains Mono',monospace;font-size:13px}
.consulta-input button{background:linear-gradient(135deg,#0284c7,#00d4ff);border:none;cursor:pointer;padding:0 22px;color:#000;font-size:11px;letter-spacing:2px;font-weight:700;font-family:'JetBrains Mono',monospace;transition:opacity .2s}
.consulta-input button:hover{opacity:.85}
.consulta-input button:disabled{opacity:.4;cursor:not-allowed}
.url-preview{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:6px}
.resultado-box{background:#030810;border:1px solid var(--border);border-radius:8px;padding:16px;font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.8;max-height:480px;overflow-y:auto;margin-top:14px}
.json-key{color:#7dd3fc}.json-str{color:#fcd34d}.json-num{color:#34d399}.json-bool{color:#f472b6}.json-null{color:var(--muted)}
/* ─── Result tabs ─── */
.rtabs{display:flex;gap:4px;margin-bottom:12px}
.rtab{padding:5px 14px;border-radius:5px;cursor:pointer;font-size:11px;border:1px solid var(--border);background:transparent;color:var(--muted);font-family:'JetBrains Mono',monospace;transition:all .18s}
.rtab.active{background:var(--accent);color:#000;border-color:var(--accent);font-weight:700}
/* ─── Modal ─── */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;width:640px;max-width:100%;max-height:90vh;overflow-y:auto}
.modal-title{font-size:14px;font-weight:700;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.modal-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:20px;padding:0 4px;transition:color .2s}
.modal-close:hover{color:var(--text)}
/* ─── Misc ─── */
.empty{text-align:center;padding:40px;color:var(--muted);font-size:13px}
.sep{border:none;border-top:1px solid var(--border);margin:16px 0}
.spin{display:inline-block;width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.alert{padding:10px 14px;border-radius:8px;font-size:12px;margin-bottom:14px}
.alert-ok{background:rgba(0,230,118,0.08);border:1px solid rgba(0,230,118,0.25);color:var(--green)}
.alert-err{background:rgba(255,71,87,0.08);border:1px solid rgba(255,71,87,0.25);color:var(--red)}
@media(max-width:900px){.grid2,.grid3{grid-template-columns:1fr}.tabs{overflow-x:auto}}
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
  <div class="tabs">
    <button class="tab active" id="tab-stats" data-tab="stats">📊 Stats</button>
    <button class="tab" id="tab-consulta" data-tab="consulta">🔍 Consulta</button>
    <button class="tab" id="tab-clientes" data-tab="clientes">👥 Clientes</button>
    <button class="tab" id="tab-keys" data-tab="keys">🔑 API Keys</button>
    <button class="tab" id="tab-docs" data-tab="docs">📄 Docs</button>
  </div>
  <div class="header-right">
    <span id="lastUp" style="font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace"></span>
    <span class="live-badge">● LIVE</span>
    <button class="btn btn-a" onclick="loadAll()">↻</button>
  </div>
</header>

<main>

<!-- ════════ STATS ════════ -->
<div class="page active" id="page-stats">
  <div class="cards">
    <div class="card c1"><div class="card-label">Total</div><div class="card-value" id="cTotal">—</div><div class="card-icon">🔢</div></div>
    <div class="card c2"><div class="card-label">Sucesso</div><div class="card-value" id="cSucesso">—</div><div class="card-icon">✅</div></div>
    <div class="card c3"><div class="card-label">Erros</div><div class="card-value" id="cErro">—</div><div class="card-icon">❌</div></div>
    <div class="card c4"><div class="card-label">Hoje</div><div class="card-value" id="cHoje">—</div><div class="card-icon">📅</div></div>
    <div class="card c5"><div class="card-label">Taxa Sucesso</div><div class="card-value" id="cTaxa">—</div><div class="card-icon">📈</div></div>
    <div class="card c6"><div class="card-label">Keys Ativas</div><div class="card-value" id="cKeysA">—</div><div class="card-icon">🔑</div></div>
  </div>
  <div class="grid2">
    <div class="panel"><div class="panel-title"><span class="panel-title-left">Consultas por Dia (7 dias)</span></div><canvas id="chartDia"></canvas></div>
    <div class="panel"><div class="panel-title"><span class="panel-title-left">Consultas por Hora</span></div><canvas id="chartHora"></canvas></div>
  </div>
  <div class="grid3">
    <div class="panel">
      <div class="panel-title">
        <span class="panel-title-left">Histórico Recente</span>
      </div>
      <div class="table-wrap" id="historicoWrap"><div class="empty">Nenhuma consulta ainda</div></div>
    </div>
    <div class="panel">
      <div class="panel-title"><span class="panel-title-left">Ranking por Tipo</span></div>
      <div id="rankingTipo"><div class="empty">—</div></div>
    </div>
  </div>
</div>

<!-- ════════ CONSULTA ════════ -->
<div class="page" id="page-consulta">
  <div class="grid2" style="margin-bottom:16px">
    <!-- Painel de consulta -->
    <div class="panel">
      <div class="panel-title"><span class="panel-title-left">Fazer Consulta</span></div>

      <div class="form-row" style="margin-bottom:12px">
        <div class="form-group" style="flex:1">
          <label>Tipo de consulta</label>
          <select id="cTipo" onchange="atualizarTipo()">
            <option value="cpf">👤 CPF</option>
            <option value="cnpj">🏢 CNPJ</option>
            <option value="cep">📍 CEP</option>
            <option value="cns">🏥 CNS</option>
            <option value="nome">🔤 Nome</option>
            <option value="email">✉️ E-mail</option>
            <option value="telefone">📞 Telefone</option>
            <option value="vizinhos">🏘️ Vizinhos</option>
            <option value="placa">🚗 Veículo (Placa)</option>
            <option value="proprietario">🔑 Proprietário (CPF)</option>
            <option value="mae">👩 Nome da Mãe</option>
            <option value="pai">👨 Nome do Pai</option>
            <option value="titulo">🗳️ Título de Eleitor</option>
          </select>
        </div>
        <div class="form-group" style="flex:1">
          <label>API Key</label>
          <input id="cApiKey" placeholder="uc_sua_chave_aqui" type="password">
        </div>
      </div>

      <div class="consulta-input">
        <div class="tipo-badge" id="tipoBadge">👤 CPF</div>
        <input id="cQuery" placeholder="000.000.000-00" onkeydown="if(event.key==='Enter')fazerConsulta()" oninput="atualizarUrl()">
        <button onclick="fazerConsulta()" id="btnConsultar">CONSULTAR</button>
      </div>
      <div class="url-preview" id="urlPreview">GET /v1/consulta/cpf?query=<valor></div>

      <div id="consultaStatus" style="margin-top:12px;display:none"></div>
    </div>

    <!-- Histórico de consultas da sessão -->
    <div class="panel">
      <div class="panel-title">
        <span class="panel-title-left">Sessão Atual</span>
        <button class="btn btn-r btn-sm" onclick="sessao=[];renderSessao()">Limpar</button>
      </div>
      <div id="sessaoWrap"><div class="empty" style="padding:20px">Nenhuma consulta ainda</div></div>
    </div>
  </div>

  <!-- Resultado -->
  <div class="panel" id="resultadoPanel" style="display:none">
    <div class="panel-title">
      <span class="panel-title-left" id="resultadoTitulo">Resultado</span>
      <div style="display:flex;gap:6px">
        <button class="btn btn-a btn-sm" onclick="copiarResultado()">📋 Copiar JSON</button>
        <button class="btn btn-g btn-sm" onclick="downloadResultado()">⬇️ Download</button>
      </div>
    </div>
    <div class="rtabs">
      <button class="rtab active" onclick="switchRtab('formatado',this)">Formatado</button>
      <button class="rtab" onclick="switchRtab('raw',this)">JSON Raw</button>
    </div>
    <div class="resultado-box" id="resultadoFormatado"></div>
    <div class="resultado-box" id="resultadoRaw" style="display:none"></div>
  </div>
</div>

<!-- ════════ CLIENTES ════════ -->
<div class="page" id="page-clientes">
  <div class="cards" id="planosCards"></div>
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-title"><span class="panel-title-left">Novo Cliente</span></div>
    <div class="form-row">
      <div class="form-group" style="flex:2;min-width:180px">
        <label>Nome do cliente / empresa</label>
        <input id="kNome" placeholder="Ex: João Silva, Empresa X">
      </div>
      <div class="form-group" style="min-width:170px">
        <label>Plano</label>
        <select id="kPlano" onchange="toggleCustom()">
          <option value="basico">Básico — 500 consultas</option>
          <option value="pro">Pro — 2.000 consultas</option>
          <option value="premium">Premium — 10.000 consultas</option>
          <option value="ilimitado">Ilimitado — ∞</option>
          <option value="custom">Personalizado</option>
        </select>
      </div>
      <div class="form-group" id="customLimiteGroup" style="min-width:140px;display:none">
        <label>Limite personalizado</label>
        <input id="kLimite" type="number" placeholder="Ex: 1500">
      </div>
      <div class="form-group" style="flex:1;min-width:130px">
        <label>Tags / projetos</label>
        <input id="kProjetos" placeholder="site,app,railway">
      </div>
      <div class="form-group">
        <label>&nbsp;</label>
        <button class="btn btn-g" onclick="criarCliente()">➕ GERAR KEY</button>
      </div>
    </div>
    <div id="novaKeyResult" style="display:none"></div>
  </div>

  <div class="panel">
    <div class="panel-title">
      <span class="panel-title-left">Clientes Cadastrados</span>
      <div style="display:flex;gap:6px">
        <button class="btn btn-a btn-sm" onclick="exportarTodas()">⬇️ Exportar Todas</button>
        <button class="btn btn-a btn-sm" onclick="loadClientes()">↻ Atualizar</button>
      </div>
    </div>
    <div id="clientesWrap"><div class="empty">Carregando...</div></div>
  </div>
</div>

<!-- ════════ API KEYS ════════ -->
<div class="page" id="page-keys">
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-title"><span class="panel-title-left">Gerenciar Keys</span></div>
    <div class="form-row">
      <div class="form-group" style="flex:2;min-width:180px">
        <label>Nome / Identificador</label>
        <input id="aKNome" placeholder="nome-do-projeto">
      </div>
      <div class="form-group" style="min-width:160px">
        <label>Limite (-1 = ilimitado)</label>
        <input id="aKLimite" type="number" value="-1">
      </div>
      <div class="form-group" style="flex:1;min-width:130px">
        <label>Projetos (separados por vírgula)</label>
        <input id="aKProjetos" placeholder="railway,github">
      </div>
      <div class="form-group">
        <label>&nbsp;</label>
        <button class="btn btn-g" onclick="criarKeyRaw()">➕ CRIAR</button>
      </div>
    </div>
    <div id="rawKeyResult" style="display:none"></div>
  </div>

  <div class="panel">
    <div class="panel-title">
      <span class="panel-title-left">Todas as Keys</span>
      <button class="btn btn-a btn-sm" onclick="loadKeys()">↻ Atualizar</button>
    </div>
    <div id="keysWrap"><div class="empty">Carregando...</div></div>
  </div>
</div>

<!-- ════════ DOCS ════════ -->
<div class="page" id="page-docs">
  <div class="panel" style="margin-bottom:16px">
    <div class="panel-title"><span class="panel-title-left">Integração Externa</span></div>
    <div style="font-size:12px;line-height:2;color:#b0c8e0;margin-bottom:14px">
      Use a Unicontroller API em qualquer projeto com uma API Key. Suporta Railway, GitHub Actions, Node.js, Python, PHP e mais.
    </div>
    <div style="margin-bottom:10px;font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace">Base URL</div>
    <div class="key-box" id="docsBase" onclick="copiarTexto(this)" style="margin-bottom:16px;font-size:13px"></div>

    <div style="margin-bottom:10px;font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace">Autenticação</div>
    <div class="key-box" style="margin-bottom:16px;color:#b0c8e0">Header: <span style="color:var(--accent)">X-API-Key: uc_sua_chave_aqui</span></div>

    <div style="margin-bottom:10px;font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace">Tipos disponíveis</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px">
      <span class="tag tag-on">cpf</span><span class="tag tag-on">cnpj</span><span class="tag tag-on">cep</span>
      <span class="tag tag-on">cns</span><span class="tag tag-on">nome</span><span class="tag tag-on">email</span>
      <span class="tag tag-on">telefone</span><span class="tag tag-on">vizinhos</span><span class="tag tag-on">placa</span>
      <span class="tag tag-on">proprietario</span><span class="tag tag-on">mae</span><span class="tag tag-on">pai</span>
      <span class="tag tag-on">titulo</span>
    </div>

    <div style="margin-bottom:10px;font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace">Rotas</div>
    <table style="margin-bottom:16px">
      <thead><tr><th>Método</th><th>Rota</th><th>Auth</th><th>Descrição</th></tr></thead>
      <tbody>
        <tr><td><span class="tag tag-on">GET</span></td><td>/v1/consulta/{tipo}?query=valor</td><td><span class="tag tag-ok">KEY</span></td><td>Consulta com metadados</td></tr>
        <tr><td><span class="tag tag-on">GET</span></td><td>/v1/consulta/{tipo}/raw?query=valor</td><td><span class="tag tag-ok">KEY</span></td><td>JSON bruto</td></tr>
        <tr><td><span class="tag tag-on">GET</span></td><td>/v1/me</td><td><span class="tag tag-ok">KEY</span></td><td>Info da sua key</td></tr>
        <tr><td><span class="tag tag-on">GET</span></td><td>/ping</td><td>—</td><td>Health check</td></tr>
        <tr><td><span class="tag tag-on">GET</span></td><td>/api/stats</td><td>—</td><td>Estatísticas</td></tr>
        <tr><td><span class="tag tag-on">POST</span></td><td>/admin/keys</td><td><span class="tag tag-fail">ADMIN</span></td><td>Criar key</td></tr>
        <tr><td><span class="tag tag-on">GET</span></td><td>/admin/export/key/{key}</td><td><span class="tag tag-fail">ADMIN</span></td><td>Exportar key JSON</td></tr>
        <tr><td><span class="tag tag-on">GET</span></td><td>/admin/export/all</td><td><span class="tag tag-fail">ADMIN</span></td><td>Exportar todas</td></tr>
      </tbody>
    </table>

    <div id="docsExemplos"></div>
  </div>
</div>

</main>

<!-- ════════ MODAL ════════ -->
<div id="modalBg" class="modal-bg" style="display:none" onclick="if(event.target===this)fecharModal()">
  <div class="modal">
    <div class="modal-title">
      <span id="modalTitulo"></span>
      <button class="modal-close" onclick="fecharModal()">✕</button>
    </div>
    <div id="modalConteudo"></div>
  </div>
</div>

<script>
const BASE = window.location.origin;
const TIPOS_LABELS = {
  cpf:'👤 CPF',cnpj:'🏢 CNPJ',cep:'📍 CEP',cns:'🏥 CNS',
  nome:'🔤 Nome',email:'✉️ E-mail',telefone:'📞 Telefone',
  vizinhos:'🏘️ Vizinhos',placa:'🚗 Veículo',proprietario:'🔑 Proprietário',
  mae:'👩 Mãe',pai:'👨 Pai',titulo:'🗳️ Título'
};
const TIPOS_EX = {
  cpf:'123.456.789-00',cnpj:'00.000.000/0001-00',cep:'01310-100',
  cns:'123456789012345',nome:'João Silva',email:'joao@email.com',
  telefone:'11999999999',vizinhos:'01310-100',placa:'ABC1234',
  proprietario:'123.456.789-00',mae:'Maria Silva',pai:'José Silva',titulo:'000000000000'
};
const PLANOS_INFO = {
  basico:   {nome:'Básico',   limite:500,   preco:'R$ 29,90', cor:'#00d4ff'},
  pro:      {nome:'Pro',      limite:2000,  preco:'R$ 79,90', cor:'#00e676'},
  premium:  {nome:'Premium',  limite:10000, preco:'R$ 199,90',cor:'#bf5af2'},
  ilimitado:{nome:'Ilimitado',limite:-1,    preco:'R$ 399,90',cor:'#ffd32a'},
  custom:   {nome:'Custom',   limite:-1,    preco:'—',        cor:'#ff9f43'},
};

let chartDia=null, chartHora=null, sessao=[], lastResult=null, adminKey='';

// ── Auth helper ──
function getAdmin(){
  if(!adminKey) adminKey = prompt('API_SECRET (chave admin):') || '';
  return adminKey;
}
function clearAdmin(){ adminKey = ''; }

// ═══ TAB NAVIGATION ═══
function goTab(t){
  document.querySelectorAll(".tab").forEach(function(b){b.classList.remove("active");});
  document.querySelectorAll(".page").forEach(function(p){p.classList.remove("active");});
  var tabEl  = document.getElementById("tab-"+t);
  var pageEl = document.getElementById("page-"+t);
  if(tabEl)  tabEl.classList.add("active");
  if(pageEl) pageEl.classList.add("active");
  if(t==="stats")    loadStats();
  if(t==="clientes") loadClientes();
  if(t==="keys")     loadKeys();
  if(t==="docs")     buildDocs();
}


  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.getElementById('tab-'+t).classList.add('active');
  document.getElementById('page-'+t).classList.add('active');
  if(t==='stats')    loadStats();
  if(t==='clientes') loadClientes();
  if(t==='keys')     loadKeys();
  if(t==='docs')     buildDocs();
}

// ── Charts ──
const chartCfg = (labels, data, color) => ({
  type:'line',
  data:{labels,datasets:[{data,borderColor:color,backgroundColor:color+'15',fill:true,tension:.4,pointRadius:3,pointBackgroundColor:color,borderWidth:2}]},
  options:{responsive:true,plugins:{legend:{display:false}},scales:{
    x:{grid:{color:'#0f2a4a'},ticks:{color:'#4a7a9b',font:{size:9,family:'JetBrains Mono'}}},
    y:{grid:{color:'#0f2a4a'},ticks:{color:'#4a7a9b',font:{size:9,family:'JetBrains Mono'},stepSize:1},beginAtZero:true}
  }}
});

// ── Stats ──
async function loadStats(){
  try{
    const d = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('cTotal').textContent   = d.total;
    document.getElementById('cSucesso').textContent = d.sucesso;
    document.getElementById('cErro').textContent    = d.erro;
    document.getElementById('cHoje').textContent    = d.hoje;
    document.getElementById('cTaxa').textContent    = d.taxa_sucesso+'%';
    document.getElementById('cKeysA').textContent   = d.keys_ativas;
    document.getElementById('lastUp').textContent   = new Date().toLocaleTimeString('pt-BR');
    if(chartDia)  chartDia.destroy();
    if(chartHora) chartHora.destroy();
    chartDia  = new Chart(document.getElementById('chartDia'),  chartCfg(Object.keys(d.por_dia),  Object.values(d.por_dia),  '#00d4ff'));
    chartHora = new Chart(document.getElementById('chartHora'), chartCfg(Object.keys(d.por_hora), Object.values(d.por_hora), '#00e676'));
    const tipos=Object.entries(d.por_tipo), max=tipos.length?tipos[0][1]:1;
    document.getElementById('rankingTipo').innerHTML = tipos.length
      ? tipos.map(([t,n])=>`<div class="bar-item"><div class="bar-label">${TIPOS_LABELS[t]||t}</div><div class="bar-track"><div class="bar-fill" style="width:${Math.round(n/max*100)}%"></div></div><div class="bar-count">${n}</div></div>`).join('')
      : '<div class="empty" style="padding:20px">—</div>';
    document.getElementById('historicoWrap').innerHTML = d.historico.length
      ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Tipo</th><th>Query</th><th>Origem</th><th>Status</th><th>Horário</th></tr></thead><tbody>${
          d.historico.map(h=>`<tr>
            <td style="color:var(--muted)">${h.id}</td>
            <td>${h.label}</td>
            <td style="color:var(--accent)">${h.query}</td>
            <td style="color:var(--muted)">${h.key}</td>
            <td><span class="tag ${h.sucesso?'tag-ok':'tag-fail'}">${h.sucesso?'OK':'ERRO'}</span></td>
            <td style="color:var(--muted)">${h.timestamp}</td>
          </tr>`).join('')
        }</tbody></table></div>`
      : '<div class="empty">Nenhuma consulta ainda</div>';
  }catch(e){console.error(e)}
}

// ── Consulta ──
function atualizarTipo(){
  const tipo = document.getElementById('cTipo').value;
  document.getElementById('tipoBadge').textContent = TIPOS_LABELS[tipo];
  document.getElementById('cQuery').placeholder = TIPOS_EX[tipo]||'valor';
  atualizarUrl();
}
function atualizarUrl(){
  const tipo  = document.getElementById('cTipo').value;
  const query = document.getElementById('cQuery').value || '<valor>';
  document.getElementById('urlPreview').textContent = `GET /v1/consulta/${tipo}?query=${query}`;
}
function switchRtab(t, btn){
  document.querySelectorAll('.rtab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('resultadoFormatado').style.display = t==='formatado'?'block':'none';
  document.getElementById('resultadoRaw').style.display       = t==='raw'?'block':'none';
}
function renderJson(data, depth=0){
  if(data===null) return '<span class="json-null">null</span>';
  if(typeof data==='boolean') return `<span class="json-bool">${data}</span>`;
  if(typeof data==='number')  return `<span class="json-num">${data}</span>`;
  if(typeof data==='string')  return `<span class="json-str">"${data}"</span>`;
  if(Array.isArray(data)){
    if(!data.length) return '[]';
    const items = data.slice(0,8).map(v=>`<div style="padding-left:${(depth+1)*16}px">${renderJson(v,depth+1)}</div>`).join('');
    const more  = data.length>8?`<div style="padding-left:${(depth+1)*16}px;color:var(--muted)">... +${data.length-8} itens</div>`:'';
    return `[${items}${more}<div style="padding-left:${depth*16}px">]</div>`;
  }
  if(typeof data==='object'){
    const entries = Object.entries(data);
    if(!entries.length) return '{}';
    const items = entries.map(([k,v])=>`<div style="padding-left:${(depth+1)*16}px"><span class="json-key">"${k}"</span>: ${renderJson(v,depth+1)}</div>`).join('');
    return `{${items}<div style="padding-left:${depth*16}px">}</div>`;
  }
  return String(data);
}
async function fazerConsulta(){
  const tipo  = document.getElementById('cTipo').value;
  const query = document.getElementById('cQuery').value.trim();
  const key   = document.getElementById('cApiKey').value.trim();
  if(!query){ showAlert('consultaStatus','Informe o valor para consultar','err'); return; }
  if(!key)  { showAlert('consultaStatus','Informe sua API Key','err'); return; }
  const btn = document.getElementById('btnConsultar');
  btn.disabled=true; btn.textContent='...';
  document.getElementById('consultaStatus').style.display='none';
  try{
    const r = await fetch(`/v1/consulta/${tipo}?query=${encodeURIComponent(query)}`,{headers:{'X-API-Key':key}});
    const d = await r.json();
    if(!r.ok){ showAlert('consultaStatus', d.detail||'Erro na consulta','err'); return; }
    lastResult = d;
    const dados = d.data || d;
    document.getElementById('resultadoPanel').style.display='block';
    document.getElementById('resultadoTitulo').textContent = `${TIPOS_LABELS[tipo]} — ${query}`;
    document.getElementById('resultadoFormatado').innerHTML = renderJson(dados);
    document.getElementById('resultadoRaw').textContent = JSON.stringify(dados, null, 2);
    document.getElementById('resultadoRaw').style.display='none';
    document.querySelector('.rtab.active')?.classList.remove('active');
    document.querySelectorAll('.rtab')[0].classList.add('active');
    document.getElementById('resultadoFormatado').style.display='block';
    sessao.unshift({tipo, label:TIPOS_LABELS[tipo], query, ok:true, ts:new Date().toLocaleTimeString('pt-BR')});
    if(sessao.length>20) sessao.pop();
    renderSessao();
    document.getElementById('resultadoPanel').scrollIntoView({behavior:'smooth'});
  }catch(e){ showAlert('consultaStatus','Erro de conexão: '+e,'err'); }
  finally{ btn.disabled=false; btn.textContent='CONSULTAR'; }
}
function renderSessao(){
  document.getElementById('sessaoWrap').innerHTML = sessao.length
    ? `<div class="table-wrap"><table><thead><tr><th>Tipo</th><th>Query</th><th>Status</th><th>Hora</th></tr></thead><tbody>${
        sessao.map(s=>`<tr style="cursor:pointer" onclick="">
          <td>${s.label}</td>
          <td style="color:var(--accent)">${s.query}</td>
          <td><span class="tag ${s.ok?'tag-ok':'tag-fail'}">${s.ok?'OK':'ERRO'}</span></td>
          <td style="color:var(--muted)">${s.ts}</td>
        </tr>`).join('')
      }</tbody></table></div>`
    : '<div class="empty" style="padding:20px">Nenhuma consulta ainda</div>';
}
function copiarResultado(){
  if(!lastResult) return;
  navigator.clipboard.writeText(JSON.stringify(lastResult.data||lastResult, null, 2));
  showAlert('consultaStatus','JSON copiado!','ok');
}
function downloadResultado(){
  if(!lastResult) return;
  const blob = new Blob([JSON.stringify(lastResult.data||lastResult, null, 2)],{type:'application/json'});
  const a = document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download=`consulta-${document.getElementById('cTipo').value}-${Date.now()}.json`; a.click();
}

// ── Clientes ──
function toggleCustom(){
  document.getElementById('customLimiteGroup').style.display = document.getElementById('kPlano').value==='custom'?'flex':'none';
}
async function criarCliente(){
  const nome  = document.getElementById('kNome').value.trim();
  const plano = document.getElementById('kPlano').value;
  const limite= document.getElementById('kLimite').value||'-1';
  const proj  = document.getElementById('kProjetos').value.trim();
  if(!nome){alert('Informe o nome do cliente!');return;}
  const ak = getAdmin(); if(!ak) return;
  try{
    let url;
    if(plano==='custom'){
      url=`/admin/keys?nome=${encodeURIComponent(nome)}&limite=${limite}&projetos=${encodeURIComponent(proj)}`;
    } else {
      url=`/admin/keys/plano?cliente=${encodeURIComponent(nome)}&plano=${plano}&projetos=${encodeURIComponent(proj)}`;
    }
    const d = await fetch(url,{method:'POST',headers:{'X-API-Key':ak}}).then(r=>r.json());
    if(d.detail){ clearAdmin(); alert('Erro: '+d.detail); return; }
    const p = PLANOS_INFO[plano]||PLANOS_INFO.custom;
    document.getElementById('novaKeyResult').style.display='block';
    document.getElementById('novaKeyResult').innerHTML=`
      <div class="alert alert-ok">✅ Key criada para <b>${nome}</b> — Plano ${p.nome}</div>
      <div class="key-box" onclick="navigator.clipboard.writeText('${d.key}');this.style.color='var(--green)';this.textContent='✅ Copiado: ${d.key}';setTimeout(()=>{this.style.color='';this.textContent='${d.key}'},2000)">${d.key}</div>
      <div style="margin-top:10px;font-size:11px;color:#b0c8e0;line-height:2;font-family:'JetBrains Mono',monospace">
        <b>Envie ao cliente:</b><br>
        🔑 Key: <span style="color:var(--accent)">${d.key}</span><br>
        🌐 URL: <span style="color:var(--accent)">${BASE}/v1/consulta/{tipo}?query={valor}</span><br>
        📦 Plano: ${p.nome} — Limite: ${d.limite===-1?'Ilimitado':d.limite} consultas
      </div>`;
    loadClientes();
  }catch(e){clearAdmin();alert('Erro: '+e)}
}
async function loadClientes(){
  try{
    const d = await fetch('/api/keys/stats').then(r=>r.json());
    const counts={};
    d.keys.forEach(k=>{counts[k.plano]=(counts[k.plano]||0)+1;});
    document.getElementById('planosCards').innerHTML = Object.entries(PLANOS_INFO).map(([id,p])=>`
      <div class="card" style="border-top:2px solid ${p.cor}">
        <div class="card-label">${p.nome}</div>
        <div class="card-value" style="color:${p.cor}">${counts[id]||0}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:5px">${p.preco}</div>
        <div class="card-icon">👥</div>
      </div>`).join('');
    document.getElementById('clientesWrap').innerHTML = d.keys.length ? `
      <div class="table-wrap"><table>
        <thead><tr><th>Cliente</th><th>Plano</th><th>Key</th><th>Uso / Limite</th><th>Restante</th><th>Último uso</th><th>Status</th><th>Ações</th></tr></thead>
        <tbody>${d.keys.map(k=>{
          const p=PLANOS_INFO[k.plano]||PLANOS_INFO.custom;
          const pct=k.limite>0?Math.round(k.usado/k.limite*100):0;
          const barColor=pct>80?'var(--red)':pct>50?'var(--yellow)':'var(--green)';
          return `<tr>
            <td style="font-weight:600">${k.cliente}</td>
            <td><span class="tag" style="background:${p.cor}22;color:${p.cor};border:1px solid ${p.cor}44">${p.nome}</span></td>
            <td style="color:var(--muted);font-size:10px">${k.key}</td>
            <td>${k.usado}/${k.limite===-1?'∞':k.limite}
              <div class="prog"><div class="prog-fill" style="width:${pct}%;background:${barColor}"></div></div>
            </td>
            <td style="color:${pct>80?'var(--red)':'var(--green)'}">${k.restante}</td>
            <td style="color:var(--muted);font-size:10px">${k.ultimo_uso||'nunca'}</td>
            <td><span class="tag ${k.ativo?'tag-on':'tag-off'}">${k.ativo?'ATIVA':'INATIVA'}</span></td>
            <td><div style="display:flex;gap:4px">
              <button class="btn btn-a btn-sm" title="Copiar key" onclick="navigator.clipboard.writeText('${k.key_full}');this.textContent='✅';setTimeout(()=>this.textContent='📋',1200)">📋</button>
              <button class="btn btn-g btn-sm" title="Exportar JSON" onclick="exportarKey('${k.key_full}')">⬇️</button>
              <button class="btn btn-y btn-sm" title="Ver .env" onclick="verEnv('${k.key_full}','${k.cliente}','${k.plano}')">⚙️</button>
              <button class="btn btn-r btn-sm" title="${k.ativo?'Desativar':'Reativar'}" onclick="toggleKey('${k.key_full}',${k.ativo})">${k.ativo?'🔒':'🔓'}</button>
            </div></td>
          </tr>`;}).join('')}</tbody>
      </table></div>` : '<div class="empty">Nenhum cliente cadastrado ainda</div>';
  }catch(e){console.error(e)}
}
async function toggleKey(key, ativo){
  const ak=getAdmin(); if(!ak) return;
  const url    = ativo ? `/admin/keys/${key}` : `/admin/keys/${key}/reativar`;
  const method = ativo ? 'DELETE' : 'PATCH';
  try{
    const d = await fetch(url,{method,headers:{'X-API-Key':ak}}).then(r=>r.json());
    if(d.detail){clearAdmin();alert('Erro: '+d.detail);return;}
    loadClientes();
    loadKeys();
  }catch(e){clearAdmin();alert('Erro: '+e)}
}
async function exportarKey(key){
  const ak=getAdmin(); if(!ak) return;
  try{
    const d = await fetch(`/admin/export/key/${key}`,{headers:{'X-API-Key':ak}}).then(r=>r.json());
    if(d.detail){clearAdmin();alert('Erro: '+d.detail);return;}
    const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download=`unicontroller-${d.unicontroller.cliente}.json`; a.click();
  }catch(e){clearAdmin();alert('Erro: '+e)}
}
async function exportarTodas(){
  const ak=getAdmin(); if(!ak) return;
  try{
    const d = await fetch('/admin/export/all',{headers:{'X-API-Key':ak}}).then(r=>r.json());
    if(d.detail){clearAdmin();alert('Erro: '+d.detail);return;}
    const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download='unicontroller-todas-keys.json'; a.click();
  }catch(e){clearAdmin();alert('Erro: '+e)}
}
function verEnv(key, cliente, plano){
  const p = PLANOS_INFO[plano]||PLANOS_INFO.custom;
  const txt =
    `# Unicontroller — ${cliente} (${p.nome})\n` +
    `UNICONTROLLER_KEY=${key}\n` +
    `UNICONTROLLER_URL=${BASE}/v1/consulta\n\n` +
    `# Uso:\n# GET $(UNICONTROLLER_URL)/{tipo}?query={valor}\n` +
    `# Header: X-API-Key: $(UNICONTROLLER_KEY)`;
  abrirModal(`⚙️ .env — ${cliente}`, `
    <div class="key-box" style="white-space:pre;font-size:11px;color:#b0c8e0;overflow-x:auto">${txt}</div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="btn btn-a" style="flex:1" onclick="navigator.clipboard.writeText(document.querySelector('.modal .key-box').textContent);this.textContent='✅ Copiado!';setTimeout(()=>this.textContent='📋 Copiar .env',1500)">📋 Copiar .env</button>
    </div>`);
}

// ── Keys raw ──
async function criarKeyRaw(){
  const nome  = document.getElementById('aKNome').value.trim();
  const limite= document.getElementById('aKLimite').value||'-1';
  const proj  = document.getElementById('aKProjetos').value.trim();
  if(!nome){alert('Informe o nome!');return;}
  const ak=getAdmin(); if(!ak) return;
  try{
    const d = await fetch(`/admin/keys?nome=${encodeURIComponent(nome)}&limite=${limite}&projetos=${encodeURIComponent(proj)}`,{method:'POST',headers:{'X-API-Key':ak}}).then(r=>r.json());
    if(d.detail){clearAdmin();alert('Erro: '+d.detail);return;}
    document.getElementById('rawKeyResult').style.display='block';
    document.getElementById('rawKeyResult').innerHTML=`
      <div class="alert alert-ok">✅ Key criada: <b>${nome}</b></div>
      <div class="key-box" onclick="navigator.clipboard.writeText('${d.key}');this.style.color='var(--green)'">${d.key}</div>`;
    loadKeys();
  }catch(e){clearAdmin();alert('Erro: '+e)}
}
async function loadKeys(){
  try{
    const d = await fetch('/api/keys/stats').then(r=>r.json());
    document.getElementById('keysWrap').innerHTML = d.keys.length ? `
      <div class="table-wrap"><table>
        <thead><tr><th>Nome</th><th>Key</th><th>Plano</th><th>Uso</th><th>Limite</th><th>Criado</th><th>Status</th><th>Ações</th></tr></thead>
        <tbody>${d.keys.map(k=>`<tr>
          <td style="font-weight:600">${k.nome}</td>
          <td style="font-size:10px;color:var(--muted)">${k.key}</td>
          <td>${k.plano}</td>
          <td>${k.usado}</td>
          <td>${k.limite===-1?'∞':k.limite}</td>
          <td style="color:var(--muted);font-size:10px">${k.criado}</td>
          <td><span class="tag ${k.ativo?'tag-on':'tag-off'}">${k.ativo?'ATIVA':'INATIVA'}</span></td>
          <td><div style="display:flex;gap:4px">
            <button class="btn btn-a btn-sm" onclick="navigator.clipboard.writeText('${k.key_full}');this.textContent='✅';setTimeout(()=>this.textContent='📋',1200)">📋</button>
            <button class="btn btn-r btn-sm" onclick="toggleKey('${k.key_full}',${k.ativo})">${k.ativo?'🔒':'🔓'}</button>
          </div></td>
        </tr>`).join('')}</tbody>
      </table></div>` : '<div class="empty">Nenhuma key ainda</div>';
  }catch(e){console.error(e)}
}

// ── Docs ──
function buildDocs(){
  document.getElementById('docsBase').textContent = BASE;
  document.getElementById('docsExemplos').innerHTML = [
    {lang:'cURL',code:`curl -H "X-API-Key: uc_sua_chave" "${BASE}/v1/consulta/cpf?query=123.456.789-00"`},
    {lang:'JavaScript / Fetch',code:`const res = await fetch('${BASE}/v1/consulta/cep?query=01310-100', {\n  headers: { 'X-API-Key': 'uc_sua_chave' }\n});\nconst data = await res.json();\nconsole.log(data);`},
    {lang:'Python',code:`import requests\n\nresp = requests.get(\n    '${BASE}/v1/consulta/cnpj',\n    params={'query': '00.000.000/0001-00'},\n    headers={'X-API-Key': 'uc_sua_chave'}\n)\nprint(resp.json())`},
    {lang:'Node.js / Axios',code:`const axios = require('axios');\n\nconst { data } = await axios.get('${BASE}/v1/consulta/placa', {\n  params: { query: 'ABC1234' },\n  headers: { 'X-API-Key': 'uc_sua_chave' }\n});\nconsole.log(data);`},
    {lang:'.env (Railway / Vercel / Docker)',code:`UNICONTROLLER_KEY=uc_sua_chave\nUNICONTROLLER_URL=${BASE}/v1/consulta`},
    {lang:'GitHub Actions',code:`- name: Consulta CPF\n  run: |\n    curl -H "X-API-Key: ${{ secrets.UNICONTROLLER_KEY }}" \\\n    "${BASE}/v1/consulta/cpf?query=123.456.789-00"`},
  ].map(e=>`
    <div style="margin-bottom:14px">
      <div style="font-size:9px;color:var(--muted);margin-bottom:5px;letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace">${e.lang}</div>
      <div class="key-box" style="font-size:11px;white-space:pre;overflow-x:auto;color:#b0c8e0;cursor:default">${e.code}</div>
    </div>`).join('');
}

// ── Modal ──
function abrirModal(titulo, html){
  document.getElementById('modalTitulo').textContent = titulo;
  document.getElementById('modalConteudo').innerHTML = html;
  document.getElementById('modalBg').style.display = 'flex';
}
function fecharModal(){ document.getElementById('modalBg').style.display='none'; }

// ── Helpers ──
function showAlert(id, msg, tipo){
  const el=document.getElementById(id);
  el.style.display='block';
  el.className=`alert alert-${tipo==='ok'?'ok':'err'}`;
  el.textContent=msg;
  if(tipo==='ok') setTimeout(()=>el.style.display='none',3000);
}
function copiarTexto(el){
  navigator.clipboard.writeText(el.textContent.trim());
  const orig=el.style.color; el.style.color='var(--green)';
  setTimeout(()=>el.style.color=orig,1200);
}
function loadAll(){ loadStats(); }


// ═══ INIT ═══
document.addEventListener("DOMContentLoaded", function(){
  // Bind tab clicks via event delegation (avoid inline onclick escaping issues)
  document.querySelectorAll(".tab[data-tab]").forEach(function(btn){
    btn.addEventListener("click", function(){
      goTab(this.getAttribute("data-tab"));
    });
  });
  loadStats();
  setInterval(loadStats, 15000);
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=False)
