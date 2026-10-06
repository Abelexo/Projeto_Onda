$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Host "Python 3.10+ nao encontrado no PATH." -ForegroundColor Red
    exit 1
}

$venvPath = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Criando ambiente virtual .venv..." -ForegroundColor Cyan
    & python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Falha ao criar .venv" }
}

Write-Host "Atualizando pip e instalando dependencias..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Falha ao atualizar pip" }
& $venvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar requirements.txt" }

$ffmpegLocal = Join-Path $ProjectRoot "ffmpeg.exe"
$ffmpegPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpegPath -and -not (Test-Path $ffmpegLocal)) {
    Write-Host "FFmpeg nao encontrado. Baixando automaticamente..." -ForegroundColor Cyan
    $ffmpegZipUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $ffmpegZip = Join-Path $ProjectRoot "ffmpeg-release-essentials.zip"
    $ffmpegTmp = Join-Path $ProjectRoot "_ffmpeg_tmp"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $ffmpegZipUrl -OutFile $ffmpegZip -UseBasicParsing
        Write-Host "Extraindo ffmpeg.exe..." -ForegroundColor Cyan
        Expand-Archive -Path $ffmpegZip -DestinationPath $ffmpegTmp -Force
        # Procura ffmpeg.exe dentro do zip (pode estar em subpasta)
        $ffmpegExe = Get-ChildItem -Path $ffmpegTmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
        if ($ffmpegExe) {
            Copy-Item $ffmpegExe.FullName $ffmpegLocal -Force
            Write-Host "FFmpeg instalado com sucesso em: $ffmpegLocal" -ForegroundColor Green
        } else {
            Write-Host "AVISO: ffmpeg.exe nao encontrado dentro do zip." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "AVISO: Falha ao baixar FFmpeg automaticamente: $_" -ForegroundColor Yellow
        Write-Host "Baixe manualmente em https://www.gyan.dev/ffmpeg/builds/ e coloque ffmpeg.exe na raiz do projeto." -ForegroundColor Yellow
    } finally {
        # Limpa arquivos temporarios
        if (Test-Path $ffmpegZip) { Remove-Item $ffmpegZip -Force }
        if (Test-Path $ffmpegTmp) { Remove-Item $ffmpegTmp -Recurse -Force }
    }
} else {
    Write-Host "FFmpeg encontrado." -ForegroundColor Green
}

$sdkDll = Join-Path $ProjectRoot "sdk_dlls\HCNetSDK.dll"
if (Test-Path $sdkDll) {
    Write-Host "SDK Hikvision encontrado: porta 8000 disponivel." -ForegroundColor Green
} else {
    Write-Host "AVISO: sdk_dlls\HCNetSDK.dll nao encontrado; sera usado ISAPI porta 80." -ForegroundColor Yellow
}

$defaultDestination = Join-Path $ProjectRoot "gravacoes"
$destination = Read-Host "Pasta de destino [$defaultDestination]"
if ([string]::IsNullOrWhiteSpace($destination)) { $destination = $defaultDestination }
New-Item -ItemType Directory -Force -Path $destination | Out-Null
$env:DVR_APP_DESTINO = $destination
$env:DVR_APP_BANCO = Join-Path $ProjectRoot "dvr_app.db"

$portBusy = $false
try {
    $portBusy = @(Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction Stop).Count -gt 0
} catch { $portBusy = $false }
if ($portBusy) {
    Write-Host "A porta 5000 ja esta ocupada. O servidor provavelmente ja esta rodando." -ForegroundColor Yellow
    Write-Host "Acesse http://localhost:5000 ou encerre a instancia anterior antes de tentar novamente."
    exit 0
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" } |
    Select-Object -First 1 -ExpandProperty IPAddress)
Write-Host "Servidor: http://localhost:5000" -ForegroundColor Green
if ($ip) { Write-Host "Acesso pela rede: http://$ip`:5000" -ForegroundColor Green }
Write-Host "Destino: $destination" -ForegroundColor Green
Write-Host "Iniciando app.py. Use Ctrl+C para parar." -ForegroundColor Cyan

& $venvPython (Join-Path $ProjectRoot "app.py")
exit $LASTEXITCODE
