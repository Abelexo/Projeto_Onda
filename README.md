# Projeto Onda – Central de Gravações Hikvision

Aplicação web em Python para baixar, cortar e converter gravações de câmeras DVR Hikvision.

## O que o projeto faz

1. Você acessa a página no navegador e preenche um formulário com o IP da câmera, usuário, senha, número das câmeras e o intervalo de tempo que deseja baixar.
2. O servidor inicia o download automaticamente. Ele tenta primeiro pelo SDK da Hikvision (porta 8000) e, se não funcionar, cai automaticamente para ISAPI (porta 80 via HTTP).
3. Você acompanha o andamento em tempo real pela barra de progresso na página.
4. Quando o download termina, o vídeo é cortado no intervalo exato que você pediu e convertido para um formato assistível.
5. O vídeo fica disponível para reprodução direto na página.

## Requisitos

- Python 3.10 ou superior
- GStreamer instalado no sistema (para corte e conversão de vídeo)
- Acesso de rede ao DVR Hikvision

## Como rodar

O projeto precisa ser iniciado pelo terminal. Não há executável de duplo-clique.

```bash
# Instale as dependências
pip install -r requirements.txt

# Inicie o servidor
python app.py
```

Depois abra o navegador em: `http://127.0.0.1:5000`

## Estrutura dos arquivos

```
projeto-onda/
├── app.py               # Servidor Flask – lógica de download, API e banco de dados
├── hikvision_sdk.py     # Conexão com o SDK nativo da Hikvision (Windows)
├── requirements.txt     # Dependências Python do projeto
├── static/
│   └── index_clean.html # Interface web (formulário, progresso e reprodução de vídeo)
└── README.md            # Este arquivo
```

## Modos de download

| Modo | Como funciona |
|------|---------------|
| Automático | Tenta SDK (porta 8000) primeiro; se falhar, usa ISAPI (porta 80) |
| SDK | Usa a DLL oficial da Hikvision. Mais rápido, mas só funciona em Windows |
| ISAPI | Download via HTTP simples. Funciona em qualquer sistema operacional |
