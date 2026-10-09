"""
Modulo de Integracao com Telegram (Telethon - Conta de Usuario) e Google Gemini para o Projeto Onda.

Por que usamos Telethon (Conta de Usuario):
- Em grupos do Telegram, a API oficial de bots (BotFather) NÃO consegue ler
  mensagens enviadas por OUTROS BOTS e exige que o bot seja administrador.
- Conectando através do Telethon como cliente de usuário, o script tem visão
  completa de 100% das mensagens do grupo (incluindo mensagens postadas por outros bots),
  sem precisar ser administrador do grupo!

Como funciona:
1. Na primeira vez, o usuario roda 'python autenticar_telegram.py' para autenticar uma unica vez.
2. Quando o servidor Projeto Onda inicia, este modulo carrega a sessao 'onda_user.session'
   e fica escutando o grupo configurado em segundo plano.
3. Quando o outro bot postar a solicitacao, o Google Gemini (Gemini 3.5 Flash)
   analisa o texto e converte em JSON estruturado com os horarios no fuso correto (America/Cuiaba UTC-4).
4. O sistema checa se a escola ja possui IP e senha cadastrados no banco de dados:
   - SE JA POSSUI IP: O job de download e criado imediatamente e o trabalhador
     do Projeto Onda comeca a baixar sozinho!
   - SE NAO POSSUI IP: A solicitacao e cadastrada na tabela "solicitacoes_telegram"
     com o status "aguardando_ip" para que o operador informe o IP na aba web.
     Assim que o operador insere o IP, o sistema ja salva o IP no cadastro da escola
     (para as proximas vezes ser 100% automatico) e inicia o download.
"""

import os
import sys
import json
import time
import sqlite3
import threading
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

from telethon import TelegramClient, events

# ---------------------------------------------------------------------------
# Carregador simples de .env (dispensa dependencias externas)
# ---------------------------------------------------------------------------
def carregar_env(caminho_env=None):
    if not caminho_env:
        caminho_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(caminho_env):
        try:
            try:
                from dotenv import load_dotenv
                load_dotenv(caminho_env, override=True)
            except ImportError:
                pass
            with open(caminho_env, "r", encoding="utf-8-sig") as f:
                for linha in f:
                    linha = linha.strip()
                    if not linha or linha.startswith("#") or "=" not in linha:
                        continue
                    chave, valor = linha.split("=", 1)
                    chave = chave.lstrip("\ufeff").strip()
                    valor = valor.strip().strip("'\"")
                    if chave:
                        os.environ[chave] = valor
        except Exception as e:
            print(f"[TelegramBot] Aviso ao carregar .env: {e}")

carregar_env()

# Variaveis de configuracao
API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
TELEGRAM_GROUP_ID = os.environ.get("TELEGRAM_GROUP_ID", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()
BANCO_PATH = os.environ.get("DVR_APP_BANCO", "dvr_app.db")
DVR_USUARIO_PADRAO = os.environ.get("DVR_USUARIO_PADRAO", "admin").strip()
DVR_SENHA_PADRAO = os.environ.get("DVR_SENHA_PADRAO", "").strip()
DVR_PORTA_SDK_PADRAO = int(os.environ.get("DVR_PORTA_SDK_PADRAO", "8000"))

NOME_FUSO = os.environ.get("DVR_TIMEZONE", "America/Cuiaba")
try:
    FUSO_EMPRESA = ZoneInfo(NOME_FUSO)
except Exception:
    FUSO_EMPRESA = ZoneInfo("America/Cuiaba")

db_lock = threading.Lock()
bot_thread = None
bot_rodando = False
ultimo_erro_bot = None
cliente_telethon = None
info_usuario_conectado = {}

PASTA_RAIZ = os.path.dirname(os.path.abspath(__file__))
CAMINHO_SESSAO = os.path.join(PASTA_RAIZ, "onda_user")


def conectar_db():
    conn = sqlite3.connect(BANCO_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def extrair_dados_gemini(texto_mensagem, solicitante=""):
    """
    Envia a mensagem informal para a API do Google Gemini com prompt estruturado
    e exige resposta em JSON rigoroso contendo as datas no fuso horario local.
    """
    if not GEMINI_API_KEY:
        raise ValueError("Chave da API do Gemini (GEMINI_API_KEY) nao configurada no .env.")

    agora = datetime.now(FUSO_EMPRESA)
    data_hoje_str = agora.strftime("%d/%m/%Y (%A)")
    hora_agora_str = agora.strftime("%H:%M")

    prompt = f"""
Voce eh um assistente inteligente do sistema de gravacoes CFTV de escolas.
Sua funcao eh analisar uma mensagem de texto enviada por um diretor ou operador (ou por um bot de pedidos) e extrair as informacoes para download de gravacao.

CONTEXTO TEMPORAL IMPORTANTE:
- Hoje eh: {data_hoje_str}
- Hora atual local: {hora_agora_str}
- Fuso horario de referencia: {NOME_FUSO} (UTC-4)

Instrucoes de extracao:
1. Verifique se o texto eh realmente um pedido de gravacao de cameras ou videos. Se for apenas saudacao, aviso ou conversa geral sem pedido de gravacao, defina 'eh_solicitacao': false.
2. 'codigo_escola': O codigo numerico da escola (ex: '3461', '3348', '3140'). Se nao tiver codigo expresso, deixe string vazia "".
3. 'nome_escola': O nome da escola mencionado (ex: 'EE Edson Bezerra', 'Antonio Joao Ribeiro'). Se nao tiver, deixe string vazia "".
4. 'cameras': As cameras solicitadas separadas por virgula (ex: '1,2' ou '3'). Se pedir 'todas' ou nao especificar o numero da camera, retorne '1'.
5. 'data_inicio': Data e hora inicial solicitada no formato ISO 8601 com offset local (ex: 'YYYY-MM-DDTHH:MM:SS-04:00'). Converta expressoes como 'ontem', 'hoje', 'tarde', 'meio dia' com base na data de hoje ({agora.strftime("%Y-%m-%d")}).
6. 'data_fim': Data e hora final solicitada no formato ISO 8601 com offset local (ex: 'YYYY-MM-DDTHH:MM:SS-04:00'). Se o solicitante informar apenas um intervalo de tempo (ex: 'das 14h as 15h'), data_fim sera 15:00 do mesmo dia. Se informar apenas 'as 14h', faca um intervalo razoavel (ex: 14:00 ate 15:00).
7. 'resumo': Um resumo conciso e claro em portugues do que foi solicitado.

Retorne APENAS um objeto JSON com o seguinte formato:
{{
  "eh_solicitacao": true ou false,
  "codigo_escola": "...",
  "nome_escola": "...",
  "cameras": "1",
  "data_inicio": "2026-10-08T14:00:00-04:00",
  "data_fim": "2026-10-08T15:30:00-04:00",
  "resumo": "..."
}}

Mensagem recebida de {solicitante}:
\"\"\"{texto_mensagem}\"\"\"
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1
        }
    }

    resp = requests.post(url, json=payload, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"Erro na API do Gemini ({resp.status_code}): {resp.text}")

    dados_resp = resp.json()
    partes = dados_resp.get("candidates", [])[0].get("content", {}).get("parts", [])
    if not partes:
        raise ValueError("Resposta do Gemini vazia.")

    texto_json = partes[0].get("text", "").strip()
    return json.loads(texto_json)


def buscar_dados_escola(conn, codigo="", nome=""):
    """Localiza a escola no banco por codigo ou por aproximacao do nome."""
    codigo = str(codigo).strip()
    nome = str(nome).strip()

    if codigo:
        linha = conn.execute(
            "SELECT codigo, nome, dvr_ip, cidade, dvr_usuario, dvr_senha FROM escolas WHERE codigo=?",
            (codigo,)
        ).fetchone()
        if linha:
            return {
                "codigo": linha[0],
                "nome": linha[1],
                "dvr_ip": linha[2] or "",
                "cidade": linha[3] or "",
                "dvr_usuario": linha[4] or DVR_USUARIO_PADRAO,
                "dvr_senha": linha[5] or DVR_SENHA_PADRAO
            }

    if nome:
        termo = f"%{nome}%"
        linha = conn.execute(
            "SELECT codigo, nome, dvr_ip, cidade, dvr_usuario, dvr_senha FROM escolas WHERE nome LIKE ? ORDER BY LENGTH(nome) ASC LIMIT 1",
            (termo,)
        ).fetchone()
        if linha:
            return {
                "codigo": linha[0],
                "nome": linha[1],
                "dvr_ip": linha[2] or "",
                "cidade": linha[3] or "",
                "dvr_usuario": linha[4] or DVR_USUARIO_PADRAO,
                "dvr_senha": linha[5] or DVR_SENHA_PADRAO
            }

    return None


def criar_job_automatico(conn, dvr_ip, usuario, senha, cameras, data_inicio, data_fim, escola, codigo_escola):
    """Cria o job de download na tabela jobs exatamente como o painel web faz."""
    try:
        inicio = datetime.fromisoformat(data_inicio.replace("Z", "+00:00"))
        fim = datetime.fromisoformat(data_fim.replace("Z", "+00:00"))
        iso_inicio = inicio.isoformat()
        iso_fim = fim.isoformat()
    except Exception:
        iso_inicio = data_inicio
        iso_fim = data_fim

    cursor = conn.execute("""
        INSERT INTO jobs (
            dvr_ip, usuario, senha, cameras, data_inicio, data_fim,
            escola, codigo_escola, modo_download, porta_sdk, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, 'novo')
    """, (
        dvr_ip, usuario, senha, cameras, iso_inicio, iso_fim,
        escola, codigo_escola, DVR_PORTA_SDK_PADRAO
    ))
    return cursor.lastrowid


def processar_texto_recebido(msg_id, texto, solicitante):
    """Processa o texto da mensagem com Gemini e atualiza banco/inicia job."""
    if not texto:
        return

    conn = conectar_db()
    with db_lock:
        try:
            ja_existe = conn.execute(
                "SELECT id FROM solicitacoes_telegram WHERE telegram_message_id=?", (msg_id,)
            ).fetchone()
            if ja_existe:
                return

            try:
                dados_gemini = extrair_dados_gemini(texto, solicitante)
            except Exception as e:
                print(f"[TelegramBot] Erro ao chamar Gemini para mensagem {msg_id}: {e}")
                conn.execute("""
                    INSERT INTO solicitacoes_telegram (
                        telegram_message_id, solicitante, mensagem_original,
                        status, erro_motivo, recebido_em
                    ) VALUES (?, ?, ?, 'erro_leitura', ?, CURRENT_TIMESTAMP)
                """, (msg_id, solicitante, texto, str(e)))
                conn.commit()
                return

            if not dados_gemini.get("eh_solicitacao"):
                return

            cod_extraido = str(dados_gemini.get("codigo_escola", "")).strip()
            nome_extraido = str(dados_gemini.get("nome_escola", "")).strip()
            cameras = str(dados_gemini.get("cameras", "1")).strip() or "1"
            data_ini = str(dados_gemini.get("data_inicio", "")).strip()
            data_fim = str(dados_gemini.get("data_fim", "")).strip()

            escola_encontrada = buscar_dados_escola(conn, cod_extraido, nome_extraido)
            codigo_final = cod_extraido
            nome_final = nome_extraido
            ip_final = ""
            usuario_final = DVR_USUARIO_PADRAO
            senha_final = DVR_SENHA_PADRAO

            if escola_encontrada:
                codigo_final = escola_encontrada["codigo"]
                nome_final = escola_encontrada["nome"]
                ip_final = escola_encontrada["dvr_ip"]
                usuario_final = escola_encontrada["dvr_usuario"] or DVR_USUARIO_PADRAO
                senha_final = escola_encontrada["dvr_senha"] or DVR_SENHA_PADRAO

            # Cenário 1: Escola tem IP -> Dispara download na hora!
            if ip_final:
                novo_job_id = criar_job_automatico(
                    conn, ip_final, usuario_final, senha_final, cameras,
                    data_ini, data_fim, nome_final, codigo_final
                )
                conn.execute("""
                    INSERT INTO solicitacoes_telegram (
                        telegram_message_id, solicitante, mensagem_original,
                        codigo_escola, nome_escola, cameras, data_inicio, data_fim,
                        dvr_ip, status, job_id, recebido_em, processado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'baixando', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (
                    msg_id, solicitante, texto, codigo_final, nome_final,
                    cameras, data_ini, data_fim, ip_final, novo_job_id
                ))
                conn.commit()
                print(f"[TelegramBot] ✅ Job #{novo_job_id} criado automaticamente para {nome_final} (IP: {ip_final})!")

            # Cenário 2: Não possui IP cadastrado -> Aguarda na aba web
            else:
                cursor = conn.execute("""
                    INSERT INTO solicitacoes_telegram (
                        telegram_message_id, solicitante, mensagem_original,
                        codigo_escola, nome_escola, cameras, data_inicio, data_fim,
                        dvr_ip, status, recebido_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 'aguardando_ip', CURRENT_TIMESTAMP)
                """, (
                    msg_id, solicitante, texto, codigo_final, nome_final,
                    cameras, data_ini, data_fim
                ))
                solic_id = cursor.lastrowid
                conn.commit()
                print(f"[TelegramBot] 📋 Solicitacao #{solic_id} registrada para {nome_final or codigo_final} (Aguardando IP na aba web).")

        except Exception as e:
            print(f"[TelegramBot] Erro ao processar mensagem do Telegram: {e}")
        finally:
            conn.close()


def loop_telethon():
    """Roda o loop de eventos assincrono do Telethon em segundo plano."""
    global bot_rodando, ultimo_erro_bot, info_usuario_conectado, cliente_telethon

    arquivo_session_real = f"{CAMINHO_SESSAO}.session"
    if not os.path.isfile(arquivo_session_real):
        msg_aviso = "Sessao nao autenticada. Execute 'python autenticar_telegram.py' uma vez no terminal para logar."
        print(f"[TelegramBot] ⚠️ {msg_aviso}")
        ultimo_erro_bot = msg_aviso
        bot_rodando = False
        return

    try:
        api_id_int = int(API_ID)
    except Exception:
        ultimo_erro_bot = "TELEGRAM_API_ID invalido no .env"
        print(f"[TelegramBot] Erro: {ultimo_erro_bot}")
        bot_rodando = False
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def main_coro():
        global bot_rodando, ultimo_erro_bot, info_usuario_conectado, cliente_telethon
        print("[TelegramBot] Conectando ao Telegram via Telethon...")
        client = TelegramClient(CAMINHO_SESSAO, api_id_int, API_HASH)
        cliente_telethon = client

        await client.connect()
        if not await client.is_user_authorized():
            ultimo_erro_bot = "Conta nao autorizada. Execute 'python autenticar_telegram.py' para autenticar."
            print(f"[TelegramBot] ❌ {ultimo_erro_bot}")
            bot_rodando = False
            return

        me = await client.get_me()
        info_usuario_conectado = {
            "id": me.id,
            "nome": f"{me.first_name} {me.last_name or ''}".strip(),
            "username": me.username or ""
        }
        bot_rodando = True
        ultimo_erro_bot = None
        print(f"[TelegramBot] ✅ Conectado como {info_usuario_conectado['nome']} (@{info_usuario_conectado['username']})!")
        print(f"[TelegramBot] 🎧 Escutando mensagens do grupo {TELEGRAM_GROUP_ID} (incluindo postagens de outros bots)...")

        # Configura o chat alvo (grupo)
        alvo_chat = None
        if TELEGRAM_GROUP_ID:
            try:
                alvo_chat = int(TELEGRAM_GROUP_ID)
            except ValueError:
                alvo_chat = TELEGRAM_GROUP_ID

        @client.on(events.NewMessage(chats=alvo_chat if alvo_chat else None))
        async def manipulador_mensagem(event):
            try:
                msg = event.message
                texto = msg.message or ""
                if not texto:
                    return

                sender = await event.get_sender()
                solic_nome = getattr(sender, "first_name", "") or getattr(sender, "title", "") or "Usuario"
                if getattr(sender, "username", None):
                    solic_nome = f"@{sender.username}"
                if getattr(sender, "bot", False):
                    solic_nome += " [Bot]"

                # Executa o processamento
                processar_texto_recebido(msg.id, texto, solic_nome)
            except Exception as e:
                print(f"[TelegramBot] Erro ao manipular nova mensagem: {e}")

        # Mantem o cliente rodando
        await client.run_until_disconnected()

    try:
        loop.run_until_complete(main_coro())
    except Exception as e:
        ultimo_erro_bot = str(e)
        print(f"[TelegramBot] Excecao no loop Telethon: {e}")
    finally:
        bot_rodando = False


def iniciar_servico_telegram():
    """Inicia a thread de escuta do Telethon em segundo plano."""
    global bot_thread, bot_rodando
    if not API_ID or not API_HASH:
        print("[TelegramBot] TELEGRAM_API_ID ou TELEGRAM_API_HASH nao configurados no .env.")
        return False

    if bot_thread and bot_thread.is_alive():
        return True

    bot_thread = threading.Thread(target=loop_telethon, daemon=True, name="TelethonUserWorker")
    bot_thread.start()
    return True


def obter_status():
    """Retorna o estado atual da integracao para a interface web."""
    arquivo_sessao = os.path.isfile(f"{CAMINHO_SESSAO}.session")
    return {
        "ativo": bot_rodando,
        "tipo": "Telethon (Conta de Usuario)",
        "sessao_autenticada": arquivo_sessao,
        "usuario_conectado": info_usuario_conectado.get("nome", ""),
        "username": info_usuario_conectado.get("username", ""),
        "grupo_id": TELEGRAM_GROUP_ID,
        "modelo_gemini": GEMINI_MODEL,
        "ultimo_erro": ultimo_erro_bot
    }
