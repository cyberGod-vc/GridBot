
# Capital.com Demo Bot — Railway Deployment

## Scope and risk notice

This repository is configured to refuse non-demo Capital.com endpoints. It is an experimental automated strategy, not investment advice or a guarantee of profitability. Grid systems increase exposure as price moves against the basket. Stops may be exceeded because of gaps, slippage, API outages, rejected orders, or failed closes.

**Run on Capital.com Demo only.** Confirm your account's market epic, permitted order size, minimum/maximum deal size, account currency, and API behavior before running unattended.

## Repository contents

- `main.py` — worker, REST client, strategy, SQLite persistence, notifications.
- `requirements.txt` — Python dependencies.
- `Procfile` — Railway worker command.
- `.env.example` — environment-variable template.
- `RAILWAY_DEPLOYMENT.md` — this guide.

## 1. Create a GitHub repository

1. Create a new empty repository on GitHub.
2. Copy these files into the repository root.
3. Do **not** commit a real `.env` file, API key, password, Telegram token, or database file.
4. Commit and push the project.

Example commands from the project directory:

```bash
git init
git add main.py requirements.txt Procfile .env.example RAILWAY_DEPLOYMENT.md
git commit -m "Add Capital.com demo trading worker"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git push -u origin main
```

## 2. Create the Railway project

1. Sign in at [Railway](https://railway.app/).
2. Select **New Project** → **Deploy from GitHub repo**.
3. Choose the repository and authorize access if prompted.
4. Railway should detect the `Procfile` and run `worker: python main.py`.
5. This is a background worker; it does not expose an HTTP web server.

## 3. Add Railway variables

Open the service's **Variables** panel and add:

| Variable | Value |
|---|---|
| `CAPITAL_API_KEY` | Your Capital.com **demo** API key |
| `CAPITAL_IDENTIFIER` | Capital.com demo account login email |
| `CAPITAL_PASSWORD` | Capital.com API/account password required by the API |
| `CAPITAL_BASE_URL` | `https://demo-api-capital.backend-capital.com/` |
| `INSTRUMENT_SEARCH_TERM` | `Gold` (search phrase sent to Capital.com) |
| `INSTRUMENT_NAME` | `Gold Spot` (expected display name; exact match only) |
| `INSTRUMENT_EPIC` | Leave blank for automatic discovery; optionally set only after verifying the exact epic |
| `TELEGRAM_TOKEN` | Optional Telegram bot token |
| `TELEGRAM_CHAT_ID` | Optional Telegram chat ID |
| `SQLITE_PATH` | `bot_state.sqlite3` |
| `LOG_LEVEL` | `INFO` |

Do not put credentials in source code or GitHub. Keep secrets restricted to the Railway service.

## 4. Important: persist SQLite data across deploys

The bot uses SQLite for local recovery state and audit history. A normal container filesystem may be ephemeral. Attach a Railway Volume to the service and set `SQLITE_PATH` to a file path inside the mounted volume, for example:

```text
/app/data/bot_state.sqlite3
```

Ensure the chosen mount path is writable by the worker. Without persistent storage, a redeploy or instance replacement may lose local state; the bot attempts broker reconciliation, but you should not rely on that as a substitute for a persistent volume.

## 5. Verify before unattended operation

1. Confirm that the API key and account are for Demo.
2. For automatic discovery, set `INSTRUMENT_SEARCH_TERM=Gold`, `INSTRUMENT_NAME=Gold Spot`, and leave `INSTRUMENT_EPIC` blank. The bot searches `GET /api/v1/markets?searchTerm=Gold`, reads the epic from Capital.com's response, and refuses to start if the match is missing or ambiguous. It also checks that at least 20 price bars are returned before entering the trading loop.
3. Deploy and open Railway's deployment logs.
4. Verify the log says the Demo session authenticated and shows `Auto-discovered Capital.com Demo market` with the returned epic. Confirm the returned name is the Gold Spot instrument shown in your account before enabling unattended operation.
5. Check that price history is returned and enough bars exist for the 20-period calculation.
6. Test opening, confirming, monitoring, and closing a small demo position manually while watching broker positions and logs.
7. Test restart recovery, API errors, rejected order sizes, close failures, and the one-hour pause.
8. Confirm that `max_grid_levels = 3` allows at most four total trades per basket (one initial + three grid additions).
9. Never enable unattended operation until you have verified account-specific size units and P/L fields.

## 6. Capital.com API details to verify

The worker uses the native REST API paths:

- `POST /api/v1/session`
- `GET /api/v1/markets?searchTerm=Gold` (automatic market discovery when `INSTRUMENT_EPIC` is blank)
- `GET /api/v1/prices/{epic}`
- `GET /api/v1/positions`
- `POST /api/v1/positions`
- `GET /api/v1/confirms/{dealReference}`
- `DELETE /api/v1/positions/{dealId}`

It uses `CST` and `X-SECURITY-TOKEN` response headers and reauthenticates proactively after eight minutes without requests, with one retry after a 401/403 response.

Capital.com API behavior and account-specific instrument dealing rules can change. Check the current official API documentation if an endpoint, request field, confirmation response, or position P/L field differs in your account. The code intentionally does not guess a stop/limit level or attempt to convert the strategy's lot-size value into a broker-specific unit.

## 7. Strategy behavior

- Entry: 20-period SMA and population standard deviation; enter BUY below SMA − 2 SD or SELL above SMA + 2 SD.
- Grid: add in the same direction after a 0.3% adverse move from the most recent grid entry; each grid size is 1.3× the previous size.
- Maximum: one initial trade plus three grid additions.
- Basket exits: floating P/L target of +8, hard stop at −8, or profit lock activated at +3 and trailing by $1 from the peak.
- Hard stop: after a successfully confirmed basket close, pause for 3,600 seconds.
- State: SQLite records basket state, positions, history, peak P/L, lock status, and pause timestamp.

## 8. Operational limitations

- Broker-side positions and profit values are the source of truth; local SQLite is recovery and audit state.
- A basket close may fail partially. The bot keeps state and retries on later ticks rather than declaring success.
- If an unexpected or opposite-direction position is detected, stop the worker and inspect the account before continuing.
- The configured risk percentage is a placeholder guardrail, not a validated position-sizing formula. It is not used to calculate size because instrument contract values and account risk details must be verified first.
- `GET /prices/{epic}` polling is used rather than WebSocket streaming.
- The default lot size of `0.1` may be invalid for some instruments/accounts. Validate minimum size and size units in Demo first.
- The bot's local state should be treated as an operational ledger, not a tax or accounting record.
