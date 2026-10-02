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
    echo "AVISO: FFmpeg nao encontrado. O corte/conversao falhara."
    echo "Ubuntu/Debian: sudo apt install ffmpeg"
fi

if command -v gst-launch-1.0 >/dev/null 2>&1; then
    echo "GStreamer encontrado."
else
    echo "AVISO: GStreamer nao encontrado. Instale gstreamer1.0-tools python3-gi python3-gst-1.0."
fi

if [[ -f "$PROJECT_ROOT/sdk_dlls/libhcnetsdk.so" ]]; then
    echo "SDK Hikvision encontrado: porta 8000 disponivel."
else
    echo "AVISO: sdk_dlls/libhcnetsdk.so nao encontrado; sera usado ISAPI porta 80."
fi

if [[ -n "${DVR_APP_DESTINO:-}" ]]; then
    DESTINO="$DVR_APP_DESTINO"
else
    DEFAULT_DEST="$PROJECT_ROOT/gravacoes"
    if [[ -t 0 ]]; then
        read -r -p "Pasta de destino [$DEFAULT_DEST]: " DESTINO_INPUT
        DESTINO="${DESTINO_INPUT:-$DEFAULT_DEST}"
    else
        DESTINO="$DEFAULT_DEST"
    fi
fi
mkdir -p "$DESTINO"
export DVR_APP_DESTINO="$DESTINO"
export DVR_APP_BANCO="$PROJECT_ROOT/dvr_app.db"

if command -v ss >/dev/null 2>&1 && ss -ltn | awk '$4 ~ /:5000$/ { found=1 } END { exit !found }'; then
    echo "A porta 5000 ja esta ocupada. O servidor provavelmente ja esta rodando."
    echo "Acesse http://localhost:5000 ou encerre a instancia anterior antes de tentar novamente."
    exit 0
fi

IP_LOCAL="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Servidor: http://localhost:5000"
[[ -n "$IP_LOCAL" ]] && echo "Acesso pela rede: http://$IP_LOCAL:5000"
echo "Destino: $DESTINO"
echo "Iniciando app.py. Use Ctrl+C para parar."

exec "$VENV_PYTHON" "$PROJECT_ROOT/app.py"
