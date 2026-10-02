# Projeto Onda – Central de Gravações Hikvision

Aplicação web em Python para baixar, cortar e converter gravações de câmeras DVR Hikvision.

## O que o projeto faz

1. Você acessa a página no navegador e preenche um formulário com o IP da câmera, usuário, senha, número das câmeras e o intervalo de tempo que deseja baixar.
2. O servidor inicia o download automaticamente. Ele tenta primeiro pelo SDK da Hikvision (porta 8000) e, se não funcionar, cai automaticamente para ISAPI (porta 80 via HTTP).
3. Você acompanha o andamento em tempo real pela barra de progresso na página.
4. Quando o download termina, o vídeo é cortado no intervalo exato que você pediu e convertido para um formato assistível.
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

```bash
# Instale as dependências Python
pip install -r requirements.txt

# Inicie o servidor
python app.py
```

Depois abra o navegador em: `http://127.0.0.1:5000`

Se estiver acessando de outro computador na mesma rede:
`http://IP_DA_MAQUINA:5000`

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

- O banco de dados SQLite (`gravacoes.db`) é criado automaticamente na primeira execução.
- Os vídeos baixados ficam salvos na pasta `gravacoes/` dentro do projeto.
- O servidor continua baixando mesmo que você feche o navegador — enquanto `app.py` estiver rodando no terminal.

## Instalação rápida com script (Windows PowerShell / Linux/macOS)

O repositório agora inclui dois **scripts de instalação** que automatizam:

1. Verificação da presença do Python
2. Criação/ativação de um *virtual environment* (`venv`)
3. Instalação das dependências listadas em `requirements.txt`
4. Pergunta interativa onde armazenar os vídeos baixados
5. Inicia o servidor Flask e exibe a URL de acesso

- **Windows** – execute `setup_and_run.ps1` no PowerShell (execute `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` se necessário).
- **Linux/macOS** – torne o script executável (`chmod +x setup_and_run.sh`) e rode `./setup_and_run.sh`.

