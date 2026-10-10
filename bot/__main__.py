import asyncio
import logging
import time
from collections import defaultdict

import discord
import httpx
from discord.ext import tasks

from .config import Config
from .services import D1, Parser, normalized, opted_out
from .state import State

log = logging.getLogger("verification")


class VerificationBot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.dm_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.state = State(config.state_file)
        self.locks = defaultdict(asyncio.Lock)
        self.api_slots = asyncio.Semaphore(4)

    async def setup_hook(self):
        self.d1_http = httpx.AsyncClient(timeout=30)
        self.ai_http = httpx.AsyncClient(timeout=30)
        self.d1 = D1(self.config, self.d1_http)
        self.parser = Parser(self.config.gemini_key, self.config.model, self.ai_http)
        self.scan.change_interval(seconds=self.config.scan_seconds)
        self.scan.start()

    def role(self, guild):
        role = guild.get_role(self.config.role_id)
        me = guild.me
        if not role or role.is_default() or role.managed or not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            raise RuntimeError("Configure a members role below the bot's role and grant Manage Roles")
        return role

    def eligible(self, member):
        return member.guild.id == self.config.guild_id and not member.bot and not member.pending

    async def grant(self, member, role):
        await member.add_roles(role, reason="Membership matched in D1")
        log.info("Membership role granted")

    async def check_member(self, member):
        if not self.eligible(member):
            return
        async with self.locks[member.id]:
            role = self.role(member.guild)
            if role in member.roles:
                return
            async with self.api_slots:
                for name in dict.fromkeys(n for n in (member.nick, member.global_name, member.name) if n):
                    if await self.d1.matches(name):
                        await self.grant(member, role)
                        return
            stopped, next_reminder, _ = self.state.get(member.id)
            if stopped or time.time() < next_reminder:
                return
            message = self.config.message()
            # Reserve before sending: crashes and closed DMs cannot cause repeated attempts.
            self.state.update(member.id, next_reminder=time.time() + self.config.reminder_seconds)
            try:
                await member.send(message)
            except discord.HTTPException:
                log.warning("Reminder delivery failed; retry at next daily interval")

    async def safe_check(self, member):
        try:
            await self.check_member(member)
        except Exception as exc:
            # Never log API response bodies, tokens, SQL parameters or message text.
            log.warning("Member check failed (%s)", type(exc).__name__)

    @tasks.loop(seconds=300)
    async def scan(self):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            log.error("Configured guild unavailable; check DISCORD_GUILD_ID and bot invitation")
            return
        try:
            self.role(guild)
            if not guild.chunked:
                await guild.chunk(cache=True)
            for member in list(guild.members):
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
            log.warning("DM handling failed (%s)", type(exc).__name__)
            try:
                await message.channel.send(self.config.message("error"))
            except (discord.HTTPException, OSError, ValueError):
                log.warning("Could not deliver error message")

    async def handle_dm(self, message):
        user_id = message.author.id
        text = message.content.strip()
        async with self.locks[user_id]:
            if opted_out(text):
                self.state.update(user_id, opted_out=1)
                await message.channel.send("Reminders stopped. I won't DM you again unless you send START.")
                return
            stopped, _, next_reply = self.state.get(user_id)
            if normalized(text) == "start":
                self.state.update(user_id, opted_out=0, next_reminder=time.time() + self.config.reminder_seconds, next_reply=0)
                await message.channel.send(self.config.message())
                return
            if stopped:
                return
            guild = self.get_guild(self.config.guild_id)
            if not guild:
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
            if time.time() < next_reply:
                return
            self.state.update(user_id, next_reply=time.time() + self.config.cooldown_seconds)
            if not text or len(text) > 2000:
                await message.channel.send("Please send your full name and email together as text, at most 2000 characters. Reply STOP to end reminders.")
                return
            async with self.api_slots:
                parsed = await self.parser.parse(text)
                if parsed and await self.d1.matches(*parsed):
                    await self.grant(member, role)
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
