# CheckData Bot 🔍

API REST + Bot do Telegram para consultas CheckData (CPF, CNPJ, CEP, placa e mais).

## Stack
- **FastAPI** — API REST com autenticação
- **python-telegram-bot** — Bot com comandos e botões
- **Railway** — Deploy gratuito com domínio HTTPS
- **GitHub** — Controle de versão + deploy automático

---

## 🚀 Deploy em 5 passos

### 1. Criar o bot no Telegram
1. Acesse [@BotFather](https://t.me/BotFather) no Telegram
2. Envie `/newbot`
3. Escolha um nome e username para o bot
4. Copie o **token** gerado

### 2. Subir no GitHub
```bash
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/checkdata-bot.git
git push -u origin main
```

### 3. Criar projeto no Railway
1. Acesse [railway.app](https://railway.app) e faça login com GitHub
2. Clique em **New Project → Deploy from GitHub repo**
3. Selecione o repositório `checkdata-bot`
4. Railway detecta automaticamente o Python e faz o deploy

### 4. Configurar variáveis de ambiente no Railway
No painel do Railway, vá em **Variables** e adicione:

| Variável | Valor |
|---|---|
| `CHECKDATA_TOKEN` | `unicontroller-api-completa` |
| `TELEGRAM_BOT_TOKEN` | token do BotFather |
| `API_SECRET` | uma chave sua (ex: `minha-api-123`) |
| `WEBHOOK_URL` | URL do Railway (ex: `https://checkdata-bot.up.railway.app`) |

### 5. Pronto! 🎉
Após o deploy, acesse:
- **Documentação da API**: `https://seu-app.up.railway.app/docs`
- **Bot no Telegram**: abra o bot e envie `/start`

---

## 📡 Endpoints da API

### Autenticação
Todas as rotas protegidas exigem o header:
```
X-API-Key: sua-chave-secreta
```

### Rotas

| Método | Rota | Descrição |
|---|---|---|
| GET | `/` | Status da API |
| GET | `/endpoints` | Lista todos os tipos disponíveis |
| GET | `/consulta/{tipo}?query=valor` | Consulta autenticada |
| GET | `/consulta/{tipo}/raw?query=valor` | Consulta pública (sem auth) |

### Tipos disponíveis
`cpf` `cns` `cep` `cnpj` `nome` `email` `telefone` `vizinhos` `placa` `proprietario` `mae` `pai` `titulo`

### Exemplos
```bash
# CPF
curl -H "X-API-Key: minha-api-123" \
  "https://seu-app.up.railway.app/consulta/cpf?query=123.456.789-00"

# CEP
curl -H "X-API-Key: minha-api-123" \
  "https://seu-app.up.railway.app/consulta/cep?query=01310-100"

# Placa
curl -H "X-API-Key: minha-api-123" \
  "https://seu-app.up.railway.app/consulta/placa?query=ABC1234"
```

---

## 🤖 Comandos do Bot

| Comando | Descrição |
|---|---|
| `/start` | Boas-vindas e instruções |
| `/menu` | Menu com botões interativos |
| `/help` | Lista todos os comandos |
| `/cpf 000.000.000-00` | Consulta direta por CPF |
| `/cnpj 00.000.000/0001-00` | Consulta direta por CNPJ |
| `/cep 01310-100` | Consulta direta por CEP |
| `/placa ABC1234` | Consulta direta por placa |
| *(todos os outros tipos também)* | |

---

## 🛠️ Rodar localmente

```bash
# Instalar dependências
pip install -r requirements.txt

# Copiar e editar variáveis
cp .env.example .env
# edite o .env com seus tokens

# Rodar
python -m uvicorn app.main:api --reload --port 8000
```

Acesse `http://localhost:8000/docs` para ver a documentação interativa.
