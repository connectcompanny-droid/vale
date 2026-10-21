# Vale · agente de cita previa de extranjería

> Arquitetura, escala (50 → 10.000 clientes/dia) e operação: veja **[ARCHITECTURE.md](ARCHITECTURE.md)**.

Assistente no WhatsApp que vigia as vagas de cita previa de extranjería e reserva a do usuário, **sempre com a confirmação dele**. Esta é a primeira versão funcional do plano "Instinct Europa", focada nos processos de regularização na Espanha.

## Como funciona

```
WhatsApp ──► UAZAPI ──► /webhook/uazapi (FastAPI)
                              │
                              ▼
                        app/router.py
     ┌────────────┬───────────┼─────────────┬──────────────┐
  consentimento  apagar     botões de     foto do        texto livre
  (código fixo)  dados      aprovação     documento      (agente Claude)
                              │              │              │
                              ▼              ▼              ▼
                      app/citas/service.py  ◄── regras de negócio + auditoria
                              │
                         Postgres (dados dos documentos criptografados)
                              ▲
run_monitor.py ── app/citas/monitor.py ── provider ICP (Playwright) ── site oficial de cita previa
```

1. **Consentimento primeiro.** Nenhum documento é lido antes do usuário tocar em "Aceito".
2. **Dados por foto, texto ou áudio, sempre confirmados.** O cliente escolhe:
   - **Foto** do passaporte/NIE/TIE: o Claude lê com visão.
   - **Texto**: ele digita nome, documento e nacionalidade; o agente registra com a ferramenta `submit_personal_data`.
   - **Áudio**: a nota de voz é transcrita (`app/documents/transcriber.py`), o Vale mostra *"🎙️ Entendi assim: …"* e segue como texto.

   Em todos os casos o Vale devolve os dados lidos com os botões **Está certo / Corrigir**. Os dados ficam **pendentes** e não são usados até o "Está certo". Na correção, basta dizer o campo errado (por texto ou áudio); o resto é mantido. Fotos e áudios nunca são salvos; só os campos, criptografados (Fernet).
   **Respostas em áudio (opcional).** Com `TTS_PROVIDER` configurado, o Vale também responde com nota de voz. Por padrão ele espelha o cliente: áudio recebe áudio, texto recebe texto. O cliente pode pedir "responde sempre em áudio" ou "só texto". Confirmações de dados, ofertas de vaga e textos legais continuam sempre em texto com botões, porque a grafia exata precisa ser lida. Se a voz falhar, a mensagem vai em texto.
3. **Pedido de cita.** O agente pergunta a província e o trâmite e abre o pedido. Fica `awaiting_documents` até ter os dados necessários e depois passa para `monitoring`.
4. **Monitor.** O worker vigia o site oficial pelo cliente, que não precisa acompanhar nada. Ele faz **uma verificação por província e trâmite**, não por cliente: com 100 pessoas esperando NIE em Granada, o site recebe uma visita a cada intervalo (mínimo de 10 min), e quando abrem vagas os clientes são atendidos por ordem de chegada.

   **Limites que o Vale respeita sempre:** se o site do governo bloquear o acesso ("Request Rejected"), todas as verificações param por 6 horas (`BLOCKED_PAUSE_MINUTES`); se aparecer captcha, o cliente conclui esse passo. O Vale **nunca** tenta contornar a proteção do site: sem troca de IP ou proxies, sem resolver captchas, sem disfarçar o navegador.
5. **Oferta com aprovação.** Quando acha vaga, mantém a sessão do navegador aberta e manda os botões **Confirmar / Não quero**. A vaga fica reservada para o usuário por 10 min.
6. **Reserva.** Só o toque em *Confirmar* leva à reserva. O agente de IA **não consegue** aprovar nem reservar.
7. **Captcha = pessoa no circuito.** O Vale nunca resolve captchas. Se aparecer um, o pedido vai para `needs_human` e o usuário recebe o link para concluir.

### Estados do pedido

`awaiting_documents → monitoring → offer_pending → booking → booked`
com saídas para `needs_human`, `cancelled` e `failed`.

### Regras de proteção (em código, não só no prompt)

- Um pedido ativo por usuário e trâmite (restrição única no banco): sem acúmulo de vagas.
- Reserva só em nome do próprio usuário, com os dados dele, após aprovação explícita.
- Uma oferta só pode ser aprovada pelo dono e antes de expirar.
- Tudo o que é feito em nome do usuário vai para `audit_events` (transparência do AI Act).
- "apaga tudo" / "bórralo todo" / "delete everything" → confirmação → apaga documentos, pedidos e usuário; o log de auditoria fica anonimizado.

## Estrutura

| Caminho | O que faz |
|---|---|
| `app/main.py` | API FastAPI, webhook da UAZAPI com segredo |
| `app/router.py` | Roteamento de cada mensagem (caminhos críticos sem IA) |
| `app/channels/` | Adaptador de canal; `uazapi.py` hoje, Meta Cloud API/Telegram depois |
| `app/agent/` | Agente Claude com ferramentas (abrir, consultar e cancelar pedidos, e-mail, apagar dados) |
| `app/documents/extractor.py` | Leitura de documentos por visão, saída estruturada |
| `app/documents/transcriber.py` | Transcrição de notas de voz (endpoint compatível com OpenAI: OpenAI, Groq, Whisper próprio) |
| `app/citas/catalog.py` | Trâmites e províncias suportados |
| `app/citas/service.py` | Regras de negócio e auditoria |
| `app/citas/monitor.py` | Monitor: verifica, oferece, reserva, expira (vários processos em paralelo) |
| `app/citas/site_guard.py` | Limite global de visitas e pausas compartilhadas por site |
| `app/queue.py`, `app/jobs.py` | Fila (Redis/arq): a API enfileira, os workers processam; trava por cliente |
| `app/llm.py` | Cliente único do Claude e limite de chamadas simultâneas |
| `migrations/` | Migrações do banco (Alembic) |
| `scripts/simulate_load.py` | Simulação de carga local |
| `app/citas/providers/icp.py` | Automação do site oficial de cita previa |
| `app/voice/tts.py` | Voz plugável: `none`, `elevenlabs`, `openai`; para outro serviço, uma classe `Synthesizer` nova |
| `app/i18n.py` | Mensagens fixas em PT, ES e EN |

## Rodando

```bash
cp .env.example .env      # preencha as chaves
docker compose up --build # migrate, api :8000, worker, monitor, postgres, redis
docker compose up -d --scale worker=3   # mais capacidade de conversa
```

Na UAZAPI, configure o webhook para `https://SEU-DOMINIO/webhook/uazapi?secret=SEU_WEBHOOK_SECRET` com o evento `messages`.

Testes:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Antes de colocar no ar

1. **Validar o provider ICP.** Os seletores em `SEL` e os textos em `TEXT` (`app/citas/providers/icp.py`) precisam ser conferidos no site real, para cada província de lançamento. Teste com `HEADLESS=false`.
2. **Conferir a UAZAPI.** Caminhos de envio (`/send/text`, `/send/menu`, `/message/download`) e campos do webhook estão isolados em `app/channels/uazapi.py`; compare com a integração do Sofia.
3. **Política do WhatsApp.** O acesso de assistentes de IA gerais ao WhatsApp na UE depende da medida provisória da Comissão Europeia. Mantenha Telegram/SMS como canal reserva.
4. **Jurídico e RGPD.** DPIA, termos de uso, política de privacidade e base legal para tratar documentos de identidade. Validar com advogado de imigração que o fluxo está dentro da lei.
5. **Certificados (TLS).** O Vale **nunca** ignora erros de certificado, porque dados de passaporte passam por essa conexão. Em servidores x86_64 ele usa o Google Chrome real, cuja lista de certificados se mantém atualizada; em servidores ARM (como o Oracle Cloud gratuito), onde não existe Chrome para Linux, usa o Chromium do Playwright (`BROWSER_CHANNEL=auto` escolhe sozinho). Se aparecer `ERR_CERT_*`, o monitor pausa todas as verificações por `TLS_PAUSE_MINUTES` e registra um `ALERT` no log: verifique proxy, antivírus ou rede do servidor antes de retomar.


## Próximos passos

- Lembrete na véspera da cita e lista do que levar.
- Preferências do usuário (escritório, dias, horários).
- Encaminhamento para advogado parceiro (G7 Migration) quando o caso for jurídico.
- Página de status para o usuário e painel admin.
- Canal reserva (Telegram/SMS).
