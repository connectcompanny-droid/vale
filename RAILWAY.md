# Vale na Railway

Alternativa ao servidor próprio: a Railway roda o mesmo Docker do Vale, com Postgres e Redis
gerenciados, HTTPS e deploy automático a cada push no GitHub.

Custo estimado: o plano Hobby custa no mínimo US$ 5/mês (incluídos como crédito de uso). O Vale
completo usa ~1,3 a 1,5 GB de RAM (o Chrome do monitor é a maior parte), o que dá em torno de
US$ 15 a 20/mês pela tabela da Railway. Confira em **Usage** depois da primeira semana.

## 1. Projeto e bancos

1. Plano **Hobby** ativo (o trial expirado não permite deploy).
2. **New Project > Empty project**. Nome: `vale`.
3. No projeto: **+ Create > Database > PostgreSQL** e **+ Create > Database > Redis**.
4. Região: em cada serviço, **Settings > Deploy > Region**, escolha a região da Europa
   (dados de passaporte devem ficar na UE). Faça isso também no Postgres e no Redis.

## 2. Variáveis compartilhadas

**Project Settings > Shared Variables** (valem para os três serviços do Vale):

| Variável | Valor |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `QUEUE_MODE` | `redis` |
| `AUTO_CREATE_TABLES` | `false` |
| `FERNET_KEY` | gerada uma vez (guarde fora da Railway também) |
| `ANTHROPIC_API_KEY` | sua chave |
| `UAZAPI_BASE_URL`, `UAZAPI_INSTANCE_TOKEN` | da instância UAZAPI |
| `WEBHOOK_SECRET`, `METRICS_TOKEN` | gerados |
| `STT_API_URL`, `STT_API_KEY`, `STT_MODEL` | Groq ou OpenAI |
| `TTS_PROVIDER` | `none` |
| `LOG_JSON` | `true` |

O Vale converte sozinho o `postgresql://` da Railway para o driver assíncrono.

## 3. Três serviços do mesmo repositório

Crie três vezes **+ Create > GitHub Repo > connectcompanny-droid/vale** e renomeie cada um.
A Railway detecta o `Dockerfile`. Em cada serviço, **Variables**:

| Serviço | Variáveis próprias | Rede |
|---|---|---|
| `api` | `VALE_ROLE=api`, `RUN_MIGRATIONS=true`, `WEB_CONCURRENCY=1` | **Settings > Networking > Generate Domain** |
| `worker` | `VALE_ROLE=worker` | nenhuma |
| `monitor` | `VALE_ROLE=monitor` | nenhuma; **sempre 1 réplica** |

No `api`, **Settings > Deploy > Healthcheck Path** = `/ready`. Não coloque healthcheck no
`worker` nem no `monitor` (eles não servem HTTP).

## 4. Testar

```
curl https://SEU-API.up.railway.app/health
curl https://SEU-API.up.railway.app/ready
```

Webhook da UAZAPI: `https://SEU-API.up.railway.app/webhook/uazapi?secret=SEU_WEBHOOK_SECRET`.
Depois siga as fases 6 a 9 do passo a passo (teste no WhatsApp, validação em Granada).

## Operação

- Logs: clique no serviço > **Deployments > View logs**. Alertas do monitor começam com `ALERT`.
- Atualizar: `git push` no `main` faz o deploy dos três serviços.
- Mais conversas: aumente as réplicas do `worker`. O `monitor` fica sempre com 1.
- Backups: o Postgres da Railway tem backups no plano pago; exporte também com `pg_dump`
  periodicamente (os dados só servem junto com a `FERNET_KEY`).
