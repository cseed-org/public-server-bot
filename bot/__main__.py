import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

import discord
import httpx
from discord.ext import tasks

from .config import Config
from .services import D1, Parser, normalized, opted_out
from .state import State

log = logging.getLogger("verification")


def error_summary(exc):
    # Only fixed service labels and status codes; never URLs or response bodies.
    if isinstance(exc, httpx.HTTPStatusError):
        service = {
            "api.cloudflare.com": "Cloudflare D1",
            "generativelanguage.googleapis.com": "Gemini",
        }.get(exc.request.url.host, "External API")
        return f"{service} HTTP {exc.response.status_code}"
    return type(exc).__name__


class VerificationBot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.dm_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=None)
        self.config = config
        self.state = State(config.state_file)
        # Active holders and waiters keep their lock alive; idle users consume no lock storage.
        self.locks = WeakValueDictionary()
        self.api_slots = asyncio.Semaphore(config.max_concurrent_api)
        self.api_starts = deque()

    @asynccontextmanager
    async def work_slot(self, user_id):
        lock = self.member_lock(user_id)
        # No waiting tasks: duplicate events and overload are retried by a later
        # scan or DM. Neither acquire suspends when its primitive is available.
        if lock.locked() or self.api_slots.locked():
            yield False
            return
        async with lock, self.api_slots, asyncio.timeout(60):
            yield True

    def reserve_api_budget(self):
        now = time.monotonic()
        while self.api_starts and self.api_starts[0] <= now - 60:
            self.api_starts.popleft()
        if len(self.api_starts) >= self.config.max_api_per_minute:
            return False
        self.api_starts.append(now)
        return True

    def member_lock(self, user_id):
        lock = self.locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[user_id] = lock
        return lock

    async def setup_hook(self):
        limits = httpx.Limits(max_connections=self.config.max_concurrent_api,
                              max_keepalive_connections=self.config.max_concurrent_api)
        self.d1_http = httpx.AsyncClient(timeout=30, limits=limits)
        self.ai_http = httpx.AsyncClient(timeout=30, limits=limits)
        self.d1 = D1(self.config, self.d1_http)
        self.parser = Parser(self.config.gemini_key, self.config.model, self.ai_http)
        self.scan.change_interval(seconds=self.config.scan_seconds)
        self.scan.start()

    def role(self, guild):
        role = guild.get_role(self.config.role_id)
        me = guild.me
        if not role or role.is_default() or role.managed or not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            raise RuntimeError("Configure a members role below the bot's role and grant Manage Roles")
        dangerous = (
            "administrator", "manage_guild", "manage_roles", "manage_channels",
            "manage_webhooks", "kick_members", "ban_members", "moderate_members",
            "manage_messages", "manage_threads", "mention_everyone",
            "manage_nicknames", "manage_expressions", "manage_events",
            "mute_members", "deafen_members", "move_members", "view_audit_log",
        )
        if any(getattr(role.permissions, permission) for permission in dangerous):
            raise RuntimeError("The members role must not grant administrative or moderation permissions")
        return role

    def eligible(self, member):
        return member.guild.id == self.config.guild_id and not member.bot and not member.pending

    async def grant(self, member, role, matched_name=None):
        # A member may leave, resume screening, or change names during API calls.
        member = await member.guild.fetch_member(member.id)
        role = self.role(member.guild)
        if not self.eligible(member) or role in member.roles:
            return False
        if matched_name is not None and matched_name not in (member.nick, member.global_name, member.name):
            return False
        await member.add_roles(role, reason="Membership matched in D1")
        log.info("Membership role granted")
        return True

    async def check_member(self, member):
        if not self.eligible(member):
            return
        async with self.work_slot(member.id) as admitted:
            if not admitted:
                return
            role = self.role(member.guild)
            if role in member.roles:
                return
            if time.time() < self.state.deadline(member.id, "next_check") or not self.reserve_api_budget():
                return
            self.state.update(member.id, next_check=time.time() + self.config.cooldown_seconds)
            for name in dict.fromkeys(n for n in (member.nick, member.global_name, member.name) if n):
                if await self.d1.matches(name):
                    await self.grant(member, role, matched_name=name)
                    return
            stopped, next_reminder, _ = self.state.get(member.id)
            if stopped or time.time() < next_reminder:
                return
            message = self.config.message()
            # Reserve before sending: crashes and closed DMs cannot cause repeated attempts.
            if not self.state.reserve_reminder(member.id, time.time(), self.config.reminder_seconds):
                return
            try:
                await member.send(message)
            except discord.HTTPException as exc:
                log.warning("Reminder delivery failed (Discord HTTP %s, code %s); retry at next daily interval",
                            exc.status, exc.code)

    async def safe_check(self, member):
        try:
            await self.check_member(member)
        except Exception as exc:
            # Never log API response bodies, tokens, SQL parameters or message text.
            log.warning("Member check failed (%s)", error_summary(exc))

    @tasks.loop(seconds=900)
    async def scan(self):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            log.error("Configured guild unavailable; check DISCORD_GUILD_ID and bot invitation")
            return
        try:
            self.role(guild)
            if not guild.chunked:
                await guild.chunk(cache=True)
            for member in guild.members:
                # Pace the single background scan so later members are not
                # permanently starved by the shared rolling request budget.
                if len(self.api_starts) >= self.config.max_api_per_minute:
                    await asyncio.sleep(max(0, self.api_starts[0] + 60 - time.monotonic()))
                await self.safe_check(member)
        except Exception as exc:
            log.error("Scan failed (%s): check guild permissions and configuration", type(exc).__name__)

    @scan.before_loop
    async def before_scan(self):
        await self.wait_until_ready()

    async def on_member_join(self, member):
        await self.safe_check(member)

    async def on_member_update(self, before, after):
        if (before.nick, before.roles, before.pending) != (after.nick, after.roles, after.pending):
            await self.safe_check(after)

    async def on_user_update(self, before, after):
        if (before.name, before.global_name) != (after.name, after.global_name):
            guild = self.get_guild(self.config.guild_id)
            member = guild.get_member(after.id) if guild else None
            if member:
                await self.safe_check(member)

    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        try:
            await self.handle_dm(message)
        except Exception as exc:
            log.warning("DM handling failed (%s)", error_summary(exc))
            try:
                async with self.work_slot(message.author.id) as admitted:
                    if admitted:
                        await message.channel.send(self.config.message("error"))
            except (discord.HTTPException, OSError, ValueError, TimeoutError):
                log.warning("Could not deliver error message")

    async def handle_dm(self, message):
        user_id = message.author.id
        text = message.content.strip()
        guild = self.get_guild(self.config.guild_id)
        member = guild.get_member(user_id) if guild else None
        # Reject outsiders before writes, replies, or Discord REST requests.
        if not member or not self.eligible(member):
            return
        stop = opted_out(text)
        # STOP takes effect even while a verification is in flight or at capacity.
        if stop and not self.state.get(user_id)[0]:
            self.state.update(user_id, opted_out=1)
        async with self.work_slot(user_id) as admitted:
            if not admitted:
                return
            stopped, _, next_reply = self.state.get(user_id)
            if stop or normalized(text) == "start":
                if not stop and stopped:
                    self.state.update(user_id, opted_out=0)
                if time.time() < self.state.deadline(user_id, "next_command"):
                    return
                self.state.update(user_id, next_command=time.time() + self.config.cooldown_seconds)
                if stop:
                    await message.channel.send("Reminders stopped. I won't DM you again unless you send START.")
                else:
                    self.state.update(user_id, opted_out=0, next_reminder=time.time() + self.config.reminder_seconds)
                    await message.channel.send(self.config.message())
                return
            if stopped or time.time() < next_reply:
                return
            if not self.reserve_api_budget():
                return
            self.state.update(user_id, next_reply=time.time() + self.config.cooldown_seconds)
            role = self.role(guild)
            if role in member.roles:
                return
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return
            if not self.eligible(member):
                return
            role = self.role(guild)
            if role in member.roles:
                return
            if not text or len(text) > 2000:
                await message.channel.send("Please send your full name and email together as text, at most 2000 characters. Reply STOP to end reminders.")
                return
            parsed = await self.parser.parse(text)
            if parsed and await self.d1.matches(*parsed):
                if await self.grant(member, role):
                    await message.channel.send("You're verified! The members role has been added.")
                return
            await message.channel.send(self.config.message("failure"))

    async def on_error(self, event_method, *args, **kwargs):
        log.error("Discord event failed: %s", event_method)

    async def close(self):
        self.scan.cancel()
        task = self.scan.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await super().close()
        for name in ("d1_http", "ai_http"):
            client = getattr(self, name, None)
            if client:
                await client.aclose()
        self.state.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.load()
    VerificationBot(config).run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
