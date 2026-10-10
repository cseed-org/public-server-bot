import asyncio
import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from bot.config import Config, identifier
from bot.services import D1, Parser, ServiceError, opted_out
from bot.state import State
from bot.__main__ import VerificationBot


class ServicesTest(unittest.IsolatedAsyncioTestCase):
    async def test_d1_bound_parameters_and_no_records(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [{"matched": 1}]}]})
        config = SimpleNamespace(table="members", name_column="full_name", email_column="email", status_column="status", status_value="active", account="account", database="db", cf_token="secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            d1 = D1(config, client)
            self.assertTrue(await d1.matches("Robert'; DROP TABLE members;--", "r@example.com"))
        self.assertEqual(seen[0]["params"], ["Robert'; DROP TABLE members;--", "r@example.com", "active"])
        self.assertNotIn("Robert", seen[0]["sql"])
        self.assertIn("SELECT 1 AS matched", seen[0]["sql"])

    async def test_duplicates_and_api_failure_fail_closed(self):
        config = SimpleNamespace(table="members", name_column="full_name", email_column="email", status_column="", account="a", database="d", cf_token="s")
        for count in (0, 2):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [{}] * count}]}))) as client:
                self.assertFalse(await D1(config, client).matches("Jane Doe"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False}))) as client:
            with self.assertRaises(ServiceError):
                await D1(config, client).matches("Jane Doe")

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

    def test_resource_defaults_and_overrides(self):
        config = self.load()
        self.assertEqual(config.scan_seconds, 900)
        self.assertEqual(config.max_concurrent_api, 2)
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


class StateTest(unittest.TestCase):
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
        self.config = SimpleNamespace(state_file=Path(self.tmp.name) / "state.db", guild_id=1, role_id=2, reminder_seconds=86400, cooldown_seconds=60, max_concurrent_api=2, message=lambda kind="verification": "Verify. Reply STOP.")
        self.bot = VerificationBot(self.config)
        self.role = object()
        self.bot.role = lambda guild: self.role
        self.bot.d1 = SimpleNamespace(matches=AsyncMock(return_value=False))
        self.bot.parser = SimpleNamespace(parse=AsyncMock(return_value=("Jane Doe", "jane@example.com")))
        self.member = SimpleNamespace(id=3, guild=SimpleNamespace(id=1), bot=False, pending=False, roles=[], nick=None, global_name="Unknown", name="unknown", send=AsyncMock(), add_roles=AsyncMock())

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
        guild = SimpleNamespace(fetch_member=AsyncMock(return_value=self.member))
        self.bot.get_guild = lambda guild_id: guild
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
        for member in members:
            member.send.assert_awaited_once()
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
