# Discord membership bot

A Dockerized Python bot for one Discord server. It checks new members, name changes, and existing members missing your members role. It reads your existing Cloudflare D1 membership list and adds the role when either:

- A server nickname, global display name, or username matches exactly one eligible full-name record.
- A DM contains a full name and email matching exactly one eligible record together.

The membership source is the `People` table in `cseed-membership`. Full names are built from trimmed `first_name` and `last_name` values separated by one space. DM verification accepts either `uw_email` or `cs_email` on the same row as the matching name. Matching trims surrounding spaces and ignores ASCII case using SQLite `lower`. It does not use fuzzy matching or remove accents. Duplicate matches fail closed. A name match is the requested trust policy, **not proof of identity**; anyone who knows a listed name can use it. Email ownership is not verified either.

See [the security review](SECURITY_REVIEW.md) for safeguards, accepted risks, and validation results. Do not use this matching policy to protect sensitive information or privileged operations.

## Prerequisites

- A Discord server where you can install a bot and manage roles.
- A Cloudflare account with a D1 database containing your membership records, plus permission to create an API token for that account.
- A Gemini API key and access to a model that supports structured output.
- Git and Docker with the Compose plugin. On Windows or macOS, use Docker Desktop; on Windows, use Linux containers for the supplied image. For local execution instead, install Python 3.13 to match the Docker image.
- A host that stays running and can connect to Discord, Cloudflare, and Google. The bot makes outbound connections; the supplied Compose setup requires no published ports, domain, or inbound webhook endpoint.

## Setup

### 1. Download the project and create your configuration

Clone the repository, or open a terminal in your existing checkout:

```sh
git clone https://github.com/cseed-org/public-server-bot.git
cd public-server-bot
```

Run all commands below from this directory, where `compose.yaml` lives. Create `.env` from the template once; if you already have `.env`, edit it without overwriting your credentials.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

macOS or Linux:

```sh
cp .env.example .env
# Open .env in your preferred text editor.
```

Use `KEY=value` entries in `.env`. Replace the blank credentials and IDs using the steps below. Keep the file private: it is excluded from Git and the Docker build context, but Compose passes its values into the container at runtime.

### 2. Create and invite the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and give it a recognizable name.
2. Open the application's **Bot** page. Generate or reset its bot token, copy it, and set `DISCORD_TOKEN` in `.env`. Use the bot token, not the application ID or client secret.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent** and save. The bot needs member events and the member list. Message Content Intent is unnecessary for its direct-message workflow. See the [discord.py intent guide](https://discordpy.readthedocs.io/en/latest/intents.html).
4. Configure a **Guild Install** with the `bot` scope and **Manage Roles** permission, then open the generated installation link and select your server. Discord's [installation walkthrough](https://docs.discord.com/developers/quick-start/getting-started) explains the portal flow.
5. In your server settings, create or choose the role verified members should receive. Use a normal role, not `@everyone` or an integration-managed role. Move the bot's role **above** that members role and ensure the bot has **Manage Roles**. The bot rejects target roles with administrative or moderation permissions, audit-log access, or mass mentions. Review channel permission overrides separately; give the bot only the permissions it needs.
6. In Discord's user settings, enable **Advanced > Developer Mode**. Right-click your server and copy its server ID into `DISCORD_GUILD_ID`. Copy the target role's ID from the server's role settings into `DISCORD_MEMBER_ROLE_ID`. Both values must be numeric IDs, not names.

The bot handles one configured server and assigns the selected role. It does not create roles or configure the channels that role can access.

### 3. Connect your Cloudflare D1 membership database

1. In the Cloudflare dashboard, select the account that owns your D1 database. Copy its account ID into `CLOUDFLARE_ACCOUNT_ID`.
2. Open the `cseed-membership` D1 database and copy its database ID into `CLOUDFLARE_D1_DATABASE_ID`. Use the database UUID, not `cseed-membership` itself or a Worker binding name.
3. Create a custom API token scoped to that account with **D1 Read** permission, then save it as `CLOUDFLARE_API_TOKEN`. The [D1 query API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/) accepts this permission. The bot issues only `SELECT` queries and never changes membership records.
4. Use the schema settings below for the existing `People` table with `first_name`, `last_name`, `uw_email`, and `cs_email` columns. These are also the defaults. The bot does not create a D1 database, import a membership list, or run migrations.

Set these values in `.env` (replace the previous `D1_NAME_COLUMN` and `D1_EMAIL_COLUMN` settings):

```dotenv
D1_TABLE=People
D1_FIRST_NAME_COLUMN=first_name
D1_LAST_NAME_COLUMN=last_name
D1_UW_EMAIL_COLUMN=uw_email
D1_CS_EMAIL_COLUMN=cs_email
D1_STATUS_COLUMN=
D1_STATUS_VALUE=active
```

With `D1_STATUS_COLUMN=` blank, every row is eligible and `D1_STATUS_VALUE` is ignored. If you later add an eligibility column, set `D1_STATUS_COLUMN` to its name and `D1_STATUS_VALUE` to the required value. Status matching uses a direct SQL equality comparison; unlike name/email matching, the bot does not trim or lowercase it.

Table and column names must start with a letter or underscore and contain only letters, digits, and underscores. Do not include SQL expressions, spaces, or schema prefixes. Each lookup must match exactly one eligible row; duplicate names can prevent automatic name verification, while a unique name-and-email pair may still verify by DM.

### 4. Configure Gemini for DM parsing

1. Open [Google AI Studio](https://aistudio.google.com/apikey), select or create a project, and create an API key. If your existing project is not listed, follow Google's [API key setup instructions](https://ai.google.dev/gemini-api/docs/api-key) to import it.
2. Copy the key into `GEMINI_API_KEY` in `.env`.
3. Set `GEMINI_MODEL` to an API model ID available to that project with [structured output support](https://ai.google.dev/gemini-api/docs/structured-output). The template uses `gemini-3.8-flash`. Use the bare model ID, without a `models/` prefix, and confirm your project has quota for it.

Gemini extracts a full name and email from a member's DM. Name-only checks query D1 directly, but both Gemini settings are required at startup. Review the data boundary below before deploying because verification DMs are sent to Google.

### 5. Customize the messages

Edit these UTF-8 text files before starting the bot:

| File | When it is sent |
| --- | --- |
| `messages/verification.txt` | Initial verification request, recurring reminder, and response to `START`. |
| `messages/error.txt` | Temporary errors while processing a DM. |
| `messages/verification_failed.txt` | A verification reply does not produce a unique eligible match. |

Replace organization-specific wording and add your membership-list and signup URLs directly to the text. Keep the instructions to send a full name and email together, and explain `STOP` and `START`. Each file must contain 1 to 2000 characters after surrounding whitespace is trimmed. Other responses, such as successful verification, are fixed in the application.

Files are read again on every send, so edits to the mounted `messages/` directory take effect without rebuilding or restarting. If you change a file's path, update its corresponding `.env` setting and recreate the container.

### 6. Review the configuration

All settings are read from `.env` or the process environment. The following values are required:

| Variable | Value to supply |
| --- | --- |
| `DISCORD_TOKEN` | Bot token from the Discord application. |
| `DISCORD_GUILD_ID` | Numeric ID of the server the bot should manage. |
| `DISCORD_MEMBER_ROLE_ID` | Numeric ID of the role to grant. |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID that owns the membership database. |
| `CLOUDFLARE_D1_DATABASE_ID` | D1 database UUID. |
| `CLOUDFLARE_API_TOKEN` | API token authorized to read D1 in that account. |
| `GEMINI_API_KEY` | Gemini API key for your project. |
| `GEMINI_MODEL` | Supported model ID; the template sets `gemini-3.8-flash`. |

Optional settings and their defaults:

| Variable | Default | Purpose |
| --- | --- | --- |
| `D1_TABLE` | `People` | Membership table in `cseed-membership`. |
| `D1_FIRST_NAME_COLUMN` | `first_name` | First-name column. |
| `D1_LAST_NAME_COLUMN` | `last_name` | Last-name column. |
| `D1_UW_EMAIL_COLUMN` | `uw_email` | UW email column; either email can match. |
| `D1_CS_EMAIL_COLUMN` | `cs_email` | CS email column; either email can match. |
| `D1_STATUS_COLUMN` | Empty | Optional eligibility column. |
| `D1_STATUS_VALUE` | `active` | Required value when a status column is configured. |
| `MESSAGE_FILE` | `messages/verification.txt` | Verification request and reminder text. |
| `ERROR_MESSAGE_FILE` | `messages/error.txt` | Temporary-error text. |
| `FAILURE_MESSAGE_FILE` | `messages/verification_failed.txt` | Unsuccessful-verification text. |
| `STATE_DB` | `data/state.sqlite3` | Local SQLite state, separate from D1. |
| `SCAN_INTERVAL_SECONDS` | `900` | Seconds between scans of server members. |
| `REMINDER_INTERVAL_HOURS` | `24` | Minimum hours between scheduled reminder attempts. |
| `DM_COOLDOWN_SECONDS` | `60` | Separate per-user cooldowns for verification replies, automatic name checks, and command acknowledgments. |
| `MAX_CONCURRENT_API` | `2` | Maximum simultaneous workflows, including API calls and Discord replies; integer from 1 to 16. |
| `MAX_API_PER_MINUTE` | `60` | Shared rolling limit on name-check and DM verification workflows; integer from 1 to 600. |
| `BOT_CPUS` | `0.5` | Docker CPU limit, measured in CPU cores. |
| `BOT_MEMORY_LIMIT` | `256m` | Docker RAM limit; container swap is disabled. |

All three timing values must be positive, finite numbers. In Docker, relative paths resolve under `/app`. Keep messages under `messages/` and state under `data/` to use the supplied mounts; the rest of the container filesystem is read-only. Container paths are not Windows host paths.

`BOT_CPUS` and `BOT_MEMORY_LIMIT` are Compose settings and do not apply when running Python directly. Runtime limits do not limit the resources used to build the image.

### 7. Start the bot and verify the setup

Make sure Docker is running, then execute:

```sh
docker compose version
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 -f bot
```

The configuration command checks Compose syntax without printing credentials; it does not validate API keys or database access. The first build installs Python and the dependencies inside the image. Look for a running `bot` service, a successful Discord connection, and no recurring scan errors. Press `Ctrl+C` to stop following logs; the detached bot keeps running.

**The first scan starts after Discord connects and can DM existing members who lack the target role.** Complete configuration and message edits before starting, and use a test server first if you need to validate behavior without contacting your main server's members.

Use a test member account without the target role to check the full flow:

1. Join with a name matching exactly one eligible D1 record and confirm the role is assigned. Complete membership screening first if your server uses it.
2. Use a separate unverified test member with no matching nickname, display name, or username. Confirm it receives a verification DM, then send the full name and email of a test membership record together and confirm the role is assigned.
3. Before granting a test member the role, try incorrect details and confirm verification fails. Wait at least `DM_COOLDOWN_SECONDS` between replies.
4. On an unverified test member, send `STOP`, restart the bot, and confirm reminders remain disabled. Send `START` to receive the verification prompt and resume reminders.

Run exactly one bot process for this server. Stop the Docker instance before running locally. More Docker operations are listed at the bottom of this README.

## Behavior and operations

The bot scans every 15 minutes by default, including after startup. Joins, name changes, and relevant member updates trigger checks without waiting for the next scan. Members still completing Discord membership screening are skipped until screening finishes. The first unsuccessful name check sends a DM, then reminders occur at most once per 24 hours, on the next scan. API outages prevent verification and retry on later scans. Closed DMs are retried at the next daily interval. Existing role holders and bots receive no reminders. No role removals occur.

`STOP`, `unsubscribe`, `not interested`, `no thanks`, and common opt-out phrases permanently disable reminders. `START` explicitly resumes them. These commands run locally without Gemini. Opt-out does not disable automatic name-based role assignment. Custom opt-out wording beyond recognized phrases should use STOP. Opt-outs, reminder deadlines, and reply rate limits persist in the `bot-state` Docker volume, including after leaving/rejoining. Do not delete this volume (`docker compose down -v`) unless you intend to erase preferences. Run exactly **one replica** per server using this state volume.

DM parsing and automatic name checks have separate persistent per-user cooldowns (default 60 seconds). `START` never resets a verification cooldown. Command acknowledgments are also throttled; `STOP` still records the opt-out immediately during overload for cached, eligible server members. An immediate `START` can resume reminders without another acknowledgment during the command cooldown.

At most two workflows run concurrently by default, each with a 60-second deadline. Duplicate events and work arriving at capacity are dropped; retry a DM later, while member checks retry on a later scan. Uncached users and users outside the configured server are ignored before any REST calls, replies, or state writes. The background scan loads the member cache after connection.

Name checks and DM verification share a default budget of 60 workflows per rolling minute. A name workflow performs at most three D1 queries; a DM workflow performs at most one Gemini call and one D1 query. This budget resets on process restart; per-user cooldowns persist. Scans pause when the budget is exhausted so later members can still be checked. Rate limits constrain abuse but are not a daily spending cap; configure provider quotas as well.

Invalid, missing, or ambiguous details produce a generic failure without revealing whether a name/email exists. An administrator should monitor logs for configuration/API errors; logs record error types without message contents or API response bodies. Large servers may need a longer scan interval and an indexed membership schema to control D1 cost. Membership, screening, target-role permissions, and the matched Discord name (for name-based checks) are checked again immediately before granting the role.

## Resource usage

The supplied defaults reduce steady resource use:

- **256 MiB RAM ceiling** for the container, with swap disabled to avoid swapping to disk. This is a limit, not a measured memory requirement or a reservation.
- **0.5 CPU core maximum** for the container, leaving the host's other CPU capacity available. This does not mean half of the entire host's CPU capacity.
- **Two concurrent API workflows** and small reusable HTTP connection pools.
- **15-minute scans**, reducing scheduled scans from 288 to 96 per day compared with the previous five-minute default. Discord events still trigger checks immediately. Membership changes made only in D1 may take until the next scan to be detected.
- **No Discord message-history cache**, and per-user locks are released once no active task needs them. The Discord member cache remains enabled for scans and member events, so memory still grows with server size.
- **Rotating Docker logs**, configured for two 5 MB files with the local logging driver, plus a 16 MiB temporary filesystem and a 64-process limit. SQLite preferences remain persistent in the state volume.

The Compose [resource limits](https://docs.docker.com/reference/compose-file/services/) constrain the bot container; they do not cap the Docker daemon or image builds. The [local logging driver](https://docs.docker.com/engine/logging/drivers/local/) rotates and compresses logs automatically.

For an existing `.env`, set these values explicitly to adopt the new scan interval and resource defaults:

```dotenv
SCAN_INTERVAL_SECONDS=900
MAX_CONCURRENT_API=2
BOT_CPUS=0.5
BOT_MEMORY_LIMIT=256m
```

Rebuild and recreate with `docker compose up -d --build --force-recreate bot`. To reduce background activity further, use `SCAN_INTERVAL_SECONDS=1800` and `MAX_CONCURRENT_API=1`; full scans and bursts of DM verification will take longer. Keep the daily reminder interval unless you intentionally want a different reminder schedule.

Measure actual usage with `docker compose stats bot` after startup and during a scan. A container that exceeds its RAM limit can be killed and restarted; check `docker compose ps -a` and logs if it repeatedly restarts, and raise `BOT_MEMORY_LIMIT` if your server needs more memory. Resource usage depends on member count and message traffic.

## Data boundary

Gemini receives only a fixed extraction instruction, a JSON schema, and the user's current DM. It has no tools, D1 credentials, database records, Discord identifiers, conversation history, or verification results. Its only output is a name and email. The bot checks output types, lengths, email shape, and whether the extracted values appear in the original reply before passing them as bound D1 query parameters. D1 returns only up to two constant match markers, never member rows. Separate HTTP clients isolate service headers.

The submitted DM itself contains personal information and is sent to Google for parsing; database privacy does not mean the submitted details stay local. Raw replies, extracted names, and emails are not stored locally or logged by this application. Discord and Google have their own data handling policies. Local SQLite stores Discord user IDs, opt-out flags, and timing fields only. Service credentials remain in the bot process, never in model requests except the Gemini API key authentication header.

## Local development and tests

Complete the same `.env` and message setup above. Use Python 3.13 to match the Docker image, and run from the repository root so relative paths resolve correctly.

Windows PowerShell (uses the virtual environment directly, so activation is unnecessary):

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m bot
```

macOS or Linux:

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m bot
```

Tests use fake Discord members and mock HTTP services; they do not contact your server or database. Running `python -m bot` connects to the configured services and can message members. Press `Ctrl+C` to stop it. Local state is created in `data/state.sqlite3` by default and is separate from the Docker volume; switching execution methods does not transfer opt-outs or reminder history.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Docker cannot connect to its daemon | Start Docker Desktop or the Docker service. On Windows, use Linux containers. |
| Startup reports `Missing ...; configure .env` | Fill in that required variable in the root `.env`, then recreate the container. |
| Discord login or privileged-intent failure | Check the bot token and enable Server Members Intent in the Developer Portal. |
| `Configured guild unavailable` | Confirm the server ID and that the bot has been invited to that server. |
| `Scan failed (RuntimeError)` or a role assignment fails | Check the target role ID, Manage Roles permission, and that the bot's highest role is above the target. The target must be a normal assignable role. |
| `Member check failed (HTTPStatusError)` | Check the Cloudflare account/database IDs, token permissions, API availability, and quota. |
| DM verification returns a temporary error | Check both D1 and Gemini credentials, the model ID, service quotas, and schema configuration. Logs intentionally omit API response bodies. |
| A known member does not match | Check spelling, the configured columns and status filter, and duplicate eligible records. Matching is not fuzzy and does not remove accents. |
| A DM reply is ignored | Check the reply cooldown, whether the user sent `STOP`, whether they already have the role, and whether they completed membership screening. Send `START` to clear an opt-out. |
| `Reminder delivery failed` | The user may block DMs or the bot. Delivery is retried after the configured reminder interval. |
| Message-file errors or a read-only filesystem error | Check UTF-8 encoding, message length, and paths. With the supplied Compose file, keep message files under `messages/` and SQLite state under `data/`. |
| Changes to `.env` do not take effect | Recreate the container with the command below; a simple restart does not reload Compose environment values. |

## Docker commands

Run these commands from the repository root. The Compose service is named `bot`.

```sh
# Check Compose configuration without displaying environment values.
docker compose config --quiet

# Build and start in the background (also rebuild after code/dependency changes).
docker compose up -d --build bot

# Show container status, including stopped containers.
docker compose ps -a

# Watch CPU and memory use; Ctrl+C exits the monitor only.
docker compose stats bot

# Follow recent logs; Ctrl+C exits the log viewer only.
docker compose logs --tail=100 -f bot

# Restart using the existing container configuration.
docker compose restart bot

# Apply changes to .env by replacing the container while preserving state.
docker compose up -d --force-recreate bot

# Stop temporarily, preserving the container and state.
docker compose stop bot

# Start the previously stopped container with its existing configuration.
docker compose start bot

# Stop and remove containers/network while retaining the state volume.
docker compose down

# Start again after docker compose down.
docker compose up -d bot
```

Use `up -d --build` after pulling code changes, and `up -d --force-recreate` after editing `.env`. A [Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/) does not apply changed environment values. Editing the existing message files takes effect on their next send without either command.

The `bot-state` named volume stores opt-outs, reminder deadlines, and reply cooldowns. Ordinary rebuilds, recreation, and `docker compose down` retain it. **Do not add `-v` / `--volumes` to `docker compose down` unless you intend to delete that state**, including users' opt-outs. See [Compose down's volume behavior](https://docs.docker.com/reference/cli/docker/compose/down/). Keep the same Compose project name when redeploying to reuse the existing volume. The membership records themselves stay in Cloudflare D1.
