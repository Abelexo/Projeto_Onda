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
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

# Garante que gi/GStreamer possam ser importados pelo ambiente virtual (.venv)
CAMINHO_DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if os.path.isdir(CAMINHO_DIST_PACKAGES) and CAMINHO_DIST_PACKAGES not in sys.path:
    sys.path.append(CAMINHO_DIST_PACKAGES)

import requests
from requests.auth import HTTPDigestAuth
from flask import Flask, jsonify, request, send_file, send_from_directory, url_for

import hikvision_sdk

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

# Pasta onde os vídeos baixados são organizados. Como o app roda na mesma
# máquina do servidor de pastas, "enviar pro servidor" aqui é simplesmente
# salvar direto no caminho da pasta compartilhada (nem precisa copiar por
# rede). Ajuste para o caminho real da sua pasta compartilhada.
PASTA_DESTINO = os.environ.get("DVR_APP_DESTINO", "gravacoes")

BANCO_PATH = os.environ.get("DVR_APP_BANCO", "dvr_app.db")

# Intervalo entre "rodadas" do trabalhador: a cada X segundos ele olha o
# banco de novo, pega jobs novos e continua baixando pendentes.
INTERVALO_CICLO_SEGUNDOS = 5

# Quando a DVR fica inacessível no meio de um download, esse é o intervalo
# entre as tentativas de "ela já voltou?".
INTERVALO_RETRY_SEGUNDOS = 15


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

def conectar_banco():
    # check_same_thread=False porque o Flask (uma thread) e o trabalhador
    # (outra thread) acessam o mesmo arquivo de banco.
    conexao = sqlite3.connect(BANCO_PATH, check_same_thread=False)
    conexao.execute("PRAGMA journal_mode=WAL")  # deixa leitura/escrita concorrente mais segura
    return conexao


banco = conectar_banco()
# Um "cadeado" para garantir que duas threads nunca escrevam no banco ao
# mesmo tempo. SQLite não gosta disso, e o cadeado evita erros esquisitos.
banco_lock = threading.Lock()


def preparar_banco():
    with banco_lock:
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
                porta_sdk INTEGER DEFAULT 8000
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
            ("gravacoes", "bytes_baixados", "INTEGER DEFAULT 0"),
            ("gravacoes", "ultimo_erro", "TEXT"),
            ("gravacoes", "atualizado_em", "TEXT"),
        ):
            try:
                banco.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
            except sqlite3.OperationalError as erro:
                if "duplicate column name" not in str(erro).lower():
                    raise
        # Qualquer trecho que ficou "baixando" de uma execução anterior que
        # foi encerrada de forma abrupta volta a ser candidato a nova
        # tentativa.
        banco.execute("UPDATE gravacoes SET status='pendente' WHERE status='baixando'")
        banco.execute(
            "UPDATE jobs SET status='novo' WHERE status IN "
                "('buscando','baixando','convertendo','cortando')"
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
        banco.commit()


# ---------------------------------------------------------------------------
# Comunicação com a DVR (ISAPI)
# ---------------------------------------------------------------------------

def dvr_esta_acessivel(ip, porta=80, timeout=3):
    try:
        with socket.create_connection((ip, porta), timeout=timeout):
            return True
    except OSError:
        return False


def esperar_dvr_voltar(ip, marcar_status=None):
    """Fica em loop até a DVR responder de novo. 'marcar_status' é uma
    função opcional para você poder ir atualizando o status do job no
    banco enquanto espera (ex.: 'aguardando conexão')."""
    if marcar_status:
        marcar_status("aguardando_conexao")
    while not dvr_esta_acessivel(ip):
        time.sleep(INTERVALO_RETRY_SEGUNDOS)


def job_foi_cancelado(job_id):
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


def buscar_gravacoes(dvr_ip, auth, camera, data_inicio, data_fim, job_id):
    """Busca todas as páginas de resultado e salva cada trecho no banco
    como 'pendente'. Se a DVR cair durante a busca, espera ela voltar e
    continua da página em que parou (não perde o que já foi catalogado)."""
    posicao = 0
    while True:
        while True:
            try:
                r = requests.post(
                    f"http://{dvr_ip}/ISAPI/ContentMgmt/search",
                    data=montar_busca(camera, data_inicio, data_fim, posicao),
                    headers={"Content-Type": "application/xml"},
                    auth=auth, timeout=30,
                )
                r.raise_for_status()
                break
            except requests.exceptions.RequestException:
                esperar_dvr_voltar(dvr_ip)

        raiz = ET.fromstring(r.text)
        itens = raiz.findall(".//{*}searchMatchItem")
        if not itens:
            break

        with banco_lock:
            for item in itens:
                uri = item.find(".//{*}playbackURI").text
                inicio = item.find(".//{*}startTime").text
                fim = item.find(".//{*}endTime").text
                parametros = parse_qs(urlparse(uri).query)
                nome_trecho = parametros.get("name", [f"trecho_{posicao}"])[0]
                tamanho_bloco = int(parametros.get("size", [0])[0])

                # Calcula o tamanho estimado proporcional para a janela de tempo pedida
                try:
                    dt_ini_tr = datetime.fromisoformat(inicio.replace("Z", "+00:00"))
                    dt_fim_tr = datetime.fromisoformat(fim.replace("Z", "+00:00"))
                    dt_ini_ped = datetime.fromisoformat(data_inicio.replace("Z", "+00:00"))
                    dt_fim_ped = datetime.fromisoformat(data_fim.replace("Z", "+00:00"))
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

        posicao += len(itens)
        if len(itens) < 50:
            break


def formatar_tempo_isapi_compacto(iso_str):
    """Converte '2026-09-24T12:55:00Z' para o formato compacto '20260924T125500Z'."""
    return iso_str.replace("-", "").replace(":", "")


def baixar_um_trecho(
    dvr_ip, auth, uri, destino, tamanho_esperado, camera=None,
    data_inicio_pedido=None, data_fim_pedido=None, usuario="admin", senha="",
    atualizar_progresso=None
):
    """
    Baixa um trecho de gravação da DVR.
    
    Estratégia:
      1. Tenta baixar via RTSP Playback com recorte de tempo (porta 554).
         Baixa direto apenas os minutos pedidos (~35 MB em vez de 1 GB).
      2. Tenta downloadRequest ISAPI com playbackURI recortada.
      3. Fallback: Download do bloco físico com Range/retomada.
    """
    parcial = destino + ".parcial"
    ja_baixado = os.path.getsize(parcial) if os.path.exists(parcial) else 0
    if atualizar_progresso:
        atualizar_progresso(ja_baixado)

    sucesso = False

    # 1. Tentativa via RTSP Playback direto por tempo se houver FFmpeg
    ffmpeg_bin = shutil.which("ffmpeg") or (
        os.path.isfile("ffmpeg.exe") and os.path.abspath("ffmpeg.exe")
    ) or (
        os.path.isfile("ffmpeg") and os.path.abspath("ffmpeg")
    )

    if ffmpeg_bin and data_inicio_pedido and data_fim_pedido and camera:
        t_ini = formatar_tempo_isapi_compacto(data_inicio_pedido)
        t_fim = formatar_tempo_isapi_compacto(data_fim_pedido)
        # Formato de URL RTSP de reprodução por tempo aceito por Hikvision e JFL
        rtsp_url = f"rtsp://{usuario}:{senha}@{dvr_ip}:554/Streaming/tracks/{camera}01?starttime={t_ini}&endtime={t_fim}"
        cmd_rtsp = [
            ffmpeg_bin, "-y", "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-c", "copy",
            "-movflags", "+faststart",
            parcial
        ]
        try:
            res_rtsp = subprocess.run(cmd_rtsp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if res_rtsp.returncode == 0 and os.path.isfile(parcial) and os.path.getsize(parcial) > 1024 * 10:
                sucesso = True
        except Exception:
            pass

    # 2. Tentativas via ISAPI HTTP
    if not sucesso:
        uris_para_tentar = []
        if data_inicio_pedido and data_fim_pedido and camera:
            t_ini = formatar_tempo_isapi_compacto(data_inicio_pedido)
            t_fim = formatar_tempo_isapi_compacto(data_fim_pedido)
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
                    if eh_tentativa_corte and resp.status_code not in (200, 206):
                        continue

                    if resp.status_code not in (200, 206):
                        resp.raise_for_status()

                    modo = "ab" if resp.status_code == 206 else "wb"
                    with open(parcial, modo) as f:
                        for bloco in resp.iter_content(1024 * 1024):
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
    Converte o arquivo da DVR (.hik) para MP4 H.264 padrão e corta exatamente
    na janela de tempo solicitada pelo usuário.

    Por que H.264?
      - Funciona em 100% dos navegadores (Chrome, Edge, Firefox, celular) sem codecs extras.
      - H.265 trava nos navegadores e no Windows Server por falta de codec licenciado.

    Por que cortar aqui?
      - A DVR frequentemente entrega blocos físicos de até 1 GB (~2h de gravação).
      - Ao calcular o deslocamento inicial e a duração, cortamos exatamente os minutos
        pedidos (ex: 40 min / ~176 MB) e apagamos o arquivo bruto de 1 GB.
    """
    dt_ini_tr = datetime.fromisoformat(inicio_trecho.replace("Z", "+00:00"))
    dt_fim_tr = datetime.fromisoformat(fim_trecho.replace("Z", "+00:00"))
    dt_ini_ped = datetime.fromisoformat(data_inicio_pedido.replace("Z", "+00:00"))
    dt_fim_ped = datetime.fromisoformat(data_fim_pedido.replace("Z", "+00:00"))

    # Janela de intersecção entre o trecho baixado e o pedido do usuário
    corte_ini = max(dt_ini_tr, dt_ini_ped)
    corte_fim = min(dt_fim_tr, dt_fim_ped)

    deslocamento = max(0.0, (corte_ini - dt_ini_tr).total_seconds())
    duracao = max(0.0, (corte_fim - corte_ini).total_seconds())

    temporario = destino + ".convertendo.mp4"
    if os.path.exists(temporario):
        os.remove(temporario)

    # 1. Tenta usar o FFmpeg se estiver disponível (máxima velocidade e simplicidade)
    ffmpeg_bin = shutil.which("ffmpeg") or (
        os.path.isfile("ffmpeg.exe") and os.path.abspath("ffmpeg.exe")
    ) or (
        os.path.isfile("ffmpeg") and os.path.abspath("ffmpeg")
    )

    if ffmpeg_bin:
        comando = [
            ffmpeg_bin, "-y",
            "-ss", f"{deslocamento:.3f}",
            "-t", f"{duracao:.3f}",
            "-i", origem,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            temporario
        ]
        resultado = subprocess.run(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if resultado.returncode != 0:
            if os.path.exists(temporario):
                os.remove(temporario)
            raise RuntimeError(f"FFmpeg falhou ao converter/cortar: {resultado.stderr.decode('utf-8', errors='ignore')}")

        if not os.path.isfile(temporario) or os.path.getsize(temporario) == 0:
            raise RuntimeError("O FFmpeg nao gerou um arquivo valido")

        os.replace(temporario, destino)
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
        f'identity name=filtro ! x264enc speed-preset=ultrafast tune=zerolatency ! mp4mux ! filesink location="{temporario}"'
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
        if os.path.exists(temporario):
            os.remove(temporario)
        raise RuntimeError(f"GStreamer falhou ao converter/cortar: {erro} ({dbg})")

    pipeline.set_state(Gst.State.NULL)

    if not os.path.isfile(temporario) or os.path.getsize(temporario) == 0:
        raise RuntimeError("O corte/conversao GStreamer nao gerou um arquivo MP4 valido")

    os.replace(temporario, destino)


def nome_pasta_job(escola, codigo_escola, data_inicio, data_fim):
    inicio = datetime.fromisoformat(data_inicio.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    fim = datetime.fromisoformat(data_fim.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    identificador = f" - {codigo_escola.strip()}" if codigo_escola and codigo_escola.strip() else ""
    nome = f"{inicio} a {fim}{identificador} - {escola or 'DVR'}"
    return "".join("_" if caractere in '<>:"/\\|?*' else caractere for caractere in nome)


def baixar_pendentes_do_job(
    job_id, dvr_ip, auth, escola, codigo_escola, data_inicio, data_fim,
    atualizar_status_job, modo_download="auto", porta_sdk=8000,
):
    with banco_lock:
        pendentes = banco.execute(
            "SELECT dvr_ip, camera, nome_trecho, uri, tamanho, inicio, fim FROM gravacoes "
            "WHERE job_id=? AND status != 'concluido'",
            (job_id,),
        ).fetchall()

    for dvr_ip, camera, nome_trecho, uri, tamanho, inicio_trecho, fim_trecho in pendentes:
        if job_foi_cancelado(job_id):
            return
        pasta = os.path.join(
            PASTA_DESTINO,
            nome_pasta_job(escola, codigo_escola, data_inicio, data_fim),
            f"camera_{camera}",
        )
        os.makedirs(pasta, exist_ok=True)
        origem = os.path.join(pasta, f"{nome_trecho}.hik")
        try:
            h_ini = datetime.fromisoformat(data_inicio.replace("Z", "+00:00")).strftime("%d-%m-%Y_%Hh%M")
            h_fim = datetime.fromisoformat(data_fim.replace("Z", "+00:00")).strftime("%Hh%M")
            nome_arquivo_mp4 = f"camera_{camera}_{h_ini}_ate_{h_fim}.mp4"
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

        # Tenta baixar; se cair a conexão, espera a DVR voltar e tenta de
        # novo, quantas vezes for preciso — é aqui que entra o "esperar a
        # internet voltar" que você pediu.
        while True:
            try:
                # 1. Modo SDK (Porta SDK configurada) se configurado para 'auto' ou 'sdk'
                sucesso_download = False
                if modo_download in ("auto", "sdk") and hikvision_sdk.sdk_disponivel():
                    try:
                        dt_ini_ped = datetime.fromisoformat(data_inicio.replace("Z", "+00:00"))
                        dt_fim_ped = datetime.fromisoformat(data_fim.replace("Z", "+00:00"))
                        print(f"[job {job_id}] Tentando download direto via NetSDK (porta {porta_sdk})...")

                        def progresso_sdk(pct):
                            t_bytes = int(tamanho * (pct / 100.0))
                            atualizar_progresso(t_bytes)

                        sucesso_download = hikvision_sdk.baixar_por_tempo_sdk(
                            dvr_ip, porta_sdk, auth.username, auth.password,
                            camera, dt_ini_ped, dt_fim_ped, origem,
                            callback_progresso=progresso_sdk,
                            checar_cancelado=lambda: job_foi_cancelado(job_id)
                        )
                    except Exception as erro_sdk:
                        print(f"[job {job_id}] Tentativa SDK falhou: {erro_sdk}")
                        if modo_download == "sdk":
                            raise erro_sdk
                        print(f"[job {job_id}] Alternando para fallback ISAPI (porta 80)...")

                # 2. Modo ISAPI (Porta 80) caso o SDK não tenha sido usado ou tenha falhado
                if not sucesso_download:
                    baixar_um_trecho(
                        dvr_ip, auth, uri, origem, tamanho,
                        camera=camera, data_inicio_pedido=data_inicio,
                        data_fim_pedido=data_fim, usuario=auth.username, senha=auth.password,
                        atualizar_progresso=atualizar_progresso
                    )

                if job_foi_cancelado(job_id):
                    with banco_lock:
                        banco.execute(
                            "UPDATE gravacoes SET status='pendente' WHERE job_id=? "
                            "AND camera=? AND nome_trecho=?",
                            (job_id, camera, nome_trecho),
                        )
                        banco.commit()
                    return

                # Converte e corta para o período exato em H.264
                atualizar_status_job("convertendo")
                converter_e_cortar(
                    origem, destino, inicio_trecho, fim_trecho, data_inicio, data_fim
                )
                atualizar_status_job("baixando")

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
            except requests.exceptions.RequestException:
                # Problema de rede/conexão: espera e tenta de novo,
                # sem marcar como erro definitivo.
                if job_foi_cancelado(job_id):
                    return
                atualizar_status_job("aguardando_conexao")
                esperar_dvr_voltar(dvr_ip)
                if job_foi_cancelado(job_id):
                    return
                atualizar_status_job("baixando")
            except (ValueError, RuntimeError) as erro:
                # Arquivo realmente incompleto/corrompido: registra e
                # segue pro próximo trecho (o job inteiro não trava por
                # causa de um trecho problemático).
                with banco_lock:
                    banco.execute(
                        "UPDATE gravacoes SET status='erro', ultimo_erro=?, "
                        "atualizado_em=CURRENT_TIMESTAMP WHERE job_id=? AND dvr_ip=? "
                        "AND camera=? AND nome_trecho=?",
                        (str(erro), job_id, dvr_ip, camera, nome_trecho),
                    )
                    banco.commit()
                print(f"[job {job_id}] erro no trecho {nome_trecho}: {erro}")
                break


# ---------------------------------------------------------------------------
# O trabalhador (roda pra sempre em segundo plano)
# ---------------------------------------------------------------------------

def processar_job(job):
    job_id = job[0]
    dvr_ip = job[1]
    usuario = job[2]
    senha = job[3]
    cameras_csv = job[4]
    data_inicio = job[5]
    data_fim = job[6]
    escola = job[7]
    codigo_escola = job[8]
    modo_download = job[9] if len(job) > 9 and job[9] else "auto"
    porta_sdk = job[10] if len(job) > 10 and job[10] else 8000

    auth = HTTPDigestAuth(usuario, senha)
    cameras = [int(c.strip()) for c in cameras_csv.split(",") if c.strip()]

    def atualizar_status_job(novo_status):
        with banco_lock:
            banco.execute(
                "UPDATE jobs SET status=?, iniciado_em=CASE WHEN ? IN "
                "('baixando','convertendo') THEN COALESCE(iniciado_em, "
                "CURRENT_TIMESTAMP) ELSE iniciado_em END "
                "WHERE id=? AND status != 'cancelado'",
                (novo_status, novo_status, job_id),
            )
            banco.commit()

    atualizar_status_job("buscando")
    for camera in cameras:
        if job_foi_cancelado(job_id):
            return
        buscar_gravacoes(dvr_ip, auth, camera, data_inicio, data_fim, job_id)

    if job_foi_cancelado(job_id):
        return
    with banco_lock:
        quantidade_gravacoes = banco.execute(
            "SELECT COUNT(*) FROM gravacoes WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    if quantidade_gravacoes == 0:
        print(f"[job {job_id}] nenhuma gravacao encontrada no periodo/camera informado")
        atualizar_status_job("sem_gravacoes")
        return
    atualizar_status_job("baixando")
    baixar_pendentes_do_job(
        job_id, dvr_ip, auth, escola, codigo_escola, data_inicio, data_fim,
        atualizar_status_job, modo_download=modo_download, porta_sdk=porta_sdk,
    )

    # Só marca como concluído se não sobrou nenhum trecho pendente/baixando
    # (trechos com erro definitivo não travam a conclusão do job, mas
    # ficam visíveis no status para você conferir).
    if job_foi_cancelado(job_id):
        return
    with banco_lock:
        restantes = banco.execute(
            "SELECT COUNT(*) FROM gravacoes WHERE job_id=? AND status IN ('pendente','baixando')",
            (job_id,),
        ).fetchone()[0]
    atualizar_status_job("concluido" if restantes == 0 else "erro")


def loop_trabalhador():
    preparar_banco()
    while True:
        with banco_lock:
            jobs = banco.execute(
                "SELECT id, dvr_ip, usuario, senha, cameras, data_inicio, data_fim, "
                "escola, codigo_escola, modo_download, porta_sdk "
                "FROM jobs WHERE status IN ('novo','erro')"
            ).fetchall()
        for job in jobs:
            try:
                processar_job(job)
            except Exception as erro:
                print(f"[job {job[0]}] falhou: {erro}")
                with banco_lock:
                    banco.execute("UPDATE jobs SET status='erro' WHERE id=?", (job[0],))
                    banco.commit()
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
    campos_obrigatorios = ["dvr_ip", "usuario", "senha", "cameras", "data_inicio", "data_fim"]
    faltando = [c for c in campos_obrigatorios if not dados.get(c)]
    if faltando:
        return jsonify({"erro": f"Campos faltando: {', '.join(faltando)}"}), 400
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

    with banco_lock:
        cursor = banco.execute(
            "INSERT INTO jobs (dvr_ip, usuario, senha, cameras, data_inicio, data_fim, escola, codigo_escola, modo_download, porta_sdk) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dados["dvr_ip"], dados["usuario"], dados["senha"], dados["cameras"],
             dados["data_inicio"], dados["data_fim"], dados.get("escola", ""),
             dados.get("codigo_escola", ""), modo_download, porta_sdk),
        )
        banco.commit()
        job_id = cursor.lastrowid

    return jsonify({"id": job_id, "status": "novo"}), 201


@app.route("/api/jobs", methods=["GET"])
def listar_jobs():
    with banco_lock:
        jobs = banco.execute(
            "SELECT id, dvr_ip, escola, codigo_escola, cameras, data_inicio, data_fim, "
            "status, criado_em, iniciado_em, modo_download, porta_sdk "
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
                tempo_segundos = max(0, int((datetime.now(timezone.utc) - inicio_download).total_seconds()))
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


@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def excluir_job(job_id):
    with banco_lock:
        banco.execute("DELETE FROM gravacoes WHERE job_id=?", (job_id,))
        banco.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        banco.commit()
    return jsonify({"id": job_id, "excluido": True})


@app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
def cancelar_job(job_id):
    with banco_lock:
        job = banco.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            return jsonify({"erro": "Job nao encontrado."}), 404
        if job[0] in ("concluido", "cancelado"):
            return jsonify({"erro": f"Job ja esta {job[0]}."}), 409
        banco.execute("UPDATE jobs SET status='cancelado' WHERE id=?", (job_id,))
        banco.execute(
            "UPDATE gravacoes SET status='pendente' WHERE job_id=? AND status='baixando'",
            (job_id,),
        )
        banco.commit()
    return jsonify({"id": job_id, "status": "cancelado"})


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
    return send_file(resultado[0], mimetype="video/mp4", conditional=True)


if __name__ == "__main__":
    # Sobe o trabalhador numa thread separada, em segundo plano, e o site
    # na thread principal. daemon=True faz a thread do trabalhador fechar
    # junto se o programa principal for encerrado.
    threading.Thread(target=loop_trabalhador, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
