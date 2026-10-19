#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10+ nao encontrado no PATH." >&2
    exit 1
fi

VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Criando ambiente virtual .venv..."
    python3 -m venv "$VENV_DIR" || {
        echo "Falha ao criar .venv. Instale o pacote python3-venv." >&2
        exit 1
    }
fi

echo "Atualizando pip e instalando dependencias..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"

if command -v ffmpeg >/dev/null 2>&1; then
    echo "FFmpeg encontrado."
else
    if [[ "${DVR_INSTALAR_DEPENDENCIAS_SISTEMA:-0}" == "1" ]] && command -v apt-get >/dev/null 2>&1; then
        echo "FFmpeg ausente. Instalando dependencia do sistema..."
        sudo apt-get update
        sudo apt-get install -y ffmpeg
    else
        echo "AVISO: FFmpeg nao encontrado. O corte/conversao usara GStreamer ou falhara."
        echo "Para instalar automaticamente no Ubuntu/Debian: DVR_INSTALAR_DEPENDENCIAS_SISTEMA=1 ./setup_and_run.sh"
        echo "Instalacao manual: sudo apt install ffmpeg"
    fi
fi

if command -v gst-launch-1.0 >/dev/null 2>&1; then
    echo "GStreamer encontrado."
else
    if [[ "${DVR_INSTALAR_DEPENDENCIAS_SISTEMA:-0}" == "1" ]] && command -v apt-get >/dev/null 2>&1; then
        echo "GStreamer ausente. Instalando dependencia do sistema..."
        sudo apt-get install -y gstreamer1.0-tools python3-gi python3-gst-1.0
    else
        echo "AVISO: GStreamer nao encontrado. Instale gstreamer1.0-tools python3-gi python3-gst-1.0."
    fi
fi

if [[ -f "$PROJECT_ROOT/sdk_dlls/libhcnetsdk.so" ]]; then
    echo "SDK Hikvision encontrado: porta 8000 disponivel."
else
    echo "AVISO: sdk_dlls/libhcnetsdk.so nao encontrado; sera usado ISAPI porta 80."
fi

SAVED_DEST_FILE="$PROJECT_ROOT/.destino_gravacoes"
DEFAULT_DEST="$PROJECT_ROOT/gravacoes"
if [[ -f "$SAVED_DEST_FILE" ]]; then
    SAVED_VAL="$(cat "$SAVED_DEST_FILE" | tr -d '\r\n')"
    if [[ -n "$SAVED_VAL" ]]; then
        DEFAULT_DEST="$SAVED_VAL"
    fi
fi

if [[ -n "${DVR_APP_DESTINO:-}" ]]; then
    DESTINO="$DVR_APP_DESTINO"
elif [[ "${1:-}" == "--auto" ]] || [[ ! -t 0 ]]; then
    DESTINO="$DEFAULT_DEST"
else
    read -r -p "Pasta de destino [$DEFAULT_DEST]: " DESTINO_INPUT
    DESTINO="${DESTINO_INPUT:-$DEFAULT_DEST}"
fi
mkdir -p "$DESTINO"
echo "$DESTINO" > "$SAVED_DEST_FILE"
export DVR_APP_DESTINO="$DESTINO"
export DVR_APP_BANCO="$PROJECT_ROOT/dvr_app.db"
export PYTHONUNBUFFERED="1"

if command -v ss >/dev/null 2>&1 && ss -ltn | awk '$4 ~ /:5000$/ { found=1 } END { exit !found }'; then
    echo "A porta 5000 ja esta ocupada. O servidor provavelmente ja esta rodando."
    echo "Acesse http://localhost:5000 ou encerre a instancia anterior antes de tentar novamente."
    exit 0
fi

IP_LOCAL="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Servidor: http://localhost:5000"
[[ -n "$IP_LOCAL" ]] && echo "Acesso pela rede: http://$IP_LOCAL:5000"
echo "Destino: $DESTINO"
echo "Watchdog de Alta Disponibilidade ATIVO: Se o servidor cair por qualquer motivo, sera reiniciado automaticamente em 5s."
echo "Iniciando app.py. Use Ctrl+C para parar definitivamente."

WATCHDOG_LOG="$PROJECT_ROOT/servidor_watchdog.log"
trap 'echo "[$(date "+%Y-%m-%d %H:%M:%S")] Servidor encerrado pelo usuario (SIGINT/SIGTERM)." >> "$WATCHDOG_LOG"; echo "Servidor encerrado."; exit 0' SIGINT SIGTERM

set +e
while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando servidor app.py..." >> "$WATCHDOG_LOG"
    "$VENV_PYTHON" "$PROJECT_ROOT/app.py"
    EXIT_CODE=$?
    if [[ $EXIT_CODE -eq 0 ]] || [[ $EXIT_CODE -eq 130 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Servidor finalizado normalmente (codigo $EXIT_CODE)." >> "$WATCHDOG_LOG"
        break
    fi
    MSG="[$(date '+%Y-%m-%d %H:%M:%S')] ALERTA: app.py fechou inesperadamente (codigo $EXIT_CODE). Reiniciando em 5 segundos..."
    echo "$MSG" | tee -a "$WATCHDOG_LOG"
    sleep 5
done

