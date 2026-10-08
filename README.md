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

2. **Trabalhador em Segundo Plano (Worker Assincrono):**
   - O Flask serve a interface web enquanto uma thread separada fica em execucao continua processando a fila de downloads.
   - Voce pode cadastrar varios pedidos, fechar a aba do navegador ou fechar a sessao que o servidor continua baixando no terminal normalmente.

3. **Corte e Conversao com FFmpeg:**
   - O sistema recorta os videos no segundo exato pedido e gera arquivos `.mp4` organizados automaticamente em pastas por data, codigo, escola e camera.

4. **Validacao Inteligente de Cameras:**
   - A aplicacao le a topologia da DVR (quantidade de canais analogicos e canais IP digitais). Se um operador pedir uma camera que nao existe no aparelho (ex: camera 20 em uma DVR de 4 canais), o sistema avisa na hora que a camera e invalida, sem perder tempo tentando baixar o que nao existe.

5. **Catalogo Automatico de Escolas:**
   - O codigo da escola e obrigatorio no cadastro. Conforme novos downloads sao feitos, o banco SQLite atualiza automaticamente o vinculo de codigo, nome e IP da DVR. Ao digitar o codigo da escola no futuro, o IP preenche sozinho.

6. **Player Web Continuo:**
   - As gravacoes concluidas podem ser assistidas direto na pagina do navegador ou baixadas pelo botao de download. O player de video continua tocando sem fechar durante as atualizacoes periodicas de progresso.

---

## Estrutura do Projeto

```
projeto-onda/
├── app.py               # Servidor Flask, API e trabalhador em segundo plano
├── hikvision_sdk.py     # Integracao com a biblioteca nativa HCNetSDK (Windows)
├── requirements.txt     # Dependencias Python
├── setup_and_run.ps1    # Instalador e inicializador automatico para Windows PowerShell
├── sdk_dlls/            # DLLs oficiais da Hikvision (HCNetSDK, OpenSSL, HCNetSDKCom)
├── static/
│   └── index.html       # Interface web (formulario, barra de progresso e player)
└── README.md            # Este manual
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
- Verifica se o Python esta instalado.
- Cria o ambiente virtual isolado (`.venv`).
- Instala todas as dependencias do `requirements.txt`.
- Verifica o FFmpeg (se nao estiver no computador, ele baixa e descompacta o FFmpeg automaticamente).
- Valida as DLLs do SDK da Hikvision na pasta `sdk_dlls/`.
- Pergunta onde voce deseja salvar as gravacoes (pasta local ou compartilhamento de rede `\\servidor\pasta`).
- Inicia o servidor e exibe o endereco de acesso.

---

## Como Acessar a Interface

Com o servidor rodando no PowerShell:

- **No proprio servidor:**
  Abra o navegador e acesse: `http://localhost:5000`

- **De outro computador na mesma rede:**
  Acesse: `http://IP_DO_SERVIDOR:5000` (o script exibe o IP exato da sua maquina na inicializacao).

---

## Como Iniciar nas Proximas Vezes

Apos a primeira instalacao, para iniciar o servidor novamente, basta rodar no PowerShell:

```powershell
.\setup_and_run.ps1
```

Ou, se preferir rodar direto pelo Python do ambiente virtual:

```powershell
.\.venv\Scripts\python.exe app.py
```

Para interromper o servidor a qualquer momento, pressione `Ctrl + C` no PowerShell.
