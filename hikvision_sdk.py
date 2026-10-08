"""
Wrapper em Python para o Hikvision Device Network SDK (HCNetSDK).
Conecta na porta 8000 (ou configurada) da DVR (Hikvision / JFL / Intelbras Hikvision OEM)
para baixar trechos por tempo exato (GetFileByTime).
Suporta Windows (HCNetSDK.dll) e Linux (libhcnetsdk.so).
Compativel com cameras analogicas e cameras IP / ONVIF conectadas a DVRs/NVRs.
"""

import os
import sys
import time
import shutil
import tempfile
import ctypes
from ctypes import c_int, c_char, c_char_p, c_byte, c_short, c_ushort, c_uint, Structure, byref, POINTER
from datetime import datetime

# Constantes de controle de reproducao e download do HCNetSDK
NET_DVR_PLAYSTART   = 1
NET_DVR_PLAYSTOP    = 2
NET_DVR_PLAYPAUSE   = 3
NET_DVR_PLAYRESTART = 4
NET_DVR_PLAYFAST    = 5
NET_DVR_PLAYSLOW    = 6
NET_DVR_PLAYNORMAL  = 7

# Dicionario explicativo dos codigos de erro mais comuns do HCNetSDK
ERROS_HIKVISION = {
    1: "Usuario ou senha incorretos (NET_DVR_PASSWORD_ERROR)",
    2: "Permissao insuficiente na DVR/NVR (NET_DVR_NOENOUGHPRI)",
    3: "SDK nao inicializado (NET_DVR_NOINIT)",
    4: "Numero de canal incorreto ou inexistente na DVR (NET_DVR_CHANNEL_ERROR)",
    5: "Limite maximo de conexoes atingido no dispositivo (NET_DVR_OVER_MAXLINK)",
    6: "Versao incompativel entre SDK e firmware (NET_DVR_VERSIONNOMATCH)",
    7: "Falha de conexao com a DVR/NVR - dispositivo offline ou porta bloqueada no Mikrotik/Firewall (NET_DVR_NETWORK_FAIL_CONNECT)",
    8: "Falha ao enviar dados para a DVR (NET_DVR_NETWORK_SEND_ERROR)",
    9: "Falha ao receber dados da DVR (NET_DVR_NETWORK_RECV_ERROR)",
    10: "Timeout de rede na resposta da DVR (NET_DVR_NETWORK_RECV_TIMEOUT)",
    11: "Dados invalidos/rejeitados pela DVR (NET_DVR_NETWORK_ERRORDATA - canal IP/ONVIF incorreto, fluxo ou protocolo conflitante)",
    12: "Ordem de chamada de APIs do SDK incorreta (NET_DVR_ORDER_ERROR)",
    13: "Operacao nao permitida na DVR (NET_DVR_OPERNOPERMIT)",
    14: "Timeout ao executar comando na DVR (NET_DVR_COMMANDTIMEOUT)",
    17: "Parametro invalido na chamada do SDK (NET_DVR_PARAMETER_ERROR)",
    23: "Disco da DVR nao formatado ou com erro (NET_DVR_DISK_ERROR)",
    29: "Erro ao criar arquivo de gravacao local (NET_DVR_CREATEFILE_ERROR)",
    30: "Erro ao abrir arquivo para gravacao (NET_DVR_FILEOPENFAIL)",
    31: "Operacao no arquivo falhou (NET_DVR_OPERNOTFINISH)",
    34: "Nenhum arquivo de gravacao encontrado para o canal/periodo (NET_DVR_NO_RECORDFILE)",
    41: "Falha ao alocar recursos internos ou inicializar OpenSSL/TLS (NET_DVR_ALLOC_RESOURCE_ERROR)",
    102: "Sessao nao autenticada na DVR (NET_DVR_USER_NOT_SUCC_LOGIN) - login V30/V40 nao foi concluido com sucesso",
}

def formatar_erro_sdk(codigo):
    desc = ERROS_HIKVISION.get(codigo, "Erro desconhecido")
    return f"codigo {codigo} ({desc})"


class NET_DVR_DEVICEINFO_V30(Structure):
    _pack_ = 1
    # Exatamente 80 bytes conforme especificacao oficial da Hikvision
    _fields_ = [
        ("sSerialNumber", c_byte * 48),  # Numero de serie
        ("byAlarmInPortNum", c_byte),
        ("byAlarmOutPortNum", c_byte),
        ("byDiskNum", c_byte),
        ("byDVRType", c_byte),
        ("byChanNum", c_byte),          # Quantidade de canais analogicos
        ("byStartChan", c_byte),        # Canal analogico inicial (ex: 1)
        ("byAudioChanNum", c_byte),
        ("byIPChanNum", c_byte),        # Quantidade de canais IP (cams ONVIF)
        ("byZeroChanNum", c_byte),
        ("byMainProto", c_byte),
        ("bySubProto", c_byte),
        ("bySupport", c_byte),
        ("bySupport1", c_byte),
        ("bySupport2", c_byte),
        ("wDevType", c_ushort),
        ("bySupport3", c_byte),
        ("byMultiStreamProto", c_byte),
        ("byStartDChan", c_byte),       # Canal digital/IP inicial (ex: 33)
        ("byStartDTalkChan", c_byte),
        ("byHighDChanNum", c_byte),     # High byte de canais IP (total = byIPChanNum + byHighDChanNum * 256)
        ("bySupport4", c_byte),
        ("byLanguageType", c_byte),
        ("byVoiceInChanNum", c_byte),
        ("byStartVoiceInChanNo", c_byte),
        ("bySupport5", c_byte),
        ("bySupport6", c_byte),
        ("byMirrorChanNum", c_byte),
        ("wStartMirrorChanNo", c_ushort),
        ("bySupport7", c_byte),
        ("byRes2", c_byte)
    ]


class NET_DVR_USER_LOGIN_INFO(Structure):
    # ATENÇÃO CRÍTICA: NÃO usar _pack_ = 1 aqui!
    # No compilador MSVC x64 usado para compilar HCCore.dll e HCNetSDK.dll,
    # esta estrutura usa alinhamento padrão de 8 bytes (devido aos ponteiros de 64-bit).
    # Com alinhamento natural, há 4 bytes de padding entre sPassword (offset 260) e cbLoginResult (offset 264).
    # A DLL interna HCCore.dll espera exatamente 416 bytes (0x1a0) e acessa:
    #   offset 130 (0x82): wPort
    #   offset 264 (0x108): cbLoginResult
    #   offset 272 (0x110): pUser
    #   offset 280 (0x118): bUseAsynLogin (int32: 0=Sincrono, 1=Assincrono) -> CRITICO: 0 para esperar resposta!
    #   offset 286 (0x11e): byLoginMode (byte: 0=Private, 1=ISAPI, 2=Self-adaptive)
    #   offset 287 (0x11f): byHttps (byte: 0=TCP, 1=TLS, 2=Self-adaptive)
    _fields_ = [
        ("sDeviceAddress", c_char * 129),
        ("byUseTransport", c_byte),
        ("wPort", c_ushort),
        ("sUserName", c_char * 64),
        ("sPassword", c_char * 64),
        ("cbLoginResult", ctypes.c_void_p),
        ("pUser", ctypes.c_void_p),
        ("bUseAsynLogin", c_int),       # 0 = Sincrono, 1 = Assincrono
        ("byProxyType", c_byte),
        ("byUseUTCTime", c_byte),
        ("byLoginMode", c_byte),        # 0 = Private (8000), 1 = ISAPI (80), 2 = Self-adaptive
        ("byHttps", c_byte),            # 0 = TCP, 1 = TLS, 2 = Self-adaptive
        ("iProxyID", c_int),
        ("byVerifyMode", c_byte),
        ("byRes3", c_byte * 123)
    ]

# Validacao em tempo de carga para evitar qualquer divergencia de ABI
assert ctypes.sizeof(NET_DVR_USER_LOGIN_INFO) == 416, f"NET_DVR_USER_LOGIN_INFO deve ter 416 bytes, obteve {ctypes.sizeof(NET_DVR_USER_LOGIN_INFO)}"
assert NET_DVR_USER_LOGIN_INFO.bUseAsynLogin.offset == 280, f"bUseAsynLogin deve estar no offset 280, obteve {NET_DVR_USER_LOGIN_INFO.bUseAsynLogin.offset}"


class NET_DVR_DEVICEINFO_V40(Structure):
    _pack_ = 1
    # Exatamente 344 bytes conforme especificacao oficial da Hikvision (#pragma pack(1))
    _fields_ = [
        ("struDeviceV30", NET_DVR_DEVICEINFO_V30),
        ("bySupportLock", c_byte),
        ("byRetryLoginTime", c_byte),
        ("byPasswordLevel", c_byte),
        ("byProxyType", c_byte),
        ("dwSurplusLockTime", c_uint),
        ("byCharEncodeType", c_byte),
        ("bySupportDev5", c_byte),
        ("bySupport", c_byte),
        ("byLoginMode", c_byte),
        ("dwOEMCode", c_uint),
        ("iResidualValidity", c_int),
        ("byResidualValidity", c_byte),
        ("bySingleStartDTalkChan", c_byte),
        ("bySingleDTalkChanNums", c_byte),
        ("byPassWordResetLevel", c_byte),
        ("bySupportStreamEncrypt", c_byte),
        ("byMarketType", c_byte),
        ("byRes2", c_byte * 238)
    ]


class NET_DVR_TIME(Structure):
    _pack_ = 1
    _fields_ = [
        ("dwYear", c_uint),
        ("dwMonth", c_uint),
        ("dwDay", c_uint),
        ("dwHour", c_uint),
        ("dwMinute", c_uint),
        ("dwSecond", c_uint)
    ]


class NET_DVR_LOCAL_SDK_PATH(Structure):
    _pack_ = 1
    # Usado para registrar os plugins de HCNetSDKCom antes de NET_DVR_Init
    _fields_ = [
        ("sPath", c_char * 256),
        ("byRes", c_byte * 128)
    ]


# ===========================================================================
# Validacao completa de integridade de ABI em tempo de carga para TODAS as estruturas C
# Garante 100% de certeza que nao havera nenhum erro de alinhamento/padding entre Python e C
# ===========================================================================
assert ctypes.sizeof(NET_DVR_DEVICEINFO_V30) == 80, f"NET_DVR_DEVICEINFO_V30 deve ter 80 bytes, obteve {ctypes.sizeof(NET_DVR_DEVICEINFO_V30)}"
assert ctypes.sizeof(NET_DVR_DEVICEINFO_V40) == 344, f"NET_DVR_DEVICEINFO_V40 deve ter 344 bytes, obteve {ctypes.sizeof(NET_DVR_DEVICEINFO_V40)}"
assert ctypes.sizeof(NET_DVR_TIME) == 24, f"NET_DVR_TIME deve ter 24 bytes, obteve {ctypes.sizeof(NET_DVR_TIME)}"
assert ctypes.sizeof(NET_DVR_LOCAL_SDK_PATH) == 384, f"NET_DVR_LOCAL_SDK_PATH deve ter 384 bytes, obteve {ctypes.sizeof(NET_DVR_LOCAL_SDK_PATH)}"
assert ctypes.sizeof(NET_DVR_USER_LOGIN_INFO) == 416, f"NET_DVR_USER_LOGIN_INFO deve ter 416 bytes, obteve {ctypes.sizeof(NET_DVR_USER_LOGIN_INFO)}"
assert NET_DVR_USER_LOGIN_INFO.wPort.offset == 130, f"wPort deve estar no offset 130"
assert NET_DVR_USER_LOGIN_INFO.cbLoginResult.offset == 264, f"cbLoginResult deve estar no offset 264"
assert NET_DVR_USER_LOGIN_INFO.pUser.offset == 272, f"pUser deve estar no offset 272"
assert NET_DVR_USER_LOGIN_INFO.bUseAsynLogin.offset == 280, f"bUseAsynLogin deve estar no offset 280"
assert NET_DVR_USER_LOGIN_INFO.byLoginMode.offset == 286, f"byLoginMode deve estar no offset 286"
assert NET_DVR_USER_LOGIN_INFO.byHttps.offset == 287, f"byHttps deve estar no offset 287"


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
        subpasta_com = os.path.join(diretorio_dll, "HCNetSDKCom")
        if sys.platform == "win32":
            # Detecta instalacoes do iVMS-4200 para compartilhar DLLs de criptografia e suporte
            pastas_suporte = [diretorio_dll, subpasta_com]
            bases_prog = [
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                r"C:", r"D:",
            ]
            for b in bases_prog:
                for s in [
                    r"iVMS-4200 Site\iVMS-4200 Client\Client",
                    r"iVMS-4200\iVMS-4200 Client\Client",
                    r"iVMS-4200\Client",
                ]:
                    p = os.path.join(b, s)
                    if os.path.isdir(p) and p not in pastas_suporte:
                        pastas_suporte.append(p)
                        sub_ivms_com = os.path.join(p, "HCNetSDKCom")
                        if os.path.isdir(sub_ivms_com) and sub_ivms_com not in pastas_suporte:
                            pastas_suporte.append(sub_ivms_com)

            for p_dir in pastas_suporte:
                if os.path.isdir(p_dir):
                    os.environ["PATH"] = p_dir + os.pathsep + os.environ.get("PATH", "")
                    if hasattr(os, "add_dll_directory"):
                        try:
                            os.add_dll_directory(p_dir)
                        except Exception:
                            pass

            # Pre-carrega APENAS bibliotecas de criptografia OpenSSL para disponibilizar simbolos TLS no processo
            for dll_ssl in ("libcrypto-1_1-x64.dll", "libcrypto.dll", "libssl-1_1-x64.dll", "libssl.dll"):
                c_ssl = os.path.join(diretorio_dll, dll_ssl)
                if os.path.isfile(c_ssl):
                    try:
                        ctypes.WinDLL(c_ssl)
                    except Exception:
                        pass

            _sdk = ctypes.WinDLL(caminho_encontrado)
        else:
            _sdk = ctypes.cdll.LoadLibrary(caminho_encontrado)

        _sdk.NET_DVR_Init.restype = c_int
        _sdk.NET_DVR_Cleanup.restype = c_int

        # NET_DVR_Login_V30
        _sdk.NET_DVR_Login_V30.argtypes = [
            c_char_p, c_ushort, c_char_p, c_char_p, POINTER(NET_DVR_DEVICEINFO_V30)
        ]
        _sdk.NET_DVR_Login_V30.restype = c_int

        # NET_DVR_Login_V40
        if hasattr(_sdk, "NET_DVR_Login_V40"):
            _sdk.NET_DVR_Login_V40.argtypes = [
                POINTER(NET_DVR_USER_LOGIN_INFO), POINTER(NET_DVR_DEVICEINFO_V40)
            ]
            _sdk.NET_DVR_Login_V40.restype = c_int

        _sdk.NET_DVR_Logout_V30.argtypes = [c_int]
        _sdk.NET_DVR_Logout_V30.restype = c_int

        _sdk.NET_DVR_GetFileByTime.argtypes = [
            c_int, c_int, POINTER(NET_DVR_TIME), POINTER(NET_DVR_TIME), c_char_p
        ]
        _sdk.NET_DVR_GetFileByTime.restype = c_int

        # NET_DVR_PlayBackControl (obrigatorio para iniciar e controlar o stream do download)
        if hasattr(_sdk, "NET_DVR_PlayBackControl"):
            _sdk.NET_DVR_PlayBackControl.argtypes = [
                c_int, c_uint, c_uint, POINTER(c_uint)
            ]
            _sdk.NET_DVR_PlayBackControl.restype = c_int

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

        # REGISTRO OBRIGATORIO: NET_DVR_SetSDKInitCfg com caminho da pasta HCNetSDKCom
        # DEVE SER CHAMADO ANTES DE NET_DVR_Init() para carregar os plugins de autenticacao e playback!
        if hasattr(_sdk, "NET_DVR_SetSDKInitCfg"):
            try:
                _sdk.NET_DVR_SetSDKInitCfg.argtypes = [c_int, ctypes.c_void_p]
                _sdk.NET_DVR_SetSDKInitCfg.restype = c_int

                # 1. NET_SDK_INIT_CFG_SDK_PATH = 2
                sdk_path_cfg = NET_DVR_LOCAL_SDK_PATH()
                ctypes.memset(byref(sdk_path_cfg), 0, ctypes.sizeof(sdk_path_cfg))
                caminho_cfg = subpasta_com if os.path.isdir(subpasta_com) else diretorio_dll
                c_bytes = caminho_cfg.encode(sys.getfilesystemencoding() or "utf-8", errors="replace")[:255]
                sdk_path_cfg.sPath = c_bytes

                res_cfg = _sdk.NET_DVR_SetSDKInitCfg(2, ctypes.cast(byref(sdk_path_cfg), ctypes.c_void_p))
                print(f"[SDK] NET_DVR_SetSDKInitCfg(SDK_PATH)={caminho_cfg} -> resultado: {res_cfg}")
                sys.stdout.flush()

                # 2. NET_SDK_INIT_CFG_LIBEAY_PATH = 3 (libcrypto)
                caminho_crypto = os.path.join(diretorio_dll, "libcrypto-1_1-x64.dll")
                if not os.path.isfile(caminho_crypto):
                    caminho_crypto = os.path.join(diretorio_dll, "libcrypto.dll")
                if os.path.isfile(caminho_crypto):
                    b_crypto = caminho_crypto.encode(sys.getfilesystemencoding() or "utf-8", errors="replace")[:255]
                    buf_crypto = ctypes.create_string_buffer(b_crypto)
                    res_crypto = _sdk.NET_DVR_SetSDKInitCfg(3, ctypes.cast(buf_crypto, ctypes.c_void_p))
                    print(f"[SDK] NET_DVR_SetSDKInitCfg(LIBEAY_PATH)={caminho_crypto} -> resultado: {res_crypto}")
                    sys.stdout.flush()

                # 3. NET_SDK_INIT_CFG_SSLEAY_PATH = 4 (libssl)
                caminho_ssl = os.path.join(diretorio_dll, "libssl-1_1-x64.dll")
                if not os.path.isfile(caminho_ssl):
                    caminho_ssl = os.path.join(diretorio_dll, "libssl.dll")
                if os.path.isfile(caminho_ssl):
                    b_ssl = caminho_ssl.encode(sys.getfilesystemencoding() or "utf-8", errors="replace")[:255]
                    buf_ssl = ctypes.create_string_buffer(b_ssl)
                    res_ssl = _sdk.NET_DVR_SetSDKInitCfg(4, ctypes.cast(buf_ssl, ctypes.c_void_p))
                    print(f"[SDK] NET_DVR_SetSDKInitCfg(SSLEAY_PATH)={caminho_ssl} -> resultado: {res_ssl}")
                    sys.stdout.flush()
            except Exception as e_cfg:
                print(f"[SDK] Aviso ao configurar NET_DVR_SetSDKInitCfg: {e_cfg}")
                sys.stdout.flush()

        _sdk.NET_DVR_Init()

        # Opcional: ativa geracao de log interno do SDK para diagnostico avancado
        if hasattr(_sdk, "NET_DVR_SetLogToFile"):
            try:
                pasta_log = os.path.join(pasta_raiz, "sdk_logs")
                os.makedirs(pasta_log, exist_ok=True)
                _sdk.NET_DVR_SetLogToFile(3, pasta_log.encode("utf-8"), 0)
            except Exception:
                pass

        if hasattr(_sdk, "NET_DVR_SetConnectTime"):
            # Timeout de 5000ms (5s) com 3 tentativas
            _sdk.NET_DVR_SetConnectTime(5000, 3)
        if hasattr(_sdk, "NET_DVR_SetReconnect"):
            _sdk.NET_DVR_SetReconnect(10000, 1)

        _sdk_carregado = True
        print(f"[SDK] HCNetSDK carregado com sucesso ({caminho_encontrado}).")
        return True
    except Exception as erro:
        print(f"[SDK] Falha ao carregar DLL do SDK ({caminho_encontrado}): {erro}")
        _sdk = None
        _sdk_carregado = True
        return False


def sdk_disponivel():
    return carregar_sdk()


def _limpar_ip_e_porta(dvr_ip, porta_padrao=8000):
    """
    Remove prefixos de protocolo (http://, https://) e separa IP de porta
    caso o Mikrotik ou usuario forneca formato '192.169.78.254:8000'.
    """
    ip_limpo = str(dvr_ip).strip()
    for prefixo in ("http://", "https://"):
        if ip_limpo.lower().startswith(prefixo):
            ip_limpo = ip_limpo[len(prefixo):]
    ip_limpo = ip_limpo.split("/")[0].strip()

    porta = int(porta_padrao) if porta_padrao else 8000
    if ":" in ip_limpo:
        partes = ip_limpo.split(":")
        ip_limpo = partes[0].strip()
        if len(partes) > 1 and partes[1].strip().isdigit():
            porta = int(partes[1].strip())

    return ip_limpo, porta


def _extrair_info_dispositivo(user_id, dev_info_v40):
    """
    Extrai topologia e numero de serie da DVR autenticada de dev_info_v40.
    """
    dev_info = dev_info_v40.struDeviceV30
    serial = bytes(dev_info.sSerialNumber).split(b"\x00")[0].decode("latin1", errors="ignore").strip()
    return dev_info, serial


def _fazer_login(dvr_ip, porta, usuario, senha):
    """
    Autentica na DVR via SDK.
    Tenta na seguinte sequencia de compatibilidade:
      1. NET_DVR_Login_V40 em modo Private com TLS adaptativo (porta 8000)
      2. NET_DVR_Login_V40 em modo Private sem TLS (porta 8000)
      3. NET_DVR_Login_V40 em modo Self-adaptive (como o iVMS-4200 negocia com DVRs modernos)
      4. NET_DVR_Login_V30 (modo classico legado sincrono)
      5. NET_DVR_Login_V40 em modo ISAPI (porta HTTP 80 caso 8000 esteja bloqueada)
    Retorna uma tupla (user_id, dev_info_v30).
    """
    ip_limpo, porta_num = _limpar_ip_e_porta(dvr_ip, porta)
    ultimo_erro = 0

    if hasattr(_sdk, "NET_DVR_Login_V40"):
        # 1. Tentativa NET_DVR_Login_V40 (Modo Privado - Porta 8000 com TLS adaptativo)
        login_info = NET_DVR_USER_LOGIN_INFO()
        ctypes.memset(byref(login_info), 0, ctypes.sizeof(login_info))
        login_info.bUseAsynLogin = 0
        login_info.wPort = porta_num
        login_info.byLoginMode = 0  # 0 = Private Protocol
        login_info.byHttps = 2      # 2 = Autoadaptativo TLS

        login_info.sDeviceAddress = ip_limpo.encode("utf-8")[:128]
        login_info.sUserName = usuario.encode("utf-8")[:63]
        login_info.sPassword = senha.encode("utf-8")[:63]

        dev_info_v40 = NET_DVR_DEVICEINFO_V40()
        ctypes.memset(byref(dev_info_v40), 0, ctypes.sizeof(dev_info_v40))
        sys.stdout.flush()
        user_id = _sdk.NET_DVR_Login_V40(byref(login_info), byref(dev_info_v40))
        sys.stdout.flush()
        if user_id >= 0:
            dev_info_final, serial = _extrair_info_dispositivo(user_id, dev_info_v40)
            print(f"[SDK] Login V40 privado TLS bem-sucedido na DVR {ip_limpo}:{porta_num} (ID: {user_id}, Serial: {serial or 'N/A'})")
            sys.stdout.flush()
            return user_id, dev_info_final
        else:
            ultimo_erro = _sdk.NET_DVR_GetLastError()
            print(f"[SDK] Login V40 privado TLS falhou ({formatar_erro_sdk(ultimo_erro)}). Tentando sem TLS...")
            sys.stdout.flush()

        # 2. Tentativa NET_DVR_Login_V40 (Modo Privado - Porta 8000 sem TLS direto)
        login_info.byHttps = 0      # 0 = Sem TLS
        ctypes.memset(byref(dev_info_v40), 0, ctypes.sizeof(dev_info_v40))
        sys.stdout.flush()
        user_id = _sdk.NET_DVR_Login_V40(byref(login_info), byref(dev_info_v40))
        sys.stdout.flush()
        if user_id >= 0:
            dev_info_final, serial = _extrair_info_dispositivo(user_id, dev_info_v40)
            print(f"[SDK] Login V40 privado TCP bem-sucedido na DVR {ip_limpo}:{porta_num} (ID: {user_id}, Serial: {serial or 'N/A'})")
            sys.stdout.flush()
            return user_id, dev_info_final
        else:
            ultimo_erro = _sdk.NET_DVR_GetLastError()
            print(f"[SDK] Login V40 privado TCP falhou ({formatar_erro_sdk(ultimo_erro)}). Tentando modo autoadaptativo...")
            sys.stdout.flush()

        # 3. Tentativa NET_DVR_Login_V40 (Modo 2 = Autoadaptativo, padrao iVMS-4200)
        login_info.byLoginMode = 2  # 2 = Self-adaptive
        login_info.byHttps = 2
        ctypes.memset(byref(dev_info_v40), 0, ctypes.sizeof(dev_info_v40))
        sys.stdout.flush()
        user_id = _sdk.NET_DVR_Login_V40(byref(login_info), byref(dev_info_v40))
        sys.stdout.flush()
        if user_id >= 0:
            dev_info_final, serial = _extrair_info_dispositivo(user_id, dev_info_v40)
            print(f"[SDK] Login V40 autoadaptativo bem-sucedido na DVR {ip_limpo}:{porta_num} (ID: {user_id}, Serial: {serial or 'N/A'})")
            sys.stdout.flush()
            return user_id, dev_info_final
        else:
            ultimo_erro = _sdk.NET_DVR_GetLastError()
            print(f"[SDK] Login V40 autoadaptativo falhou ({formatar_erro_sdk(ultimo_erro)}). Tentando fallback V30...")
            sys.stdout.flush()

    # 4. Fallback para NET_DVR_Login_V30 (Padrao clássico para DVRs mais antigos)
    info_v30 = NET_DVR_DEVICEINFO_V30()
    ctypes.memset(byref(info_v30), 0, ctypes.sizeof(info_v30))
    sys.stdout.flush()
    user_id = _sdk.NET_DVR_Login_V30(
        ip_limpo.encode("utf-8"),
        porta_num,
        usuario.encode("utf-8"),
        senha.encode("utf-8"),
        byref(info_v30)
    )
    sys.stdout.flush()
    if user_id >= 0:
        serial = bytes(info_v30.sSerialNumber).split(b"\x00")[0].decode("latin1", errors="ignore").strip()
        print(f"[SDK] Login V30 bem-sucedido na DVR {ip_limpo}:{porta_num} (ID: {user_id}, Serial: {serial or 'N/A'})")
        sys.stdout.flush()
        return user_id, info_v30
    else:
        ultimo_erro = _sdk.NET_DVR_GetLastError()
        print(f"[SDK] Login V30 retornou {formatar_erro_sdk(ultimo_erro)}.")
        sys.stdout.flush()

    # 5. Fallback adicional para V40 no modo ISAPI (conecta via HTTP/ISAPI na porta 80 caso a porta 8000 seja bloqueada no roteador/Mikrotik)
    if hasattr(_sdk, "NET_DVR_Login_V40"):
        portas_isapi = [80, 8080] if porta_num != 80 else [80]
        for p_test in portas_isapi:
            try:
                print(f"[SDK] Tentando login alternativo V40 ISAPI na porta {p_test}...")
                sys.stdout.flush()
                login_info = NET_DVR_USER_LOGIN_INFO()
                ctypes.memset(byref(login_info), 0, ctypes.sizeof(login_info))
                login_info.bUseAsynLogin = 0
                login_info.wPort = p_test
                login_info.byLoginMode = 1  # 1 = ISAPI
                login_info.byHttps = 0
                login_info.sDeviceAddress = ip_limpo.encode("utf-8")[:128]
                login_info.sUserName = usuario.encode("utf-8")[:63]
                login_info.sPassword = senha.encode("utf-8")[:63]

                dev_info_v40 = NET_DVR_DEVICEINFO_V40()
                ctypes.memset(byref(dev_info_v40), 0, ctypes.sizeof(dev_info_v40))
                sys.stdout.flush()
                user_id = _sdk.NET_DVR_Login_V40(byref(login_info), byref(dev_info_v40))
                sys.stdout.flush()
                if user_id >= 0:
                    dev_info_final, serial = _extrair_info_dispositivo(user_id, dev_info_v40)
                    print(f"[SDK] Login V40 ISAPI bem-sucedido na DVR {ip_limpo}:{p_test} (ID: {user_id}, Serial: {serial or 'N/A'})")
                    sys.stdout.flush()
                    return user_id, dev_info_final
            except Exception:
                pass

    return -1, None


def _determinar_canais_candidatos(camera_solicitada, dev_info):
    """
    Determina os canais SDK a serem tentados.
    Para cameras analogicas e IP / ONVIF conectadas a DVRs ou NVRs:
      - O numero da camera solicitada (ex: 1) é SEMPRE a prioridade máxima!
      - Em NVRs e canais IP, testa tambem o deslocamento digital (33, etc).
    Retorna uma lista ordenada sem duplicatas priorizando o canal mais provavel.
    """
    cam_num = int(camera_solicitada)
    candidatos = []

    start_dchan = int(getattr(dev_info, "byStartDChan", 0)) if dev_info else 0
    start_chan = int(getattr(dev_info, "byStartChan", 1)) if dev_info else 1
    chan_num = int(getattr(dev_info, "byChanNum", 0)) if dev_info else 0
    ip_chan_num = int(getattr(dev_info, "byIPChanNum", 0)) if dev_info else 0
    high_dchan = int(getattr(dev_info, "byHighDChanNum", 0)) if dev_info else 0
    total_ip = ip_chan_num + (high_dchan * 256)

    canal_digital_offset = (start_dchan + cam_num - 1) if start_dchan > 0 else (32 + cam_num)

    if dev_info:
        print(f"[SDK] Topologia da DVR: analogicos={chan_num} (inicio {start_chan}), digitais_ip={total_ip}, canal_inicial_ip={start_dchan}")

    # Prioridade 1: testar o canal correspondente a camera pedida (ex: 1)
    candidatos.append(cam_num)

    # Prioridade 2: canal analogico com offset caso start_chan > 1
    if chan_num > 0 and start_chan > 1:
        candidatos.append(start_chan + cam_num - 1)

    # Prioridade 3: canais digitais/IP (ex: 33)
    candidatos.append(canal_digital_offset)
    candidatos.append(32 + cam_num)
    if start_dchan > 0:
        candidatos.append(start_dchan + cam_num - 1)

    # Remove duplicados preservando a ordem de prioridade
    resultado = []
    for c in candidatos:
        if c > 0 and c not in resultado:
            resultado.append(c)

    return resultado


def _obter_caminho_curto_win(caminho):
    """
    No Windows, converte caminhos longos ou com acentuacao para o formato 8.3
    evitando que a DLL em C++ falhe ao criar o arquivo de gravacao local.
    """
    if sys.platform == "win32":
        try:
            from ctypes import wintypes
            _GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
            _GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            _GetShortPathNameW.restype = wintypes.DWORD

            buf = ctypes.create_unicode_buffer(1024)
            tam = _GetShortPathNameW(caminho, buf, 1024)
            if tam > 0:
                return buf.value
        except Exception:
            pass
    return caminho


def baixar_por_tempo_sdk(
    dvr_ip, porta, usuario, senha, camera, inicio_dt, fim_dt, caminho_destino,
    callback_progresso=None, checar_cancelado=None
):
    """
    Realiza o download de uma faixa de tempo usando a porta 8000 via HCNetSDK.
    A DVR envia exclusivamente o trecho recortado.
    Totalmente compativel com cameras ONVIF / IP e canais analogicos.
    """
    if not carregar_sdk() or _sdk is None:
        raise RuntimeError("DLL/biblioteca do SDK da Hikvision/JFL nao encontrada (pasta sdk_dlls).")

    ip_limpo, porta_num = _limpar_ip_e_porta(dvr_ip, porta)
    print(f"[SDK] Conectando em {ip_limpo}:{porta_num} (porta SDK)...")

    user_id, dev_info = _fazer_login(ip_limpo, porta_num, usuario, senha)
    if user_id < 0:
        cod_erro = _sdk.NET_DVR_GetLastError()
        raise RuntimeError(f"Falha de login SDK na DVR {ip_limpo}:{porta_num} - {formatar_erro_sdk(cod_erro)}")

    # Prepara o caminho destino no disco
    caminho_absoluto = os.path.normpath(os.path.abspath(caminho_destino))
    pasta_destino = os.path.dirname(caminho_absoluto)
    os.makedirs(pasta_destino, exist_ok=True)

    # O arquivo temporario durante o download do SDK e gravado OBRIGATORIAMENTE
    # no disco local (tempfile.gettempdir(), ex: C:\\Users\\...\\AppData\\Local\\Temp).
    # Isso impede falhas de escrita caso o destino final seja um compartilhamento de rede UNC
    # (\\\\172.168.12.3\\...) ou contenha acentos/espacos que a DLL nativa em C/C++ rejeite.
    pasta_temp_local = tempfile.gettempdir()
    timestamp_tmp = int(time.time())
    nome_arquivo_temp = f"hksdk_tmp_{timestamp_tmp}_cam{camera}.hik"
    caminho_arquivo_temp = os.path.join(pasta_temp_local, nome_arquivo_temp)

    # Converte caminho temporario para 8.3 seguro no Windows
    caminho_salvar_sdk = _obter_caminho_curto_win(caminho_arquivo_temp)
    bytes_caminho = caminho_salvar_sdk.encode(sys.getfilesystemencoding() or "utf-8", errors="replace")

    try:
        t_inicio = NET_DVR_TIME(
            inicio_dt.year, inicio_dt.month, inicio_dt.day,
            inicio_dt.hour, inicio_dt.minute, inicio_dt.second
        )
        t_fim = NET_DVR_TIME(
            fim_dt.year, fim_dt.month, fim_dt.day,
            fim_dt.hour, fim_dt.minute, fim_dt.second
        )

        canais_candidatos = _determinar_canais_candidatos(camera, dev_info)
        print(f"[SDK] Solicitando download camera {camera}. Canais SDK candidatos: {canais_candidatos}")

        handle = -1
        ultimo_erro_canal = 0
        canal_utilizado = -1

        for c_cand in canais_candidatos:
            if checar_cancelado and checar_cancelado():
                return False

            print(f"[SDK] Tentando iniciar download no canal {c_cand} ({inicio_dt.strftime('%H:%M:%S')} ate {fim_dt.strftime('%H:%M:%S')})...")
            h = _sdk.NET_DVR_GetFileByTime(
                user_id, c_cand, byref(t_inicio), byref(t_fim), bytes_caminho
            )

            if h >= 0:
                # OBRIGATORIO: no HCNetSDK e mandatorio chamar PlayBackControl(PLAYSTART)
                # para que o dispositivo inicie efetivamente o envio do fluxo!
                # Sem isso, GetDownloadPos retorna -1 e gera erro 11 (NET_DVR_NETWORK_ERRORDATA).
                iniciou = True
                if hasattr(_sdk, "NET_DVR_PlayBackControl"):
                    iniciou = _sdk.NET_DVR_PlayBackControl(h, NET_DVR_PLAYSTART, 0, None)
                    if not iniciou:
                        cod_ctrl = _sdk.NET_DVR_GetLastError()
                        print(f"[SDK] Aviso: PlayBackControl(PLAYSTART) retornou {iniciou} ({formatar_erro_sdk(cod_ctrl)})")

                handle = h
                canal_utilizado = c_cand
                print(f"[SDK] Sucesso ao abrir canal {c_cand} (Handle: {handle}). Transmissao iniciada.")
                break
            else:
                ultimo_erro_canal = _sdk.NET_DVR_GetLastError()
                print(f"[SDK] Canal {c_cand} falhou com {formatar_erro_sdk(ultimo_erro_canal)}. Tentando proximo canal...")

        if handle < 0:
            raise RuntimeError(
                f"NET_DVR_GetFileByTime falhou para camera {camera} em todos os canais ({canais_candidatos}) - {formatar_erro_sdk(ultimo_erro_canal)}"
            )

        try:
            # Monitoramento do progresso da transmissao
            tempo_inicio = time.time()
            tentativas_iniciais_negativas = 0

            while True:
                if checar_cancelado and checar_cancelado():
                    _sdk.NET_DVR_StopGetFile(handle)
                    if os.path.exists(caminho_arquivo_temp):
                        try:
                            os.remove(caminho_arquivo_temp)
                        except OSError:
                            pass
                    return False

                pos = _sdk.NET_DVR_GetDownloadPos(handle)

                if pos == 100:
                    if callback_progresso:
                        callback_progresso(100)
                    print(f"[SDK] Download concluido com 100% no canal {canal_utilizado}.")
                    break

                elif pos < 0 or pos > 100:
                    # Durante os primeiros 3 segundos, o buffer pode demorar a sincronizar
                    if time.time() - tempo_inicio < 4.0 and tentativas_iniciais_negativas < 4:
                        tentativas_iniciais_negativas += 1
                        time.sleep(1)
                        continue

                    cod_erro_pos = _sdk.NET_DVR_GetLastError()
                    raise RuntimeError(
                        f"Download SDK falhou durante transmissao (posicao: {pos}, {formatar_erro_sdk(cod_erro_pos)})"
                    )

                if callback_progresso and pos >= 0:
                    callback_progresso(pos)

                time.sleep(1)

            # Move o arquivo temporario da pasta temp local para o destino final definitivo
            if os.path.exists(caminho_arquivo_temp):
                if os.path.exists(caminho_absoluto):
                    try:
                        os.remove(caminho_absoluto)
                    except OSError:
                        pass
                shutil.move(caminho_arquivo_temp, caminho_absoluto)
                return True
            else:
                raise RuntimeError("O arquivo baixado nao foi encontrado no disco apos a conclusao.")

        finally:
            _sdk.NET_DVR_StopGetFile(handle)

    finally:
        _sdk.NET_DVR_Logout_V30(user_id)
        if os.path.exists(caminho_arquivo_temp):
            try:
                os.remove(caminho_arquivo_temp)
            except OSError:
                pass
