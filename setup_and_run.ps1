$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Detecta se as DLLs do SDK em sdk_dlls sao 32-bit (x86) ou 64-bit (x64)
$sdkDll = Join-Path $ProjectRoot "sdk_dlls\HCNetSDK.dll"
$isSdk32 = $false
if (Test-Path $sdkDll) {
    try {
        $bytes = [System.IO.File]::ReadAllBytes($sdkDll)
        if ($bytes.Length -ge 0x40 -and $bytes[0] -eq 0x4D -and $bytes[1] -eq 0x5A) {
            $peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
            $machine = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
            if ($machine -eq 0x14C) { $isSdk32 = $true }
        }
    } catch { }
}

$venvPath = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

function Find-Python32 {
    # 1. Tenta pelo launcher py oficial do Windows
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $bits = & py -3-32 -c "import struct; print(struct.calcsize('P')*8)" 2>$null
            if ($bits -match "32") { return "py -3-32" }
        } catch { }
        try {
            $bits = & py -32 -c "import struct; print(struct.calcsize('P')*8)" 2>$null
            if ($bits -match "32") { return "py -32" }
        } catch { }
    }

    # 2. Varre pastas padrao onde o Python 32-bit e instalado no Windows
    $localApp = [Environment]::GetFolderPath("LocalApplicationData")
    $progFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $candidates = @(
        "$localApp\Programs\Python\Python*-32\python.exe",
        "$localApp\Programs\Python\Python3*\python.exe",
        "$localApp\Python\pythoncore-*-32\python.exe",
        "$localApp\Python\pythoncore-*\python.exe",
        "$progFilesX86\Python*\python.exe",
        "C:\Program Files (x86)\Python*\python.exe",
        "C:\Python*-32\python.exe",
        "C:\Python3*\python.exe"
    )
    foreach ($pattern in $candidates) {
        $found = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue
        foreach ($f in $found) {
            try {
                $bits = & $f.FullName -c "import struct; print(struct.calcsize('P')*8)" 2>$null
                if ($bits -match "32") {
                    return $f.FullName
                }
            } catch { }
        }
    }
    return $null
}

# Se as DLLs forem 32-bit (como as originais do WD-Desk V2), o Python DEVE ser 32-bit
if ($isSdk32) {
    Write-Host "[SDK Arquitetura] DLLs detectadas: 32-bit (x86 do WD-DESK V2)." -ForegroundColor Cyan
    $venvPath = Join-Path $ProjectRoot ".venv32"
    $venvPython = Join-Path $venvPath "Scripts\python.exe"

    if (-not (Test-Path $venvPython)) {
        $py32Target = Find-Python32
        if (-not $py32Target) {
            Write-Host "==========================================================================" -ForegroundColor Yellow
            Write-Host "AVISO: As DLLs do WD-Desk V2 sao 32-bit (x86). Para o SDK carregar no Windows," -ForegroundColor Yellow
            Write-Host "e necessario ter o Python 32-bit instalado lado a lado com o 64-bit." -ForegroundColor Yellow
            Write-Host ""
            Write-Host "Instale rapidamente o Python 32-bit pelo PowerShell executando:" -ForegroundColor Green
            Write-Host "  winget install Python.Python.3.11 --architecture x86" -ForegroundColor Green
            Write-Host "Ou baixe o instalador oficial 32-bit em:" -ForegroundColor Cyan
            Write-Host "  https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe" -ForegroundColor Cyan
            Write-Host "==========================================================================" -ForegroundColor Yellow
            exit 1
        }
        Write-Host "Python 32-bit localizado: $py32Target" -ForegroundColor Green
        Write-Host "Criando ambiente virtual 32-bit (.venv32)..." -ForegroundColor Cyan
        if ($py32Target -like "py *") {
            $parts = $py32Target.Split(" ")
            & $parts[0] $parts[1] -m venv $venvPath
        } else {
            & $py32Target -m venv $venvPath
        }
        if ($LASTEXITCODE -ne 0) { throw "Falha ao criar .venv32" }
    }
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Write-Host "Python 3.10+ nao encontrado no PATH." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $venvPython)) {
        Write-Host "Criando ambiente virtual .venv..." -ForegroundColor Cyan
        & python -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw "Falha ao criar .venv" }
    }
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
    Write-Host "SDK Hikvision/JFL encontrado: porta 8000 disponivel." -ForegroundColor Green
} else {
    Write-Host "AVISO: sdk_dlls\HCNetSDK.dll nao encontrado; sera usado ISAPI porta 80." -ForegroundColor Yellow
}
$scriptImportarWd = Join-Path $ProjectRoot "importar_dlls_wddesk.py"
if (Test-Path $scriptImportarWd) {
    Write-Host "DICA DHD/JFL: Para sincronizar as DLLs homologadas do WD-DESK V2 para as DVRs JFL DHD, execute: python importar_dlls_wddesk.py" -ForegroundColor Cyan
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
