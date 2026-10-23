# Projeto Onda 2.0 - Central de Gravacoes Hikvision

Aplicacao web para download automatizado, recorte e organizacao de gravacoes de DVRs Hikvision e JFL.

---

## Como o sistema funciona

O Projeto Onda resolve o problema de ter que baixar horas de gravacoes manualmente pelo aplicativo de monitoramento ou pela interface web lenta das DVRs. 

### Pontos principais do funcionamento:

1. **Dois Motores de Download (SDK e ISAPI):**
   - **SDK Nativo Hikvision (Porta 8000):** Conecta diretamente no protocolo proprietario da Hikvision usando as DLLs nativas em C/C++ (`HCNetSDK.dll`). A DVR envia exclusivamente o intervalo de tempo solicitado, sem precisar baixar blocos fisicos gigantescos. E a opcao mais rapida e estavel.
     *(Nota: O SDK nativo funciona exclusivamente em sistemas Windows, pois utiliza as DLLs de 64 bits da Hikvision).*
   - **ISAPI Web (Porta 80 HTTP):** Protocolo REST/XML via HTTP da Hikvision. Serve como contingencia universal e funciona em qualquer sistema operacional.
   - **Modo Automatico Inteligente:** O sistema testa a porta 8000 via SDK. Se autenticar com sucesso, baixa direto pelo SDK. Se a porta 8000 estiver bloqueada na rede ou a senha for diferente da web, o sistema alterna automaticamente no mesmo instante para ISAPI na porta 80, garantindo o download sem travar a fila.

2. **Pool de Downloads Simultaneos (Multi-Thread por DVR):**
   - O sistema processa ate **5 pedidos de gravacao simultaneamente** em paralelo por padrao quando sao de DVRs (IPs) diferentes.
   - Esse limite de concorrencia pode ser configurado pela variavel de ambiente `DVR_MAX_WORKERS` (seja no arquivo `.env` ou no ambiente do sistema antes da execucao).
   - Regra de seguranca por DVR: se houver dois ou mais pedidos para a **mesma DVR (mesmo IP)** ao mesmo tempo, a aplicacao sempre baixa **um pedido por vez**, utilizando uma trava inteligente por IP (`lock_dvr`) para enfileirar os demais sem derrubar ou sobrecarregar a DVR.
   - Reuso de sessao SDK: faz um unico login na porta 8000 por pedido e reutiliza a mesma sessao autenticada em todas as cameras solicitadas, evitando bloqueio por excesso de logins ("Illegal Login Lock").

3. **Alta Disponibilidade (Watchdog Auto-Restart Nativo):**
   - Os scripts `setup_and_run.ps1` (Windows) e `setup_and_run.sh` (Linux) possuem um **Watchdog integrado**: se o processo Python cair por qualquer falha externa, oscilacao de rede ou erro inesperado do sistema operacional, o proprio script reinicia o servidor automaticamente em 5 segundos (mantendo a mesma pasta de destino de rede escolhida) e retoma os downloads exatamente do trecho onde parou.
   - No Windows PowerShell, o script desativa automaticamente o *QuickEdit Mode* da janela para impedir que um clique acidental do mouse na tela preta congele o servidor.

4. **Corte e Conversao com FFmpeg:**
   - O sistema recorta os videos no segundo exato pedido e gera arquivos `.mp4` (H.264) organizados automaticamente em pastas por data, codigo, escola e camera.

5. **Validacao Inteligente de Cameras e Contagem Real de Pedidos:**
   - A aplicacao le a topologia da DVR (quantidade de canais analogicos e canais IP digitais) antes de iniciar.
   - O painel e as estatisticas contabilizam cada **pedido de gravacao concluido** (ex: um pedido de 2 horas dividido internamente em 20 partes conta como 1 gravacao concluida).

6. **Catalogo Automatico de Escolas e Integracao Telegram + IA (Gemini):**
   - Conforme novos downloads sao feitos, o banco SQLite salva automaticamente o vinculo de codigo, nome, IP e credenciais da DVR.
   - Integracao via Telethon (`telegram_bot.py`) com reconexao automatica infinita: le pedidos postados no grupo do Telegram, interpreta datas e cameras com Google Gemini e inicia os downloads sozinho quando a escola ja possui IP cadastrado.

7. **Player Web Continuo:**
   - As gravacoes concluidas podem ser assistidas direto na pagina do navegador ou baixadas pelo botao de download.

8. **Gestao da Fila e Preservacao de Estatisticas:**
   - O botao **"Remover finalizadas"** (e "Remover da lista") na interface web remove somente solicitacoes ja concluidas, com erro ou canceladas — tarefas em andamento nunca sao afetadas.
   - As estatisticas gerais de unidades e contagens de gravacoes permanecem preservadas no banco SQLite mesmo apos a remocao visual das tarefas da lista.
   - **Menu de Acoes da Atividade (menu ⋮):**
     - **🔁 Tentar novamente:** no menu ⋮ da atividade; aparece so quando a atividade deu erro, reiniciando o processamento do pedido.
     - **✏️ Reutilizar dados / Editar pedido:** preenche o formulario com os dados do pedido para corrigir so o que errou (ex.: horario, data ou camera).

9. **Aba "💾 Armazenamento":**
   - Mostra a pasta de destino, espaco total/usado/livre, quantidade de gravacoes, ha quanto tempo cada uma foi baixada, aviso quando faltar ≤1 dia para exclusao, e botao de excluir gravacao (apaga so o arquivo; estatisticas preservadas no banco de dados).

---

## Estrutura do Projeto

```
projeto-onda/
├── app.py                   # Servidor Flask, API REST e Pool de Trabalhadores (Workers)
├── hikvision_sdk.py         # Integracao com a biblioteca nativa HCNetSDK (Windows porta 8000)
├── telegram_bot.py          # Integracao Telethon + Google Gemini com auto-reconexao
├── autenticar_telegram.py   # Script unico para login inicial da sessao do Telegram
├── requirements.txt         # Dependencias Python
├── setup_and_run.ps1        # Instalador + Watchdog automatico para Windows PowerShell
├── setup_and_run.sh         # Instalador + Watchdog automatico para Linux
├── sdk_dlls/                # DLLs oficiais da Hikvision (HCNetSDK, OpenSSL, HCNetSDKCom)
├── static/
│   └── index.html           # Interface web (formulario, fila simultanea, estatisticas, armazenamento e player)
└── README.md                # Este manual
```

---

## SDK Nativo Hikvision e DLLs (sdk_dlls/)

O Projeto Onda 2.0 suporta comunicacao de alta velocidade na porta 8000 via SDK nativo da Hikvision (`HCNetSDK.dll` e componentes relacionados):

- **Origem no servidor do mantenedor:** No servidor do mantenedor, as DLLs foram copiadas diretamente da instalacao do cliente oficial para desktop da Hikvision/JFL (como o **WD Desk** / **WD-DESK V2** ou **iVMS-4200**).
- **Nao estao no GitHub:** Por restricoes legais de licenca de distribuicao de binarios proprietarios e pelo tamanho dos arquivos, as DLLs da pasta `sdk_dlls/` **NAO sao versionadas no GitHub**.
- **Contingencia automatica (ISAPI Web):** Sem as DLLs, o sistema continua funcionando perfeitamente em qualquer sistema operacional (Linux ou Windows). O Projeto Onda detecta a ausencia das DLLs e utiliza automaticamente o protocolo **ISAPI Web (porta 80 HTTP)** para todos os downloads.
- **Como o usuario pode colocar as proprias DLLs (somente Windows 64-bit):**
  1. Instale o software cliente Hikvision ou JFL oficial (ex.: **WD Desk / WD-DESK V2 Client** ou **iVMS-4200**).
  2. Localize a pasta de instalacao do programa (geralmente em `C:\Program Files\wdesk v2\client`, `C:\Program Files\WD-DESK V2\Client` ou diretorio equivalente).
  3. Copie os arquivos de DLL (`HCNetSDK.dll`, `HCCore.dll`, bibliotecas OpenSSL, `PlayCtrl.dll`, etc.) e a subpasta `HCNetSDKCom/` para a pasta `sdk_dlls/` na raiz do Projeto Onda.
  4. Ao reiniciar o servidor no Windows, o modulo `hikvision_sdk.py` carregara as DLLs e ativara a porta 8000 prioritariamente.

---

## Integracao com o Telegram e Inteligencia Artificial (Google Gemini)

O modulo de integracao captura de forma autonoma pedidos de gravacao postados em grupos do Telegram e os converte em downloads prontos.

### 1. Leitura de Mensagens via Telethon (User Client)
- A API padrao de bots do Telegram (BotFather) possui uma limitacao intrinseca em grupos: ela **nao consegue visualizar mensagens enviadas por outros bots**, a menos que o bot seja administrador.
- Para superar isso sem exigir cargos administrativos no grupo, o `telegram_bot.py` utiliza a biblioteca **Telethon** atuando como conta de usuario (*User Client*).
- Dessa forma, o script tem visibilidade completa de todas as mensagens do grupo, incluindo pedidos gerados automaticamente por outros bots.

### 2. Autenticacao Inicial em Uma Etapa (`autenticar_telegram.py`)
Antes de iniciar o servidor com o bot pela primeira vez, execute o script de login uma unica vez no terminal:
```bash
python autenticar_telegram.py
```
O script interativo solicitara:
1. Seu numero de telefone internacional (ex: `+55...`);
2. O codigo de confirmacao enviado pelo Telegram na conversa oficial do app;
3. Sua senha de verificacao em duas etapas (caso tenha configurado).

Apos a autenticacao, ele salva o arquivo de sessao local `onda_user.session`. Nas inicializacoes seguintes, o Projeto Onda conecta silenciosamente em segundo plano, com reconexao automatica caso a conexao de rede oscile.

### 3. Extracao com IA (Google Gemini)
Ao receber mensagens de solicitacao no grupo configurado:
- O texto e encaminhado para o **Google Gemini** (`gemini-3.5-flash`);
- A IA extrai as informacoes estruturadas em JSON:
  - Codigo e/ou nome da escola;
  - Relacao de cameras solicitadas;
  - Data e horario de inicio e fim, convertidos com precisao no fuso horario local (`America/Cuiaba` UTC-4).

### 4. Fluxo de Decisao e Status na Aba Telegram
O sistema pesquisa a escola no banco SQLite local (`dvr_app.db`):
- **Escola com IP cadastrado:** O job e cadastrado imediatamente na fila e o download comeca de forma 100% autonoma.
- **Escola sem IP cadastrado (`aguardando_ip`):** A solicitacao fica registrada na tabela com o status `aguardando_ip` e surge na aba **Telegram** do painel web.
- **Acoes do operador na aba Telegram:**
  - **Iniciar e Salvar IP:** O operador insere o endereco IP da DVR e clica em **"▶ Baixar e Salvar IP"**. O sistema agenda o download e salva o IP no cadastro da escola, tornando todos os futuros pedidos dessa unidade 100% automaticos.
  - **Descartar:** Caso o pedido seja invalido, duplicado ou cancelado, basta clicar em **"✕ Descartar"**.

### 5. Variaveis de Ambiente Necessarias (`.env`)
Crie ou configure o arquivo `.env` na raiz do projeto com as variaveis necessarias:

```ini
# Telegram - Dados da aplicacao obtidos em https://my.telegram.org
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_GROUP_ID=

# Opcional: Token do bot BotFather caso queira notificacoes de status no grupo
TELEGRAM_BOT_TOKEN=

# Google Gemini - Chave de API obtida no Google AI Studio (https://aistudio.google.com)
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash

# Concorrencia e Configuracoes de DVR
DVR_MAX_WORKERS=5
DVR_TIMEZONE=America/Cuiaba
DVR_USUARIO_PADRAO=admin
DVR_SENHA_PADRAO=
DVR_PORTA_SDK_PADRAO=8000

# Armazenamento e Politica de Retencao
DVR_APP_DESTINO=
DVR_RETENCAO_DIAS=30
DVR_RETENCAO_AUTO=0
```

#### Detalhamento das Variaveis de Armazenamento:
- `DVR_APP_DESTINO`: pasta de destino (pode ser pasta compartilhada de rede: no Windows use caminho UNC como `\\IP-DO-SERVIDOR\pasta` ou unidade mapeada; no Linux uma montagem SMB/CIFS; o usuario que roda o app precisa de permissao de leitura/escrita/exclusao na pasta).
- `DVR_RETENCAO_DIAS`: padrao 30, usado para calcular "expira em".
- `DVR_RETENCAO_AUTO`: padrao 0 = nao exclui nada sozinho; use 1 para apagar automaticamente gravacoes mais velhas que a retencao, checando a cada hora).

---

## Instalacao e Execucao no Windows (PowerShell)

A instalacao e a execucao sao feitas totalmente via terminal utilizando o script automatizado `setup_and_run.ps1`.

### Pre-requisito:
- Windows com Python 3.10 ou superior instalado (marcar a opcao "Add Python to PATH" durante a instalacao do Python).

### Passo a passo no PowerShell:

1. Abra o **PowerShell** e navegue ate a pasta do projeto:
   ```powershell
   cd C:\caminho\para\projeto-onda
   ```

2. Permita a execucao de scripts no PowerShell para esta sessao (caso ainda nao tenha permitido):
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```

3. Execute o instalador automatico:
   ```powershell
   .\setup_and_run.ps1
   ```

### O que o instalador faz sozinho:
- Desativa o congelamento por clique (*QuickEdit Mode*) do terminal Windows.
- Verifica se o Python esta instalado e cria o ambiente virtual isolado (`.venv`).
- Instala todas as dependencias do `requirements.txt`.
- Verifica o FFmpeg (se nao estiver no computador, baixa e extrai o `ffmpeg.exe` automaticamente).
- Valida as DLLs do SDK da Hikvision na pasta `sdk_dlls/`.
- Pergunta onde voce deseja salvar as gravacoes (pasta local ou servidor na rede `\\servidor\pasta`, lembrando sua ultima escolha como sugestao padrao).
- Mantem o servidor monitorado em loop (**Watchdog**): se o processo fechar por qualquer imprevisto, ele sobe novamente sozinho em 5 segundos usando a mesma pasta de destino e retoma os downloads pendentes.

---

## Como Acessar a Interface

Com o servidor rodando:

- **No proprio servidor:**
  Abra o navegador e acesse: `http://localhost:5000`

- **De outro computador na mesma rede:**
  Acesse: `http://IP_DO_SERVIDOR:5000` (o script exibe o IP exato da sua maquina na inicializacao).

---

## Como Iniciar nas Proximas Vezes

Apos a primeira instalacao, basta rodar no PowerShell:

```powershell
.\setup_and_run.ps1
```

Para interromper o servidor a qualquer momento, pressione `Ctrl + C` no terminal.

