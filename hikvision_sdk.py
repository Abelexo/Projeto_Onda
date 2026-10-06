"""
Wrapper em Python para o Hikvision Device Network SDK (HCNetSDK).
Conecta na porta 8000 da DVR (Hikvision / JFL) para baixar por tempo exato (GetFileByTime).
Suporta Windows (HCNetSDK.dll) e Linux (libhcnetsdk.so).
"""

import os
import sys
import time
import ctypes
from ctypes import c_int, c_char_p, c_byte, c_short, Structure, byref
from datetime import datetime

class NET_DVR_DEVICEINFO_V30(Structure):
    _fields_ = [
        ("sSerialNumber", c_byte * 48),
        ("byAlarmInPortNum", c_byte),
        ("byAlarmOutPortNum", c_byte),
        ("byDiskNum", c_byte),
        ("byDVRType", c_byte),
        ("byChanNum", c_byte),
        ("byStartChan", c_byte),
        ("byAudioChanNum", c_byte),
        ("byIPChanNum", c_byte),
        ("byZeroChanNum", c_byte),
        ("byMainProto", c_byte),
        ("bySubProto", c_byte),
        ("bySupport", c_byte),
        ("bySupport1", c_byte),
        ("bySupport2", c_byte),
        ("wDevType", c_short),
        ("bySupport3", c_byte),
        ("byMultiStream", c_byte),
        ("wStartDChan", c_short),
        ("wStartZeroChan", c_short),
        ("wNetStartChan", c_short),
        ("wNetChanNum", c_short),
        ("wSmartHddNum", c_short),
        ("byRes", c_byte * 10)
    ]

class NET_DVR_TIME(Structure):
    _fields_ = [
        ("dwYear", c_int),
        ("dwMonth", c_int),
        ("dwDay", c_int),
        ("dwHour", c_int),
        ("dwMinute", c_int),
        ("dwSecond", c_int)
    ]

_sdk = None
_sdk_carregado = False

def carregar_sdk():
    global _sdk, _sdk_carregado
    if _sdk_carregado:
        return _sdk is not None

    pasta_raiz = os.path.dirname(os.path.abspath(__file__))
    pastas_dll = [
        os.path.join(pasta_raiz, "sdk_dlls"),
        pasta_raiz,
    ]

    nomes_dll = ["HCNetSDK.dll", "libhcnetsdk.so"]

    caminho_encontrado = None
    diretorio_dll = None
    for pasta in pastas_dll:
        for nome in nomes_dll:
            candidato = os.path.join(pasta, nome)
            if os.path.isfile(candidato):
                caminho_encontrado = candidato
                diretorio_dll = pasta
                break
        if caminho_encontrado:
            break

    if not caminho_encontrado:
        _sdk_carregado = True
        _sdk = None
        return False

    try:
        if sys.platform == "win32":
            # No Windows, adiciona o diretório e a subpasta HCNetSDKCom ao PATH e DLL directory
            os.environ["PATH"] = diretorio_dll + os.pathsep + os.environ.get("PATH", "")
            subpasta_com = os.path.join(diretorio_dll, "HCNetSDKCom")
            if os.path.isdir(subpasta_com):
                os.environ["PATH"] = subpasta_com + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(diretorio_dll)
                if os.path.isdir(subpasta_com):
                    os.add_dll_directory(subpasta_com)
            _sdk = ctypes.WinDLL(caminho_encontrado)
        else:
            _sdk = ctypes.cdll.LoadLibrary(caminho_encontrado)

        _sdk.NET_DVR_Init.restype = c_int
        _sdk.NET_DVR_Cleanup.restype = c_int

        _sdk.NET_DVR_Login_V30.argtypes = [c_char_p, c_int, c_char_p, c_char_p, ctypes.POINTER(NET_DVR_DEVICEINFO_V30)]
        _sdk.NET_DVR_Login_V30.restype = c_int

        _sdk.NET_DVR_Logout_V30.argtypes = [c_int]
        _sdk.NET_DVR_Logout_V30.restype = c_int

        _sdk.NET_DVR_GetFileByTime.argtypes = [c_int, c_int, ctypes.POINTER(NET_DVR_TIME), ctypes.POINTER(NET_DVR_TIME), c_char_p]
        _sdk.NET_DVR_GetFileByTime.restype = c_int

        _sdk.NET_DVR_GetDownloadPos.argtypes = [c_int]
        _sdk.NET_DVR_GetDownloadPos.restype = c_int

        _sdk.NET_DVR_StopGetFile.argtypes = [c_int]
        _sdk.NET_DVR_StopGetFile.restype = c_int

        _sdk.NET_DVR_GetLastError.restype = c_int
        if hasattr(_sdk, "NET_DVR_SetConnectTime"):
            _sdk.NET_DVR_SetConnectTime.argtypes = [c_int, c_int]
            _sdk.NET_DVR_SetConnectTime.restype = c_int
        if hasattr(_sdk, "NET_DVR_SetReconnect"):
            _sdk.NET_DVR_SetReconnect.argtypes = [c_int, c_int]
            _sdk.NET_DVR_SetReconnect.restype = c_int

        _sdk.NET_DVR_Init()
        if hasattr(_sdk, "NET_DVR_SetConnectTime"):
            # Timeout de 5000ms (5s) com 3 tentativas
            _sdk.NET_DVR_SetConnectTime(5000, 3)
        if hasattr(_sdk, "NET_DVR_SetReconnect"):
            _sdk.NET_DVR_SetReconnect(10000, 1)

        _sdk_carregado = True
        print("[SDK] HCNetSDK carregado com sucesso na porta 8000.")
        return True
    except Exception as erro:
        print(f"[SDK] Falha ao carregar DLL do SDK ({caminho_encontrado}): {erro}")
        _sdk = None
        _sdk_carregado = True
        return False

def sdk_disponivel():
    return carregar_sdk()

def baixar_por_tempo_sdk(dvr_ip, porta, usuario, senha, camera, inicio_dt, fim_dt, caminho_destino, callback_progresso=None, checar_cancelado=None):
    """
    Realiza o download de uma faixa de tempo usando a porta 8000 via HCNetSDK.
    A DVR envia exclusivamente o trecho recortado.
    """
    if not carregar_sdk() or _sdk is None:
        raise RuntimeError("DLL/biblioteca do SDK da Hikvision/JFL nao encontrada (pasta sdk_dlls).")

    print(f"[SDK] Conectando em {dvr_ip}:{porta} (porta SDK)...")
    info = NET_DVR_DEVICEINFO_V30()
    user_id = _sdk.NET_DVR_Login_V30(
        dvr_ip.encode('utf-8'),
        int(porta),
        usuario.encode('utf-8'),
        senha.encode('utf-8'),
        byref(info)
    )

    if user_id < 0:
        erro = _sdk.NET_DVR_GetLastError()
        raise RuntimeError(f"Falha de login SDK na DVR {dvr_ip}:{porta} (codigo de erro: {erro})")

    try:
        t_inicio = NET_DVR_TIME(inicio_dt.year, inicio_dt.month, inicio_dt.day, inicio_dt.hour, inicio_dt.minute, inicio_dt.second)
        t_fim = NET_DVR_TIME(fim_dt.year, fim_dt.month, fim_dt.day, fim_dt.hour, fim_dt.minute, fim_dt.second)

        canal = int(camera)
        handle = _sdk.NET_DVR_GetFileByTime(user_id, canal, byref(t_inicio), byref(t_fim), caminho_destino.encode('utf-8'))
        if handle < 0:
            erro = _sdk.NET_DVR_GetLastError()
            raise RuntimeError(f"NET_DVR_GetFileByTime falhou para camera {camera} (codigo de erro: {erro})")

        try:
            while True:
                if checar_cancelado and checar_cancelado():
                    _sdk.NET_DVR_StopGetFile(handle)
                    return False

                pos = _sdk.NET_DVR_GetDownloadPos(handle)
                if pos == 100:
                    if callback_progresso:
                        callback_progresso(100)
                    break
                elif pos > 100 or pos < 0:
                    erro = _sdk.NET_DVR_GetLastError()
                    raise RuntimeError(f"Download SDK falhou durante transmissao (codigo: {pos}, erro: {erro})")

                if callback_progresso:
                    callback_progresso(pos)

                time.sleep(1)
            return True
        finally:
            _sdk.NET_DVR_StopGetFile(handle)
    finally:
        _sdk.NET_DVR_Logout_V30(user_id)

