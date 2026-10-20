"""
Diagnostico do SDK Hikvision (rodar no Windows onde o SDK nao baixa):

    python diagnostico_sdk.py [IP] [USUARIO] [SENHA] [PORTA]

Mostra: arquitetura do Python, DLLs ausentes, se cada plugin carrega (com o erro
do Windows), resultado de NET_DVR_Init/SetSDKInitCfg com variacoes de caminho,
login na DVR (codigo de erro) e as linhas do log interno do SDK (LoadDSo/syserror).
"""
import ctypes
import glob
import os
import platform
import struct
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
SDK = os.path.join(RAIZ, "sdk_dlls")
COM = os.path.join(SDK, "HCNetSDKCom")

# Mapa extraido por engenharia reversa da HCCore.dll (enumDllType -> DLL, pasta)
RAIZ_DLLS = ["HCNetSDK.dll", "HCCore.dll", "PlayCtrl.dll", "libcrypto-1_1-x64.dll", "libssl-1_1-x64.dll"]
COM_DLLS = ["SystemTransform.dll", "StreamTransClient.dll", "AnalyzeData.dll", "libiconv2.dll",
            "AudioIntercom.dll", "HCPlayBack.dll", "HCPreview.dll", "HCCoreDevCfg.dll",
            "HCGeneralCfgMgr.dll", "HCAlarm.dll", "HCDisplay.dll", "HCIndustry.dll", "HCVoiceTalk.dll"]
OPCIONAIS = ["zlib1.dll", "HCNetUtils.dll", "QosControl.dll", "DsSDK.dll", "EZP2PCloudClient.dll"]


def titulo(t):
    print(f"\n=== {t} ===")


def curto(p):
    try:
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 512):
            return buf.value
    except Exception:
        pass
    return p


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else None
    usr = sys.argv[2] if len(sys.argv) > 2 else "admin"
    pwd = sys.argv[3] if len(sys.argv) > 3 else ""
    porta = int(sys.argv[4]) if len(sys.argv) > 4 else 8000

    titulo("Ambiente")
    print("SO:", platform.platform())
    bits = struct.calcsize("P") * 8
    print("Python:", sys.version.split()[0], f"{bits}-bit")
    if sys.platform != "win32":
        print("ERRO: o HCNetSDK.dll so carrega no Windows."); return
    if bits != 64:
        print("ERRO CRITICO: Python 32-bit nao carrega DLLs x64 do SDK (WinError 193). Instale Python 64-bit.")
    print("Caminho do projeto:", RAIZ)
    if any(ord(c) > 127 for c in RAIZ) or " " in RAIZ:
        print("AVISO: caminho com espaco/acento (o SDK usa ANSI; o projeto usa caminho curto 8.3 para contornar).")

    titulo("Arquivos")
    for pasta, lista in ((SDK, RAIZ_DLLS), (COM, COM_DLLS)):
        for n in lista:
            ok = os.path.isfile(os.path.join(pasta, n))
            print(("OK      " if ok else "FALTANDO"), os.path.relpath(os.path.join(pasta, n), RAIZ))
    for n in OPCIONAIS:
        ok = os.path.isfile(os.path.join(COM, n)) or os.path.isfile(os.path.join(SDK, n))
        print(("OK      " if ok else "ausente "), n, "(opcional)")

    titulo("Carga individual de cada DLL (erro do Windows)")
    os.environ["PATH"] = SDK + os.pathsep + COM + os.pathsep + os.environ.get("PATH", "")
    for d in (SDK, COM):
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    for pasta, lista in ((SDK, RAIZ_DLLS[1:]), (COM, COM_DLLS)):
        for n in lista:
            p = os.path.join(pasta, n)
            if not os.path.isfile(p):
                continue
            try:
                ctypes.WinDLL(p)
                print("carregou ", n)
            except OSError as e:
                print("FALHOU   ", n, "->", e,
                      "(126=dependencia ausente, ex.: VC++ Redistributable; 193=arquitetura errada)")

    titulo("Init do SDK com variacoes do caminho SDK_PATH")

    class PATHCFG(ctypes.Structure):
        _fields_ = [("sPath", ctypes.c_char * 256), ("byRes", ctypes.c_byte * 128)]

    sdk = ctypes.WinDLL(os.path.join(SDK, "HCNetSDK.dll"))
    sdk.NET_DVR_SetSDKInitCfg.argtypes = [ctypes.c_int, ctypes.c_void_p]
    pasta_log = os.path.join(RAIZ, "sdk_logs")
    os.makedirs(pasta_log, exist_ok=True)
    variantes = {
        "curto sem barra": curto(SDK),
        "curto com barra": curto(SDK) + "\\",
        "longo com barra": SDK + "\\",
    }
    for nome, caminho in variantes.items():
        cfg = PATHCFG()
        cfg.sPath = caminho.encode("mbcs", errors="replace")[:255]
        r = sdk.NET_DVR_SetSDKInitCfg(2, ctypes.byref(cfg))
        print(f"SetSDKInitCfg(SDK_PATH={caminho!r}) -> {r}")
    # Mantem a ultima (com barra, mais segura); o app usa a mesma convencao
    ok_init = sdk.NET_DVR_Init()
    print("NET_DVR_Init ->", ok_init)
    try:
        sdk.NET_DVR_SetLogToFile(3, curto(pasta_log).encode("mbcs"), 0)
    except Exception:
        pass
    v = sdk.NET_DVR_GetSDKBuildVersion()
    print(f"Versao SDK: {(v >> 24) & 255}.{(v >> 16) & 255}.{(v >> 8) & 255}.{v & 255}")

    if ip:
        titulo(f"Login V30 em {ip}:{porta}")

        class DEV30(ctypes.Structure):
            _fields_ = [("raw", ctypes.c_byte * 256)]

        info = DEV30()
        sdk.NET_DVR_Login_V30.restype = ctypes.c_long
        uid = sdk.NET_DVR_Login_V30(ip.encode(), porta, usr.encode(), pwd.encode(), ctypes.byref(info))
        print("user_id =", uid, "| GetLastError =", sdk.NET_DVR_GetLastError())
        if uid >= 0:
            sdk.NET_DVR_Logout(uid)
        sdk.NET_DVR_Cleanup()
    else:
        print("(passe IP USUARIO SENHA para testar login)")

    titulo("Log interno do SDK (LoadDSo / syserror / falhas)")
    achou = False
    for f in glob.glob(os.path.join(pasta_log, "*")) + glob.glob(os.path.join(RAIZ, "SDKLog*")):
        try:
            with open(f, "r", errors="replace") as fh:
                for linha in fh:
                    if any(k in linha for k in ("LoadDSo", "syserror", "failed", "Load ", "Not Load")):
                        print(linha.rstrip()); achou = True
        except Exception:
            pass
    if not achou:
        print("(sem linhas relevantes)")


if __name__ == "__main__":
    main()
