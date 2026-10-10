# Discord membership bot

A Dockerized Python bot for one Discord server. It checks new members, name changes, and existing members missing your members role. It reads your existing Cloudflare D1 membership list and adds the role when either:

- A server nickname, global display name, or username matches exactly one eligible full-name record.
- A DM contains a full name and email matching exactly one eligible record together.

Matching trims surrounding spaces and ignores ASCII case using SQLite `lower`. It does not use fuzzy matching or remove accents. Duplicate matches fail closed. A name match is the requested trust policy, **not proof of identity**; anyone who knows a listed name can use it. Email ownership is not verified either.

## Setup

1. Copy `.env.example` to `.env` and fill in every service credential and ID. Never commit `.env`.
2. Create a Discord application and bot in the [Developer Portal](https://discord.com/developers/applications). Enable **Server Members Intent**. Message Content Intent is unnecessary for direct messages. Invite the bot with the `bot` scope and **Manage Roles** permission. Place its role above the members role. Copy the server and members role IDs with Discord Developer Mode enabled.
3. Set your Cloudflare account ID, D1 database ID, and an account-scoped API token authorized for D1 queries. The [query API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/) documents the accepted permissions. The application only issues `SELECT` statements and does not modify your membership database.
4. Set `D1_TABLE`, `D1_NAME_COLUMN`, and `D1_EMAIL_COLUMN` to your existing schema. Optionally set `D1_STATUS_COLUMN` and `D1_STATUS_VALUE` to require a qualifying status. Names must be simple SQL identifiers. Defaults expect `members(full_name TEXT, email TEXT)`. No automatic migration runs against your database.
5. Set `GEMINI_API_KEY` and `GEMINI_MODEL` to an available model supporting [structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output). The example uses `gemini-3.8-flash`; change it to suit your account.
6. Edit `messages/verification.txt` for the daily reminder, `messages/error.txt` for temporary verification errors, and `messages/verification_failed.txt` for unsuccessful verification. Add your website's membership-list and signup links directly to the text. Each file must be UTF-8, 1–2000 characters. Paths can be overridden with `MESSAGE_FILE`, `ERROR_MESSAGE_FILE`, and `FAILURE_MESSAGE_FILE` in `.env`. Files are read on every send, and the messages directory is mounted read-only into Docker. Other bot responses are fixed application strings.
7. Run:

```sh
docker compose up -d --build
docker compose logs -f bot
```

All service setup is through `.env`. Restart with `docker compose up -d --force-recreate` after changing environment values. Container paths default to `messages/verification.txt` and `data/state.sqlite3`; keep state under `data/` and messages under `messages/` to use the supplied mounts.

## Behavior and operations

The bot scans every five minutes by default, including after startup. Members still completing Discord membership screening are skipped until screening finishes. The first unsuccessful name check sends a DM, then reminders occur at most once per 24 hours, on the next scan. API outages prevent verification and retry on later scans. Closed DMs are retried at the next daily interval. Existing role holders and bots receive no reminders. No role removals occur.

`STOP`, `unsubscribe`, `not interested`, `no thanks`, and common opt-out phrases permanently disable reminders. `START` explicitly resumes them. These commands run locally without Gemini. Opt-out does not disable automatic name-based role assignment. Custom opt-out wording beyond recognized phrases should use STOP. Opt-outs, reminder deadlines, and reply rate limits persist in the `bot-state` Docker volume, including after leaving/rejoining. Do not delete this volume (`docker compose down -v`) unless you intend to erase preferences. Run exactly **one replica** per server using this state volume.

DM parsing is rate-limited per user (default 60 seconds), with up to four concurrent API workflows. Invalid, missing, or ambiguous details produce a generic failure without revealing whether a name/email exists. An administrator should monitor logs for configuration/API errors; logs record error types without message contents or API response bodies. Large servers may need a longer scan interval and an indexed membership schema to control D1 cost.

## Data boundary

Gemini receives only a fixed extraction instruction, a JSON schema, and the user's current DM. It has no tools, D1 credentials, database records, Discord identifiers, conversation history, or verification results. Its only output is a name and email. The bot checks output types, lengths, email shape, and whether the extracted values appear in the original reply before passing them as bound D1 query parameters. D1 returns only up to two constant match markers, never member rows. Separate HTTP clients isolate service headers.

The submitted DM itself contains personal information and is sent to Google for parsing; database privacy does not mean the submitted details stay local. Raw replies, extracted names, and emails are not stored locally or logged by this application. Discord and Google have their own data handling policies. Local SQLite stores Discord user IDs, opt-out flags, and timing fields only. Service credentials remain in the bot process, never in model requests except the Gemini API key authentication header.

## Local development and tests

```sh
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m bot
```

Tests use fake Discord members and mock HTTP services; they do not contact your server or database. For deployment validation, use a test Discord account: join with a matching name, join with a nonmatching name then DM valid name/email, try incorrect details, send STOP and restart, then send START. Check role hierarchy and closed-DM behavior before inviting real members.

Discord event and intent details: [discord.py documentation](https://discordpy.readthedocs.io/en/latest/intents.html).
