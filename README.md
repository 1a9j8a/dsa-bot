# DSA Bot (WhatsApp + Z-API)

Assistente da **DSA Cristal Química** feito em FastAPI, integrado à **Z-API** para receber e enviar mensagens do WhatsApp.

## Como funciona
- Webhook: `POST /api/webhook/receber`
- Healthcheck: `GET /health`
- Fluxos:
  - Menu de boas-vindas
  - Captura de lead (nome → empresa → cidade) salvo em `leads.csv`
  - Envio de catálogos (Rezymol / Pitty) por link ou arquivo (se configurado)

## Variáveis de ambiente (NÃO comitar)
```env
ZAPI_BASE=https://api.z-api.io
ZAPI_INSTANCE_ID=SEU_ID
ZAPI_TOKEN=SEU_TOKEN
ZAPI_CLIENT_TOKEN=SEU_CLIENT_TOKEN
# opcionais (links públicos dos PDFs)
CATALOG_REZYMOL_URL=https://seu-link-publico.pdf
CATALOG_PITTY_URL=https://seu-link-publico.pdf
