# EVE Diagnostics API

A backend for booking diagnostic tests at diagnostic centres and paying for them through a simulated payment provider. It uses FastAPI, PostgreSQL, SQLAlchemy, Redis and Celery.

The parts I spent the most time on are the ones that are easy to get subtly wrong: how pricing is modelled, the booking state machine, and making payment webhooks safe to deliver more than once (including at the same moment) and safe to retry when processing fails.

> This is a take-home assignment that demonstrates backend fundamentals. It is not production healthcare software. See [What I would improve](#what-i-would-improve-with-more-time).

---

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Database design](#database-design)
- [Booking lifecycle](#booking-lifecycle)
- [Payment lifecycle and refunds](#payment-lifecycle-and-refunds)
- [Webhooks: idempotency and retries](#webhooks-idempotency-and-retries)
- [Background jobs (Celery)](#background-jobs-celery)
- [Caching (Redis)](#caching-redis)
- [Rate limiting](#rate-limiting)
- [Authentication and authorization](#authentication-and-authorization)
- [Logging](#logging)
- [API reference](#api-reference)
- [Example requests](#example-requests)
- [Configuration](#configuration)
- [Running locally without Docker](#running-locally-without-docker)
- [Migrations](#migrations)
- [Tests](#tests)
- [Seed data](#seed-data)
- [Assumptions](#assumptions)
- [Edge cases handled](#edge-cases-handled)
- [Trade-offs and things I deliberately did not build](#trade-offs-and-things-i-deliberately-did-not-build)
- [What I would improve with more time](#what-i-would-improve-with-more-time)

---

## Quick start

```bash
cp .env.example .env
docker compose up --build                     # db, redis, migrate (one-shot), api, worker, beat
docker compose exec api python -m app.seed    # optional: sample centres/tests + a dev admin
```

- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs (use **Authorize** and paste an access token from `/auth/login`)

Postgres is published on host port **5433** and Redis on **6380**, so they don't clash with locally installed copies.

---

## Architecture

```
                 ┌──────────┐   enqueue    ┌─────────┐   ┌──────────┐
  client ──────▶ │   api    │ ───────────▶ │  redis  │◀──│   beat   │ (schedules periodic jobs)
  provider ────▶ │ FastAPI  │   cache /    │ broker, │   └──────────┘
                 └────┬─────┘   limits     │ cache   │
                      │                    └────┬────┘
                      │                         │ tasks
                      ▼                         ▼
                 ┌──────────┐            ┌──────────┐
                 │ postgres │ ◀───────── │  worker  │ (webhook processing, booking expiry)
                 └──────────┘            └──────────┘
```

```
app/
  main.py                 app factory: routers, error handlers, request-id middleware, /health
  db.py                   engine, session dependency, Base + id/timestamp mixins
  tasks.py                Celery app, beat schedule, background tasks + retry policy
  seed.py                 idempotent dev data
  core/
    config.py             settings from environment (pydantic-settings)
    security.py           password hashing, JWT, refresh tokens, webhook HMAC
    errors.py             AppError hierarchy + consistent error responses
    logging.py            JSON/text formatters, request id, log_event()
    redis_client.py       shared Redis connection (short timeouts)
    cache.py              catalogue read-through cache with version-key invalidation
    rate_limit.py         fixed-window limiter
  models/                 SQLAlchemy models (user, catalogue, booking, payment, refresh token)
  schemas/                Pydantic request/response models
  api/
    dependencies.py       DB session, current user, admin check, rate limits, webhook signature
    routes/               auth, centres, diagnostic_tests, bookings, payments
  services/
    auth_service.py       signup/login, refresh-token rotation and reuse detection
    booking_service.py    create / fetch-owned / list / cancel (+refund) / expire
    payment_service.py    simulated payment, webhook intake + processing, inbox admin
    payment_provider.py   the simulated gateway (payment ids, refunds)
    catalogue_service.py  commit + cache invalidation for every catalogue write
alembic/                  migrations
tests/
  unit/                   state machines, security, logging, retry backoff (no DB)
  integration/            API flows, background jobs, cache, DB constraints (real Postgres + Redis)
```

**Why it's shaped like this:**

- **A service layer only where there is business logic.** Bookings, payments and token rotation involve locking, state transitions and multi-row transactions, so that logic lives in `services/`. Catalogue CRUD is plain create/read/update/delete and stays in the routes. Its one shared concern (commit, then invalidate the cache) is a single helper.
- **Synchronous SQLAlchemy.** Each request does a few short queries. Sync code is simpler to reason about around transactions and row locks. FastAPI runs sync endpoints in a thread pool.
- **One Docker image, four roles.** `migrate`, `api`, `worker` and `beat` run the same code with different commands, so they can never drift apart.
- **Redis is optional at runtime.** The cache and rate limiter *fail open*: if Redis is unreachable, requests still succeed and a warning is logged. Only Postgres is required for correctness. `/health` reports both but returns 503 only when the database is down.
- **Schemas separate from models**, and request models use `extra="forbid"`: a client that sends `"amount"` or `"role"` gets a 422 instead of having the field silently ignored.
- **State machines live on the models.** `Booking.transition_to()` and `Payment.transition_to()` each check one transition table. No code sets a status directly.

---

## Database design

```
users 1───* bookings *───1 centre_tests *───1 diagnostic_centres
  │              │                   *
  │              │                   └───1 diagnostic_tests
  │              1
  │              │
  │         0..1 payments 1───* payment_webhook_events
  │
  └───* refresh_tokens
```

| Table | Purpose | Notable constraints |
|---|---|---|
| `users` | Patients and admins | `UNIQUE(email)`, role `CHECK` |
| `diagnostic_centres` | Name, location | – |
| `diagnostic_tests` | Catalogue of test types | `UNIQUE(name)` |
| `centre_tests` | **A test offered by a centre, at that centre's price** | `UNIQUE(centre_id, test_id)`, `CHECK(price > 0)`, FKs `ON DELETE CASCADE` |
| `bookings` | A user's appointment for one offering | FK to `centre_tests` `ON DELETE RESTRICT`, status `CHECK`, `CHECK(amount > 0)`, index `(user_id, created_at)` |
| `payments` | One payment per booking | `UNIQUE(booking_id)`, `UNIQUE(provider_payment_id)`, status `CHECK` |
| `payment_webhook_events` | Inbox of provider events + processing state | **`UNIQUE(provider_event_id)`**, status `CHECK`, index `(status, received_at)`, raw `payload` JSONB |
| `refresh_tokens` | Login sessions (hashed) | `UNIQUE(token_hash)`, FK `ON DELETE CASCADE` |

### Key decisions

**`CentreTest` is its own table because price belongs to the relationship.** The same CBC can cost ₹450 at one centre and ₹500 at another. A booking references the offering, so "a booking for a test the centre doesn't offer" can't be stored at all.

**`bookings.amount` is stored on purpose.** It records what the patient agreed to pay at booking time. If an admin changes the price later, existing bookings must not change, and a test checks this.

**The amount is calculated on the server.** `BookingCreate` has no amount field and rejects extra fields. Prices for bookings are always read from Postgres, never from the cache.

**`payments.booking_id` is unique.** A failed payment makes the booking `FAILED`, which is final, so a booking never needs a second payment. The constraint also blocks double charges from a double-clicked Pay button.

**Deletes are guarded by foreign keys.** If a centre or test has bookings, deleting it returns 409 (`RESTRICT`), so booking history can never be orphaned.

**Status columns are `VARCHAR` + `CHECK`, not native Postgres enums.** The second migration shows why: adding `EXPIRED` and `REFUNDED` was just a constraint swap, with no `ALTER TYPE`.

**Refresh tokens are stored as SHA-256 hashes.** If the database leaks, the attacker gets hashes, not usable sessions.

---

## Booking lifecycle

```
                ┌── payment SUCCESS ─────────▶ CONFIRMED ──── user cancels ──▶ CANCELLED (+ refund)
                │                              (before appointment)
PENDING ────────┼── payment FAILED  ─────────▶ FAILED
                ├── user cancels    ─────────▶ CANCELLED
                └── unpaid past window ──────▶ EXPIRED   (background job)
```

| From | Allowed to |
|---|---|
| `PENDING` | `CONFIRMED`, `FAILED`, `CANCELLED`, `EXPIRED` |
| `CONFIRMED` | `CANCELLED` (only before the appointment time; the payment is refunded) |
| `FAILED`, `CANCELLED`, `EXPIRED` | – (final) |

- No endpoint sets a status directly. Status changes only as a side effect of pay, cancel, a webhook or the expiry job, and all of them go through `transition_to()`. An invalid move returns `409 INVALID_STATE_TRANSITION`.
- **Payment window:** a booking must be paid within `BOOKING_PAYMENT_WINDOW_MINUTES` (default 30) and before its appointment time. `POST /payments/` enforces this itself (`409 BOOKING_EXPIRED`) rather than waiting for the job. A beat job runs every minute and marks such bookings `EXPIRED`.
- Bookings with a payment **in flight** (DEFERRED) are never expired. The webhook decides those.

---

## Payment lifecycle and refunds

`POST /payments/` takes a `booking_id` and an explicit `outcome`:

| `outcome` | Simulates | Payment | Booking |
|---|---|---|---|
| `SUCCESS` | provider approves immediately | `SUCCESS` | `CONFIRMED` |
| `FAILED` | provider declines | `FAILED` | `FAILED` |
| `DEFERRED` | provider answers later via webhook | `PENDING` | `PENDING` |

The outcome is explicit rather than random, so tests and demos are deterministic. A declined payment still returns **201**: a payment was created, and its status is `FAILED`.

Payment states: `PENDING → SUCCESS | FAILED`, `SUCCESS → REFUNDED`.

All steps run in **one transaction**, holding a lock on the booking row (`SELECT … FOR UPDATE`):

1. Load the booking **only if it belongs to the caller**.
2. Require that it is `PENDING`, has no payment yet, and is still inside its payment window.
3. Create the payment (`provider_payment_id = pay_…`, amount = booking amount).
4. Apply the outcome, then commit the payment and booking together.

**Refunds** are simulated synchronously by `payment_provider.refund()`. They happen in two cases:
- The user cancels a `CONFIRMED` booking before the appointment.
- A payment **succeeds after its booking was cancelled or expired**. The payment records the provider's truth (`SUCCESS`), the booking is not brought back, and the money is refunded straight away (payment → `REFUNDED`).

**Concurrency:** the booking row lock is the single serialisation point for everything that changes a booking: pay, cancel, webhook processing and expiry. A test fires 5 concurrent payment requests and asserts exactly one payment is created.

---

## Webhooks: idempotency and retries

`POST /payments/webhook/`:

```json
{ "event_id": "evt_123", "event_type": "payment.succeeded", "payment_id": "pay_…" }
```

The flow is **receive → store → acknowledge → process in the background**, the "inbox" pattern:

```
provider ──▶ verify signature + timestamp ──▶ INSERT event ON CONFLICT DO NOTHING ──▶ 202 accepted
                                                     │ (nothing inserted)                │ enqueue
                                                     └──▶ 200 duplicate                  ▼
                                                                                 worker: lock event,
                                                                                 lock booking, apply,
                                                                                 mark PROCESSED/IGNORED
                                                                                         │ transient DB error
                                                                                         ▼
                                                                                 retry 10s,20s,40s,80s,160s
                                                                                 → FAILED (admin can replay)
```

### Authenticity and replay protection

The provider sends:
- `X-Webhook-Timestamp`: unix seconds
- `X-Webhook-Signature`: `hex(HMAC-SHA256(WEBHOOK_SECRET, "{timestamp}." + raw_body))`

The timestamp is part of the signed message, so it can't be edited. Requests older than `WEBHOOK_TOLERANCE_SECONDS` (default 5 minutes) are rejected, which means a captured request can't be replayed later. If the signature is bad, missing or expired, the server returns 401 and stores nothing. Without this check, anyone could send `payment.succeeded` and confirm a booking for free.

### Exactly-once storage

`INSERT … ON CONFLICT (provider_event_id) DO NOTHING RETURNING id`:
- A row comes back: this is a new event. Return `202 {"result": "accepted"}` and queue it.
- Nothing comes back: the event id is already known. Return `200 {"result": "duplicate"}`. Nothing is stored or queued, even if the replayed body claims a different outcome.

**Why a unique constraint rather than checking first:** two identical deliveries arriving at the same moment would both see "not seen yet". With the constraint, Postgres makes the second `INSERT` wait for the first transaction and then insert nothing. A test sends 8 deliveries at once and asserts one `202` and seven `200`s.

An unknown `payment_id` returns 404 and stores nothing, so if it was a provider-side race, the provider's retry will still be accepted.

### Exactly-once processing

Celery delivers tasks **at least once**: `acks_late` means a worker crash re-delivers the task, and the sweeper may re-queue an event. So processing is idempotent too:

1. Lock the event row. If its status isn't `RECEIVED`, someone already handled it, so stop.
2. Lock the booking (the same lock as pay/cancel/expiry).
3. If the payment is `PENDING`, apply the result: `SUCCESS` → booking `CONFIRMED` (or refund if the booking is no longer pending); `FAILED` → booking `FAILED`. Mark the event `PROCESSED`.
4. If the payment is already final, change nothing. Mark the event `IGNORED`, and log a warning if the event contradicts the payment.
5. Commit the event status together with the payment and booking changes.

### Retry handling

| Failure | Behaviour |
|---|---|
| Transient (`OperationalError`, `InterfaceError`: connection drops, deadlocks, serialisation failures) | Retried with exponential backoff: 10s, 20s, 40s, 80s, 160s. Each failure is recorded (`attempts`, `last_error`). After 5 retries the event is marked `FAILED`. |
| Anything else (a bug, bad data) | Not retried, since retrying won't fix it. Marked `FAILED` immediately, with the exception logged. |
| Broker down when the event arrives | The event is already committed, so the response is still 202. The **sweeper** (beat, every 5 minutes) re-queues events stuck in `RECEIVED` for more than 15 minutes. |
| Worker crashes mid-task | `acks_late` + `task_reject_on_worker_lost` re-deliver it, and step 1 above makes the re-run harmless. |
| Event marked `FAILED` | Admins list them with `GET /payments/webhook-events?status_filter=FAILED`, fix the cause, and replay with `POST /payments/webhook-events/{id}/retry`. |

---

## Background jobs (Celery)

Redis is the broker. The jobs are in [app/tasks.py](app/tasks.py).

| Task | Trigger | Purpose |
|---|---|---|
| `process_webhook_event` | queued by the webhook endpoint | apply a provider event, with retries |
| `expire_unpaid_bookings` | beat, every 60s | `PENDING` + no payment + past the window or the appointment → `EXPIRED` |
| `requeue_stuck_webhook_events` | beat, every 5 minutes | safety net for events that were never queued |

The expiry job uses `FOR UPDATE SKIP LOCKED`, so it never waits on a booking someone is paying for at that moment. After taking the lock, it re-checks that there's still no payment.

I chose these because they are things that **must not happen inside a request**: retrying with backoff over minutes, and time-based state changes. Emails or notifications would be the next obvious tasks, but the brief doesn't ask for any.

---

## Caching (Redis)

Public catalogue reads are cached: centre list, centre detail with prices, a centre's tests, test list, and test detail. The TTL is `CATALOGUE_CACHE_TTL_SECONDS` (default 5 minutes).

**Invalidation uses a version key.** Every cache key includes the current `catalogue:version`, and any admin write bumps that version after it commits. Old entries simply stop being read and expire on their own. There's no need to track or pattern-delete keys, and there's no race: a reader fetches the version *before* querying the database, so data loaded just before a write is stored under the old version and is never served afterwards. Every catalogue write goes through `commit_catalogue_change()`, so no write path can forget to invalidate.

What is deliberately **not** cached: anything user-specific (bookings, payments), and the price used to create a booking. That price always comes from Postgres.

404s are not cached. If Redis is down, reads fall back to the database.

---

## Rate limiting

This is a fixed-window counter in Redis (`INCR` + `EXPIRE`), shared across API instances. When a limit is hit, the response is **429** `RATE_LIMITED` with a `Retry-After` header.

| Scope | Keyed by | Default |
|---|---|---|
| `/auth/signup`, `/auth/login`, `/auth/refresh`, `/auth/logout` | client IP | 10/min (`RATE_LIMIT_AUTH_PER_MINUTE`) |
| `POST /bookings`, `POST /payments/` | user id | 30/min (`RATE_LIMIT_WRITES_PER_MINUTE`) |

The webhook is not rate-limited: it's authenticated by signature, and throttling the provider would only cause retries. A fixed window can let up to twice the limit through across a window boundary; that's an accepted trade-off for one Redis round-trip. The limiter fails open if Redis is unavailable.

The client IP is the direct peer. Behind a load balancer, uvicorn's `--forwarded-allow-ips` would need configuring so the real client IP is used.

---

## Authentication and authorization

- **Signup:** passwords are hashed with **Argon2**. Emails are lower-cased. `"role"` in the body is rejected, so every signup is a `USER`.
- **Login** returns:
  - an **access token**: a JWT (HS256) with `sub`, `iat` and `exp`, lasting 15 minutes by default.
  - a **refresh token**: an opaque random string, stored hashed, lasting 7 days.
  - A wrong password and an unknown email produce the same error.
- **`POST /auth/refresh`** swaps a refresh token for a new pair, and the old refresh token stops working (*rotation*). The swap is one conditional `UPDATE … WHERE revoked_at IS NULL … RETURNING`, so two concurrent refreshes with the same token can't both succeed.
- **Reuse detection:** presenting a refresh token that was already used means it was copied. Every session for that user is revoked and they must log in again.
- **`POST /auth/logout`** revokes a refresh token. It's idempotent.
- Protected endpoints decode the access token **and load the user**, so a deleted user's token stops working immediately.

| Role | Can |
|---|---|
| anonymous | browse centres, tests and prices |
| `USER` | the above + create/list/view/cancel **their own** bookings, pay for them |
| `ADMIN` | the above + manage centres, tests and offerings; inspect and replay webhook events |

**Why an admin role exists:** the brief asks for "management" APIs but doesn't say who may use them. Letting any patient change prices is clearly wrong, so I added the smallest thing that works: a role column and an `AdminUser` dependency. Admins are created by the seed script.

**Why another user's booking is a 404, not a 403:** every booking query is filtered by the current user. A 403 would confirm that the id exists.

---

## Logging

- JSON by default in containers (`LOG_FORMAT=json`) and `key=value` text locally (`LOG_FORMAT=text`).
- Every line carries an `event` name plus structured fields, e.g. `booking.created`, `payment.refunded`, `webhook.duplicate`, `webhook.retry_scheduled`, `auth.refresh_token_reused`, `rate_limit.exceeded`.
- A **request id** is taken from `X-Request-ID` (or generated), returned in the response header, and attached to every log line written during that request.
- Every request produces one `http.request` line with method, path, status and `duration_ms`. Uvicorn's own access log is disabled to avoid duplicates.
- Celery workers use the same formatter, so API and worker logs look the same.
- Passwords, tokens and secrets are never logged. Validation errors don't echo submitted values.

```json
{"timestamp": "2026-10-01T07:56:04.919+00:00", "level": "INFO", "logger": "app.services.payment_provider",
 "event": "payment.refunded", "request_id": "83d405aa…", "payment_id": "5e12…", "amount": "400.00",
 "reason": "booking_cancelled"}
```

---

## API reference

Full interactive docs are at `/docs`. 🔓 = public, 🔑 = any logged-in user, 🛡️ = admin. ⏱ = rate limited.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/signup` | 🔓 ⏱ | Create a USER account → 201 |
| POST | `/auth/login` | 🔓 ⏱ | Access + refresh token |
| POST | `/auth/refresh` | 🔓 ⏱ | Rotate refresh token, new pair |
| POST | `/auth/logout` | 🔓 ⏱ | Revoke a refresh token → 204 |
| GET | `/auth/me` | 🔑 | Current user |
| GET | `/centres` | 🔓 | List centres (`limit`, `offset`) – cached |
| GET | `/centres/{centre_id}` | 🔓 | Centre + offered tests with prices – cached |
| POST | `/centres` | 🛡️ | Create centre |
| PATCH | `/centres/{centre_id}` | 🛡️ | Update name/location |
| DELETE | `/centres/{centre_id}` | 🛡️ | Delete (409 if it has bookings) |
| GET | `/centres/{centre_id}/tests` | 🔓 | Tests offered by the centre – cached |
| POST | `/centres/{centre_id}/tests` | 🛡️ | Offer a test at a price |
| PATCH | `/centres/{centre_id}/tests/{test_id}` | 🛡️ | Change price (existing bookings unaffected) |
| DELETE | `/centres/{centre_id}/tests/{test_id}` | 🛡️ | Stop offering (409 if booked) |
| GET | `/tests` | 🔓 | List tests (`limit`, `offset`) – cached |
| GET | `/tests/{test_id}` | 🔓 | Test details – cached |
| POST | `/tests` | 🛡️ | Create test (409 on duplicate name) |
| PATCH | `/tests/{test_id}` | 🛡️ | Update |
| DELETE | `/tests/{test_id}` | 🛡️ | Delete (409 if booked) |
| POST | `/bookings` | 🔑 ⏱ | Book a test → `PENDING` |
| GET | `/bookings` | 🔑 | Own bookings, newest first (`limit`, `offset`) |
| GET | `/bookings/{booking_id}` | 🔑 | Own booking |
| POST | `/bookings/{booking_id}/cancel` | 🔑 | Cancel own `PENDING`, or `CONFIRMED` before the appointment (refund) |
| POST | `/payments/` | 🔑 ⏱ | Simulated payment for own booking |
| POST | `/payments/webhook/` | signature | Provider event → 202 accepted / 200 duplicate |
| GET | `/payments/webhook-events` | 🛡️ | Webhook inbox (`status_filter`, `limit`, `offset`) |
| POST | `/payments/webhook-events/{id}/retry` | 🛡️ | Replay a `FAILED` event |
| GET | `/health` | 🔓 | DB + Redis status (503 if DB down) |

The payment paths keep the trailing slash used in the brief.

### Error format

Every error, including validation errors and unknown routes, has the same shape. Internal exceptions become a generic 500 and are logged server-side with the request id.

```json
{ "error": { "code": "TEST_NOT_OFFERED", "message": "This test is not offered at the selected centre" } }
```

| Status | When |
|---|---|
| 422 | Malformed body, bad UUID, past or timezone-less appointment, test not offered at centre |
| 401 | Missing/invalid/expired token, bad credentials, invalid/reused refresh token, bad or old webhook signature |
| 403 | Logged in but not an admin |
| 404 | Resource doesn't exist, or isn't yours |
| 409 | Duplicate email/test/offering, invalid state transition, booking not payable or expired, appointment passed, delete blocked by bookings |
| 429 | Rate limit exceeded (`Retry-After` header) |

---

## Example requests

```bash
# Sign up, log in
curl -s -X POST localhost:8000/auth/signup -H 'Content-Type: application/json' \
  -d '{"name":"Asha","email":"asha@example.com","password":"password123"}'
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"asha@example.com","password":"password123"}' | jq -r .access_token)

# Browse and book
curl -s localhost:8000/centres/<centre_id>        # includes tests + prices
curl -s -X POST localhost:8000/bookings -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"centre_id":"<centre_id>","test_id":"<test_id>","appointment_at":"2030-01-15T09:30:00+05:30"}'
# → {"id":"…","status":"PENDING","amount":"400.00","centre":{…},"test":{…},"patient":{…},…}

# Pay (SUCCESS | FAILED | DEFERRED)
curl -s -X POST localhost:8000/payments/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"booking_id":"<booking_id>","outcome":"DEFERRED"}'
# → {"provider_payment_id":"pay_b500…","status":"PENDING","booking_status":"PENDING",…}

# Deliver a signed webhook (send the same body twice → 202 accepted, then 200 duplicate)
BODY='{"event_id":"evt_1","event_type":"payment.succeeded","payment_id":"pay_b500…"}'
TS=$(date +%s)
SIG=$(printf '%s' "$TS.$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
curl -s -X POST localhost:8000/payments/webhook/ -H 'Content-Type: application/json' \
  -H "X-Webhook-Timestamp: $TS" -H "X-Webhook-Signature: $SIG" -d "$BODY"
# → {"event_id":"evt_1","result":"accepted"}   (the worker confirms the booking moments later)

# Cancel a confirmed booking → refund
curl -s -X POST localhost:8000/bookings/<booking_id>/cancel -H "Authorization: Bearer $TOKEN"
```

Amounts are serialised as strings (`"400.00"`) to avoid floating-point rounding. Currency is assumed to be INR.

---

## Configuration

All configuration comes from environment variables (or `.env`). See [.env.example](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | – (required) | `postgresql+psycopg://…` |
| `JWT_SECRET` | – (required) | ≥ 32 chars; the app refuses to start without it |
| `WEBHOOK_SECRET` | – (required) | ≥ 16 chars, shared with the provider |
| `REDIS_URL` | `redis://localhost:6380/0` | cache + rate limits |
| `CELERY_BROKER_URL` | `redis://localhost:6380/1` | Celery broker (separate Redis DB) |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | run tasks inline (tests / quick local runs) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 15 | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 7 | |
| `WEBHOOK_TOLERANCE_SECONDS` | 300 | max age of a signed webhook |
| `BOOKING_PAYMENT_WINDOW_MINUTES` | 30 | unpaid bookings expire after this |
| `CATALOGUE_CACHE_TTL_SECONDS` | 300 | |
| `RATE_LIMIT_ENABLED` | `true` | |
| `RATE_LIMIT_AUTH_PER_MINUTE` | 10 | per IP |
| `RATE_LIMIT_WRITES_PER_MINUTE` | 30 | per user |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `text` for terminals |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` | – | only used by `python -m app.seed` |

`.env` is git-ignored. The values in `.env.example` are for local development only.

---

## Running locally without Docker

Requires Python 3.12+, Postgres and Redis. The easiest option is `docker compose up -d db redis`.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload
celery -A app.tasks worker --loglevel=INFO           # second terminal (Windows: add --pool=solo)
celery -A app.tasks beat --loglevel=INFO             # third terminal
```

To skip running a worker locally, set `CELERY_TASK_ALWAYS_EAGER=true` and tasks run inside the API process.

---

## Migrations

The schema is managed only by Alembic. The app never calls `create_all()`.

```bash
alembic upgrade head          # apply
alembic downgrade base        # drop everything
alembic check                 # verify models and migrations agree
```

- `…_initial_schema`: the original tables. Autogenerated, then edited by hand: enum columns written as `VARCHAR` + one named `CHECK`, and constraint names following a naming convention.
- `…_refunds_expiry_webhook_inbox_refresh_tokens`: written by hand. It swaps the status `CHECK` constraints to add `EXPIRED`/`REFUNDED`, and turns webhook events into an inbox. **Existing rows are backfilled**: events that were already processed become `PROCESSED` with one attempt, and `received_at` is copied from `processed_at`. The downgrade maps new states back to the closest old ones.

In Docker, migrations run in the one-shot `migrate` service. `api`, `worker` and `beat` start only after it exits successfully, so several replicas never race to migrate.

---

## Tests

```bash
docker compose up -d db redis
pytest
```

Tests run against **real Postgres and Redis**: row locks, `ON CONFLICT`, `SKIP LOCKED`, `CHECK`/`UNIQUE` constraints and JSONB are exactly what's being tested.
- The suite uses its own `eve_test` database (created if missing) and Redis DB 15.
- The schema is built with the real migrations (`downgrade base` → `upgrade head`).
- Postgres tables and Redis are emptied around every test.
- Celery runs in **eager mode**: queued tasks run inline, and retries happen immediately, so retry behaviour is deterministic and needs no broker.

**139 tests, about 20 seconds:**

| Area | What's covered |
|---|---|
| Unit | every booking/payment transition; hashing, JWTs, refresh tokens; webhook signature incl. timestamp tampering and age; JSON/text log format; backoff schedule |
| Auth | signup, duplicate email, can't self-assign ADMIN, validation doesn't echo input, login, indistinguishable failures, missing/invalid/deleted-user tokens, **refresh rotation, reuse revokes all sessions, logout, expired refresh**, **login rate limit + `Retry-After`**, **limiter fails open** |
| Catalogue | public browsing, pagination, per-centre prices, admin CRUD, 403/401, duplicates, validation, delete blocked by bookings |
| Cache / ops | **cache hit doesn't touch the DB**, **admin write invalidates**, 404 not cached, **served when Redis is down**, `/health`, request-id propagation |
| Bookings | server-side amount, ownership isolation, invalid combos, cancel, **cancel confirmed → refund**, **can't cancel after appointment**, **expiry job (window / appointment passed / skips in-flight payments)** |
| Payments | success, failure, deferred, ownership, state checks, **expired window refused**, 5 concurrent pays → 1 payment |
| Webhooks | accepted/processed, duplicate, reused id with different outcome, **8 concurrent deliveries → stored and applied once**, **late success on cancelled booking → refund**, contradicting event ignored, extra fields kept, forged/**stale** signatures, unknown payment, **transient failures retried then succeed**, **gives up → FAILED after max retries**, **bugs not retried**, **re-running a processed event is harmless**, **sweeper re-queues stuck events**, **admin list + replay** |
| DB constraints | duplicate event id, duplicate email, duplicate offering, second payment per booking, status `CHECK` (bypassing the ORM) |

Lint/format: `ruff check . && ruff format --check .`

---

## Seed data

`python -m app.seed` (or `docker compose exec api python -m app.seed`) is deterministic and safe to re-run. It creates:
- 5 tests
- 2 centres with **overlapping tests at different prices**
- an admin from `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` (dev-only credentials in `.env.example`)

---

## Assumptions

1. A centre offers many tests and a test can be offered by many centres. **Price belongs to the offering.**
2. The booking amount is captured at booking time and never changes afterwards.
3. Payment success → `CONFIRMED`, failure → `FAILED`. **A failed booking is final; the patient books again** (as the brief's state list implies).
4. A booking must be paid within 30 minutes and before its appointment, or it expires.
5. A `PENDING` booking can always be cancelled. A `CONFIRMED` booking can be cancelled until the appointment time, with a **full refund**. There are no cancellation fees, because the brief defines no pricing policy.
6. One payment per booking.
7. Appointment slots and capacity are **not** modelled (see below).
8. The provider's `event_id` identifies an event. A re-delivery with the same id is the same event, even if its body differs.
9. Webhooks are signed with a shared HMAC secret and a timestamp, as real providers do.
10. The admin role exists for catalogue management and webhook-inbox operations. Admins are created by the seed script.
11. Currency is INR, with 2 decimal places.

---

## Edge cases handled

- Invalid bodies, unknown fields, bad UUIDs, past or timezone-less appointments → 422 with field-level details
- A client-supplied `amount` or `role` → rejected
- Booking a test the centre doesn't offer → 422. A missing centre or test → a specific 404
- Someone else's booking (read, cancel, pay) → 404
- Invalid transitions (double cancel, pay a final booking, cancel after the appointment) → 409
- Paying after the payment window, even before the expiry job runs → 409 `BOOKING_EXPIRED`
- Double-submitted payment, sequential or concurrent → exactly one payment
- Duplicate webhook, sequential or concurrent → stored and applied once
- A reused event id with a different outcome → treated as duplicate
- Success arriving after cancel or expiry → payment recorded, booking stays put, **automatic refund**
- A contradicting event for a final payment → ignored and logged
- Forged, unsigned or **replayed old** webhook → 401, nothing stored
- Webhook for an unknown payment → 404, nothing stored
- Database blip during webhook processing → retried with backoff; eventually `FAILED`, then admin replay
- Broker down at webhook intake → event kept; the sweeper re-queues it
- Worker crash mid-task → re-delivered; processing is idempotent
- Expiry job racing a payment → `SKIP LOCKED` + re-check under the lock
- Concurrent refreshes with the same refresh token → one wins; reuse revokes all sessions
- Login brute force → 429 per IP
- Redis down → cache and rate limits degrade, the API keeps working
- Deleting catalogue entries that have bookings → 409
- A deleted user's token → 401
- Unexpected errors → generic 500, details only in server logs (with request id)

---

## Trade-offs and things I deliberately did not build

- **Retrying a failed payment on the same booking.** The brief says a failed payment makes the booking `FAILED`, and keeping that state final keeps the model simple: the patient books again. Supporting retries would mean `FAILED → PENDING` and turning the unique `booking_id` into a partial unique index (at most one `PENDING`/`SUCCESS` payment per booking).
- **Appointment slots and capacity.** This needs business rules the brief doesn't give: opening hours, slot length, and capacity per centre or per test. Inventing them would be guesswork. The booking-row lock and the expiry job are the foundations a capacity check would build on.
- **Refunds are simulated synchronously.** A real gateway refund is a network call that should happen outside the DB transaction, probably as a Celery task with its own retries and a `REFUND_PENDING` state.
- **Fixed-window rate limiting** rather than sliding-window or token-bucket: one `INCR` per request, at the cost of bursts at window edges.
- **Offset pagination** is fine at this data size. Cursor pagination would be the choice for large booking histories.
- **Webhook processing is async**, so the webhook response no longer includes the resulting booking status. Clients read it from `GET /bookings/{id}`. That's the cost of fast acknowledgement and proper retries.
- **Fail-open Redis** favours availability over strict limiting; a deliberate choice for this domain.

---

## What I would improve with more time

- Real refund processing as a background job with a `REFUND_PENDING` state, plus a reconciliation job that asks the provider about payments stuck in `PENDING`
- Slot capacity once the business rules are known
- A dead-letter alert (e.g. a metric or notification) when a webhook event reaches `FAILED`
- Password policy beyond minimum length, plus email verification
- Metrics (Prometheus) and tracing; the request id is already in place for correlation
- CI (GitHub Actions running ruff + pytest with Postgres and Redis services)
