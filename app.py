"""
App de download de gravações de DVR (Hikvision / JFL via ISAPI).

Como está organizado:
  - Um banco SQLite guarda dois tipos de coisa:
      * "jobs"      -> os pedidos que você cria pelo site (qual DVR, quais
                       câmeras, qual período).
      * "gravacoes" -> cada trecho de vídeo que a DVR devolveu na busca,
                       um por linha, com o status de download dele.
  - Uma thread em segundo plano ("trabalhador") fica rodando pra sempre,
    processando os jobs pendentes: busca as gravações na DVR, cataloga no
    banco e baixa uma por uma, com retomada quando dá pra retomar e espera
    automática quando a DVR/rede cai.
  - O Flask só serve o site (HTML) e uma API simples (JSON) para o site
    conversar com o banco. Ele NÃO baixa nada sozinho: quem baixa é a
    thread do trabalhador, que roda independente de alguém estar com o
    site aberto ou não.

Rodar:
    pip install flask requests
    python app.py
Depois abra http://localhost:5000 (ou http://IP_DO_SERVIDOR:5000 de outro PC
da rede).
"""

import os
import sys
import shutil
import subprocess
import time
import socket
import sqlite3
import threading
import uuid
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
from xml.sax.saxutils import escape
import json
import re
import xml.etree.ElementTree as ET

# Garante que gi/GStreamer possam ser importados pelo ambiente virtual (.venv)
CAMINHO_DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if os.path.isdir(CAMINHO_DIST_PACKAGES) and CAMINHO_DIST_PACKAGES not in sys.path:
    sys.path.append(CAMINHO_DIST_PACKAGES)

import requests
from requests.auth import HTTPDigestAuth
from flask import Flask, jsonify, request, send_file, send_from_directory, url_for

import hikvision_sdk
import telegram_bot

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

# Pasta onde os vídeos baixados são organizados. Como o app roda na mesma
# máquina do servidor de pastas, "enviar pro servidor" aqui é simplesmente
# salvar direto no caminho da pasta compartilhada (nem precisa copiar por
# rede). Ajuste para o caminho real da sua pasta compartilhada.
PASTA_DESTINO = os.environ.get("DVR_APP_DESTINO", "gravacoes")

# Retenção de gravações em disco
RETENCAO_DIAS = int(os.environ.get("DVR_RETENCAO_DIAS", "30"))
RETENCAO_AUTO = os.environ.get("DVR_RETENCAO_AUTO", "0") == "1"

BANCO_PATH = os.environ.get("DVR_APP_BANCO", "dvr_app.db")

# Intervalo entre "rodadas" do trabalhador: a cada X segundos ele olha o
# banco de novo, pega jobs novos e continua baixando pendentes.
INTERVALO_CICLO_SEGUNDOS = 5

# Quando a DVR fica inacessível no meio de um download (queda de energia/rede):
# - Nos primeiros 60 segundos, tenta reconectar rápido a cada 5 segundos.
# - Após 60 segundos fora do ar, passa a tentar a cada 60 segundos (1 minuto)
#   sem parar até a energia/internet da escola voltar ou o usuário cancelar!
INTERVALO_RETRY_SEGUNDOS = 5
INTERVALO_RETRY_LONGO_SEGUNDOS = 60
TEMPO_FASE_RAPIDA_SEGUNDOS = 60

# Quantidade máxima de pedidos (jobs) de DVRs diferentes baixando simultaneamente.
# Permite baixar 2 ou 3 escolas ao mesmo tempo sem um download travar a fila do outro!
MAX_JOBS_SIMULTANEOS = int(os.environ.get("DVR_MAX_WORKERS", "5"))

# Fuso horario da regiao onde estao as DVRs (Cuiaba/Campo Grande = America/Cuiaba UTC-4)
NOME_FUSO = os.environ.get("DVR_TIMEZONE", "America/Cuiaba")
try:
    FUSO_EMPRESA = ZoneInfo(NOME_FUSO)
except Exception:
    FUSO_EMPRESA = ZoneInfo("America/Cuiaba")


class DownloadCancelado(Exception):
    """Interrompe uma transferência sem transformar cancelamento em erro."""


def descrever_erro(excecao_ou_texto):
    """
    Padroniza mensagens de erro no formato: 'Causa: ... | Sugestão: ...'
    Traduz exceções comuns de rede, autenticação, DVR e processamento de vídeo para português claro.
    """
    if not excecao_ou_texto:
        return None

    texto = str(excecao_ou_texto).strip()
    if not texto:
        return None

    if texto.startswith("Causa:") and " | Sugestão:" in texto:
        return texto

    texto_lower = texto.lower()

    # 1. Limite de conexões simultâneas da DVR (OVER_MAXLINK)
    if (
        "over_maxlink" in texto_lower
        or "over maxlink" in texto_lower
        or "limite maximo de conexoes" in texto_lower
        or "limite máximo de conexões" in texto_lower
    ):
        return (
            "Causa: Limite máximo de conexões simultâneas atingido na DVR (OVER_MAXLINK) | "
            "Sugestão: Aguarde alguns instantes antes de tentar novamente ou desconecte outros acessos ativos no equipamento."
        )

    # 2. Câmera ou canal inválido/inexistente
    if (
        isinstance(excecao_ou_texto, getattr(hikvision_sdk, "CameraInvalidaError", ()))
        or "badtrackid" in texto_lower
        or "invalidtrack" in texto_lower
        or ("câmera" in texto_lower and ("inválida" in texto_lower or "inexistente" in texto_lower))
        or ("camera" in texto_lower and ("invalida" in texto_lower or "inexistente" in texto_lower))
        or "net_dvr_channel_error" in texto_lower
        or "canal incorreto" in texto_lower
    ):
        return (
            f"Causa: Canal ou câmera inexistente ou inválida na DVR ({texto}) | "
            "Sugestão: Verifique o número correto das câmeras disponíveis no equipamento ou catálogo de escolas."
        )

    # 3. Login / senha inválidos (401 / NET_DVR_PASSWORD_ERROR / etc.)
    if (
        "401" in texto_lower
        or "unauthorized" in texto_lower
        or "password_error" in texto_lower
        or "usuario ou senha incorretos" in texto_lower
        or "falha de autenticacao" in texto_lower
        or "falha de autenticação" in texto_lower
        or "user_not_succ_login" in texto_lower
        or "permissao insuficiente" in texto_lower
        or "permissão insuficiente" in texto_lower
        or "noenoughpri" in texto_lower
    ):
        return (
            "Causa: Usuário ou senha inválidos na DVR (401 / Autenticação rejeitada) | "
            "Sugestão: Verifique as credenciais de acesso cadastradas para esta unidade ou se a porta está liberada."
        )

    # 4. Sem gravações no período
    if (
        "nenhuma gravacao encontrada" in texto_lower
        or "nenhuma gravação encontrada" in texto_lower
        or "sem_gravacoes" in texto_lower
        or "no_recordfile" in texto_lower
        or "nao encontrou gravacoes" in texto_lower
        or "não encontrou gravações" in texto_lower
    ):
        return (
            "Causa: Nenhuma gravação encontrada no período informado | "
            "Sugestão: Verifique se a DVR estava gravando no horário solicitado ou amplie o intervalo de busca."
        )

    # 5. Disco cheio (local ou na DVR)
    if (
        "no space left on device" in texto_lower
        or "espaço insuficiente" in texto_lower
        or "espaco insuficiente" in texto_lower
        or "disk full" in texto_lower
        or "disco cheio" in texto_lower
        or "net_dvr_disk_error" in texto_lower
    ):
        return (
            "Causa: Espaço em disco insuficiente no servidor de armazenamento | "
            "Sugestão: Libere espaço na partição de disco onde as gravações são salvas."
        )

    # 6. Falha do FFmpeg / conversão
    if (
        "ffmpeg" in texto_lower
        or "gstreamer" in texto_lower
        or "converter/cortar" in texto_lower
        or "converter_e_cortar" in texto_lower
        or "falha ao converter" in texto_lower
        or "falhou ao converter" in texto_lower
        or "arquivo recebido muito pequeno" in texto_lower
        or "verificacao final falhou" in texto_lower
        or "verificação final falhou" in texto_lower
    ):
        trecho_resumo = texto[-140:] if len(texto) > 140 else texto
        return (
            f"Causa: Falha durante o processamento ou corte do vídeo via FFmpeg ({trecho_resumo}) | "
            "Sugestão: Verifique a integridade do arquivo bruto baixado e a instalação do FFmpeg no servidor."
        )

    # 7. SDK indisponível (fallback ISAPI)
    if (
        "sdk indisponivel" in texto_lower
        or "sdk indisponível" in texto_lower
        or "hcnetsdk" in texto_lower
        or "net_dvr_noinit" in texto_lower
    ):
        return (
            "Causa: SDK Hikvision nativo indisponível no ambiente operacional | "
            "Sugestão: O sistema utilizará o protocolo ISAPI (porta 80 HTTP) como alternativa automática."
        )

    # 8. Timeout
    if (
        isinstance(excecao_ou_texto, (requests.exceptions.Timeout, socket.timeout))
        or "timed out" in texto_lower
        or "timeout" in texto_lower
        or "recv_timeout" in texto_lower
        or "commandtimeout" in texto_lower
    ):
        return (
            "Causa: Tempo limite de comunicação com a DVR esgotado (Timeout) | "
            "Sugestão: A conexão de rede com a DVR pode estar congestionada ou oscilando; tente novamente em instantes."
        )

    # 9. DVR inacessível / offline / erro de rede
    if (
        isinstance(excecao_ou_texto, (requests.exceptions.ConnectionError, socket.error))
        or "connection refused" in texto_lower
        or "failed to establish a new connection" in texto_lower
        or "network_fail_connect" in texto_lower
        or "host is unreachable" in texto_lower
        or "no route to host" in texto_lower
        or "network is unreachable" in texto_lower
        or "inacessivel" in texto_lower
        or "inacessível" in texto_lower
        or "sem resposta" in texto_lower
        or "queda de conexao" in texto_lower
        or "queda de conexão" in texto_lower
        or "dispositivo offline" in texto_lower
        or "offline" in texto_lower
    ):
        return (
            "Causa: DVR/NVR inacessível ou offline na rede | "
            "Sugestão: Verifique se a unidade escolar está com energia e conexão de rede ativas e se o IP/porta estão liberados no firewall."
        )

    # 10. Trechos não puderam ser baixados
    if "nao puderam ser baixados" in texto_lower or "não puderam ser baixados" in texto_lower:
        return (
            "Causa: Um ou mais trechos de gravação não puderam ser baixados da DVR | "
            "Sugestão: Verifique a estabilidade da conexão da escola e solicite novamente o download dos trechos pendentes."
        )

    # Fallback genérico
    return f"Causa: {texto} | Sugestão: Verifique os parâmetros informados, a conectividade com a DVR e os logs do sistema."


# ---------------------------------------------------------------------------
# Banco de dados e Sincronização de Concorrência
# ---------------------------------------------------------------------------

def conectar_banco():
    # check_same_thread=False porque o Flask e as threads do trabalhador
    # acessam o mesmo arquivo de banco.
    # timeout=30.0 evita erros de 'database is locked' quando o bot do Telegram
    # e o servidor web gravam simultaneamente.
    conexao = sqlite3.connect(BANCO_PATH, timeout=30.0, check_same_thread=False)
    conexao.execute("PRAGMA journal_mode=WAL")  # deixa leitura/escrita concorrente mais segura
    conexao.execute("PRAGMA synchronous=NORMAL")
    conexao.execute("PRAGMA busy_timeout=30000")
    return conexao


banco = conectar_banco()
# Um "cadeado" para garantir que duas threads nunca escrevam no banco ao
# mesmo tempo. SQLite não gosta disso, e o cadeado evita erros esquisitos.
banco_lock = threading.Lock()

# Controle de jobs ativos, cancelados e locks individuais por endereço IP de DVR:
# - Permite que DVRs de escolas diferentes baixem em paralelo ao mesmo tempo.
# - Garante que se houver 2 pedidos para a MESMA DVR, o segundo aguarde o primeiro
#   terminar naquela DVR, evitando erro de limite de conexões (NET_DVR_OVER_MAXLINK).
jobs_ativos_lock = threading.Lock()
jobs_em_execucao = set()
jobs_cancelados = set()
locks_por_dvr = {}


def obter_lock_dvr(dvr_ip):
    """Retorna um cadeado exclusivo para o IP da DVR informada."""
    chave_ip = str(dvr_ip or "").strip().lower()
    with jobs_ativos_lock:
        if chave_ip not in locks_por_dvr:
            locks_por_dvr[chave_ip] = threading.Lock()
        return locks_por_dvr[chave_ip]


worker_state = {
    "status": "iniciando",
    "erro": None,
    "atualizado_em": None,
}


def preparar_banco():
    with banco_lock:
        banco.execute("""
            CREATE TABLE IF NOT EXISTS escolas (
                codigo TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                dvr_ip TEXT DEFAULT '',
                cidade TEXT DEFAULT '',
                atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        banco.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dvr_ip TEXT NOT NULL,
                usuario TEXT NOT NULL,
                senha TEXT NOT NULL,
                cameras TEXT NOT NULL,      -- ex: "1,2,5"
                data_inicio TEXT NOT NULL,  -- formato ISAPI: 2026-09-23T08:00:00Z
                data_fim TEXT NOT NULL,
                escola TEXT,
                codigo_escola TEXT,
                status TEXT DEFAULT 'novo', -- novo / buscando / baixando / convertendo / concluido / erro / sem_gravacoes / cancelado
                iniciado_em TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                modo_download TEXT DEFAULT 'auto', -- auto (SDK 8000 + Fallback ISAPI 80) / sdk / isapi
                porta_sdk INTEGER DEFAULT 8000,
                finalizado_em TEXT,
                protocolo_ativo TEXT
            )
        """)
        banco.execute("""
            CREATE TABLE IF NOT EXISTS gravacoes (
                job_id INTEGER,
                dvr_ip TEXT,
                camera INTEGER,
                nome_trecho TEXT,
                inicio TEXT,
                fim TEXT,
                uri TEXT,
                tamanho INTEGER,
                bytes_baixados INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pendente',  -- pendente / baixando / concluido / erro
                caminho TEXT,
                ultimo_erro TEXT,
                atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (job_id, dvr_ip, camera, nome_trecho)
            )
        """)
        banco.execute("""
            CREATE TABLE IF NOT EXISTS solicitacoes_telegram (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER,
                solicitante TEXT,
                mensagem_original TEXT,
                codigo_escola TEXT,
                nome_escola TEXT,
                cameras TEXT,
                data_inicio TEXT,
                data_fim TEXT,
                dvr_ip TEXT DEFAULT '',
                status TEXT DEFAULT 'aguardando_ip', -- aguardando_ip / baixando / concluido / descartado / erro_leitura
                job_id INTEGER,
                erro_motivo TEXT,
                recebido_em TEXT DEFAULT CURRENT_TIMESTAMP,
                processado_em TEXT
            )
        """)
        banco.execute("""
            CREATE TABLE IF NOT EXISTS historico_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                codigo_escola TEXT,
                protocolo TEXT,
                trechos INTEGER,
                bytes INTEGER,
                arquivado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        chave_gravacoes = [
            linha[1] for linha in banco.execute("PRAGMA table_info(gravacoes)")
            if linha[5]
        ]
        if chave_gravacoes != ["job_id", "dvr_ip", "camera", "nome_trecho"]:
            banco.execute("ALTER TABLE gravacoes RENAME TO gravacoes_antigas")
            banco.execute("""
                CREATE TABLE gravacoes (
                    job_id INTEGER,
                    dvr_ip TEXT,
                    camera INTEGER,
                    nome_trecho TEXT,
                    inicio TEXT,
                    fim TEXT,
                    uri TEXT,
                    tamanho INTEGER,
                    bytes_baixados INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pendente',
                    caminho TEXT,
                    ultimo_erro TEXT,
                    atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (job_id, dvr_ip, camera, nome_trecho)
                )
            """)
            banco.execute("""
                INSERT INTO gravacoes
                (job_id, dvr_ip, camera, nome_trecho, inicio, fim, uri, tamanho,
                 status, caminho)
                SELECT job_id, dvr_ip, camera, nome_trecho, inicio, fim, uri, tamanho,
                       status, caminho
                FROM gravacoes_antigas
            """)
            banco.execute("DROP TABLE gravacoes_antigas")
        for tabela, coluna, tipo in (
            ("jobs", "codigo_escola", "TEXT"),
            ("jobs", "iniciado_em", "TEXT"),
            ("jobs", "criado_em", "TEXT DEFAULT CURRENT_TIMESTAMP"),
            ("jobs", "modo_download", "TEXT DEFAULT 'auto'"),
            ("jobs", "porta_sdk", "INTEGER DEFAULT 8000"),
            ("jobs", "erro_detalhe", "TEXT"),
            ("jobs", "finalizado_em", "TEXT"),
            ("jobs", "protocolo_ativo", "TEXT"),
            ("gravacoes", "bytes_baixados", "INTEGER DEFAULT 0"),
            ("gravacoes", "ultimo_erro", "TEXT"),
            ("gravacoes", "atualizado_em", "TEXT"),
            ("escolas", "dvr_usuario", "TEXT DEFAULT 'admin'"),
            ("escolas", "dvr_senha", "TEXT DEFAULT ''"),
            ("escolas", "dvr_modelo", "TEXT DEFAULT ''"),
            ("escolas", "dvr_canais", "TEXT DEFAULT ''"),
            ("escolas", "dvrs_json", "TEXT DEFAULT '[]'"),
        ):
            try:
                banco.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
            except sqlite3.OperationalError as erro:
                if "duplicate column name" not in str(erro).lower():
                    raise
        # Qualquer job ou trecho que ficou em andamento ("buscando", "baixando",
        # "convertendo") de uma execução anterior encerrada de forma abrupta
        # volta para a fila ("novo" / "pendente") para ser retomado automaticamente!
        banco.execute(
            "UPDATE gravacoes SET status='pendente', atualizado_em=CURRENT_TIMESTAMP "
            "WHERE status='baixando'"
        )
        banco.execute(
            "UPDATE jobs SET status='novo', finalizado_em=NULL "
            "WHERE status IN ('buscando','baixando','convertendo','cortando','aguardando_conexao')"
        )
        banco.execute(
            "UPDATE jobs SET status='sem_gravacoes' WHERE status='concluido' "
            "AND NOT EXISTS (SELECT 1 FROM gravacoes WHERE gravacoes.job_id=jobs.id)"
        )
        concluidos = banco.execute(
            "SELECT dvr_ip, camera, nome_trecho, caminho FROM gravacoes "
            "WHERE status='concluido'"
        ).fetchall()
        for dvr_ip, camera, nome_trecho, caminho in concluidos:
            if not caminho or not os.path.isfile(caminho):
                banco.execute(
                    "UPDATE gravacoes SET status='pendente', atualizado_em=CURRENT_TIMESTAMP "
                    "WHERE dvr_ip=? AND camera=? AND nome_trecho=?",
                    (dvr_ip, camera, nome_trecho),
                )
        
        # Popula escolas a partir de data_all_sheets.json ou escolas.json se estiver vazio
        total_escolas = banco.execute("SELECT COUNT(*) FROM escolas").fetchone()[0]
        if total_escolas == 0:
            pasta_raiz = os.path.dirname(os.path.abspath(__file__))
            for nome_json in ("data_all_sheets.json", "escolas.json"):
                caminho_json = os.path.join(pasta_raiz, nome_json)
                if os.path.isfile(caminho_json):
                    try:
                        with open(caminho_json, "r", encoding="utf-8") as fj:
                            lista = json.load(fj)
                            for item in lista:
                                cod = str(item.get("codigo_do_local") or item.get("codigo") or "").strip()
                                nm = str(item.get("nome") or "").strip()
                                cid = str(item.get("cidade_do_local") or item.get("cidade") or "").strip()
                                if cod and nm:
                                    banco.execute(
                                        "INSERT OR IGNORE INTO escolas (codigo, nome, cidade) VALUES (?, ?, ?)",
                                        (cod, nm, cid)
                                    )
                        banco.commit()
                        break
                    except Exception as e:
                        print(f"Aviso: nao foi possivel carregar escolas de {nome_json}: {e}")
        banco.commit()


def arquivar_job_concluido(job_id):
    """
    Arquiva as métricas de um job concluído na tabela historico_jobs antes da exclusão.
    Deve ser chamada dentro de banco_lock e NÃO faz commit próprio.
    Só arquiva se o job tiver status='concluido'.
    """
    linha = banco.execute(
        "SELECT status, codigo_escola, protocolo_ativo, modo_download FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if not linha or linha[0] != "concluido":
        return

    ja_arquivado = banco.execute(
        "SELECT 1 FROM historico_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if ja_arquivado:
        return

    _, codigo_escola, protocolo_ativo, modo_download = linha
    proto = str(protocolo_ativo or (modo_download.upper() if modo_download else "") or "ISAPI").strip()
    if not proto or proto.upper() in ("AUTO", "NONE", "NULL"):
        proto = "ISAPI"
    else:
        proto = proto.upper()

    grav_stats = banco.execute(
        "SELECT COUNT(*), COALESCE(SUM(bytes_baixados), 0) FROM gravacoes WHERE job_id=? AND status='concluido'",
        (job_id,),
    ).fetchone()
    trechos = (grav_stats[0] if grav_stats else 0) or 0
    bytes_baixados = (grav_stats[1] if grav_stats else 0) or 0

    banco.execute(
        "INSERT INTO historico_jobs (job_id, codigo_escola, protocolo, trechos, bytes) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, codigo_escola, proto, trechos, bytes_baixados),
    )


# ---------------------------------------------------------------------------
# Comunicação com a DVR (ISAPI)
# ---------------------------------------------------------------------------

_cache_porta_sdk = {}  # ip -> (porta, epoch) ; porta do servidor (DEV_MANAGE) informada pela propria DVR
_PORTA_SDK_TTL = 6 * 3600


def detectar_porta_sdk(ip, usuario, senha, porta_http=80, usar_cache=True):
    """
    Descobre a 'porta do servidor' (usada pelo SDK) perguntando a propria DVR via ISAPI
    (GET /ISAPI/Security/adminAccesses -> protocolo DEV_MANAGE). Leitura simples, sem login SDK.
    Retorna a porta (int) ou None se a DVR nao responder/nao suportar (ex.: MHDX sem ISAPI).
    """
    ip_limpo, _ = hikvision_sdk._limpar_ip_e_porta(ip, 8000)
    if not ip_limpo or not senha:
        return None
    agora = time.time()
    if usar_cache:
        em_cache = _cache_porta_sdk.get(ip_limpo)
        if em_cache and agora - em_cache[1] < _PORTA_SDK_TTL:
            return em_cache[0]
    try:
        resp = requests.get(
            f"http://{ip_limpo}:{int(porta_http or 80)}/ISAPI/Security/adminAccesses",
            auth=HTTPDigestAuth(usuario or "admin", senha), timeout=5,
        )
        if resp.status_code != 200:
            return None
        achado = re.search(r"<protocol>\s*DEV_MANAGE\s*</protocol>\s*<portNo>\s*(\d+)\s*</portNo>", resp.text)
        if not achado:
            return None
        porta = int(achado.group(1))
        if 1 <= porta <= 65535:
            _cache_porta_sdk[ip_limpo] = (porta, agora)
            return porta
    except (requests.exceptions.RequestException, ValueError):
        pass
    return None


def dvr_esta_acessivel(ip, porta=80, timeout=3):
    ip_limpo, _ = hikvision_sdk._limpar_ip_e_porta(ip, porta)
    try:
        with socket.create_connection((ip_limpo, int(porta)), timeout=timeout):
            return True
    except OSError:
        return False


def dvr_qualquer_porta_acessivel(ip, portas=(80, 8000), timeout=3):
    """Retorna True se a DVR responder em pelo menos uma das portas (ex: HTTP 80 ou SDK 8000)."""
    portas_vistas = set()
    for p in portas:
        if not p or int(p) in portas_vistas:
            continue
        portas_vistas.add(int(p))
        if dvr_esta_acessivel(ip, porta=int(p), timeout=timeout):
            return True
    return False


def esperar_dvr_voltar(
    ip, porta=80, marcar_status=None, checar_cancelado=None,
    max_segundos=None, portas_extras=()
):
    """
    Aguarda a DVR voltar ao ar quando cai a energia ou a internet da escola:
      - Nos primeiros 60 segundos: tenta reconectar rápido a cada 5 segundos.
      - Após 60 segundos: tenta reconectar a cada 1 minuto (60s) sem parar
        até a DVR voltar ao ar ou até o operador cancelar o download!
    """
    portas_teste = (porta,) + tuple(portas_extras or ())
    if dvr_qualquer_porta_acessivel(ip, portas=portas_teste):
        return

    if marcar_status:
        try:
            marcar_status(
                "aguardando_conexao",
                detalhe=f"Queda de conexão/energia na DVR ({ip}). Tentando reconectar a cada {INTERVALO_RETRY_SEGUNDOS}s..."
            )
        except TypeError:
            marcar_status("aguardando_conexao")

    inicio_espera = time.time()
    aviso_modo_1min_enviado = False

    while not dvr_qualquer_porta_acessivel(ip, portas=portas_teste):
        if checar_cancelado and checar_cancelado():
            raise DownloadCancelado()

        decorrido = time.time() - inicio_espera
        if max_segundos is not None and decorrido > max_segundos:
            raise RuntimeError(f"DVR {ip}:{porta} nao respondeu apos {max_segundos}s.")

        # Escolhe o intervalo: 5s no primeiro minuto, 60s (1 min) depois disso
        if decorrido < TEMPO_FASE_RAPIDA_SEGUNDOS:
            espera_atual = INTERVALO_RETRY_SEGUNDOS
        else:
            espera_atual = INTERVALO_RETRY_LONGO_SEGUNDOS
            minutos = max(1, int(decorrido // 60))
            if not aviso_modo_1min_enviado:
                print(f"[conexao] DVR {ip} ainda sem resposta apos 1 minuto. Alternando para tentativa automatica a cada 60s sem parar ate voltar...")
                aviso_modo_1min_enviado = True
            if marcar_status:
                try:
                    marcar_status(
                        "aguardando_conexao",
                        detalhe=f"Escola/DVR ({ip}) sem energia ou internet há {minutos} min. Tentando reconectar automaticamente a cada 1 minuto..."
                    )
                except TypeError:
                    marcar_status("aguardando_conexao")

        # Dorme em fatias de 1s para responder no mesmo segundo caso o usuário clique em Cancelar
        passos = max(1, int(espera_atual))
        for _ in range(passos):
            if checar_cancelado and checar_cancelado():
                raise DownloadCancelado()
            time.sleep(1)

    print(f"[conexao] ✅ DVR {ip} voltou a responder na rede! Retomando download automaticamente...")


def job_foi_cancelado(job_id):
    with jobs_ativos_lock:
        if job_id in jobs_cancelados:
            return True
    with banco_lock:
        status = banco.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    return status is None or status[0] == "cancelado"


def montar_busca(camera, data_inicio, data_fim, posicao):
    search_id = str(uuid.uuid4()).upper()
    xml = (
        "<CMSearchDescription>"
        f"<searchID>{search_id}</searchID>"
        f"<trackList><trackID>{camera}01</trackID></trackList>"
        f"<timeSpanList><timeSpan><startTime>{data_inicio}</startTime>"
        f"<endTime>{data_fim}</endTime></timeSpan></timeSpanList>"
        "<maxResults>50</maxResults>"
        f"<searchResultPostion>{posicao}</searchResultPostion>"
        "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com"
        "</metadataDescriptor></metadataList>"
        "</CMSearchDescription>"
    )
    return xml.encode("utf-8")


def buscar_gravacoes(dvr_ip, auth, camera, data_inicio, data_fim, job_id, checar_cancelado=None):
    """Busca trechos via ISAPI. Se a DVR nao suportar ISAPI ou falhar, retorna False sem travar o worker."""
    data_inicio_isapi = horario_dvr_local(data_inicio).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_fim_isapi = horario_dvr_local(data_fim).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    posicao = 0
    total_encontrados = 0
    tentativas_erro = 0
    while True:
        if checar_cancelado and checar_cancelado():
            raise DownloadCancelado()
        try:
            r = requests.post(
                f"http://{dvr_ip}/ISAPI/ContentMgmt/search",
                data=montar_busca(camera, data_inicio_isapi, data_fim_isapi, posicao),
                headers={"Content-Type": "application/xml"},
                auth=auth, timeout=8,
            )
            if r.status_code == 400 and ("badTrackID" in r.text or "invalidtrack" in r.text.lower()):
                raise hikvision_sdk.CameraInvalidaError(f"Câmera {camera} inválida ou inexistente nesta DVR (ISAPI badTrackID).")
            r.raise_for_status()
            tentativas_erro = 0
        except Exception as e_req:
            tentativas_erro += 1
            if tentativas_erro >= 2:
                print(f"[job {job_id}] Busca ISAPI na DVR {dvr_ip} camera {camera} nao respondeu ({e_req}). Prosseguindo...")
                return total_encontrados > 0
            time.sleep(2)
            continue

        try:
            raiz = ET.fromstring(r.text)
        except Exception:
            break
        itens = raiz.findall(".//{*}searchMatchItem")
        if not itens:
            break

        with banco_lock:
            for item in itens:
                uri_el = item.find(".//{*}playbackURI")
                ini_el = item.find(".//{*}startTime")
                fim_el = item.find(".//{*}endTime")
                if uri_el is None or ini_el is None or fim_el is None:
                    continue
                uri = uri_el.text
                inicio = ini_el.text
                fim = fim_el.text
                parametros = parse_qs(urlparse(uri).query)
                nome_trecho = parametros.get("name", [f"trecho_{posicao}"])[0]
                tamanho_bloco = int(parametros.get("size", [0])[0])

                # Calcula o tamanho estimado proporcional para a janela de tempo pedida
                try:
                    dt_ini_tr = datetime.fromisoformat(inicio[:19])
                    dt_fim_tr = datetime.fromisoformat(fim[:19])
                    dt_ini_ped = horario_dvr_local(data_inicio)
                    dt_fim_ped = horario_dvr_local(data_fim)
                    dur_bloco = max(1.0, (dt_fim_tr - dt_ini_tr).total_seconds())
                    corte_ini = max(dt_ini_tr, dt_ini_ped)
                    corte_fim = min(dt_fim_tr, dt_fim_ped)
                    dur_ped = max(0.0, (corte_fim - corte_ini).total_seconds())
                    tamanho = int(tamanho_bloco * (dur_ped / dur_bloco)) if dur_bloco > 0 else tamanho_bloco
                    tamanho = max(1024 * 1024, tamanho) # No mínimo 1 MB
                except Exception:
                    tamanho = tamanho_bloco

                banco.execute(
                    "INSERT OR IGNORE INTO gravacoes "
                    "(job_id, dvr_ip, camera, nome_trecho, inicio, fim, uri, tamanho) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, dvr_ip, camera, nome_trecho, inicio, fim, uri, tamanho),
                )
            banco.commit()

        total_encontrados += len(itens)
        posicao += len(itens)
        if len(itens) < 50:
            break

    return total_encontrados > 0


def formatar_tempo_isapi_compacto(iso_str):
    """Converte '2026-09-24T12:55:00Z' para o formato compacto '20260924T125500Z'."""
    return iso_str.replace("-", "").replace(":", "")


def horario_dvr_local(iso_str):
    """Converte o UTC armazenado no job para horario local usado pelo SDK."""
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(FUSO_EMPRESA).replace(tzinfo=None)


def baixar_um_trecho(
    dvr_ip, auth, uri, destino, tamanho_esperado, camera=None,
    data_inicio_pedido=None, data_fim_pedido=None, usuario="admin", senha="",
    atualizar_progresso=None, checar_cancelado=None
):
    """
    Baixa um trecho de gravação da DVR via ISAPI HTTP (porta 80).

    Estratégia:
      1. Tenta downloadRequest ISAPI com playbackURI recortada no tempo.
      2. Fallback: Download do bloco físico com Range/retomada.
    """
    parcial = destino + ".parcial"
    ja_baixado = os.path.getsize(parcial) if os.path.exists(parcial) else 0
    if atualizar_progresso:
        atualizar_progresso(ja_baixado)
    if checar_cancelado and checar_cancelado():
        raise DownloadCancelado()

    sucesso = False

    # Download via ISAPI HTTP (porta 80)
    uris_para_tentar = []
    if data_inicio_pedido and data_fim_pedido and camera:
        data_ini_isapi = horario_dvr_local(data_inicio_pedido).strftime("%Y-%m-%dT%H:%M:%SZ")
        data_fim_isapi = horario_dvr_local(data_fim_pedido).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_ini = formatar_tempo_isapi_compacto(data_ini_isapi)
        t_fim = formatar_tempo_isapi_compacto(data_fim_isapi)
        uri_recortada = f"rtsp://{dvr_ip}/Streaming/tracks/{camera}01/?starttime={t_ini}&endtime={t_fim}"
        uris_para_tentar.append((uri_recortada, True))

    uris_para_tentar.append((uri, False))

    for uri_alvo, eh_tentativa_corte in uris_para_tentar:
        corpo = (
            '<downloadRequest version="1.0" '
            'xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<playbackURI>{escape(uri_alvo)}</playbackURI></downloadRequest>"
        ).encode("utf-8")

        cabecalhos = {"Content-Type": "application/xml"}
        if ja_baixado > 0 and not eh_tentativa_corte:
            cabecalhos["Range"] = f"bytes={ja_baixado}-"

        try:
            with requests.post(
                f"http://{dvr_ip}/ISAPI/ContentMgmt/download",
                data=corpo, headers=cabecalhos, auth=auth,
                stream=True, timeout=60,
            ) as resp:
                if resp.status_code == 400 and ("badTrackID" in resp.text or "invalidtrack" in resp.text.lower()):
                    raise hikvision_sdk.CameraInvalidaError(f"Câmera {camera} inválida ou inexistente nesta DVR (ISAPI badTrackID).")

                if eh_tentativa_corte and resp.status_code not in (200, 206):
                    continue

                if resp.status_code not in (200, 206):
                    resp.raise_for_status()

                modo = "ab" if resp.status_code == 206 else "wb"
                with open(parcial, modo) as f:
                    for bloco in resp.iter_content(1024 * 1024):
                        if checar_cancelado and checar_cancelado():
                            raise DownloadCancelado()
                        f.write(bloco)
                        if atualizar_progresso:
                            atualizar_progresso(f.tell())
                sucesso = True
                break
        except requests.exceptions.RequestException:
            if eh_tentativa_corte:
                continue
            raise

    if not sucesso:
        raise RuntimeError("Nao foi possivel baixar o trecho da DVR")

    tamanho_real = os.path.getsize(parcial)
    if tamanho_real < 1000:
        if os.path.exists(parcial):
            os.remove(parcial)
        raise ValueError(f"Arquivo recebido muito pequeno ({tamanho_real} bytes)")

    os.replace(parcial, destino)
    if not os.path.isfile(destino) or os.path.getsize(destino) == 0:
        raise ValueError("Verificacao final falhou: o arquivo nao foi salvo corretamente")


def converter_e_cortar(origem, destino, inicio_trecho, fim_trecho, data_inicio_pedido, data_fim_pedido):
    """
    Corta o arquivo da DVR na janela pedida. Tenta primeiro preservar o codec
    original (H.265 ou H.264) sem recodificar; usa H.264 como fallback web.

    Por que cortar aqui?
      - A DVR frequentemente entrega blocos físicos de até 1 GB (~2h de gravação).
      - Ao calcular o deslocamento inicial e a duração, cortamos exatamente os minutos
        pedidos (ex: 40 min / ~176 MB) e apagamos o arquivo bruto de 1 GB.
    """
    dt_ini_tr = datetime.fromisoformat(inicio_trecho[:19]) # Remove o 'Z' para ficar naive
    dt_fim_tr = datetime.fromisoformat(fim_trecho[:19])
    dt_ini_ped = horario_dvr_local(data_inicio_pedido)
    dt_fim_ped = horario_dvr_local(data_fim_pedido)

    # Janela de intersecção entre o trecho baixado e o pedido do usuário
    corte_ini = max(dt_ini_tr, dt_ini_ped)
    corte_fim = min(dt_fim_tr, dt_fim_ped)

    deslocamento = max(0.0, (corte_ini - dt_ini_tr).total_seconds())
    duracao = max(0.0, (corte_fim - corte_ini).total_seconds())

    temporario_codec = destino + ".codec.mp4"
    temporario_h264 = destino + ".convertendo.mp4"
    for temporario in (temporario_codec, temporario_h264):
        if os.path.exists(temporario):
            os.remove(temporario)

    # 1. Tenta usar o FFmpeg se estiver disponível (máxima velocidade e simplicidade)
    ffmpeg_bin = shutil.which("ffmpeg") or (
        os.path.isfile("ffmpeg.exe") and os.path.abspath("ffmpeg.exe")
    ) or (
        os.path.isfile("ffmpeg") and os.path.abspath("ffmpeg")
    )

    if ffmpeg_bin:
        args_tempo = []
        if deslocamento > 0.05:
            args_tempo.extend(["-ss", f"{deslocamento:.3f}"])
        if duracao > 0:
            args_tempo.extend(["-t", f"{duracao:.3f}"])

        # Primeira tentativa: copia o fluxo original, sem recodificar H.265/H.264.
        comando_codec = [
            ffmpeg_bin, "-y",
            *args_tempo,
            "-i", origem,
            "-map", "0:v:0",
            "-c:v", "copy",
            "-an",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            temporario_codec,
        ]
        resultado_codec = subprocess.run(
            comando_codec, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3600,
        )
        if resultado_codec.returncode == 0 and os.path.isfile(temporario_codec):
            if os.path.getsize(temporario_codec) > 1000:
                os.replace(temporario_codec, destino)
                return
        if os.path.exists(temporario_codec):
            os.remove(temporario_codec)

        # Segunda tentativa: recodificação H.264 para reprodução ampla no navegador.
        comando = [
            ffmpeg_bin, "-y",
            *args_tempo,
            "-i", origem,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            temporario_h264
        ]
        resultado = subprocess.run(
            comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600
        )
        if resultado.returncode == 0 and os.path.isfile(temporario_h264) and os.path.getsize(temporario_h264) > 1000:
            os.replace(temporario_h264, destino)
            return

        # Terceira tentativa: recodificação com -an (sem áudio) caso o canal de áudio da DVR seja incompatível
        comando_sem_audio = [
            ffmpeg_bin, "-y",
            *args_tempo,
            "-i", origem,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-an",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            temporario_h264
        ]
        resultado_an = subprocess.run(
            comando_sem_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600
        )
        if resultado_an.returncode == 0 and os.path.isfile(temporario_h264) and os.path.getsize(temporario_h264) > 1000:
            os.replace(temporario_h264, destino)
            return

        if os.path.exists(temporario_h264):
            os.remove(temporario_h264)
        erro_final = (resultado.stderr or resultado_an.stderr or b"").decode("utf-8", errors="ignore")[-400:]
        raise RuntimeError(f"FFmpeg falhou ao converter/cortar: {erro_final}")

        os.replace(temporario_h264, destino)
        return

    # 2. Se FFmpeg não estiver instalado, usa GStreamer com filtro de buffers (pad probe)
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as erro:
        raise RuntimeError("Nem FFmpeg nem GStreamer estao instalados para converter/cortar video") from erro

    Gst.init(None)

    # O pipeline usa decodebin (detecta H.265 ou H.264 automaticamente) e x264enc para gerar H.264
    pipeline = Gst.parse_launch(
        f'filesrc location="{origem}" ! decodebin ! videoconvert ! '
        f'identity name=filtro ! x264enc speed-preset=ultrafast tune=zerolatency ! mp4mux ! filesink location="{temporario_h264}"'
    )

    filtro = pipeline.get_by_name("filtro")
    pad = filtro.get_static_pad("src")

    start_ns = int(deslocamento * Gst.SECOND)
    end_ns = int((deslocamento + duracao) * Gst.SECOND)
    primeiro_pts = None

    def probe_cb(pad_elem, info):
        nonlocal primeiro_pts
        buf = info.get_buffer()
        if not buf or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK

        if primeiro_pts is None:
            primeiro_pts = buf.pts

        rel_pts = buf.pts - primeiro_pts

        # Descarta frames anteriores ao início do recorte pedido
        if rel_pts < start_ns:
            return Gst.PadProbeReturn.DROP

        # Quando atinge o fim do recorte, encerra o stream
        if rel_pts > end_ns:
            pad_elem.push_event(Gst.Event.new_eos())
            return Gst.PadProbeReturn.DROP

        # Reindexa os timestamps para o MP4 começar em 00:00:00
        buf.pts = rel_pts - start_ns
        buf.dts = buf.pts
        return Gst.PadProbeReturn.OK

    pad.add_probe(Gst.PadProbeType.BUFFER, probe_cb)

    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    mensagem = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS)

    if mensagem.type == Gst.MessageType.ERROR:
        erro, dbg = mensagem.parse_error()
        pipeline.set_state(Gst.State.NULL)
        if os.path.exists(temporario_h264):
            os.remove(temporario_h264)
        raise RuntimeError(f"GStreamer falhou ao converter/cortar: {erro} ({dbg})")

    pipeline.set_state(Gst.State.NULL)

    if not os.path.isfile(temporario_h264) or os.path.getsize(temporario_h264) == 0:
        raise RuntimeError("O corte/conversao GStreamer nao gerou um arquivo MP4 valido")

    os.replace(temporario_h264, destino)


def nome_pasta_job(escola, codigo_escola, data_inicio, data_fim):
    inicio = datetime.fromisoformat(data_inicio.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    fim = datetime.fromisoformat(data_fim.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    identificador = f" - {codigo_escola.strip()}" if codigo_escola and codigo_escola.strip() else ""
    nome = f"{inicio} a {fim}{identificador} - {escola or 'DVR'}"
    return "".join("_" if caractere in '<>:"/\\|?*' else caractere for caractere in nome)


def baixar_pendentes_do_job(
    job_id, dvr_ip, auth, escola, codigo_escola, data_inicio, data_fim,
    atualizar_status_job, modo_download="auto", porta_sdk=8000, sessao_sdk=None,
):
    """
    Processa todos os trechos pendentes do job.
    Usa busca iterativa no banco (sem recursão) e trava o protocolo em 'isapi'
    caso o SDK falhe uma vez no modo 'auto', impedindo o loop infinito de
    tentar SDK -> cair pro fallback -> tentar SDK de novo no próximo trecho.
    """
    modo_atual = modo_download

    # Garante que qualquer trecho que tenha ficado como 'baixando' antes volte para 'pendente'
    with banco_lock:
        banco.execute(
            "UPDATE gravacoes SET status='pendente' WHERE job_id=? AND status='baixando'",
            (job_id,),
        )
        banco.commit()

    while True:
        if job_foi_cancelado(job_id):
            return

        with banco_lock:
            item = banco.execute(
                "SELECT dvr_ip, camera, nome_trecho, uri, tamanho, inicio, fim FROM gravacoes "
                "WHERE job_id=? AND status='pendente' "
                "ORDER BY camera ASC, inicio ASC, nome_trecho ASC LIMIT 1",
                (job_id,),
            ).fetchone()

        if not item:
            break

        dvr_ip, camera, nome_trecho, uri, tamanho, inicio_trecho, fim_trecho = item

        pasta = os.path.join(
            PASTA_DESTINO,
            nome_pasta_job(escola, codigo_escola, data_inicio, data_fim),
            f"camera_{camera}",
        )
        os.makedirs(pasta, exist_ok=True)
        origem = os.path.join(pasta, f"{nome_trecho}.hik")
        try:
            h_ini = horario_dvr_local(data_inicio).strftime("%d-%m-%Y_%Hh%M")
            h_fim = horario_dvr_local(data_fim).strftime("%Hh%M")
            nome_arquivo_mp4 = f"camera_{camera}_{h_ini}_ate_{h_fim}_{nome_trecho}.mp4"
        except Exception:
            nome_arquivo_mp4 = f"{nome_trecho}.mp4"
        destino = os.path.join(pasta, nome_arquivo_mp4)

        with banco_lock:
            banco.execute(
                "UPDATE gravacoes SET status='baixando', ultimo_erro=NULL, "
                "atualizado_em=CURRENT_TIMESTAMP WHERE job_id=? AND dvr_ip=? "
                "AND camera=? AND nome_trecho=?",
                (job_id, dvr_ip, camera, nome_trecho),
            )
            banco.commit()

        def atualizar_progresso(bytes_baixados):
            with banco_lock:
                banco.execute(
                    "UPDATE gravacoes SET bytes_baixados=?, atualizado_em=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND dvr_ip=? AND camera=? AND nome_trecho=?",
                    (bytes_baixados, job_id, dvr_ip, camera, nome_trecho),
                )
                banco.commit()

        # Tenta baixar o trecho atual; se a rede oscilar no ISAPI, aguarda reconexão
        while True:
            try:
                sucesso_download = False

                # 1. Tenta via SDK somente se modo_atual permitir ('auto' ou 'sdk')
                if modo_atual in ("auto", "sdk") and hikvision_sdk.sdk_disponivel():
                    try:
                        try:
                            if inicio_trecho and fim_trecho:
                                dt_ini_ped = datetime.fromisoformat(inicio_trecho[:19])
                                dt_fim_ped = datetime.fromisoformat(fim_trecho[:19])
                            else:
                                dt_ini_ped = horario_dvr_local(data_inicio)
                                dt_fim_ped = horario_dvr_local(data_fim)
                        except Exception:
                            dt_ini_ped = horario_dvr_local(data_inicio)
                            dt_fim_ped = horario_dvr_local(data_fim)

                        print(f"[job {job_id}] Baixando via NetSDK (porta {porta_sdk}) camera {camera}...")
                        atualizar_status_job("baixando", protocolo="SDK")

                        def progresso_sdk(pct):
                            t_bytes = int(tamanho * (pct / 100.0))
                            atualizar_progresso(t_bytes)

                        sucesso_download = hikvision_sdk.baixar_por_tempo_sdk(
                            dvr_ip, porta_sdk, auth.username, auth.password,
                            camera, dt_ini_ped, dt_fim_ped, origem,
                            callback_progresso=progresso_sdk,
                            checar_cancelado=lambda: job_foi_cancelado(job_id),
                            sessao_aberta=sessao_sdk
                        )
                        if job_foi_cancelado(job_id):
                            raise DownloadCancelado()
                    except hikvision_sdk.CameraInvalidaError as e_invalida:
                        msg_invalida = descrever_erro(e_invalida)
                        print(f"[job {job_id}] Câmera inválida detectada: {msg_invalida}")
                        with banco_lock:
                            banco.execute(
                                "UPDATE gravacoes SET status='erro', ultimo_erro=?, atualizado_em=CURRENT_TIMESTAMP "
                                "WHERE job_id=? AND dvr_ip=? AND camera=? AND nome_trecho=?",
                                (msg_invalida, job_id, dvr_ip, camera, nome_trecho),
                            )
                            banco.commit()
                        atualizar_status_job("erro", detalhe=msg_invalida)
                        return
                    except Exception as erro_sdk:
                        if isinstance(erro_sdk, DownloadCancelado) or job_foi_cancelado(job_id):
                            raise DownloadCancelado()

                        # Verifica se a falha no SDK ocorreu porque caiu a energia/internet da escola!
                        porta_teste_sdk = porta_sdk if modo_atual == "sdk" else (porta_sdk, 80)
                        portas_alvos = (porta_teste_sdk,) if isinstance(porta_teste_sdk, int) else porta_teste_sdk
                        if not dvr_qualquer_porta_acessivel(dvr_ip, portas=portas_alvos):
                            print(f"[job {job_id}] Queda de energia/rede detectada na DVR {dvr_ip} durante SDK ({erro_sdk}). Aguardando voltar...")
                            if sessao_sdk and sessao_sdk[0] >= 0:
                                hikvision_sdk.fechar_sessao_sdk(sessao_sdk[0])
                                sessao_sdk[0] = -1
                            esperar_dvr_voltar(
                                dvr_ip,
                                porta=porta_sdk,
                                portas_extras=() if modo_atual == "sdk" else (80,),
                                marcar_status=atualizar_status_job,
                                checar_cancelado=lambda: job_foi_cancelado(job_id),
                            )
                            if job_foi_cancelado(job_id):
                                raise DownloadCancelado()
                            # Tenta reautenticar a sessão SDK após a DVR voltar ao ar
                            u_novo, d_novo = hikvision_sdk._fazer_login(dvr_ip, porta_sdk, auth.username, auth.password)
                            if u_novo >= 0 and sessao_sdk is not None:
                                sessao_sdk[0] = u_novo
                                sessao_sdk[1] = d_novo
                            atualizar_status_job("baixando", detalhe=None)
                            continue

                        print(f"[job {job_id}] Tentativa SDK falhou na camera {camera}: {erro_sdk}")
                        if modo_atual == "sdk":
                            # Se o usuário escolheu estritamente 'sdk', não faz fallback
                            raise

                        # Fecha a sessão SDK aberta e fixa o modo em 'isapi' pelo resto deste job
                        # para NUNCA ficar repetindo tentativas de SDK nos próximos trechos!
                        if sessao_sdk and sessao_sdk[0] >= 0:
                            hikvision_sdk.fechar_sessao_sdk(sessao_sdk[0])
                            sessao_sdk[0] = -1
                        modo_atual = "isapi"
                        print(f"[job {job_id}] Fixando protocolo em ISAPI (porta 80) para concluir este pedido sem repetir falhas de SDK...")

                # 2. Modo ISAPI (Porta 80) caso o SDK não tenha sido usado ou tenha acionado fallback
                if not sucesso_download:
                    if os.path.exists(origem):
                        try:
                            os.remove(origem)
                        except OSError:
                            pass
                    atualizar_status_job("baixando", protocolo="ISAPI")
                    try:
                        if uri.startswith("sdk://"):
                            # Converte todos os agendamentos 'sdk://' pendentes deste job em trechos reais ISAPI de uma só vez
                            with banco_lock:
                                cams_sdk = [
                                    r[0] for r in banco.execute(
                                        "SELECT DISTINCT camera FROM gravacoes WHERE job_id=? AND uri LIKE 'sdk://%' AND status != 'concluido'",
                                        (job_id,)
                                    ).fetchall()
                                ]
                            if not cams_sdk:
                                cams_sdk = [camera]

                            algum_encontrado = False
                            for cam_busca in cams_sdk:
                                print(f"[job {job_id}] Buscando trechos via ISAPI search na porta 80 para camera {cam_busca}...")
                                encontrou = buscar_gravacoes(
                                    dvr_ip, auth, cam_busca, data_inicio, data_fim, job_id,
                                    checar_cancelado=lambda: job_foi_cancelado(job_id)
                                )
                                if encontrou:
                                    algum_encontrado = True
                                    with banco_lock:
                                        banco.execute(
                                            "DELETE FROM gravacoes WHERE job_id=? AND camera=? AND uri LIKE 'sdk://%'",
                                            (job_id, cam_busca)
                                        )
                                        banco.commit()
                                else:
                                    with banco_lock:
                                        banco.execute(
                                            "UPDATE gravacoes SET status='erro', ultimo_erro=?, atualizado_em=CURRENT_TIMESTAMP "
                                            "WHERE job_id=? AND camera=? AND uri LIKE 'sdk://%'",
                                            (f"Fallback ISAPI nao encontrou gravacoes na porta 80 para camera {cam_busca}.", job_id, cam_busca)
                                        )
                                        banco.commit()

                            # Sai do loop interno para o while externo pegar os novos trechos ISAPI recém-cadastrados
                            break
                        else:
                            baixar_um_trecho(
                                dvr_ip, auth, uri, origem, tamanho,
                                camera=camera, data_inicio_pedido=data_inicio,
                                data_fim_pedido=data_fim, usuario=auth.username, senha=auth.password,
                                atualizar_progresso=atualizar_progresso,
                                checar_cancelado=lambda: job_foi_cancelado(job_id),
                            )
                    except hikvision_sdk.CameraInvalidaError as e_invalida_isapi:
                        msg_invalida = descrever_erro(e_invalida_isapi)
                        print(f"[job {job_id}] Câmera inválida detectada no ISAPI: {msg_invalida}")
                        with banco_lock:
                            banco.execute(
                                "UPDATE gravacoes SET status='erro', ultimo_erro=?, atualizado_em=CURRENT_TIMESTAMP "
                                "WHERE job_id=? AND dvr_ip=? AND camera=? AND nome_trecho=?",
                                (msg_invalida, job_id, dvr_ip, camera, nome_trecho),
                            )
                            banco.commit()
                        atualizar_status_job("erro", detalhe=msg_invalida)
                        return

                if job_foi_cancelado(job_id):
                    return

                # Converte e corta para o período exato em H.264
                atualizar_status_job("convertendo", detalhe=None)
                converter_e_cortar(
                    origem, destino, inicio_trecho, fim_trecho, data_inicio, data_fim
                )
                atualizar_status_job("baixando", detalhe=None)

                # Remove o arquivo bruto .hik imediatamente para liberar espaço no servidor
                if os.path.exists(origem):
                    try:
                        os.remove(origem)
                    except OSError:
                        pass

                tamanho_final = os.path.getsize(destino) if os.path.exists(destino) else 0

                with banco_lock:
                    banco.execute(
                        "UPDATE gravacoes SET status='concluido', caminho=?, "
                        "bytes_baixados=?, tamanho=?, ultimo_erro=NULL, atualizado_em=CURRENT_TIMESTAMP "
                        "WHERE job_id=? AND dvr_ip=? AND camera=? AND nome_trecho=?",
                        (destino, tamanho_final, tamanho_final, job_id, dvr_ip, camera, nome_trecho),
                    )
                    banco.commit()
                break
            except DownloadCancelado:
                return
            except requests.exceptions.RequestException:
                # Problema de rede/energia durante download ISAPI: espera a DVR voltar (5s no 1º min, depois 1 min sem parar)
                if job_foi_cancelado(job_id):
                    return
                esperar_dvr_voltar(
                    dvr_ip,
                    porta=80,
                    portas_extras=(porta_sdk,),
                    marcar_status=atualizar_status_job,
                    checar_cancelado=lambda: job_foi_cancelado(job_id),
                )
                if job_foi_cancelado(job_id):
                    return
                atualizar_status_job("baixando", detalhe=None)
            except (ValueError, RuntimeError) as erro:
                if job_foi_cancelado(job_id):
                    return
                # Se a falha ocorreu porque a DVR ficou inacessível (queda de energia na escola),
                # aguarda a energia voltar e tenta o mesmo trecho novamente sem perdê-lo!
                if not dvr_qualquer_porta_acessivel(dvr_ip, portas=(80, porta_sdk)):
                    print(f"[job {job_id}] DVR {dvr_ip} ficou offline durante trecho {nome_trecho} ({erro}). Aguardando voltar...")
                    esperar_dvr_voltar(
                        dvr_ip,
                        porta=80,
                        portas_extras=(porta_sdk,),
                        marcar_status=atualizar_status_job,
                        checar_cancelado=lambda: job_foi_cancelado(job_id),
                    )
                    if job_foi_cancelado(job_id):
                        return
                    atualizar_status_job("baixando", detalhe=None)
                    continue

                # Arquivo realmente incompleto/corrompido com DVR online: registra e segue pro próximo trecho
                with banco_lock:
                    banco.execute(
                        "UPDATE gravacoes SET status='erro', ultimo_erro=?, "
                        "atualizado_em=CURRENT_TIMESTAMP WHERE job_id=? AND dvr_ip=? "
                        "AND camera=? AND nome_trecho=?",
                        (descrever_erro(erro), job_id, dvr_ip, camera, nome_trecho),
                    )
                    banco.commit()
                print(f"[job {job_id}] erro no trecho {nome_trecho}: {erro}")
                break


# ---------------------------------------------------------------------------
# O trabalhador (roda pra sempre em segundo plano com suporte a múltiplos jobs)
# ---------------------------------------------------------------------------

def processar_job(job):
    job_id = job[0]
    dvr_ip = str(job[1] or "").strip()
    usuario = str(job[2] or "admin").strip()
    senha = str(job[3] or "").strip()
    cameras_csv = job[4]
    data_inicio = job[5]
    data_fim = job[6]
    escola = job[7]
    codigo_escola = job[8]
    modo_download = job[9] if len(job) > 9 and job[9] else "auto"
    porta_sdk = job[10] if len(job) > 10 and job[10] else 8000

    auth = HTTPDigestAuth(usuario, senha)
    cameras = [int(c.strip()) for c in cameras_csv.split(",") if c.strip()]

    def atualizar_status_job(novo_status, detalhe=None, protocolo=None):
        if job_foi_cancelado(job_id):
            return
        if detalhe is not None and novo_status in ("erro", "sem_gravacoes"):
            detalhe = descrever_erro(detalhe)
        with banco_lock:
            banco.execute(
                "UPDATE jobs SET status=?, erro_detalhe=?, protocolo_ativo=COALESCE(?, protocolo_ativo), "
                "finalizado_em=CASE WHEN ? IN ('concluido','erro','sem_gravacoes','cancelado') "
                "THEN COALESCE(finalizado_em, CURRENT_TIMESTAMP) ELSE finalizado_em END, "
                "iniciado_em=CASE WHEN ? IN "
                "('baixando','convertendo') THEN COALESCE(iniciado_em, "
                "CURRENT_TIMESTAMP) ELSE iniciado_em END "
                "WHERE id=? AND status != 'cancelado'",
                (novo_status, detalhe, protocolo, novo_status, novo_status, job_id),
            )
            banco.commit()

    # Usa cadeado por IP da DVR:
    # - Se houver 2 pedidos de escolas diferentes (IPs diferentes), eles baixam AO MESMO TEMPO!
    # - Se houver 2 pedidos para a MESMA DVR, o segundo aguarda o primeiro terminar a conexão,
    #   evitando conflito de sessão e erro NET_DVR_OVER_MAXLINK na DVR.
    lock_desta_dvr = obter_lock_dvr(dvr_ip)
    with lock_desta_dvr:
        if job_foi_cancelado(job_id):
            return

        atualizar_status_job("buscando")
        sessao_sdk = [-1, None]

        try:
            # Se a escola/DVR estiver sem energia ou sem internet no momento do início,
            # aguarda ela voltar (a cada 5s no 1º minuto, depois a cada 1 min sem parar)!
            portas_iniciais = (porta_sdk,) if modo_download == "sdk" else ((80,) if modo_download == "isapi" else (porta_sdk, 80))
            if not dvr_qualquer_porta_acessivel(dvr_ip, portas=portas_iniciais):
                print(f"[job {job_id}] DVR {dvr_ip} sem resposta inicial. Entrando em modo de reconexao automatica...")
                esperar_dvr_voltar(
                    dvr_ip,
                    porta=portas_iniciais[0],
                    portas_extras=portas_iniciais[1:],
                    marcar_status=atualizar_status_job,
                    checar_cancelado=lambda: job_foi_cancelado(job_id),
                )
                if job_foi_cancelado(job_id):
                    return
                atualizar_status_job("buscando", detalhe=None)

            # Se o pedido veio com a porta padrao (8000), pergunta a propria DVR qual e a porta do servidor.
            if modo_download in ("auto", "sdk") and int(porta_sdk) == 8000:
                porta_real = detectar_porta_sdk(dvr_ip, usuario, senha)
                if porta_real and porta_real != porta_sdk:
                    print(f"[job {job_id}] Porta SDK detectada na propria DVR: {porta_real} (pedido usava {porta_sdk}).")
                    porta_sdk = porta_real
                    with banco_lock:
                        banco.execute("UPDATE jobs SET porta_sdk=? WHERE id=?", (porta_real, job_id))
                        banco.commit()

            # 1. Avalia se o SDK (porta 8000) autentica nesta DVR e mantém a sessão aberta!
            usar_sdk = False
            if modo_download in ("auto", "sdk") and hikvision_sdk.sdk_disponivel():
                print(f"[job {job_id}] Conectando e autenticando via SDK (porta {porta_sdk}) em {dvr_ip}...")
                u_id, d_info = hikvision_sdk._fazer_login(dvr_ip, porta_sdk, usuario, senha)
                if u_id >= 0:
                    usar_sdk = True
                    # Mantém u_id aberto em sessao_sdk para reutilizar no download sem fazer Logout->Login!
                    sessao_sdk = [u_id, d_info]
                    print(f"[job {job_id}] SDK autenticado com sucesso na porta {porta_sdk} (Sessao ID: {u_id})! Prioridade SDK ativa.")
                else:
                    print(f"[job {job_id}] SDK nao autenticou na porta {porta_sdk} ({dvr_ip}).")
                    if modo_download == "sdk":
                        atualizar_status_job("erro", detalhe=descrever_erro(f"Falha de autenticacao SDK na porta {porta_sdk} ({dvr_ip}). Verifique usuario, senha ou se a porta {porta_sdk} esta liberada."))
                        return
                    print(f"[job {job_id}] Modo auto: alternando definitivamente para ISAPI (porta 80 HTTP) neste job...")

            if usar_sdk:
                atualizar_status_job("buscando", protocolo="SDK")
                for camera in cameras:
                    if job_foi_cancelado(job_id):
                        return
                    dt_ini_loc = horario_dvr_local(data_inicio)
                    dt_fim_loc = horario_dvr_local(data_fim)
                    dur_seg = max(1.0, (dt_fim_loc - dt_ini_loc).total_seconds())
                    tam_estimado = max(10 * 1024 * 1024, int(dur_seg * 250_000))
                    nome_tr = f"rec_cam{camera}_{dt_ini_loc.strftime('%Y%m%d_%H%M%S')}"
                    with banco_lock:
                        banco.execute(
                            "INSERT OR IGNORE INTO gravacoes "
                            "(job_id, dvr_ip, camera, nome_trecho, inicio, fim, uri, tamanho) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (job_id, dvr_ip, camera, nome_tr,
                             dt_ini_loc.isoformat(), dt_fim_loc.isoformat(),
                             f"sdk://{dvr_ip}:{porta_sdk}/ch{camera}", tam_estimado)
                        )
                        banco.commit()
                    print(f"[job {job_id}] Cadastrado intervalo para download direto via SDK camera {camera}: {dt_ini_loc.strftime('%H:%M:%S')} ate {dt_fim_loc.strftime('%H:%M:%S')}")
            else:
                # Fallback ISAPI (porta 80 HTTP)
                atualizar_status_job("buscando", protocolo="ISAPI")
                for camera in cameras:
                    if job_foi_cancelado(job_id):
                        return
                    try:
                        buscar_gravacoes(dvr_ip, auth, camera, data_inicio, data_fim, job_id, checar_cancelado=lambda: job_foi_cancelado(job_id))
                    except hikvision_sdk.CameraInvalidaError as e_invalida_busca:
                        msg_invalida = descrever_erro(e_invalida_busca)
                        print(f"[job {job_id}] {msg_invalida}")
                        atualizar_status_job("erro", detalhe=msg_invalida)
                        return
                    except DownloadCancelado:
                        return
                    except Exception as e_busca:
                        print(f"[job {job_id}] Busca ISAPI camera {camera} erro: {e_busca}")

            if job_foi_cancelado(job_id):
                return
            with banco_lock:
                quantidade_gravacoes = banco.execute(
                    "SELECT COUNT(*) FROM gravacoes WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            if quantidade_gravacoes == 0:
                detalhe = descrever_erro("Nenhuma gravacao encontrada no periodo e camera informados.")
                print(f"[job {job_id}] {detalhe}")
                atualizar_status_job("sem_gravacoes", detalhe)
                return

            proto_final = "SDK" if usar_sdk else "ISAPI"
            # CORREÇÃO CRÍTICA: Se usar_sdk for False, passamos modo_efetivo='isapi'
            # para que baixar_pendentes_do_job NÃO tente conectar no SDK em cada trecho!
            modo_efetivo = modo_download if usar_sdk else "isapi"
            atualizar_status_job("baixando", protocolo=proto_final)
            baixar_pendentes_do_job(
                job_id, dvr_ip, auth, escola, codigo_escola, data_inicio, data_fim,
                atualizar_status_job, modo_download=modo_efetivo, porta_sdk=porta_sdk,
                sessao_sdk=sessao_sdk,
            )

            # Só marca como concluído se não sobrou nenhum trecho pendente/baixando
            if job_foi_cancelado(job_id):
                return
            with banco_lock:
                restantes, erros = banco.execute(
                    "SELECT SUM(status IN ('pendente','baixando')), SUM(status='erro') "
                    "FROM gravacoes WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            if (restantes or 0) == 0 and (erros or 0) == 0:
                atualizar_status_job("concluido")
            else:
                atualizar_status_job("erro", descrever_erro("Um ou mais trechos nao puderam ser baixados."))
        except DownloadCancelado:
            return
        finally:
            if sessao_sdk and sessao_sdk[0] >= 0:
                hikvision_sdk.fechar_sessao_sdk(sessao_sdk[0])
                sessao_sdk[0] = -1


def _executar_job_seguro(job):
    """Wrapper executado na ThreadPool para garantir limpeza de estado e captura de erros."""
    job_id = job[0]
    try:
        processar_job(job)
    except Exception as erro:
        if not job_foi_cancelado(job_id):
            detalhe = descrever_erro(f"Falha no trabalhador: {erro}")
            print(f"[job {job_id}] {detalhe}")
            traceback.print_exc()
            with banco_lock:
                banco.execute(
                    "UPDATE jobs SET status='erro', erro_detalhe=?, finalizado_em=COALESCE(finalizado_em, CURRENT_TIMESTAMP) WHERE id=?",
                    (detalhe, job_id),
                )
                banco.commit()
    finally:
        with jobs_ativos_lock:
            jobs_em_execucao.discard(job_id)
            jobs_cancelados.discard(job_id)


def loop_trabalhador():
    """
    Loop principal do trabalhador em segundo plano.
    Utiliza um ThreadPoolExecutor para permitir até MAX_JOBS_SIMULTANEOS downloads
    concorrentes sem travar a fila quando um download longo está em andamento.
    """
    worker_state["status"] = "executando"
    executor = ThreadPoolExecutor(max_workers=MAX_JOBS_SIMULTANEOS, thread_name_prefix="WorkerDVR")
    while True:
        try:
            worker_state["status"] = "executando"
            worker_state["erro"] = None
            worker_state["atualizado_em"] = datetime.now(timezone.utc).isoformat()
            with banco_lock:
                jobs = banco.execute(
                    "SELECT id, dvr_ip, usuario, senha, cameras, data_inicio, data_fim, "
                    "escola, codigo_escola, modo_download, porta_sdk "
                    "FROM jobs WHERE status = 'novo' ORDER BY id ASC"
                ).fetchall()
            for job in jobs:
                job_id = job[0]
                with jobs_ativos_lock:
                    if job_id in jobs_em_execucao:
                        continue
                    jobs_em_execucao.add(job_id)
                executor.submit(_executar_job_seguro, job)
        except Exception as erro:
            worker_state["status"] = "erro"
            worker_state["erro"] = str(erro)
            worker_state["atualizado_em"] = datetime.now(timezone.utc).isoformat()
            print(f"[worker] falha na rodada: {erro}")
            traceback.print_exc()
        finally:
            time.sleep(INTERVALO_CICLO_SEGUNDOS)


# ---------------------------------------------------------------------------
# Site (Flask) — só cria jobs e mostra status, quem baixa é o trabalhador
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static")


@app.route("/")
def pagina_inicial():
    return send_from_directory("static", "index.html")


@app.route("/api/jobs", methods=["POST"])
def criar_job():
    dados = request.get_json()
    if not isinstance(dados, dict):
        return jsonify({"erro": "O corpo da requisicao deve ser JSON."}), 400
    campos_obrigatorios = ["dvr_ip", "usuario", "senha", "cameras", "data_inicio", "data_fim", "codigo_escola"]
    faltando = [c for c in campos_obrigatorios if not str(dados.get(c, "")).strip()]
    if faltando:
        return jsonify({"erro": f"Campos obrigatórios faltando: {', '.join(faltando)}"}), 400

    cod_escola = str(dados.get("codigo_escola", "")).strip()
    if not cod_escola:
        return jsonify({"erro": "O código da escola é obrigatório."}), 400

    cameras_brutas = str(dados.get("cameras", "")).strip()
    cameras_validas = []
    for c in cameras_brutas.split(","):
        c_str = c.strip()
        if not c_str:
            continue
        if not c_str.isdigit() or int(c_str) < 1:
            return jsonify({"erro": f"Câmera '{c_str}' inválida. Informe números positivos (ex: 1, 2, 5)."}), 400
        cameras_validas.append(c_str)
    if not cameras_validas:
        return jsonify({"erro": "Informe ao menos uma câmera válida."}), 400
    cameras_csv = ",".join(cameras_validas)

    try:
        inicio = datetime.fromisoformat(dados["data_inicio"].replace("Z", "+00:00"))
        fim = datetime.fromisoformat(dados["data_fim"].replace("Z", "+00:00"))
    except ValueError:
        return jsonify({"erro": "As datas devem estar no formato ISO valido."}), 400
    if fim <= inicio:
        return jsonify({"erro": "O horario final deve ser depois do horario inicial."}), 400

    modo_download = dados.get("modo_download", "auto")
    if modo_download not in ("auto", "sdk", "isapi"):
        modo_download = "auto"

    try:
        porta_sdk = int(dados.get("porta_sdk", 8000))
    except (ValueError, TypeError):
        porta_sdk = 8000

    dvr_ip_bruto = str(dados.get("dvr_ip", "")).strip()
    dvr_ip_limpo, porta_detectada = hikvision_sdk._limpar_ip_e_porta(dvr_ip_bruto, porta_sdk)
    if "porta_sdk" not in dados or not dados["porta_sdk"]:
        porta_sdk = porta_detectada

    usuario_limpo = str(dados["usuario"]).strip()
    senha_limpa = str(dados["senha"]).strip()

    with banco_lock:
        cursor = banco.execute(
            "INSERT INTO jobs (dvr_ip, usuario, senha, cameras, data_inicio, data_fim, escola, codigo_escola, modo_download, porta_sdk) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dvr_ip_limpo, usuario_limpo, senha_limpa, cameras_csv,
             dados["data_inicio"], dados["data_fim"], dados.get("escola", ""),
             cod_escola, modo_download, porta_sdk),
        )
        
        # Atualiza o IP e credenciais da escola na tabela escolas caso tenha codigo informado
        nome_escola = str(dados.get("escola", "")).strip()
        dvr_ip = dvr_ip_limpo
        banco.execute("""
            INSERT INTO escolas (codigo, nome, dvr_ip, dvr_usuario, dvr_senha, atualizado_em)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(codigo) DO UPDATE SET
                nome=CASE WHEN ? != '' THEN ? ELSE nome END,
                dvr_ip=CASE WHEN ? != '' THEN ? ELSE dvr_ip END,
                dvr_usuario=CASE WHEN ? != '' THEN ? ELSE dvr_usuario END,
                dvr_senha=CASE WHEN ? != '' THEN ? ELSE dvr_senha END,
                atualizado_em=CURRENT_TIMESTAMP
        """, (
            cod_escola, nome_escola or cod_escola, dvr_ip, usuario_limpo, senha_limpa,
            nome_escola, nome_escola, dvr_ip, dvr_ip, usuario_limpo, usuario_limpo, senha_limpa, senha_limpa
        ))

        banco.commit()
        job_id = cursor.lastrowid

    return jsonify({"id": job_id, "status": "novo"}), 201


def _parsear_dvrs_json(valor):
    if not valor:
        return []
    try:
        dados = json.loads(valor)
        return dados if isinstance(dados, list) else []
    except Exception:
        return []


@app.route("/api/escolas", methods=["GET"])
def buscar_escolas():
    termo = request.args.get("q", "").strip()
    with banco_lock:
        if termo:
            linhas = banco.execute(
                "SELECT codigo, nome, dvr_ip, cidade, dvr_usuario, dvr_senha, dvr_modelo, dvr_canais, dvrs_json FROM escolas "
                "WHERE codigo LIKE ? OR nome LIKE ? OR cidade LIKE ? "
                "ORDER BY nome ASC LIMIT 50",
                (f"%{termo}%", f"%{termo}%", f"%{termo}%")
            ).fetchall()
        else:
            linhas = banco.execute(
                "SELECT codigo, nome, dvr_ip, cidade, dvr_usuario, dvr_senha, dvr_modelo, dvr_canais, dvrs_json FROM escolas ORDER BY nome ASC"
            ).fetchall()
    return jsonify([
        {
            "codigo": l[0],
            "nome": l[1],
            "dvr_ip": l[2] or "",
            "cidade": l[3] or "",
            "dvr_usuario": l[4] or "admin",
            "dvr_senha": l[5] or "",
            "dvr_modelo": l[6] or "",
            "dvr_canais": l[7] or "",
            "dvrs": _parsear_dvrs_json(l[8]),
        }
        for l in linhas
    ])


@app.route("/api/escolas/<codigo>", methods=["GET"])
def obter_escola(codigo):
    with banco_lock:
        linha = banco.execute(
            "SELECT codigo, nome, dvr_ip, cidade, dvr_usuario, dvr_senha, dvr_modelo, dvr_canais, dvrs_json FROM escolas WHERE codigo=?",
            (str(codigo).strip(),)
        ).fetchone()
    if not linha:
        return jsonify({"erro": "Escola nao encontrada"}), 404
    return jsonify({
        "codigo": linha[0],
        "nome": linha[1],
        "dvr_ip": linha[2] or "",
        "cidade": linha[3] or "",
        "dvr_usuario": linha[4] or "admin",
        "dvr_senha": linha[5] or "",
        "dvr_modelo": linha[6] or "",
        "dvr_canais": linha[7] or "",
        "dvrs": _parsear_dvrs_json(linha[8]),
    })


@app.route("/api/escolas/<codigo>/salvar-ip", methods=["POST"])
def salvar_ip_escola(codigo):
    dados = request.get_json(silent=True) or {}
    novo_ip = str(dados.get("dvr_ip", "")).strip()
    codigo = str(codigo).strip()

    if novo_ip:
        if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", novo_ip):
            return jsonify({"erro": "Endereço IP inválido (exemplo: 192.168.1.10)."}), 400

    with banco_lock:
        escola = banco.execute("SELECT codigo, nome FROM escolas WHERE codigo=?", (codigo,)).fetchone()
        if not escola:
            return jsonify({"erro": f"Unidade com código {codigo} não encontrada."}), 404

        banco.execute(
            "UPDATE escolas SET dvr_ip=?, atualizado_em=CURRENT_TIMESTAMP WHERE codigo=?",
            (novo_ip if novo_ip else None, codigo)
        )
        banco.commit()

    return jsonify({
        "sucesso": True,
        "codigo": codigo,
        "nome": escola[1],
        "dvr_ip": novo_ip
    })


@app.route("/api/dvr/status", methods=["GET"])
def verificar_status_dvr():
    """
    Verifica rapidamente (timeout 1.2s) se o IP da DVR está online na rede
    (testando a porta SDK 8000 e/ou porta HTTP 80), sem interferir na estrutura de download.
    Pode receber ?ip=10.x.x.x e/ou ?codigo=3112.
    """
    ip_param = request.args.get("ip", "").strip()
    codigo_param = request.args.get("codigo", "").strip()
    try:
        porta_sdk = int(request.args.get("porta_sdk", 8000))
    except (ValueError, TypeError):
        porta_sdk = 8000

    nome_escola = ""
    if codigo_param:
        with banco_lock:
            esc = banco.execute(
                "SELECT nome, dvr_ip FROM escolas WHERE codigo=?",
                (codigo_param,)
            ).fetchone()
            if esc:
                nome_escola = esc[0] or ""
                if not ip_param:
                    ip_param = (esc[1] or "").strip()

    if not ip_param:
        return jsonify({
            "online": False,
            "sem_ip": True,
            "ip": "",
            "codigo": codigo_param,
            "nome": nome_escola,
            "porta_ativa": None
        })

    ip_limpo, porta_detectada = hikvision_sdk._limpar_ip_e_porta(ip_param, porta_sdk)
    online_sdk = dvr_esta_acessivel(ip_limpo, porta=porta_detectada, timeout=1.2)
    online_http = False if online_sdk else dvr_esta_acessivel(ip_limpo, porta=80, timeout=1.2)
    online = bool(online_sdk or online_http)
    porta_ativa = porta_detectada if online_sdk else (80 if online_http else None)

    return jsonify({
        "online": online,
        "sem_ip": False,
        "ip": ip_limpo,
        "codigo": codigo_param,
        "nome": nome_escola,
        "porta_ativa": porta_ativa
    })


@app.route("/api/dvr/porta-sdk", methods=["POST"])
def api_detectar_porta_sdk():
    """Consulta a propria DVR e devolve a porta do servidor (SDK). Corpo: {dvr_ip, usuario, senha, porta_http?}."""
    dados = request.get_json(silent=True) or {}
    ip = str(dados.get("dvr_ip", "")).strip()
    if not ip:
        return jsonify({"porta_sdk": None, "erro": "IP nao informado"}), 400
    try:
        porta_http = int(dados.get("porta_http") or 80)
    except (ValueError, TypeError):
        porta_http = 80
    porta = detectar_porta_sdk(ip, str(dados.get("usuario") or "admin").strip(),
                               str(dados.get("senha") or "").strip(), porta_http, usar_cache=False)
    return jsonify({"porta_sdk": porta, "detectada": porta is not None})


@app.route("/api/jobs", methods=["GET"])
def listar_jobs():
    with banco_lock:
        jobs = banco.execute(
            "SELECT id, dvr_ip, escola, codigo_escola, cameras, data_inicio, data_fim, "
            "status, criado_em, iniciado_em, modo_download, porta_sdk, erro_detalhe, "
            "finalizado_em, protocolo_ativo "
            "FROM jobs ORDER BY id DESC"
        ).fetchall()
        resultado = []
        for j in jobs:
            job_id = j[0]
            total, concluidos, erros, bytes_total, bytes_baixados = banco.execute(
                "SELECT COUNT(*), SUM(status='concluido'), SUM(status='erro'), "
                "COALESCE(SUM(tamanho), 0), COALESCE(SUM(bytes_baixados), 0) "
                "FROM gravacoes WHERE job_id=?", (job_id,)
            ).fetchone()
            ativo = banco.execute(
                "SELECT nome_trecho, camera, status, ultimo_erro FROM gravacoes "
                "WHERE job_id=? AND status IN ('baixando','erro') "
                "ORDER BY atualizado_em DESC LIMIT 1", (job_id,)
            ).fetchone()
            percentual = min(100.0, round(bytes_baixados * 100 / bytes_total, 1)) if bytes_total else 0
            tempo_segundos = 0
            if j[9]:
                inicio_download = datetime.fromisoformat(j[9].replace("Z", "+00:00"))
                if inicio_download.tzinfo is None:
                    inicio_download = inicio_download.replace(tzinfo=timezone.utc)
                if j[13]:
                    fim_download = datetime.fromisoformat(j[13].replace("Z", "+00:00"))
                elif j[7] in ("concluido", "cancelado", "erro", "sem_gravacoes"):
                    # Se o job ja foi finalizado mas nao tinha finalizado_em registrado, congela
                    fim_download = inicio_download
                else:
                    fim_download = datetime.now(timezone.utc)
                if fim_download.tzinfo is None:
                    fim_download = fim_download.replace(tzinfo=timezone.utc)
                tempo_segundos = max(0, int((fim_download - inicio_download).total_seconds()))

            if j[7] in ("concluido", "cancelado", "erro", "sem_gravacoes"):
                velocidade = 0
                restante = 0
            else:
                velocidade = bytes_baixados / tempo_segundos if tempo_segundos else 0
                restante = max(0, int((bytes_total - bytes_baixados) / velocidade)) if (velocidade and bytes_total > bytes_baixados) else 0
            arquivos = banco.execute(
                "SELECT camera, nome_trecho FROM gravacoes "
                "WHERE job_id=? AND status='concluido' ORDER BY camera, nome_trecho",
                (job_id,),
            ).fetchall()
            resultado.append({
                "id": j[0], "dvr_ip": j[1], "escola": j[2], "codigo_escola": j[3],
                "cameras": j[4], "data_inicio": j[5], "data_fim": j[6], "status": j[7],
                "criado_em": j[8], "iniciado_em": j[9], "modo_download": j[10] or "auto",
                "porta_sdk": j[11] or 8000,
                "erro_detalhe": j[12], "finalizado_em": j[13],
                "protocolo_ativo": j[14],
                "tempo_segundos": tempo_segundos,
                "segundos_restantes": restante, "bytes_por_segundo": int(velocidade),
                "trechos_total": total, "trechos_concluidos": concluidos or 0,
                "trechos_erro": erros or 0, "bytes_total": bytes_total,
                "bytes_baixados": bytes_baixados, "progresso_percentual": percentual,
                "trecho_atual": ativo[0] if ativo else None,
                "camera_atual": ativo[1] if ativo else None,
                "erro_atual": ativo[3] if ativo and ativo[2] == "erro" else None,
                "arquivos": [
                    {
                        "camera": arquivo[0],
                        "nome": arquivo[1],
                        "url": url_for(
                            "servir_gravacao", job_id=job_id, camera=arquivo[0],
                            nome_trecho=arquivo[1],
                        ),
                    }
                    for arquivo in arquivos
                ],
            })
    return jsonify(resultado)


@app.route("/api/health", methods=["GET"])
def saude_aplicacao():
    resposta = dict(worker_state)
    resposta["fuso"] = NOME_FUSO
    try:
        with banco_lock:
            # Conta cada pedido de gravação (job) concluído como 1 gravação concluída
            # (em vez de contar cada trecho/segmento individual de vídeo).
            total_c = banco.execute("SELECT COUNT(*) FROM jobs WHERE status='concluido'").fetchone()[0]
        resposta["total_concluidas"] = total_c or 0
    except Exception:
        resposta["total_concluidas"] = 0
    return jsonify(resposta)


@app.route("/api/jobs/finalizados", methods=["DELETE"])
def excluir_jobs_finalizados():
    with banco_lock:
        status_finalizados = ("concluido", "erro", "sem_gravacoes", "cancelado")
        placeholders_fin = ",".join("?" for _ in status_finalizados)

        protegidos = banco.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status NOT IN ({placeholders_fin})",
            status_finalizados,
        ).fetchone()[0] or 0

        jobs_para_remover = banco.execute(
            f"SELECT id, status FROM jobs WHERE status IN ({placeholders_fin})",
            status_finalizados,
        ).fetchall()

        removidos = 0
        if jobs_para_remover:
            for j_id, j_status in jobs_para_remover:
                if j_status == "concluido":
                    arquivar_job_concluido(j_id)

            ids_remover = [j[0] for j in jobs_para_remover]
            removidos = len(ids_remover)
            chunk_size = 500
            for i in range(0, len(ids_remover), chunk_size):
                chunk = ids_remover[i:i + chunk_size]
                chunk_placeholders = ",".join("?" for _ in chunk)
                banco.execute(f"DELETE FROM gravacoes WHERE job_id IN ({chunk_placeholders})", chunk)
                banco.execute(f"DELETE FROM jobs WHERE id IN ({chunk_placeholders})", chunk)
            banco.commit()

    return jsonify({"removidos": removidos, "protegidos": protegidos})


@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def excluir_job(job_id):
    with banco_lock:
        arquivar_job_concluido(job_id)
        banco.execute("DELETE FROM gravacoes WHERE job_id=?", (job_id,))
        banco.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        banco.commit()
    return jsonify({"id": job_id, "excluido": True})


@app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
def cancelar_job(job_id):
    # Registra o ID em memória para abortar imediatamente qualquer download/reconexão em andamento
    jobs_cancelados.add(job_id)
    with banco_lock:
        job = banco.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            return jsonify({"erro": "Job nao encontrado."}), 404
        # Ao clicar em cancelar, interrompe o download e remove o pedido da lista imediatamente
        arquivar_job_concluido(job_id)
        banco.execute("DELETE FROM gravacoes WHERE job_id=?", (job_id,))
        banco.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        banco.commit()
    return jsonify({"id": job_id, "status": "cancelado", "excluido": True})


@app.route("/api/jobs/<int:job_id>/retry", methods=["POST"])
def reiniciar_job(job_id):
    """
    Recoloca um job com erro ou sem gravações de volta na fila com o status inicial 'novo'.
    Preserva trechos de gravação já concluídos para retomar o download de onde parou.
    """
    with banco_lock:
        job = banco.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return jsonify({"erro": "Job não encontrado."}), 404

        status_atual = job[0]
        if status_atual not in ("erro", "sem_gravacoes"):
            return jsonify({
                "erro": f"Job com status '{status_atual}' não pode ser reiniciado. Apenas jobs com status 'erro' ou 'sem_gravacoes' são permitidos."
            }), 409

        # O worker seleciona jobs pendentes com status = 'novo'
        banco.execute(
            "UPDATE jobs SET status='novo', erro_detalhe=NULL, iniciado_em=NULL, "
            "finalizado_em=NULL, protocolo_ativo=NULL WHERE id=?",
            (job_id,),
        )
        # Apaga em gravacoes apenas linhas com status != 'concluido'
        # para o worker refazer a busca e retomar trechos concluídos com segurança
        banco.execute(
            "DELETE FROM gravacoes WHERE job_id=? AND status != 'concluido'",
            (job_id,),
        )
        banco.commit()

    with jobs_ativos_lock:
        jobs_cancelados.discard(job_id)

    return jsonify({"id": job_id, "status": "novo"}), 200


@app.route("/api/gravacoes/<int:job_id>/<int:camera>/<path:nome_trecho>")
def servir_gravacao(job_id, camera, nome_trecho):
    with banco_lock:
        resultado = banco.execute(
            "SELECT caminho FROM gravacoes WHERE job_id=? AND camera=? "
            "AND nome_trecho=? AND status='concluido'",
            (job_id, camera, nome_trecho),
        ).fetchone()
    if not resultado or not resultado[0] or not os.path.isfile(resultado[0]):
        return jsonify({"erro": "Gravacao ainda nao esta disponivel."}), 404
    resp = send_file(resultado[0], mimetype="video/mp4", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/estatisticas", methods=["GET"])
def obter_estatisticas():
    with banco_lock:
        # 1. Unidades / Escolas
        total_escolas = banco.execute("SELECT COUNT(*) FROM escolas").fetchone()[0] or 0
        escolas_com_ip = banco.execute("SELECT COUNT(*) FROM escolas WHERE dvr_ip IS NOT NULL AND TRIM(dvr_ip) != ''").fetchone()[0] or 0
        escolas_sem_ip = total_escolas - escolas_com_ip

        # Lista de escolas com contagem de pedidos de gravação (jobs) concluídos (vivos + arquivados)
        query_escolas = """
            SELECT e.codigo, e.nome, e.cidade, e.dvr_ip,
                   (
                       COALESCE((SELECT COUNT(*) FROM jobs j WHERE j.codigo_escola = e.codigo AND j.status = 'concluido'), 0)
                       +
                       COALESCE((SELECT COUNT(*) FROM historico_jobs h WHERE h.codigo_escola = e.codigo), 0)
                   ) as total_gravacoes
            FROM escolas e
            ORDER BY total_gravacoes DESC, e.nome ASC
        """
        linhas_escolas = banco.execute(query_escolas).fetchall()
        lista_escolas = [
            {
                "codigo": r[0], "nome": r[1], "cidade": r[2] or "",
                "dvr_ip": r[3] or "", "gravacoes": r[4] or 0
            }
            for r in linhas_escolas
        ]

        # 2. Top 5 escolas com mais gravações (pedidos concluídos)
        top5 = [e for e in lista_escolas if e["gravacoes"] > 0][:5]

        # 3. Downloads por protocolo (SDK vs ISAPI) contados por pedido/job concluído (vivos + arquivados)
        sdk_vivos = banco.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE status='concluido' AND (protocolo_ativo = 'SDK' OR (protocolo_ativo IS NULL AND modo_download = 'sdk'))"
        ).fetchone()[0] or 0
        sdk_hist = banco.execute(
            "SELECT COUNT(*) FROM historico_jobs WHERE protocolo = 'SDK'"
        ).fetchone()[0] or 0
        sdk_count = sdk_vivos + sdk_hist

        isapi_vivos = banco.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE status='concluido' AND (protocolo_ativo = 'ISAPI' OR (protocolo_ativo IS NULL AND modo_download = 'isapi'))"
        ).fetchone()[0] or 0
        isapi_hist = banco.execute(
            "SELECT COUNT(*) FROM historico_jobs WHERE protocolo != 'SDK'"
        ).fetchone()[0] or 0
        isapi_count = isapi_vivos + isapi_hist

        jobs_vivos_total = banco.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] or 0
        jobs_hist_total = banco.execute("SELECT COUNT(*) FROM historico_jobs").fetchone()[0] or 0
        jobs_total = jobs_vivos_total + jobs_hist_total

        jobs_concluidos_vivos = banco.execute("SELECT COUNT(*) FROM jobs WHERE status='concluido'").fetchone()[0] or 0
        jobs_concluidos = jobs_concluidos_vivos + jobs_hist_total
        total_concluidas = jobs_concluidos

        outros = max(0, total_concluidas - (sdk_count + isapi_count))
        if outros > 0:
            isapi_count += outros

        total_trechos_vivos = banco.execute("SELECT COUNT(*) FROM gravacoes WHERE status='concluido'").fetchone()[0] or 0
        trechos_hist = banco.execute("SELECT SUM(trechos) FROM historico_jobs").fetchone()[0] or 0
        total_trechos_concluidos = total_trechos_vivos + (trechos_hist or 0)

        total_bytes_vivos = banco.execute("SELECT SUM(bytes_baixados) FROM gravacoes WHERE status='concluido'").fetchone()[0] or 0
        bytes_hist = banco.execute("SELECT SUM(bytes) FROM historico_jobs").fetchone()[0] or 0
        total_bytes = (total_bytes_vivos or 0) + (bytes_hist or 0)

        # 4. Estatísticas do Telegram
        total_telegram = banco.execute("SELECT COUNT(*) FROM solicitacoes_telegram").fetchone()[0] or 0
        tg_automaticos = banco.execute("SELECT COUNT(*) FROM solicitacoes_telegram WHERE job_id IS NOT NULL").fetchone()[0] or 0
        tg_aguardando_ip = banco.execute("SELECT COUNT(*) FROM solicitacoes_telegram WHERE status='aguardando_ip'").fetchone()[0] or 0
        tg_descartados = banco.execute("SELECT COUNT(*) FROM solicitacoes_telegram WHERE status='descartado'").fetchone()[0] or 0

    return jsonify({
        "unidades": {
            "total": total_escolas,
            "com_ip": escolas_com_ip,
            "sem_ip": escolas_sem_ip,
            "percentual_com_ip": round((escolas_com_ip / total_escolas * 100), 1) if total_escolas else 0,
            "lista": lista_escolas
        },
        "top5_unidades": top5,
        "protocolos": {
            "sdk": sdk_count,
            "isapi": isapi_count,
            "total": total_concluidas
        },
        "gravacoes": {
            "total_concluidas": total_concluidas,
            "total_trechos": total_trechos_concluidos,
            "total_bytes": total_bytes or 0
        },
        "telegram": {
            "total": total_telegram,
            "automaticos": tg_automaticos,
            "aguardando_ip": tg_aguardando_ip,
            "descartados": tg_descartados
        },
        "jobs": {
            "total": jobs_total,
            "concluidos": jobs_concluidos
        }
    })


# ---------------------------------------------------------------------------
# Armazenamento e Retenção de Gravações
# ---------------------------------------------------------------------------

_cache_armazenamento_lock = threading.Lock()
_cache_armazenamento_dados = None
_cache_armazenamento_expira_em = 0.0
CACHE_ARMAZENAMENTO_TTL = 20.0  # 20 segundos


def invalidar_cache_armazenamento():
    """Invalida o cache em memória das gravações armazenadas."""
    global _cache_armazenamento_dados, _cache_armazenamento_expira_em
    with _cache_armazenamento_lock:
        _cache_armazenamento_dados = None
        _cache_armazenamento_expira_em = 0.0


def excluir_gravacao_arquivo_seguro(rel_id):
    """
    Exclui com validação estrita de segurança um arquivo .mp4 dentro de PASTA_DESTINO.
    Remove pastas de câmera e job se ficarem vazias (via os.rmdir).
    NÃO altera linhas do banco SQLite (preserva estatísticas).
    Retorna (sucesso: bool, bytes_liberados: int, erro_msg: str | None, status_code: int).
    """
    if not rel_id or not isinstance(rel_id, str):
        return False, 0, "Identificador de gravação inválido.", 400

    rel_limpo = rel_id.strip()
    if not rel_limpo:
        return False, 0, "Identificador de gravação não informado.", 400

    # Rejeita caminhos absolutos
    if os.path.isabs(rel_limpo) or rel_limpo.startswith("/") or rel_limpo.startswith("\\"):
        return False, 0, "Caminho absoluto não permitido.", 400

    # Rejeita path traversal ('..')
    componentes = rel_limpo.replace("\\", "/").split("/")
    if ".." in componentes or "." in componentes:
        return False, 0, "Caminho inválido (path traversal não permitido).", 400

    if not rel_limpo.lower().endswith(".mp4"):
        return False, 0, "Apenas arquivos .mp4 podem ser excluídos.", 400

    try:
        pasta_base = os.path.realpath(PASTA_DESTINO)
        caminho_alvo = os.path.realpath(os.path.join(PASTA_DESTINO, rel_limpo))
    except Exception as e_res:
        return False, 0, f"Erro ao resolver caminho: {e_res}", 400

    try:
        caminho_comum = os.path.commonpath([pasta_base, caminho_alvo])
    except Exception:
        return False, 0, "Caminho fora da pasta de gravações.", 403

    if caminho_comum != pasta_base or caminho_alvo == pasta_base:
        return False, 0, "Acesso negado: o arquivo deve estar dentro da pasta de gravações.", 403

    if not os.path.isfile(caminho_alvo):
        return False, 0, "Arquivo de gravação não encontrado.", 404

    try:
        bytes_liberados = os.path.getsize(caminho_alvo)
        os.remove(caminho_alvo)
    except Exception as e_del:
        return False, 0, f"Falha ao remover arquivo: {e_del}", 500

    # Limpeza de pastas vazias (camera_N e pasta do job)
    try:
        pasta_camera = os.path.dirname(caminho_alvo)
        if pasta_camera != pasta_base and os.path.commonpath([pasta_base, pasta_camera]) == pasta_base:
            try:
                os.rmdir(pasta_camera)
                pasta_job = os.path.dirname(pasta_camera)
                if pasta_job != pasta_base and os.path.commonpath([pasta_base, pasta_job]) == pasta_base:
                    try:
                        os.rmdir(pasta_job)
                    except OSError:
                        pass
            except OSError:
                pass
    except Exception:
        pass

    invalidar_cache_armazenamento()
    return True, bytes_liberados, None, 200


def _coletar_dados_armazenamento():
    """Varre PASTA_DESTINO e coleta estatísticas e lista de arquivos .mp4."""
    pasta_abs = os.path.abspath(PASTA_DESTINO)

    # Valida acessibilidade da pasta de gravações
    acessivel = True
    erro_acesso = None
    try:
        if not os.path.exists(PASTA_DESTINO):
            try:
                os.makedirs(PASTA_DESTINO, exist_ok=True)
            except Exception as e_mkdir:
                acessivel = False
                erro_acesso = str(e_mkdir)
        else:
            with os.scandir(PASTA_DESTINO):
                pass
    except (PermissionError, OSError, Exception) as e_acc:
        acessivel = False
        erro_acesso = str(e_acc)

    if not acessivel:
        return {
            "pasta": pasta_abs,
            "acessivel": False,
            "erro": erro_acesso,
            "disco": None,
            "total_gravacoes": 0,
            "total_bytes": 0,
            "retencao_dias": RETENCAO_DIAS,
            "retencao_auto": RETENCAO_AUTO,
            "truncado": False,
            "gravacoes": [],
        }

    # Informações de disco
    disco_info = None
    try:
        du = shutil.disk_usage(PASTA_DESTINO)
        perc = round((du.used / du.total) * 100, 2) if du.total > 0 else 0.0
        disco_info = {
            "total": du.total,
            "usado": du.used,
            "livre": du.free,
            "percentual_usado": perc,
        }
    except Exception:
        disco_info = None

    LIMITE_ARQUIVOS = 5000
    truncado = False
    arquivos_coletados = []
    agora_ts = time.time()

    try:
        for raiz, _, files in os.walk(PASTA_DESTINO):
            for f in files:
                if not f.lower().endswith(".mp4"):
                    continue
                # Ignora arquivos temporários e intermediários
                if f.startswith(".") or f.endswith(".codec.mp4") or f.endswith(".convertendo.mp4") or f.endswith(".tmp") or f.endswith(".part"):
                    continue

                caminho_completo = os.path.join(raiz, f)
                try:
                    st = os.stat(caminho_completo)
                except (OSError, PermissionError):
                    continue

                mtime = st.st_mtime
                tamanho = st.st_size

                try:
                    rel = os.path.relpath(caminho_completo, PASTA_DESTINO).replace("\\", "/")
                except Exception:
                    continue

                partes = rel.split("/")
                pasta_job = partes[0] if len(partes) > 1 else ""

                camera_val = None
                if len(partes) >= 2:
                    m_cam = re.search(r"camera_?(\d+)", partes[-2], re.IGNORECASE)
                    if not m_cam:
                        m_cam = re.search(r"camera_?(\d+)", partes[-1], re.IGNORECASE)
                    if m_cam:
                        try:
                            camera_val = int(m_cam.group(1))
                        except ValueError:
                            camera_val = m_cam.group(1)
                else:
                    m_cam = re.search(r"camera_?(\d+)", partes[-1], re.IGNORECASE)
                    if m_cam:
                        try:
                            camera_val = int(m_cam.group(1))
                        except ValueError:
                            camera_val = m_cam.group(1)

                idade_segundos = max(0, int(agora_ts - mtime))
                dias_rest = max(0.0, round(float(RETENCAO_DIAS) - (idade_segundos / 86400.0), 1))
                if dias_rest == 0.0:
                    dias_rest = 0
                aviso_exclusao = bool(dias_rest <= 1)

                try:
                    baixado_em = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                except Exception:
                    baixado_em = datetime.now(timezone.utc).isoformat()

                item = {
                    "id": rel,
                    "pasta_job": pasta_job,
                    "camera": camera_val,
                    "arquivo": f,
                    "bytes": tamanho,
                    "baixado_em": baixado_em,
                    "idade_segundos": idade_segundos,
                    "dias_restantes": dias_rest,
                    "aviso_exclusao": aviso_exclusao,
                }
                arquivos_coletados.append((mtime, item))
                if len(arquivos_coletados) >= LIMITE_ARQUIVOS:
                    truncado = True
                    break
            if truncado:
                break
    except (PermissionError, OSError, Exception) as e_walk:
        return {
            "pasta": pasta_abs,
            "acessivel": False,
            "erro": str(e_walk),
            "disco": disco_info,
            "total_gravacoes": 0,
            "total_bytes": 0,
            "retencao_dias": RETENCAO_DIAS,
            "retencao_auto": RETENCAO_AUTO,
            "truncado": False,
            "gravacoes": [],
        }

    # Ordena da mais recente para a mais antiga (maior mtime primeiro)
    arquivos_coletados.sort(key=lambda x: x[0], reverse=True)
    gravacoes = [x[1] for x in arquivos_coletados]
    total_bytes = sum(x["bytes"] for x in gravacoes)

    return {
        "pasta": pasta_abs,
        "acessivel": True,
        "erro": None,
        "disco": disco_info,
        "total_gravacoes": len(gravacoes),
        "total_bytes": total_bytes,
        "retencao_dias": RETENCAO_DIAS,
        "retencao_auto": RETENCAO_AUTO,
        "truncado": truncado,
        "gravacoes": gravacoes,
    }


def obter_dados_armazenamento(forcar=False):
    """Retorna os dados de armazenamento usando cache de 20s (com lock)."""
    global _cache_armazenamento_dados, _cache_armazenamento_expira_em
    agora_ts = time.time()

    if not forcar:
        with _cache_armazenamento_lock:
            if _cache_armazenamento_dados is not None and agora_ts < _cache_armazenamento_expira_em:
                return _cache_armazenamento_dados

    dados = _coletar_dados_armazenamento()

    if dados.get("acessivel"):
        with _cache_armazenamento_lock:
            _cache_armazenamento_dados = dados
            _cache_armazenamento_expira_em = time.time() + CACHE_ARMAZENAMENTO_TTL

    return dados


def loop_retencao_automatica():
    """
    Thread daemon de retenção automática: executa 1x por hora (3600s).
    Apaga gravações .mp4 cuja idade seja superior a RETENCAO_DIAS.
    """
    print(f"[retencao] Thread de retenção automática iniciada (limite: {RETENCAO_DIAS} dias).")
    while True:
        try:
            limite_segundos = RETENCAO_DIAS * 86400
            agora = time.time()
            if os.path.exists(PASTA_DESTINO):
                for raiz, _, files in os.walk(PASTA_DESTINO):
                    for f in files:
                        if not f.lower().endswith(".mp4"):
                            continue
                        if f.startswith(".") or f.endswith(".codec.mp4") or f.endswith(".convertendo.mp4") or f.endswith(".tmp") or f.endswith(".part"):
                            continue
                        caminho_completo = os.path.join(raiz, f)
                        try:
                            st = os.stat(caminho_completo)
                            idade = agora - st.st_mtime
                            if idade > limite_segundos:
                                rel = os.path.relpath(caminho_completo, PASTA_DESTINO).replace("\\", "/")
                                ok, bytes_lib, erro_msg, _ = excluir_gravacao_arquivo_seguro(rel)
                                if ok:
                                    print(f"[retencao] Gravação expirada excluída: {rel} ({bytes_lib} bytes liberados)")
                                else:
                                    print(f"[retencao] Falha ao excluir gravação expirada {rel}: {erro_msg}")
                        except Exception as e_arq:
                            print(f"[retencao] Erro ao verificar arquivo {caminho_completo}: {e_arq}")
        except Exception as e_ciclo:
            print(f"[retencao] Erro no ciclo de retenção automática: {e_ciclo}")
        time.sleep(3600)


@app.route("/api/armazenamento", methods=["GET"])
def api_obter_armazenamento():
    forcar = request.args.get("forcar") in ("1", "true", "True")
    dados = obter_dados_armazenamento(forcar=forcar)
    return jsonify(dados)


@app.route("/api/armazenamento/gravacao", methods=["DELETE"])
def api_excluir_gravacao():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict) or "id" not in dados:
        return jsonify({"erro": "O corpo da requisição deve ser JSON contendo a chave 'id'."}), 400

    rel_id = dados.get("id")
    sucesso, bytes_liberados, erro_msg, status_code = excluir_gravacao_arquivo_seguro(rel_id)
    if not sucesso:
        return jsonify({"erro": erro_msg}), status_code

    return jsonify({"excluido": True, "bytes_liberados": bytes_liberados}), 200


# ---------------------------------------------------------------------------
# Rotas de Integracao com o Telegram
# ---------------------------------------------------------------------------

@app.route("/api/telegram/status", methods=["GET"])
def status_telegram():
    status = telegram_bot.obter_status()
    with banco_lock:
        total_pendentes = banco.execute(
            "SELECT COUNT(*) FROM solicitacoes_telegram WHERE status='aguardando_ip'"
        ).fetchone()[0]
        total_hoje = banco.execute(
            "SELECT COUNT(*) FROM solicitacoes_telegram WHERE date(recebido_em) = date('now')"
        ).fetchone()[0]
    status["pendentes_ip"] = total_pendentes
    status["total_hoje"] = total_hoje
    return jsonify(status)


@app.route("/api/telegram/solicitacoes", methods=["GET"])
def listar_solicitacoes_telegram():
    status_filtro = request.args.get("status", "").strip()
    with banco_lock:
        query = """
            SELECT s.id, s.telegram_message_id, s.solicitante, s.mensagem_original,
                   s.codigo_escola, s.nome_escola, s.cameras, s.data_inicio, s.data_fim,
                   s.dvr_ip, s.status, s.job_id, s.erro_motivo, s.recebido_em, s.processado_em,
                   j.status as job_status, e.dvr_ip as escola_ip_cadastrado
            FROM solicitacoes_telegram s
            LEFT JOIN jobs j ON s.job_id = j.id
            LEFT JOIN escolas e ON s.codigo_escola = e.codigo
        """
        params = []
        if status_filtro:
            query += " WHERE s.status = ?"
            params.append(status_filtro)
        query += " ORDER BY s.id DESC LIMIT 100"

        linhas = banco.execute(query, params).fetchall()

    resultado = []
    for l in linhas:
        resultado.append({
            "id": l[0],
            "telegram_message_id": l[1],
            "solicitante": l[2] or "",
            "mensagem_original": l[3] or "",
            "codigo_escola": l[4] or "",
            "nome_escola": l[5] or "",
            "cameras": l[6] or "1",
            "data_inicio": l[7] or "",
            "data_fim": l[8] or "",
            "dvr_ip": l[9] or l[16] or "",
            "status": l[10],
            "job_id": l[11],
            "erro_motivo": l[12] or "",
            "recebido_em": l[13] or "",
            "processado_em": l[14] or "",
            "job_status": l[15] or "",
            "tem_ip_cadastrado": bool(l[16])
        })
    return jsonify(resultado)


@app.route("/api/telegram/solicitacoes/<int:solic_id>/iniciar", methods=["POST"])
def iniciar_solicitacao_telegram(solic_id):
    dados = request.get_json() or {}
    dvr_ip_req = str(dados.get("dvr_ip", "")).strip()
    senha_req = str(dados.get("senha", "")).strip()
    usuario_req = str(dados.get("usuario", "")).strip() or "admin"
    cameras_req = str(dados.get("cameras", "")).strip()

    with banco_lock:
        solic = banco.execute("SELECT * FROM solicitacoes_telegram WHERE id=?", (solic_id,)).fetchone()
        if not solic:
            return jsonify({"erro": "Solicitacao nao encontrada."}), 404

        colunas = [c[1] for c in banco.execute("PRAGMA table_info(solicitacoes_telegram)").fetchall()]
        dados_solic = dict(zip(colunas, solic))

        cod_escola = str(dados_solic.get("codigo_escola") or "").strip()
        nome_escola = str(dados_solic.get("nome_escola") or "").strip()

        linha_escola = None
        if cod_escola:
            linha_escola = banco.execute(
                "SELECT dvr_ip, dvr_usuario, dvr_senha FROM escolas WHERE codigo=?", (cod_escola,)
            ).fetchone()

        ip_cadastrado = linha_escola[0] if linha_escola and linha_escola[0] else ""
        senha_cadastrada = linha_escola[2] if linha_escola and linha_escola[2] else ""
        usuario_cadastrado = linha_escola[1] if linha_escola and linha_escola[1] else "admin"

        ip_final = dvr_ip_req or ip_cadastrado or str(dados_solic.get("dvr_ip") or "").strip()
        if not ip_final:
            return jsonify({"erro": "Informe o endereco IP da DVR."}), 400

        senha_final = senha_req or senha_cadastrada or os.environ.get("DVR_SENHA_PADRAO", "")
        usuario_final = usuario_req or usuario_cadastrado or "admin"
        cams_final = cameras_req or str(dados.get("cameras") or dados_solic.get("cameras") or "1").strip()
        data_ini = str(dados.get("data_inicio", "")).strip() or dados_solic.get("data_inicio")
        data_fim = str(dados.get("data_fim", "")).strip() or dados_solic.get("data_fim")
        nome_escola = str(dados.get("nome_escola", "")).strip() or nome_escola
        cod_escola = str(dados.get("codigo_escola", "")).strip() or cod_escola

        if cod_escola:
            banco.execute("""
                INSERT INTO escolas (codigo, nome, dvr_ip, dvr_usuario, dvr_senha, atualizado_em)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(codigo) DO UPDATE SET
                    dvr_ip=?,
                    dvr_usuario=CASE WHEN ? != '' THEN ? ELSE dvr_usuario END,
                    dvr_senha=CASE WHEN ? != '' THEN ? ELSE dvr_senha END,
                    atualizado_em=CURRENT_TIMESTAMP
            """, (cod_escola, nome_escola or cod_escola, ip_final, usuario_final, senha_final,
                  ip_final, usuario_final, usuario_final, senha_final, senha_final))

        cursor = banco.execute("""
            INSERT INTO jobs (dvr_ip, usuario, senha, cameras, data_inicio, data_fim, escola, codigo_escola, modo_download, porta_sdk, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', 8000, 'novo')
        """, (ip_final, usuario_final, senha_final, cams_final, data_ini, data_fim, nome_escola, cod_escola))
        novo_job_id = cursor.lastrowid

        banco.execute("""
            UPDATE solicitacoes_telegram
            SET dvr_ip=?, status='baixando', job_id=?, processado_em=CURRENT_TIMESTAMP,
                cameras=?, data_inicio=?, data_fim=?, nome_escola=?, codigo_escola=?
            WHERE id=?
        """, (ip_final, novo_job_id, cams_final, data_ini, data_fim, nome_escola, cod_escola, solic_id))
        banco.commit()

    if telegram_bot.TELEGRAM_GROUP_ID:
        telegram_bot.enviar_mensagem_telegram(
            telegram_bot.TELEGRAM_GROUP_ID,
            f"🚀 <b>Download Iniciado pelo Operador!</b>\n"
            f"🏫 <b>Escola:</b> {nome_escola or 'Local'} (Cód: {cod_escola or '-'})\n"
            f"🌐 <b>IP Definido:</b> {ip_final}\n"
            f"📹 <b>Câmeras:</b> {cams_final}\n"
            f"📥 <i>Job #{novo_job_id} na fila de download do servidor.</i>",
            reply_to_message_id=dados_solic.get("telegram_message_id")
        )

    return jsonify({"sucesso": True, "job_id": novo_job_id, "status": "baixando"})


@app.route("/api/telegram/solicitacoes/<int:solic_id>/descartar", methods=["POST"])
def descartar_solicitacao_telegram(solic_id):
    with banco_lock:
        banco.execute(
            "UPDATE solicitacoes_telegram SET status='descartado', processado_em=CURRENT_TIMESTAMP WHERE id=?",
            (solic_id,)
        )
        banco.commit()
    return jsonify({"sucesso": True, "id": solic_id, "status": "descartado"})


# Carrega opcionalmente o plugin local de integração com o Rocket (ignorado pelo Git).
# Se a pasta `rocket_integracao/` não existir (ex: clone limpo do GitHub), o app segue 100% normal.
try:
    from rocket_integracao import registrar_rotas_rocket
    registrar_rotas_rocket(app, banco_path=BANCO_PATH)
except ImportError:
    pass


if __name__ == "__main__":
    preparar_banco()
    # Sobe o trabalhador numa thread separada, em segundo plano, e o site
    # na thread principal. daemon=True faz a thread do trabalhador fechar
    # junto se o programa principal for encerrado.
    threading.Thread(target=loop_trabalhador, daemon=True, name="WorkerPrincipal").start()
    if RETENCAO_AUTO:
        threading.Thread(target=loop_retencao_automatica, daemon=True, name="RetencaoAutomatica").start()
    telegram_bot.iniciar_servico_telegram()
    # threaded=True garante que o painel web continue respondendo mesmo se um
    # vídeo estiver sendo assistido pelo player simultaneamente.
    app.run(host="0.0.0.0", port=5000, threaded=True)
