"""
Script de Autenticacao Inicial do Telegram (Telethon) para o Projeto Onda.

Execute este script no terminal UMA UNICA VEZ para autenticar sua conta de usuario:
    python autenticar_telegram.py

Ele vai solicitar:
1. Seu numero de telefone com o codigo do pais (ex: +5567999998888)
2. O codigo de confirmacao que chega no seu aplicativo do Telegram (na conversa oficial "Telegram")
3. Sua senha de duas etapas (caso tenha configurado no Telegram)

Apos fazer login com sucesso, ele salva o arquivo 'onda_user.session' localmente.
Nas proximas vezes em que o servidor Projeto Onda for iniciado, ele entrara automaticamente
sem pedir nenhuma informacao!
"""

import os
import sys
from telethon.sync import TelegramClient

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
                k, v = linha.split("=", 1)
                k = k.lstrip("\ufeff").strip()
                v = v.strip().strip("'\"")
                if k:
                    os.environ[k] = v
    except Exception as e:
        print(f"Aviso ao carregar .env: {e}")

API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()

if not API_ID or not API_HASH:
    print("ERRO: TELEGRAM_API_ID ou TELEGRAM_API_HASH nao estao preenchidos no arquivo .env!")
    sys.exit(1)

try:
    API_ID_INT = int(API_ID)
except ValueError:
    print(f"ERRO: TELEGRAM_API_ID deve ser um numero. Valor atual: {API_ID}")
    sys.exit(1)

pasta_raiz = os.path.dirname(os.path.abspath(__file__))
caminho_sessao = os.path.join(pasta_raiz, "onda_user")

print("=" * 70)
print("AUTENTICACAO DO TELEGRAM - PROJETO ONDA")
print("=" * 70)
print(f"Usando App API ID: {API_ID}")
print("Iniciando conexao com os servidores do Telegram...")
print()

client = TelegramClient(caminho_sessao, API_ID_INT, API_HASH)

try:
    client.start()
    me = client.get_me()
    print()
    print("=" * 70)
    print("✅ SUCESSO! Login realizado com sucesso!")
    print(f"Conectado como: {me.first_name} {me.last_name or ''} (@{me.username or 'sem username'})")
    print(f"ID da Conta: {me.id}")
    print(f"Arquivo de sessao salvo em: {caminho_sessao}.session")
    print("=" * 70)
    print("Agora voce ja pode iniciar o servidor Projeto Onda normalmente!")
    print("O script vai escutar o grupo em segundo plano lendo mensagens de outros bots.")
    print("=" * 70)
except Exception as e:
    print()
    print(f"❌ Erro durante a autenticacao: {e}")
finally:
    client.disconnect()
