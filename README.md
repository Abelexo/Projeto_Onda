# Projeto Onda - Central de Gravacoes Hikvision

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
   - O sistema processa ate **3 pedidos de gravacao simultaneamente** em paralelo quando sao de DVRs (IPs) diferentes.
   - Se houver dois pedidos para a **mesma DVR (mesmo IP)** ao mesmo tempo, o sistema utiliza uma trava inteligente por IP (`lock_dvr`) para enfileirar o segundo pedido sem derrubar a conexao da DVR.
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
│   └── index.html           # Interface web (formulario, fila simultanea, estatisticas e player)
└── README.md                # Este manual
```

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

