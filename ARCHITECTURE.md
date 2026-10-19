# Vale · Arquitetura e plano de capacidade

Objetivo: começar com **50 clientes novos por dia** e chegar a **10.000 por dia** sem reescrever o sistema, só adicionando máquinas e trocando configurações.

## 1. Visão geral

```
                 WhatsApp
                    │
             ┌──────▼──────┐   valida o segredo, grava nada, só enfileira
             │  api (N×)   │   responde em milissegundos
             └──────┬──────┘
                    │ Redis (fila arq)
             ┌──────▼──────┐   conversa (Claude), leitura de documentos,
             │ worker (N×) │   áudio (STT/TTS), confirmações
             └──────┬──────┘
                    │
             ┌──────▼──────┐   dados pessoais criptografados (Fernet),
             │  Postgres   │   pedidos, ofertas, auditoria, estado do site
             └──────▲──────┘
                    │
             ┌──────┴──────┐   vigia vagas e reserva; tráfego ao site
             │ monitor (N×)│   LIMITADO GLOBALMENTE pelo banco
             └──────┬──────┘
                    │  1 visita a cada SITE_MIN_GAP_SECONDS (somando tudo)
                    ▼
        Site oficial de cita previa
```

Uma única imagem Docker, cinco papéis (`docker-compose.yml`): `migrate`, `api`, `worker`, `monitor`, mais `db` e `redis`.

## 2. Os dois gargalos, e por que são tratados de forma diferente

| | Conversas (WhatsApp, IA, áudio) | Site do governo |
|---|---|---|
| Cresce com o número de clientes? | Sim | **Não pode** |
| Como escala | Mais réplicas de `api` e `worker` | Não escala: tráfego fixo e educado |
| Limite real | Rate limit da API do Claude e do WhatsApp | Vagas que o governo libera |

**Conversas** escalam horizontalmente: a API só enfileira, os workers processam, e qualquer réplica pode atender qualquer cliente.

**O site do governo** tem proteção anti-robô e já bloqueou acessos repetidos durante os testes. Por isso:

- **Uma verificação por província e trâmite, não por cliente.** 1 ou 5.000 pessoas esperando NIE em Granada geram a mesma visita.
- **Limite global** (`SITE_MIN_GAP_SECONDS`, padrão 30 s = no máximo 2.880 visitas/dia), aplicado por um `UPDATE` atômico no banco. Dez servidores ou um, o site vê o mesmo ritmo.
- **Pausa compartilhada**: um bloqueio ("Request Rejected") pausa todos os processos por 6 h; um erro de certificado, por 30 min.
- **O que o Vale nunca faz**: trocar de IP, usar proxies residenciais, resolver captcha, disfarçar o navegador ou ignorar erros de certificado. Escalar o acesso ao site por esses meios é exatamente o que o governo combate, e colocaria o produto e os clientes em risco jurídico.

## 3. Garantias com vários processos

| Problema | Solução | Onde |
|---|---|---|
| WhatsApp reenviar a mesma mensagem | `processed_messages` com chave única + `_job_id` na fila | `router._first_time`, `queue.enqueue_message` |
| Duas mensagens do mesmo cliente ao mesmo tempo | Trava por telefone no Redis (ordem garantida) | `queue.user_lock` |
| Dois monitores checarem o mesmo trâmite | Reivindicação atômica do grupo | `service.claim_group` |
| Monitores somados passarem do limite do site | Ritmo global no banco | `site_guard.acquire_visit` |
| Reserva feita por um processo que não tem a sessão aberta | Sessão pertence ao processo (`instance_id` no `hold_key`) | `ICPProvider.owns` |
| Processo morrer com sessão aberta | A oferta expira e o cliente volta à fila; `sweep` fecha sessões órfãs | `Monitor.tick`, `ICPProvider.sweep` |
| Pico de chamadas ao Claude | Um cliente e um semáforo por processo (`LLM_CONCURRENCY`) | `app/llm.py` |

Tudo isso tem testes em `tests/test_scaling.py`.

## 4. Plano de capacidade

Premissas (ajustar com dados reais): cada cliente troca ~20 mensagens no total, ~1,5 chamada ao Claude por mensagem, picos de 10× a média.

| | Estágio 1 | Estágio 2 | Estágio 3 |
|---|---|---|---|
| Clientes novos/dia | 50 | 1.000 | 10.000 |
| Mensagens/dia | ~1.000 | ~20.000 | ~200.000 |
| Média / pico | 0,01 / 0,1 msg/s | 0,2 / 2 msg/s | 2,3 / 23 msg/s |
| Chamadas ao Claude/dia | ~1.500 | ~30.000 | ~300.000 |
| `api` | 1 | 2 | 3+ atrás de load balancer |
| `worker` | 1 | 1 a 2 | 3 a 4 (concorrência 20 cada) |
| `monitor` | 1 | 1 | 1 a 2 |
| Banco | Postgres no mesmo VPS | Postgres gerenciado na UE | Postgres gerenciado + pooler |
| WhatsApp | UAZAPI | **Meta Cloud API** | Meta Cloud API |
| Infra | 1 VPS (4 vCPU, 8 GB) | 2 VPS ou 1 maior | Vários nós (Swarm/Kubernetes) |

Medição local (`scripts/simulate_load.py`): sem IA, o próprio Vale processa ~110 msg/s num único processo com SQLite. Com 3 s de latência simulada do Claude e concorrência 20, um worker faz ~8 msg/s (~700 mil mensagens/dia). **O gargalo do sistema não é o código, e sim o rate limit da API do Claude e do canal de WhatsApp.**

### O limite que software não resolve

Com `SITE_MIN_GAP_SECONDS=30`, o site recebe no máximo 2.880 visitas por dia. Isso é dividido entre os grupos (província × trâmite):

| Grupos ativos | Cada grupo é verificado a cada |
|---|---|
| 10 (Granada + Madri, 5 trâmites) | ~10 min (o mínimo configurado) |
| 50 | ~25 min |
| 250 (Espanha inteira) | ~2 h |

E, principalmente: **o Vale só reserva as vagas que o governo libera.** Com 10.000 clientes novos por dia, o número de citas conseguidas depende da oferta real de vagas, não da capacidade do sistema. Isso precisa estar claro no marketing e nos preços (por exemplo, cobrar só quando a cita é conseguida).

## 5. Como escalar, na prática

1. **Mais conversas:** `docker compose up -d --scale worker=3` (ou suba `WORKER_CONCURRENCY`).
2. **Mais webhooks:** mais réplicas de `api` atrás de um load balancer (Caddy, Nginx, Traefik), roteando só para `/ready`.
3. **Mais províncias:** mais grupos. Se a frequência por grupo cair demais, **não** aumente o tráfego ao site sem antes medir bloqueios nos logs e validar com o jurídico.
4. **Banco:** some o pool de todos os processos (`DB_POOL_SIZE + DB_MAX_OVERFLOW`) e mantenha abaixo do limite do Postgres; no Supabase, use o pooler em modo transaction.
5. **WhatsApp:** antes de ~1.000 clientes/dia, migre para a Meta Cloud API com um novo adaptador em `app/channels/` (o resto do código não muda). A UAZAPI usa um número comum e corre risco de banimento com volume alto. Como o Vale é um serviço específico de agendamento, e não um assistente de IA de uso geral, ele tende a se enquadrar nas regras atuais do WhatsApp Business; confirme antes de migrar.

## 6. Operação

- **Saúde:** `/health` (processo vivo), `/ready` (banco e Redis acessíveis).
- **Métricas:** `/metrics?token=...` em formato Prometheus: pedidos por estado, site pausado, visitas ao site, tamanho da fila. Sem dados pessoais.
- **Logs:** `LOG_JSON=true` para enviar a um agregador. Alertas importantes começam com `ALERT` (bloqueio do site, certificado).
- **Migrações:** o serviço `migrate` roda `alembic upgrade head` antes de tudo. Para mudar o banco: altere `app/models.py`, rode `alembic revision --autogenerate -m "..."` e revise o arquivo gerado. Prefira mudanças aditivas.
- **Limpeza:** um cron diário no worker apaga ids de mensagens com mais de 7 dias.

## 7. Próximos passos recomendados

1. Validar a segunda metade do fluxo de Granada (`scripts/try_icp.py --check`), uma vez, depois que o bloqueio passar.
2. Validação jurídica com advogado de extranjería.
3. Adaptador da Meta Cloud API.
4. Aviso ao cliente quando o site estiver pausado (no máximo uma vez por dia).
5. Painel de operação (pedidos, fila, bloqueios), aproveitando `/metrics`.
