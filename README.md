# Projeto Onda – Central de Gravações Hikvision

Aplicação web em Python para baixar, cortar e converter gravações de câmeras DVR Hikvision.

## O que o projeto faz

1. Você acessa a página no navegador e preenche um formulário com o IP da câmera, usuário, senha, número das câmeras e o intervalo de tempo que deseja baixar.
2. O servidor inicia o download automaticamente. Ele tenta primeiro pelo SDK da Hikvision (porta 8000) e, se não funcionar, cai automaticamente para ISAPI (porta 80 via HTTP).
3. Você acompanha o andamento em tempo real pela barra de progresso na página.
4. Quando o download termina, o vídeo é cortado no intervalo exato. O sistema tenta primeiro manter o codec original sem recodificar; se isso não for aceito pelo FFmpeg, usa H.264 como fallback para o navegador.
5. O vídeo fica disponível para reprodução direto na página.

## Requisitos

### Python e bibliotecas pip

- Python 3.10 ou superior
- Instale as dependências Python com:
  ```bash
  pip install -r requirements.txt
  ```

### Dependências de sistema (instalar separadamente)

O projeto precisa de FFmpeg ou GStreamer para cortar e converter os vídeos.
Sem um deles, o download funciona mas o arquivo final não é gerado.

**Linux (Ubuntu/Debian):**
```bash
sudo apt install ffmpeg
# ou GStreamer como alternativa:
sudo apt install gstreamer1.0-tools python3-gi python3-gst-1.0
```

**Windows:**
- Instale o FFmpeg: https://ffmpeg.org/download.html
- Para o modo SDK (porta 8000): instale o SDK oficial da Hikvision (HCNetSDK.dll)

## Como rodar

O projeto precisa ser iniciado pelo terminal. Não há executável de duplo-clique.

Use o script de setup ou o ambiente virtual do projeto:

```bash
./setup_and_run.sh
```

Execucao manual:

```bash
.venv/bin/python app.py
```

Depois abra o navegador em: `http://127.0.0.1:5000`

Se estiver acessando de outro computador na mesma rede:
`http://IP_DA_MAQUINA:5000`

## Execucao atual no notebook/Linux

Enquanto o projeto estiver sendo executado neste computador/notebook, use o
ambiente virtual local:

```bash
.venv/bin/python app.py
```

O processo precisa continuar aberto para o trabalhador continuar baixando. A
interface pode ser acessada em `http://localhost:5000` ou pelo IP da máquina
na rede local.

Para salvar diretamente em uma pasta compartilhada da empresa, essa pasta
precisa estar montada no Linux. Depois defina `DVR_APP_DESTINO` antes de
iniciar o app:

```bash
export DVR_APP_DESTINO="/mnt/servidor_gravacoes/local"
.venv/bin/python app.py
```

O caminho `/mnt/servidor_gravacoes/local` é apenas um exemplo. A montagem SMB
deve ser feita pelo sistema operacional com uma conta que tenha permissão de
leitura e gravação. Um endereço Windows como `\\192.168.1.20\local` não deve
ser usado diretamente pelo Python no Linux sem antes ser montado.

No Windows, o equivalente poderá ser um caminho UNC:

```powershell
$env:DVR_APP_DESTINO = "\\192.168.1.20\local"
.venv\Scripts\python.exe app.py
```

O app cria dentro desse destino as pastas por data, código, escola e câmera.

## Estrutura dos arquivos

```
projeto-onda/
├── app.py               # Servidor Flask – lógica de download, API e banco de dados
├── hikvision_sdk.py     # Conexão com o SDK nativo da Hikvision (Windows)
├── requirements.txt     # Dependências Python do projeto
├── static/
│   └── index.html       # Interface web (formulário, progresso e reprodução de vídeo)
└── README.md            # Este arquivo
```

## Modos de download

| Modo | Como funciona |
|------|---------------|
| Automático | Tenta SDK (porta 8000) primeiro; se falhar, usa ISAPI (porta 80) |
| SDK | Usa a DLL oficial da Hikvision. Mais rápido, mas só funciona em Windows |
| ISAPI | Download via HTTP simples. Funciona em qualquer sistema operacional |

## Observações

- O banco de dados SQLite (`dvr_app.db`) é criado automaticamente na primeira execução.
- Os vídeos baixados ficam salvos na pasta `gravacoes/` dentro do projeto.
- O servidor continua baixando mesmo que você feche o navegador — enquanto `app.py` estiver rodando no terminal.

## Instalação rápida com script (Windows PowerShell / Linux/macOS)

O repositório agora inclui dois **scripts de instalação** que automatizam:

1. Verificação da presença do Python
2. Criação do ambiente virtual `.venv`
3. Instalação das dependências listadas em `requirements.txt`
4. Verificação de FFmpeg, GStreamer e SDK
5. Pergunta interativa onde armazenar os vídeos baixados
6. Verifica se a porta 5000 já está ocupada
7. Inicia o servidor Flask e exibe a URL de acesso

- **Windows** – execute `setup_and_run.ps1` no PowerShell (execute `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` se necessário).
- **Linux/macOS/notebook** – torne o script executável (`chmod +x setup_and_run.sh`) e rode `./setup_and_run.sh`.

Os scripts não enviam bancos, gravações ou arquivos temporários para o GitHub.
Eles criam o ambiente `.venv`, instalam `requirements.txt`, verificam FFmpeg,
GStreamer e o SDK e detectam se a porta 5000 já está em uso.

O FFmpeg e o GStreamer são dependências do sistema, não pacotes Python. No
Ubuntu/Debian, o setup pode instalá-los com:

```bash
DVR_INSTALAR_DEPENDENCIAS_SISTEMA=1 ./setup_and_run.sh
```

O SDK Hikvision não é baixado automaticamente porque é uma biblioteca
proprietária. Para usar o modo SDK, coloque a biblioteca correspondente em
`sdk_dlls/` (`HCNetSDK.dll` no Windows ou `libhcnetsdk.so` no Linux). Sem ela,
o modo automático usa ISAPI na porta 80.

Se a porta 5000 já estiver ocupada, isso significa que o servidor já está
rodando. Nesse caso o script encerra sem abrir uma segunda instância; acesse
`http://localhost:5000`.

## Testes

O arquivo `testes_download.ipynb` contém testes seguros dos contratos de
cancelamento do SDK e do ISAPI, sem credenciais reais. O teste final na DVR
deve ser feito com uma câmera e um intervalo curto em cada modo:

- `SDK`: porta 8000 e DLLs Hikvision instaladas.
- `ISAPI`: porta 80, sem depender do SDK.

O modo automático tenta SDK e usa ISAPI como fallback. O painel mostra o
estado do trabalhador e o detalhe de erros de busca ou de processamento.

