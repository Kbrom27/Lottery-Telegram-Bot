# Telegram lottery bot

Participants pay, send a screenshot of the receipt to the bot, and automatically get sequential ticket numbers.
Built for 3,000–10,000 participants.

## How verification works (read this first)

**A screenshot is never trusted.** It is only used to read a *transaction reference*. That reference is then
matched against money that really arrived in your receiving account:

```
 user ──screenshot──► bot ──► queue ──► worker
                                          │ 1. OCR / vision model reads the reference
                                          │ 2. look it up in `bank_credits`  ◄── SMS from your receiving phone  (POST /sms)
                                          │                                  ◄── or bank statement CSV import
                                          │ 3. checks: not already used · amount · campaign window · sender name
                                          ▼
                    approved ──► gap-free ticket numbers (one DB transaction) ──► user notified
                    unclear  ──► admin review queue (screenshot + ✅/❌ buttons in Telegram)
                    invalid  ──► rejected with a reason
```

An edited or fake screenshot cannot produce tickets: without matching incoming money, it ends in manual review.
If the bank SMS arrives *after* the screenshot, the payment waits (`CREDIT_WAIT_MINUTES`) and is retried automatically.

| Layer | Guards against |
|---|---|
| Reference must exist in `bank_credits` | fake / edited screenshots |
| One credit ↔ one payment (row lock + unique constraints) | the same payment used twice, even concurrently |
| SHA-256 of the image | the exact same file sent again |
| Amount taken from the bank record, not the image | edited amounts |
| Amount must be a multiple of the ticket price | partial / odd payments → review |
| Screenshot amount ≠ bank amount, or sender names differ | suspicious → review |
| Locked counter row, all in one transaction | duplicate or skipped ticket numbers |

## Quick start

```bash
cp .env.example .env          # fill BOT_TOKEN, ADMIN_IDS, SMS_SECRET, payment instructions, dates, price
docker compose up -d --build  # Postgres + the bot (long polling by default)
```

Local development without Docker: Python 3.12+, PostgreSQL, `tesseract-ocr` (+ `tesseract-ocr-amh`),
`pip install -r requirements-dev.txt`, then `uvicorn app.main:app`.
Run **one** uvicorn process (it runs the bot, the workers and the HTTP endpoints together).

1. **Create the bot** with @BotFather → put the token in `BOT_TOKEN`.
2. **Admins:** put your numeric Telegram user ID(s) in `ADMIN_IDS`.
3. **Receive payment notifications** (this is what makes verification automatic):
   install an SMS-forwarder app on the phone that holds the receiving SIM and have it `POST` to
   `https://YOUR-DOMAIN/sms` with header `X-Secret: <SMS_SECRET>` and JSON body `{"text": "<sms body>"}`.
   (If the app can't set headers, use `https://YOUR-DOMAIN/sms?key=<SMS_SECRET>`; raw-text bodies also work.)
   Put an HTTPS reverse proxy (Caddy / nginx) in front of port 8000.
   Missed some? `python -m app.import_credits statement.csv` (columns: `reference,amount,sender,received_at`) is idempotent.
4. **Calibrate the parsers before launch — required.** The regexes in `app/extract.py` are generic, and the sample
   SMS in `tests/test_parsing.py` are *illustrative, not real provider formats*. Collect 30–50 real receipt
   screenshots and ~20 real "money received" SMS from every provider you accept, add them as test cases,
   and fix the patterns until they pass. If a provider's SMS does not contain a transaction reference, it cannot
   be matched: use a statement import or a payment-gateway API for that provider instead.
5. **Pilot** with 20–50 people (include a few fakes, crops, re-uploads) before announcing.

## Admin commands (only for `ADMIN_IDS`)

| Command | Does |
|---|---|
| `/pending` | resend cards for payments waiting for review (each also arrives automatically) |
| review card buttons | ✅ approve (suggested ticket count from the bank amount) / ❌ reject; user is notified |
| `/stats` | payments by status, tickets issued, participants, next number |
| `/find 123` · `/find @user` · `/find <reference>` | who owns a ticket / a user's tickets / status of a reference |
| `/export` | full CSV (number, telegram_id, username, payment_id, issued_at) |
| `/export public` | publishable CSV (number, anonymous holder id, masked handle) |

## The draw (`draw.py`, stdlib only)

1. **Before sales close:** `python draw.py commit` → publish the printed commitment hash; keep `draw_seed.txt` secret.
2. **After close:** `/export` and `/export public`; publish the public list. Pick a public value nobody could know when you
   committed (e.g. the hash of a future blockchain block, or one announced live by an independent third party) and run
   `python draw.py draw --tickets tickets_full.csv --entropy "<value>" --winners 3 [--one-prize-per-person]`.
3. Publish the seed. Anyone can check `sha256(seed) == commitment` and re-run the draw on the public list.

## Tests

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://USER:PASS@localhost:5432/lottery_test   # a throwaway database: tables are dropped!
pytest -q
```
Covers parsing, concurrent allocation (200 parallel approvals → numbers 1..N, no gaps/duplicates), the whole verification
flow (fake screenshots, reuse, late SMS, OCR confusions, crashed workers), the Telegram handlers and admin flow with a
fake Telegram session, the `/sms` endpoint, and the draw.

## Layout

```
app/main.py      FastAPI: /sms, Telegram webhook, starts bot + worker      app/verify.py   the decision rules
app/bot.py       participant + admin handlers                              app/service.py  approve (allocation) / reject / review
app/worker.py    queue (SKIP LOCKED), OCR, verification, notifications     app/extract.py  OCR / vision extraction + parsing
app/sms.py       bank-SMS → bank_credits                                   app/messages.py all user-facing text (translate here)
app/models.py    schema                                                    draw.py         verifiable draw
```

## Known limits & decisions

- **First valid claimant wins.** Whoever submits a valid, unused reference first gets the tickets, so someone who sees
  another person's receipt could claim it before the payer. The sender-name check helps only when both names are readable.
  For stronger binding, use a payment gateway with a per-user checkout, or ask payers to include a code.
- **Queue is the database** (`FOR UPDATE SKIP LOCKED`), not Redis/Celery: fewer moving parts, no lost jobs on crash.
- **No perceptual image hash.** Receipts from one app look nearly identical, so near-duplicate detection would flag
  innocent users. Reuse is stopped by reference uniqueness + exact-file SHA-256 instead.
- **OCR is the throughput limit** (~1 s per image per core for Tesseract). Normal traffic is spread over days; for a
  sudden burst, run more cores/processes or use `EXTRACTOR=claude`. Messages to Telegram are rate-limited (~30/s); the bot backs off automatically.
- Schema is created with `create_all`. Add Alembic migrations before changing the schema on a live campaign.
- Back up Postgres **and** `data/screenshots` (evidence for disputes). The `audit_log` table records every status change.
- **Legal:** lotteries are regulated. Confirm licensing/permission requirements in Ethiopia before collecting money.
