$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Desativa o QuickEdit Mode do console do Windows para evitar que um clique
# acidental na janela do terminal congele o processo do servidor por dias.
try {
    $consoleHelper = @"
using System;
using System.Runtime.InteropServices;
public class ConsoleQuickEdit {
    const uint ENABLE_QUICK_EDIT = 0x0040;
    const int STD_INPUT_HANDLE = -10;
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll")]
    static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
    [DllImport("kernel32.dll")]
    static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
    public static void Disable() {
        IntPtr handle = GetStdHandle(STD_INPUT_HANDLE);
        uint mode;
        if (GetConsoleMode(handle, out mode)) {
            mode &= ~ENABLE_QUICK_EDIT;
            SetConsoleMode(handle, mode);
        }
    }
}
"@
    Add-Type -TypeDefinition $consoleHelper -ErrorAction SilentlyContinue
    [ConsoleQuickEdit]::Disable()
} catch { }

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
$savedDestFile = Join-Path $ProjectRoot ".destino_gravacoes"
if (Test-Path $savedDestFile) {
    $savedDest = (Get-Content $savedDestFile -Raw).Trim()
    if (-not [string]::IsNullOrWhiteSpace($savedDest)) {
        $defaultDestination = $savedDest
    }
}

if ($args -contains "--auto" -or -not [Environment]::UserInteractive) {
    $destination = $defaultDestination
} else {
    $destination = Read-Host "Pasta de destino [$defaultDestination]"
    if ([string]::IsNullOrWhiteSpace($destination)) { $destination = $defaultDestination }
}

# Validate the path (UNC or local) before using it
$validPath = $true
if ($destination -like "\\\\*") {
    if (-not (Test-Path $destination)) {
        Write-Host "AVISO: O caminho UNC \"$destination\" não está acessível. Verifique a conexão e as permissões." -ForegroundColor Yellow
        $validPath = $false
    }
}
if ($validPath) {
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    $env:DVR_APP_DESTINO = $destination
    Set-Content -Path $savedDestFile -Value $destination -Encoding UTF8
} else {
    $fallbackDest = Join-Path $ProjectRoot "gravacoes"
    Write-Host "Usando pasta padrão: $fallbackDest" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $fallbackDest | Out-Null
    $env:DVR_APP_DESTINO = $fallbackDest
}
$env:DVR_APP_BANCO = Join-Path $ProjectRoot "dvr_app.db"
$env:PYTHONUNBUFFERED = "1"

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
Write-Host "Destino: $env:DVR_APP_DESTINO" -ForegroundColor Green
Write-Host "Watchdog de Alta Disponibilidade ATIVO: Se o servidor cair por qualquer motivo, sera reiniciado automaticamente em 5s." -ForegroundColor Cyan
Write-Host "Iniciando app.py. Use Ctrl+C para parar definitivamente." -ForegroundColor Cyan

$watchdogLog = Join-Path $ProjectRoot "servidor_watchdog.log"
$ErrorActionPreference = "Continue"

while ($true) {
    $startTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $watchdogLog -Value "[$startTime] Iniciando servidor app.py..."
    & $venvPython (Join-Path $ProjectRoot "app.py")
    $exitCode = $LASTEXITCODE
    $endTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    # Se foi encerrado voluntariamente (Ctrl+C gera código -1073741510 no Windows ou 0)
    if ($exitCode -eq 0 -or $exitCode -eq -1073741510 -or $exitCode -eq 130) {
        Add-Content -Path $watchdogLog -Value "[$endTime] Servidor encerrado manualmente (codigo $exitCode)."
        Write-Host "Servidor encerrado." -ForegroundColor Yellow
        break
    }

    $msg = "[$endTime] ALERTA: O processo app.py encerrou inesperadamente (codigo $exitCode). Reiniciando automaticamente em 5 segundos..."
    Add-Content -Path $watchdogLog -Value $msg
    Write-Host $msg -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
exit 0
