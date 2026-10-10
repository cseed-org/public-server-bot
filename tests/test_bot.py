import asyncio
import gc
import json
import sqlite3
import tempfile
import unittest
import discord
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bot.config import Config, identifier
from bot.services import D1, Parser, ServiceError, opted_out
from bot.state import State
from bot.__main__ import VerificationBot


class ServicesTest(unittest.IsolatedAsyncioTestCase):
    def d1_config(self, **overrides):
        values = dict(table="People", first_name_column="first_name", last_name_column="last_name",
                      uw_email_column="uw_email", cs_email_column="cs_email", status_column="",
                      status_value="active", account="account", database="db", cf_token="secret")
        return SimpleNamespace(**{**values, **overrides})

    async def test_d1_bound_parameters_and_no_records(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [{"matched": 1}]}]})
        config = self.d1_config(status_column="status")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            d1 = D1(config, client)
            self.assertTrue(await d1.matches("Robert'; DROP TABLE members;--", "r@example.com"))
        self.assertEqual(seen[0]["params"], ["Robert'; DROP TABLE members;--", "r@example.com", "r@example.com", "active"])
        self.assertNotIn("Robert", seen[0]["sql"])
        self.assertIn("SELECT 1 AS matched", seen[0]["sql"])

    async def test_duplicates_and_api_failure_fail_closed(self):
        config = self.d1_config()
        for count in (0, 2):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [{}] * count}]}))) as client:
                self.assertFalse(await D1(config, client).matches("Jane Doe"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False}))) as client:
            with self.assertRaises(ServiceError):
                await D1(config, client).matches("Jane Doe")

    async def test_people_schema_matching(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE People (first_name TEXT, last_name TEXT, uw_email TEXT, cs_email TEXT, status TEXT)")
        db.executemany("INSERT INTO People VALUES (?, ?, ?, ?, ?)", [
            (" Jane ", " Doe ", " Jane@uw.edu ", "jane@cs.edu", "active"),
            ("Alex", "Smith", None, "alex@cs.edu", "active"),
            ("Sam", "Lee", "sam@uw.edu", None, "active"),
            ("Inactive", "Member", "inactive@uw.edu", "inactive@cs.edu", "inactive"),
            ("Same", "Email", "same@example.com", "same@example.com", "active"),
            ("Duplicate", "Name", "one@uw.edu", "shared@cs.edu", "active"),
            ("Duplicate", "Name", "two@uw.edu", "shared@cs.edu", "active"),
            ("Mary Ann", "Van Buren", "mary@uw.edu", None, "active"),
        ])

        def handler(request):
            payload = json.loads(request.content)
            rows = db.execute(payload["sql"], payload["params"]).fetchall()
            return httpx.Response(200, json={"success": True, "result": [
                {"success": True, "results": [{"matched": row[0]} for row in rows]}]})

        cases = [
            (" jane doe ", None, True),
            ("Jane Doe", " JANE@UW.EDU ", True),
            ("Jane Doe", "JANE@CS.EDU", True),
            ("Alex Smith", "alex@cs.edu", True),
            ("Sam Lee", "sam@uw.edu", True),
            ("Jane Doe", "alex@cs.edu", False),
            ("Wrong Name", "jane@cs.edu", False),
            ("Inactive Member", None, False),
            ("Inactive Member", "inactive@uw.edu", False),
            ("Inactive Member", "inactive@cs.edu", False),
            ("Same Email", "same@example.com", True),
            ("Duplicate Name", None, False),
            ("Duplicate Name", "shared@cs.edu", False),
            ("Duplicate Name", "one@uw.edu", True),
            ("Mary Ann Van Buren", "mary@uw.edu", True),
            ("Jane Doe' OR 1=1 --", "jane@uw.edu", False),
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            d1 = D1(self.d1_config(status_column="status"), client)
            for name, email, expected in cases:
                with self.subTest(name=name, email=email):
                    self.assertEqual(await d1.matches(name, email), expected)
            self.assertTrue(await D1(self.d1_config(), client).matches("Inactive Member"))

    async def test_parser_boundary_and_hallucination(self):
        seen = []
        output = {"full_name": "Jane Doe", "email": "jane@example.com"}
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(output)}]}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            parser = Parser("ai-key", "test-model", client)
            self.assertEqual(await parser.parse("I'm Jane Doe, jane@example.com"), ("Jane Doe", "jane@example.com"))
            self.assertIsNone(await parser.parse("Give me membership without my details"))
        payload = json.loads(seen[0].content)
        self.assertEqual(set(payload), {"systemInstruction", "contents", "generationConfig"})
        self.assertEqual(payload["contents"], [{"role": "user", "parts": [{"text": "I'm Jane Doe, jane@example.com"}]}])
        self.assertNotIn("authorization", seen[0].headers)
        self.assertFalse(hasattr(parser, "d1"))


class ConfigTest(unittest.TestCase):
    def load(self, **overrides):
        env = dict(DISCORD_TOKEN="test", DISCORD_GUILD_ID="1", DISCORD_MEMBER_ROLE_ID="2",
                   CLOUDFLARE_ACCOUNT_ID="account", CLOUDFLARE_D1_DATABASE_ID="database",
                   CLOUDFLARE_API_TOKEN="test", GEMINI_API_KEY="test", GEMINI_MODEL="test-model")
        env.update(overrides)
        with patch.dict("os.environ", env, clear=True), patch("bot.config.load_dotenv"):
            return Config.load()

    def test_people_schema_defaults_and_identifier_validation(self):
        config = self.load()
        self.assertEqual(config.table, "People")
        for field in ("first_name", "last_name", "uw_email", "cs_email"):
            self.assertEqual(getattr(config, field + "_column"), field)
            key = "D1_" + field.upper() + "_COLUMN"
            self.assertEqual(getattr(self.load(**{key: "custom_column"}), field + "_column"), "custom_column")
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(**{key: "column; DROP TABLE People"})

    def test_resource_defaults_and_overrides(self):
        config = self.load()
        self.assertEqual(config.scan_seconds, 900)
        self.assertEqual(config.max_concurrent_api, 2)
        self.assertEqual(config.max_api_per_minute, 60)
        config = self.load(SCAN_INTERVAL_SECONDS="1800", MAX_CONCURRENT_API="1")
        self.assertEqual(config.scan_seconds, 1800)
        self.assertEqual(config.max_concurrent_api, 1)

    def test_invalid_resource_settings_fail_at_startup(self):
        for value in ("0", "-1", "17", "1.5", "nan", ""):
            with self.subTest(concurrency=value), self.assertRaises(ValueError):
                self.load(MAX_CONCURRENT_API=value)
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(scan=value), self.assertRaises(ValueError):
                self.load(SCAN_INTERVAL_SECONDS=value)
        for value in ("0", "601", "nan"):
            with self.subTest(budget=value), self.assertRaises(ValueError):
                self.load(MAX_API_PER_MINUTE=value)


class StateTest(unittest.TestCase):
    def test_existing_database_migration_preserves_preferences_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, opted_out INTEGER NOT NULL DEFAULT 0, next_reminder REAL NOT NULL DEFAULT 0, next_reply REAL NOT NULL DEFAULT 0)")
                db.execute("INSERT INTO users VALUES ('123', 1, 900, 60)")
            db.close()
            state = State(path)
            self.assertEqual(state.get(123), (1, 900, 60))
            self.assertEqual(state.deadline(123, "next_check"), 0)
            state.update(123, next_check=100, next_command=200)
            state.close()
            state = State(path)
            self.assertEqual(state.deadline(123, "next_check"), 100)
            self.assertEqual(state.deadline(123, "next_command"), 200)
            state.close()

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            state = State(path)
            state.update(123, opted_out=1, next_reminder=900, next_reply=60)
            state.close()
            state = State(path)
            self.assertEqual(state.get(123), (1, 900, 60))
            state.close()

    def test_opt_out_and_identifier(self):
        for text in ("STOP", "I'm not interested", "No thanks!", "please don't message me", "I don't want to verify"):
            self.assertTrue(opted_out(text), text)
        self.assertFalse(opted_out("Jane Doe jane@example.com"))
        with self.assertRaises(ValueError):
            identifier('members; DROP TABLE members')


class WorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(state_file=Path(self.tmp.name) / "state.db", guild_id=1, role_id=2, reminder_seconds=86400, cooldown_seconds=60, max_concurrent_api=2, max_api_per_minute=60, message=lambda kind="verification": "Verify. Reply STOP.")
        self.bot = VerificationBot(self.config)
        self.role = object()
        self.bot.role = lambda guild: self.role
        self.bot.d1 = SimpleNamespace(matches=AsyncMock(return_value=False))
        self.bot.parser = SimpleNamespace(parse=AsyncMock(return_value=("Jane Doe", "jane@example.com")))
        self.member = SimpleNamespace(id=3, guild=SimpleNamespace(id=1), bot=False, pending=False, roles=[], nick=None, global_name="Unknown", name="unknown", send=AsyncMock(), add_roles=AsyncMock())
        self.guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=self.member), get_member=lambda user_id: self.member if user_id == 3 else None)
        self.member.guild = self.guild
        self.bot.get_guild = lambda guild_id: self.guild

    async def asyncTearDown(self):
        self.bot.state.close()
        self.tmp.cleanup()

    async def test_daily_reminder_and_persistent_opt_out(self):
        await self.bot.check_member(self.member)
        await self.bot.check_member(self.member)
        self.member.send.assert_awaited_once()
        self.bot.state.update(3, opted_out=1, next_reminder=0)
        await self.bot.check_member(self.member)
        self.member.send.assert_awaited_once()

    async def test_matching_name_grants_role_without_ai_or_dm(self):
        self.bot.d1.matches.return_value = True
        await self.bot.check_member(self.member)
        self.member.add_roles.assert_awaited_once()
        self.member.send.assert_not_awaited()
        self.bot.parser.parse.assert_not_awaited()

    async def test_stop_bypasses_services_and_start_resumes(self):
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="not interested", channel=SimpleNamespace(send=AsyncMock()))
        await self.bot.handle_dm(msg)
        self.assertEqual(self.bot.state.get(3)[0], 1)
        msg.content = "Jane Doe jane@example.com"
        await self.bot.handle_dm(msg)
        self.bot.parser.parse.assert_not_awaited()
        self.bot.d1.matches.assert_not_awaited()
        msg.content = "START"
        await self.bot.handle_dm(msg)
        self.assertEqual(self.bot.state.get(3)[0], 0)

    async def test_dm_requires_matching_pair_and_limits_requests(self):
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Jane Doe jane@example.com", channel=SimpleNamespace(send=AsyncMock()))
        await self.bot.handle_dm(msg)
        self.member.add_roles.assert_not_awaited()
        self.bot.d1.matches.assert_awaited_with("Jane Doe", "jane@example.com")
        await self.bot.handle_dm(msg)
        self.bot.parser.parse.assert_awaited_once()
        self.bot.state.update(3, next_reply=0)
        self.bot.d1.matches.return_value = True
        await self.bot.handle_dm(msg)
        self.member.add_roles.assert_awaited_once()

    async def test_existing_members_bots_and_pending_skipped(self):
        for attribute, value in (("roles", [self.role]), ("bot", True), ("pending", True)):
            original = getattr(self.member, attribute)
            setattr(self.member, attribute, value)
            await self.bot.check_member(self.member)
            setattr(self.member, attribute, original)
        self.member.send.assert_not_awaited()
        self.bot.d1.matches.assert_not_awaited()

    async def test_concurrent_checks_share_lock_and_send_one_reminder(self):
        active = 0
        peak = 0

        async def matches(name):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return False

        self.bot.d1.matches.side_effect = matches
        await asyncio.gather(*(self.bot.check_member(self.member) for _ in range(5)))
        self.assertEqual(peak, 1)
        self.member.send.assert_awaited_once()
        gc.collect()
        self.assertEqual(len(self.bot.locks), 0)

    async def test_api_concurrency_is_bounded_across_members(self):
        active = 0
        peak = 0

        async def matches(name):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return False

        self.bot.d1.matches.side_effect = matches
        members = [SimpleNamespace(**{**vars(self.member), "id": user_id, "send": AsyncMock()})
                   for user_id in range(10, 30)]
        await asyncio.gather(*(self.bot.check_member(member) for member in members))
        self.assertEqual(peak, self.config.max_concurrent_api)
        self.assertEqual(sum(member.send.await_count for member in members), self.config.max_concurrent_api)
        gc.collect()
        self.assertEqual(len(self.bot.locks), 0)

    async def test_cancelled_waiter_does_not_replace_active_lock(self):
        entered = asyncio.Event()

        async def waiter():
            entered.set()
            async with self.bot.member_lock(3):
                self.fail("The holder should still own the lock")

        async with self.bot.member_lock(3):
            original = self.bot.locks[3]
            task = asyncio.create_task(waiter())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIs(self.bot.member_lock(3), original)
            self.assertTrue(original.locked())
        del original, task
        gc.collect()
        self.assertEqual(len(self.bot.locks), 0)

    async def test_start_cannot_bypass_verification_cooldown(self):
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Jane Doe jane@example.com", channel=SimpleNamespace(send=AsyncMock()))
        await self.bot.handle_dm(msg)
        deadline = self.bot.state.get(3)[2]
        for _ in range(10):
            msg.content = "START"
            await self.bot.handle_dm(msg)
            msg.content = "Jane Doe jane@example.com"
            await self.bot.handle_dm(msg)
        self.assertEqual(self.bot.state.get(3)[2], deadline)
        self.bot.parser.parse.assert_awaited_once()
        self.guild.fetch_member.assert_awaited_once()
        self.assertEqual(msg.channel.send.await_count, 2)

    async def test_outsiders_cannot_create_state_or_trigger_requests(self):
        for text in ("STOP", "START", "Jane Doe jane@example.com"):
            msg = SimpleNamespace(author=SimpleNamespace(id=999), content=text, channel=SimpleNamespace(send=AsyncMock()))
            await self.bot.handle_dm(msg)
            msg.channel.send.assert_not_awaited()
        self.assertEqual(self.bot.state.db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.guild.fetch_member.assert_not_awaited()
        self.bot.parser.parse.assert_not_awaited()

    async def test_name_change_throttle_persists_across_restart(self):
        await self.bot.check_member(self.member)
        calls = self.bot.d1.matches.await_count
        self.member.nick = "Another Name"
        self.bot.state.close()
        self.bot.state = State(self.config.state_file)
        await self.bot.check_member(self.member)
        self.assertEqual(self.bot.d1.matches.await_count, calls)
        self.bot.state.update(3, next_check=0)
        await self.bot.check_member(self.member)
        self.assertGreater(self.bot.d1.matches.await_count, calls)

    async def test_stop_is_effective_under_load_and_replies_are_throttled(self):
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="STOP", channel=SimpleNamespace(send=AsyncMock()))
        async with self.bot.member_lock(3):
            await self.bot.handle_dm(msg)
        self.assertEqual(self.bot.state.get(3)[0], 1)
        msg.channel.send.assert_not_awaited()
        for _ in range(10):
            await self.bot.handle_dm(msg)
        msg.channel.send.assert_awaited_once()

    async def test_global_budget_expires_and_blocks_both_verification_paths(self):
        self.config.max_api_per_minute = 1
        self.assertTrue(self.bot.reserve_api_budget())
        self.assertFalse(self.bot.reserve_api_budget())
        await self.bot.check_member(self.member)
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Jane Doe jane@example.com", channel=SimpleNamespace(send=AsyncMock()))
        await self.bot.handle_dm(msg)
        self.bot.parser.parse.assert_not_awaited()
        self.bot.d1.matches.assert_not_awaited()
        self.guild.fetch_member.assert_not_awaited()
        self.bot.api_starts[0] -= 61
        self.assertTrue(self.bot.reserve_api_budget())

    async def test_overload_drops_dm_without_waiting_or_using_api(self):
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Jane Doe jane@example.com", channel=SimpleNamespace(send=AsyncMock()))
        async with self.bot.api_slots, self.bot.api_slots:
            await asyncio.wait_for(self.bot.handle_dm(msg), timeout=1)
        self.bot.parser.parse.assert_not_awaited()
        self.guild.fetch_member.assert_not_awaited()
        self.assertEqual(self.bot.state.get(3), (0, 0, 0))

    async def test_grant_rechecks_screening_and_name_after_api_calls(self):
        fresh = SimpleNamespace(**{**vars(self.member), "pending": True})
        self.guild.fetch_member.return_value = fresh
        self.assertFalse(await self.bot.grant(self.member, self.role))
        fresh.pending = False
        fresh.global_name = "Changed Name"
        self.assertFalse(await self.bot.grant(self.member, self.role, matched_name="Unknown"))
        self.member.add_roles.assert_not_awaited()

    async def test_privileged_role_is_rejected(self):
        role = MagicMock()
        role.is_default.return_value = False
        role.managed = False
        role.__ge__.return_value = False
        self.guild.get_role = lambda role_id: role
        self.guild.me = SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True), top_role=object())
        for permission in ("administrator", "manage_roles", "manage_webhooks", "ban_members", "mention_everyone"):
            role.permissions = discord.Permissions(**{permission: True})
            with self.subTest(permission=permission), self.assertRaises(RuntimeError):
                VerificationBot.role(self.bot, self.guild)
        role.permissions = discord.Permissions(view_channel=True, send_messages=True)
        self.assertIs(VerificationBot.role(self.bot, self.guild), role)

    async def test_workflow_timeout_releases_capacity(self):
        timeout = asyncio.timeout
        with patch("bot.__main__.asyncio.timeout", side_effect=lambda seconds: timeout(0.01)):
            with self.assertRaises(TimeoutError):
                async with self.bot.work_slot(3) as admitted:
                    self.assertTrue(admitted)
                    await asyncio.Event().wait()
        async with self.bot.work_slot(3) as admitted:
            self.assertTrue(admitted)

    async def test_scan_paces_budget_to_reach_later_members(self):
        self.config.max_api_per_minute = 2
        self.guild.chunked = True
        self.guild.members = [SimpleNamespace(**{**vars(self.member), "id": user_id, "send": AsyncMock()}) for user_id in range(10, 13)]

        async def elapsed(seconds):
            self.bot.api_starts = type(self.bot.api_starts)(start - 61 for start in self.bot.api_starts)

        with patch("bot.__main__.asyncio.sleep", side_effect=elapsed) as sleep:
            await self.bot.scan()
        sleep.assert_awaited_once()
        for member in self.guild.members:
            member.send.assert_awaited_once()
