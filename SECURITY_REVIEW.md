# Security review — 2026-10-09

Reviewed the Python application, Discord event and DM handlers, D1 SQL construction, Gemini boundary, SQLite state, dependencies, Docker configuration, and tracked configuration files. This was a source review and mocked regression testing, not a penetration test of the deployed server. No live bot was started and no member was contacted.

## Findings and disposition

| Severity | Finding | Disposition |
| --- | --- | --- |
| High | Anyone who knows an eligible name can impersonate a member by changing their Discord name. A name/email pair also proves knowledge, not ownership. The same record can authorize multiple accounts. | **Accepted by the application owner for this review.** Matching behavior is preserved. For identity assurance, replace this policy with account linking or a single-use challenge delivered to a trusted address, bound to the Discord account. |
| High | `START` reset the DM verification cooldown, allowing repeated paid API requests. | Fixed: verification deadlines are preserved across commands; command replies have their own persistent cooldown. |
| Medium | Name changes triggered repeated D1 requests without a per-user cooldown. Concurrency limits alone did not limit total API consumption. | Fixed: persistent name-check deadlines and a shared rolling API-workflow budget. |
| Medium | Event handlers could wait indefinitely behind member locks or the API semaphore, accumulating tasks during floods or service delays. | Fixed: admission rejects duplicate or excess work without waiting, with a 60-second deadline on admitted workflows and bounded error replies. |
| Medium | Outsiders could write local state and elicit command replies; throttled DMs still fetched Discord membership before checking the cooldown. | Fixed: cached server membership and eligibility are required before writes or requests, and cooldowns precede REST requests. |
| High, if misconfigured | An administrator could configure an assignable target role with administrative or moderation powers. | Fixed for checked role permissions: reject administration, role/channel/webhook management, moderation, audit-log access, and mass mentions. Channel overrides and other integrations still require administrator review. |
| Medium | A member's eligibility or name could change while remote verification was in progress. | Reduced: fetch membership again and recheck screening, role configuration, and the matched name immediately before granting. Discord operations are not an atomic transaction, so a final API race remains possible. |
| Low | `.env` was excluded, but alternate `.env.*` credential files were not. | Fixed: exclude these variants from Git and Docker build context; keep `.env.example` trackable. This does not remove any secrets already committed in history. |

## Safeguards verified

- D1 values use bound SQL parameters; table and column identifiers are restricted. Optional eligibility filters apply to both matching paths. Zero and duplicate matches deny access; API errors do not grant roles.
- Gemini receives only the current DM and extraction instructions/schema. It has no database access, tools, Discord identifiers, or conversation history. Output types, lengths, email shape, and presence in the submitted text are checked before SQL lookup. Prompt injection cannot directly invoke a tool or assign a role; the accepted identity weakness still applies to supplied matching details.
- Outgoing mentions are disabled. Application error logs omit replies, extracted personal data, credentials, and API bodies. Local state stores IDs, preferences, and deadlines rather than names or emails.
- The supplied container runs without root, drops Linux capabilities, prevents privilege escalation, uses a read-only root filesystem, publishes no ports, and limits memory, CPU, process count, temporary storage, and log retention.
- State migration preserves existing opt-outs and reply/reminder deadlines while adding name-check and command deadlines.

## Dependency review

Updated `python-dotenv` from 1.2.1 to 1.2.2, the publisher-designated patched release in [GHSA-mf9w-mj56-hr94](https://github.com/theskumar/python-dotenv/security/advisories/GHSA-mf9w-mj56-hr94). The advisory concerns `.env` rewriting with `set_key`/`unset_key`; this application uses only `load_dotenv`, so this is defense in depth rather than a demonstrated remotely exploitable application flaw. The advisory's affected-version metadata and patched-version boundary are inconsistent; this review does not classify 1.2.1 as a confirmed vulnerable runtime path.

`pip-audit -r requirements.txt --progress-spinner off --format json` reported **no known vulnerabilities in 18 resolved runtime packages**, including discord.py 2.6.4, httpx 0.28.1, python-dotenv 1.2.2, aiohttp 3.14.4, and h11 0.16.0. This is a point-in-time result. Transitive dependencies and the Docker base tag are not locked, so audit each new build. The local audit tool was installed in `.venv`; it is not a production dependency.

## Validation and limits

- 26 unit/integration-style tests passed with mocked HTTP services and Discord members, including malicious SQL values, duplicate matches, ungrounded model output, cooldown bypass attempts, persistent throttles, outsider rejection, overload, timeout cleanup, scan progress under rate limits, privileged roles, and state migration.
- `pip check` reported no broken requirements. `docker compose config --quiet` succeeded, with a local Docker configuration-file access warning.
- Tests ran on Windows with Python 3.14.6; the Docker target is Python 3.13. The image was not built or scanned, and production Discord permissions, channel overrides, Cloudflare token scope, credentials, provider quotas, and historical secret exposure were not independently verified.
- Matching does not revoke existing roles when membership expires or a name changes. A successful role grant is itself a membership-existence signal. The owner must treat membership names/emails as discoverable, not authentication secrets.
- Limits reduce request amplification and retained tasks; they do not prevent all denial of service, coordinated multi-account abuse, or ongoing costs within the allowed budget. The Discord member cache and SQLite state still grow with the server population. Run one replica, retain the state volume, and set service quotas suitable for the deployment.

To apply the reviewed changes, rebuild and recreate the bot with `docker compose up -d --build bot`. This review did not deploy the changes. Keep the existing state volume.
