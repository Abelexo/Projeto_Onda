#!/usr/bin/env bash
# setup_and_run.sh – Instalador rápido para Linux/macOS
# ---------------------------------------------------
# 1. Verifica se o Python está disponível
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 não encontrado. Instale Python 3.10+ antes de continuar." >&2
    exit 1
fi

# 2. Cria ambiente virtual se ainda não existir
VENV_DIR="venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Criando ambiente virtual..."
    python3 -m venv "$VENV_DIR" || { echo "Falha ao criar venv" >&2; exit 1; }
fi

# 3. Ativa o venv
source "$VENV_DIR/bin/activate"

# 4. Instala dependências Python
echo "Instalando dependências via pip..."
pip install -r requirements.txt || { echo "Instalação de dependências falhou" >&2; exit 1; }

# 5. Pergunta onde salvar os vídeos
DEFAULT_DEST="/opt/gravacoes_onda"
read -p "Informe a pasta onde os vídeos serão salvos [$DEFAULT_DEST]: " DESTINO
DESTINO=${DESTINO:-$DEFAULT_DEST}
# cria a pasta caso não exista
mkdir -p "$DESTINO"

# 6. Exporta variável de ambiente usada pelo app
export DVR_APP_DESTINO="$DESTINO"
 echo "Variável DVR_APP_DESTINO definida para: $DESTINO"

# 7. Inicia o servidor Flask
echo "Iniciando o servidor..."
python3 app.py
# Quando o servidor iniciar, ele escuta em 0.0.0.0:5000
# Acesse via http://localhost:5000 ou http://<IP_da_Maquina>:5000
