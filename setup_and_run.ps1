#!/usr/bin/env pwsh
# setup_and_run.ps1 – Instalador + verificador rápido para Windows PowerShell
# ---------------------------------------------------------------
# 1. Verifica se o Python está instalado
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python não encontrado no PATH. Por favor, instale Python 3.10+ e adicione ao PATH antes de continuar." -ForegroundColor Red
    exit 1
}

# 2. Cria um ambiente virtual (venv) se ainda não existir
$venvPath = "venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "Criando ambiente virtual..."
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { Write-Host "Falha ao criar venv" -ForegroundColor Red; exit 1 }
}

# 3. Ativa o venv
$activateScript = Join-Path $venvPath "Scripts\activate.ps1"
if (Test-Path $activateScript) {
    . $activateScript
} else {
    Write-Host "Não foi possível encontrar o script de ativação. Abortando." -ForegroundColor Red
    exit 1
}

# 4. Instala dependências Python
Write-Host "Instalando dependências via pip..."
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Instalação de dependências falhou" -ForegroundColor Red; exit 1 }

# 5. Pergunta onde deseja guardar os vídeos (pasta de destino)
$destinoDefault = "C:\GravacoesOnda"
$destino = Read-Host "Informe a pasta onde os vídeos serão salvos [$destinoDefault]"
if ([string]::IsNullOrWhiteSpace($destino)) { $destino = $destinoDefault }

# cria a pasta caso não exista
if (-not (Test-Path $destino)) { New-Item -ItemType Directory -Path $destino | Out-Null }

# 6. Exporta a variável de ambiente usada pelo app
$env:DVR_APP_DESTINO = $destino
Write-Host "Variável DVR_APP_DESTINO definida para: $destino"

# 7. Inicia o servidor Flask
Write-Host "Iniciando o servidor..."
python app.py

# 8. Quando o servidor iniciar, ele escuta em 0.0.0.0:5000. Exibe o endereço local.
# (O próprio app imprime a URL, mas aqui reforçamos a informação)
Write-Host "Acesse a aplicação no seu navegador via: http://localhost:5000 ou http://<IP_da_Maquina>:5000" -ForegroundColor Green