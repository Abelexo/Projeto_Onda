#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Utilitário: importar_dlls_wddesk.py
=========================================
Localiza a instalação do WD-DESK V2 (JFL) ou iVMS-4200 (Hikvision) no Windows
e sincroniza as DLLs homologadas para a pasta `sdk_dlls/` do Projeto Onda.

Por que isso é necessário?
- Os DVRs da linha DHD (JFL) são produzidos sob plataforma OEM da Hikvision.
- O software WD-Desk V2 é a versão licenciada do iVMS-4200 pela JFL.
- As DLLs que acompanham o WD-Desk V2 possuem os pacotes de assinatura e compatibilidade
  exatos para autenticar nos DVRs JFL (DHD) e Hikvision sem gerar erro de recusa de login
  (código 1 - NET_DVR_PASSWORD_ERROR).
"""

import os
import sys
import shutil
import struct

PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))
PASTA_SDK_DESTINO = os.path.join(PASTA_PROJETO, "sdk_dlls")
PASTA_COM_DESTINO = os.path.join(PASTA_SDK_DESTINO, "HCNetSDKCom")


def obter_arquitetura_pe(caminho_dll):
    """Lê o cabeçalho PE da DLL e retorna se ela é 64-bit (x64) ou 32-bit (x86)."""
    try:
        with open(caminho_dll, "rb") as f:
            cab = f.read(1024)
            if cab[:2] != b"MZ":
                return "desconhecido"
            pe_offset = struct.unpack("<I", cab[0x3C:0x40])[0]
            f.seek(pe_offset)
            pe_sig = f.read(4)
            if pe_sig != b"PE\x00\x00":
                return "desconhecido"
            machine = struct.unpack("<H", f.read(2))[0]
            if machine == 0x8664:
                return "64-bit (x64)"
            elif machine == 0x14C:
                return "32-bit (x86)"
            return f"outro ({hex(machine)})"
    except Exception:
        return "desconhecido"

# Pastas comuns de instalação do WD-Desk V2 e iVMS-4200 no Windows
SUBPASTAS_CANDIDATAS = [
    r"wdesk v2 client",
    r"wdesk v2\client",
    r"wdesk v2",
    r"wdesk",
    r"WD-DESK V2",
    r"WD-DESK V2\Client",
    r"WD-Desk V2",
    r"WD-Desk V2\Client",
    r"WD DESK V2",
    r"WD DESK V2\Client",
    r"JFL Alarmes\WD-DESK V2",
    r"JFL Alarmes\WD-DESK V2\Client",
    r"JFL\WD-DESK V2",
    r"JFL\WD-DESK V2\Client",
    r"iVMS-4200 Site\iVMS-4200 Client\Client",
    r"iVMS-4200\iVMS-4200 Client\Client",
    r"iVMS-4200\Client",
    r"iVMS-4200 Lite\Client",
]

BASES_DRIVE = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    r"C:",
    r"D:",
    r"E:",
]


def encontrar_instalacoes_suporte():
    encontradas = []
    pastas_verificadas = set()

    for base in BASES_DRIVE:
        for sub in SUBPASTAS_CANDIDATAS:
            caminho = os.path.join(base, sub)
            if caminho in pastas_verificadas:
                continue
            pastas_verificadas.add(caminho)

            if os.path.isdir(caminho):
                dll_principal = os.path.join(caminho, "HCNetSDK.dll")
                if os.path.isfile(dll_principal):
                    encontradas.append(caminho)

    return encontradas


def copiar_dlls(pasta_origem):
    dll_check = os.path.join(pasta_origem, "HCNetSDK.dll")
    arq = obter_arquitetura_pe(dll_check) if os.path.isfile(dll_check) else "desconhecido"
    print("=" * 76)
    print(f"📦 Origem das DLLs : {pasta_origem}")
    print(f"⚙️  Arquitetura     : {arq}")
    print(f"📂 Pasta de destino: {PASTA_SDK_DESTINO}")
    print("=" * 76)

    os.makedirs(PASTA_SDK_DESTINO, exist_ok=True)
    os.makedirs(PASTA_COM_DESTINO, exist_ok=True)

    arquivos_copiados = 0

    # 1. Copia DLLs raiz
    for item in os.listdir(pasta_origem):
        origem = os.path.join(pasta_origem, item)
        if os.path.isfile(origem) and item.lower().endswith((".dll", ".lib")):
            destino = os.path.join(PASTA_SDK_DESTINO, item)
            try:
                shutil.copy2(origem, destino)
                arquivos_copiados += 1
            except Exception as e:
                print(f"  [Aviso] Falha ao copiar {item}: {e}")

    # 2. Copia pasta HCNetSDKCom
    com_origem = os.path.join(pasta_origem, "HCNetSDKCom")
    if os.path.isdir(com_origem):
        for item in os.listdir(com_origem):
            origem = os.path.join(com_origem, item)
            if os.path.isfile(origem) and item.lower().endswith((".dll", ".lib")):
                destino = os.path.join(PASTA_COM_DESTINO, item)
                try:
                    shutil.copy2(origem, destino)
                    arquivos_copiados += 1
                except Exception as e:
                    print(f"  [Aviso] Falha ao copiar HCNetSDKCom/{item}: {e}")

    print(f"✅ Sincronização concluída com sucesso! Total de {arquivos_copiados} bibliotecas copiadas.")
    print("Agora o Projeto Onda usará os binários originais do WD-Desk / iVMS para baixar via SDK.")


def main():
    print("=" * 76)
    print("🔍 LOCALIZADOR DE DLLS HOMOLOGADAS (WD-DESK V2 / iVMS-4200)")
    print("=" * 76)

    # Permite passar o caminho da pasta manualmente se não estiver nas pastas padrão:
    # Ex: python importar_dlls_wddesk.py "C:\Caminho\Personalizado\WD-Desk"
    if len(sys.argv) > 1:
        caminho_manual = sys.argv[1].strip()
        if os.path.isdir(caminho_manual):
            dll = os.path.join(caminho_manual, "HCNetSDK.dll")
            if os.path.isfile(dll):
                copiar_dlls(caminho_manual)
                return
            else:
                print(f"❌ HCNetSDK.dll não encontrada em: {caminho_manual}")
                sys.exit(1)
        else:
            print(f"❌ Diretório não encontrado: {caminho_manual}")
            sys.exit(1)

    instalacoes = encontrar_instalacoes_suporte()

    if not instalacoes:
        print("❌ Nenhuma instalação automática do WD-DESK V2 ou iVMS-4200 foi encontrada nos caminhos padrão.")
        print("\nComo usar com caminho manual:")
        print("1. Abra as propriedades do atalho do WD-Desk V2 no seu Desktop.")
        print("2. Veja a pasta de destino (onde está o executável).")
        print("3. Execute o comando passando essa pasta entre aspas, por exemplo:")
        print(r'   python importar_dlls_wddesk.py "C:\Arquivos de Programas\WD-DESK V2"')
        sys.exit(1)

    print(f"Encontrada(s) {len(instalacoes)} instalação(ões) compatível(is):")
    for idx, inst in enumerate(instalacoes, 1):
        print(f"  [{idx}] {inst}")

    # Prioriza WD-Desk se houver mais de uma
    escolhida = instalacoes[0]
    for inst in instalacoes:
        if "wd" in inst.lower() or "jfl" in inst.lower():
            escolhida = inst
            break

    print(f"\nSelecionando automaticamente: {escolhida}")
    copiar_dlls(escolhida)


if __name__ == "__main__":
    main()
