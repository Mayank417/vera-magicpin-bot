# Vera Merchant AI Assistant - Magicpin AI Challenge

This project implements the required public HTTP bot surface for the magicpin Vera challenge.

## Implemented endpoints

- `POST /v1/context` - accepts category, merchant, customer and trigger contexts with idempotent version handling.
- `POST /v1/tick` - inspects active triggers and returns proactive bot actions.
- `POST /v1/reply` - handles merchant/customer replies synchronously.
- `GET /v1/healthz` - returns service status and loaded context counts.
- `GET /v1/metadata` - returns team and approach metadata.
- `POST /v1/teardown` - optional cleanup endpoint to clear in-memory state.

## Approach

The bot uses a deterministic context composer instead of relying on an external LLM API. This keeps responses fast, stable, and within the challenge timeout budget. The composer uses:

1. CategoryContext for voice, offers, peer stats and digest items.
2. MerchantContext for identity, performance, active offers, signals and customer aggregates.
3. TriggerContext for the reason to message now.
4. CustomerContext for customer-facing messages when available.

It also includes simple routing for:

- Research/compliance/category digest messages.
- Performance dips and spikes.
- Review theme alerts.
- Competitor signals.
- Renewal/winback/dormancy triggers.
- Customer recall, lapse, appointment, refill and trial follow-up flows.
- WhatsApp Business auto-reply detection.
- Stop intent detection.
- Explicit action intent detection.
- Off-topic redirection.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Open:

```text
http://localhost:8080/v1/healthz
```

## Deploy on Render

1. Create a new GitHub repository.
2. Push this project to the repository.
3. In Render, create a new Web Service from the repository.
4. Use these settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
5. Add optional environment variables:
   - `TEAM_NAME`
   - `TEAM_MEMBERS`
   - `CONTACT_EMAIL`
   - `MODEL_NAME`
6. Submit the Render base URL in the challenge portal.

Only submit the base URL, for example:

```text
https://your-service-name.onrender.com
```

The judge will automatically call `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, and `/v1/metadata`.

## Deploy with Docker

```bash
docker build -t vera-bot .
docker run -p 8080:8080 vera-bot
```

## Notes

- The bot uses in-memory state, which is acceptable for challenge testing if the service does not restart during the test window.
- Do not restart the service after warmup because the judge expects contexts to persist during the test.
- The bot avoids external data calls and does not transmit merchant/customer payloads to non-LLM external APIs.
