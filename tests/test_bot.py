import asyncio
import gc
import json
import sqlite3
import tempfile
import unittest
import discord
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from bot.config import Config, identifier
from bot.cache import MembershipCache, CacheUnavailable, REFRESH_SECONDS
from bot.services import D1, Parser, ServiceError, opted_out
from bot.state import State
from bot.__main__ import VerificationBot, error_summary


class ServicesTest(unittest.IsolatedAsyncioTestCase):
    def cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = MembershipCache(Path(tmp.name) / "members.db")
        self.addCleanup(cache.close)
        return cache

    def d1_config(self, **overrides):
        values = dict(table="People", first_name_column="first_name", last_name_column="last_name",
                      uw_email_column="uw_email", cs_email_column="cs_email", status_column="",
                      status_value="active", account="account", database="db", cf_token="secret")
        return SimpleNamespace(**{**values, **overrides})

    async def test_d1_downloads_only_names_emails_and_binds_status(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [
                {"full_name": "Robert'; DROP TABLE members;--", "uw_email": "r@example.com", "cs_email": None}]}]})
        config = self.d1_config(status_column="status", status_value="active' OR 1=1 --")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = self.cache()
            d1 = D1(config, client, cache)
            await d1.refresh()
            self.assertTrue(await d1.matches("Robert'; DROP TABLE members;--", "r@example.com"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["params"], [config.status_value])
        self.assertNotIn("Robert", seen[0]["sql"])
        self.assertNotIn(config.status_value, seen[0]["sql"])
        self.assertNotIn("status", seen[0]["sql"].split(" FROM ")[0])
        self.assertEqual([row[1] for row in cache.db.execute("PRAGMA table_info(members)")],
                         ["full_name", "uw_email", "cs_email"])

    async def test_duplicates_and_api_failure_fail_closed(self):
        config = self.d1_config()
        for count in (0, 2):
            record = {"full_name": "Jane Doe", "uw_email": "jane@example.com", "cs_email": None}
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True, "result": [{"success": True, "results": [record] * count}]}))) as client:
                d1 = D1(config, client, self.cache())
                await d1.refresh()
                self.assertFalse(await d1.matches("Jane Doe"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False}))) as client:
            with self.assertRaises(ServiceError):
                await D1(config, client, self.cache()).refresh()

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
                {"success": True, "results": [dict(zip(("full_name", "uw_email", "cs_email"), row)) for row in rows]}]})

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
            d1 = D1(self.d1_config(status_column="status"), client, self.cache())
            await d1.refresh()
            for name, email, expected in cases:
                with self.subTest(name=name, email=email):
                    self.assertEqual(await d1.matches(name, email), expected)
            unfiltered = D1(self.d1_config(), client, self.cache())
            await unfiltered.refresh()
            self.assertTrue(await unfiltered.matches("Inactive Member"))

    async def test_refresh_six_hour_boundary_concurrency_and_restart(self):
        records = [{"full_name": "Jane Doe", "uw_email": "jane@example.com", "cs_email": None}]
        calls = []

        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": records}]})

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "members.db"
            cache = MembershipCache(path)
            try:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch("bot.services.time.time", return_value=1000) as now:
                        d1 = D1(self.d1_config(), client, cache)
                        with self.assertRaises(CacheUnavailable):
                            await d1.matches("Jane Doe")
                        await asyncio.gather(*(d1.refresh() for _ in range(10)))
                        self.assertEqual(len(calls), 1)
                        self.assertTrue(await d1.matches("Jane Doe"))
                        cache.close()
                        cache = MembershipCache(path)
                        d1 = D1(self.d1_config(), client, cache)
                        now.return_value = 1000 + REFRESH_SECONDS - 1
                        self.assertFalse(await d1.refresh())
                        self.assertTrue(await d1.matches("Jane Doe"))
                        self.assertFalse(await d1.matches("New Member"))
                        self.assertEqual(len(calls), 1)
                        now.return_value += 1
                        with self.assertRaises(CacheUnavailable):
                            await d1.matches("Jane Doe")
                        records[:] = [{"full_name": "New Member", "uw_email": None, "cs_email": "new@example.com"}]
                        self.assertTrue(await d1.refresh())
                        self.assertEqual(len(calls), 2)
                        self.assertFalse(await d1.matches("Jane Doe"))
                        self.assertTrue(await d1.matches("New Member", "new@example.com"))
            finally:
                cache.close()

    async def test_failed_refresh_keeps_snapshot_but_denies_stale_matches(self):
        cache = self.cache()
        record = {"full_name": "Jane Doe", "uw_email": "jane@example.com", "cs_email": None}
        response = {"success": True, "result": [{"success": True, "results": [record]}]}
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("bot.services.time.time", return_value=1000) as now:
                d1 = D1(self.d1_config(), client, cache)
                await d1.refresh()
                now.return_value += REFRESH_SECONDS
                response["result"][0]["results"] = [record, {"full_name": "Incomplete"}]
                with self.assertRaises(ServiceError):
                    await d1.refresh()
                self.assertEqual(cache.db.execute("SELECT full_name FROM members").fetchall(), [("Jane Doe",)])
                self.assertFalse(await d1.refresh())
                with self.assertRaises(CacheUnavailable):
                    await d1.matches("Jane Doe")
                self.assertEqual(len(calls), 2)
                now.return_value += REFRESH_SECONDS
                response["result"][0]["results"] = []
                self.assertTrue(await d1.refresh())
                self.assertFalse(await d1.matches("Jane Doe"))
                self.assertEqual(cache.db.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)

    async def test_first_refresh_network_failure_is_not_retried_on_restart(self):
        cache = self.cache()
        path = Path(cache.db.execute("PRAGMA database_list").fetchone()[2])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            with patch("bot.services.time.time", return_value=1000):
                with self.assertRaises(httpx.HTTPStatusError):
                    await D1(self.d1_config(), client, cache).refresh()
                cache.close()
                restarted_cache = MembershipCache(path)
                try:
                    self.assertFalse(await D1(self.d1_config(), client, restarted_cache).refresh())
                    with self.assertRaises(CacheUnavailable):
                        await D1(self.d1_config(), client, restarted_cache).matches("Jane Doe")
                finally:
                    restarted_cache.close()

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

    async def test_parser_rejects_partial_identity_tokens(self):
        output = {"full_name": "Jane Doe", "email": "jane@example.com"}
        def handler(request):
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps(output)}]}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            parser = Parser("ai-key", "test-model", client)
            for reply in ("Jane Doe notjane@example.com", "Jane Doe jane@example.com.au",
                          "Jane Doe other+jane@example.com", "NotJane Doe jane@example.com"):
                with self.subTest(reply=reply):
                    self.assertIsNone(await parser.parse(reply))
            for reply in ("I'm Jane Doe, email: jane@example.com.",
                          "Jane Doe <jane@example.com>", "JANE DOE JANE@EXAMPLE.COM"):
                with self.subTest(reply=reply):
                    self.assertEqual(await parser.parse(reply), ("Jane Doe", "jane@example.com"))

    async def test_parser_malformed_responses_are_service_errors(self):
        bodies = [{"candidates": [None]}, {"candidates": ["invalid"]},
                  {"candidates": [{"finishReason": "STOP", "content": {"parts": [None]}}]}]
        for body in bodies:
            with self.subTest(body=body):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=body))) as client:
                    with self.assertRaises(ServiceError):
                        await Parser("ai-key", "test-model", client).parse("Jane Doe jane@example.com")

    async def test_parser_uses_generate_content_response_format_enum(self):
        def handler(request):
            config = json.loads(request.content)["generationConfig"]
            # The REST API expects an enum here, not the legacy MIME string.
            if config["responseFormat"]["text"]["mimeType"] != "APPLICATION_JSON":
                return httpx.Response(400)
            self.assertNotIn("temperature", config)
            self.assertNotIn("topP", config)
            self.assertNotIn("topK", config)
            self.assertEqual(request.url.path, "/v1beta/models/gemini-3.8-flash:generateContent")
            return httpx.Response(200, json={"candidates": [{
                "finishReason": "STOP", "content": {"parts": [{"text": json.dumps({
                    "full_name": "Jane Doe", "email": "jane@example.com",
                })}]},
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            parser = Parser("ai-key", "gemini-3.8-flash", client)
            self.assertEqual(await parser.parse("Jane Doe jane@example.com"),
                             ("Jane Doe", "jane@example.com"))


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
        self.assertEqual(config.cache_file, Path("data/members.sqlite3"))
        self.assertEqual(self.load(MEMBERSHIP_CACHE_DB="data/custom.db").cache_file, Path("data/custom.db"))
        self.assertEqual(config.scan_seconds, 900)
        self.assertEqual(config.max_concurrent_api, 2)
        self.assertEqual(config.max_api_per_minute, 60)
        config = self.load(SCAN_INTERVAL_SECONDS="1800", MAX_CONCURRENT_API="1")
        self.assertEqual(config.scan_seconds, 1800)
        self.assertEqual(config.max_concurrent_api, 1)

    def test_reminders_cannot_be_configured_more_than_daily(self):
        self.assertEqual(self.load().reminder_seconds, 86400)
        self.assertEqual(self.load(REMINDER_INTERVAL_HOURS="48").reminder_seconds, 172800)
        for value in ("0", "-1", "1", "23.99", "nan", "inf"):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                self.load(REMINDER_INTERVAL_HOURS=value)

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

    def test_reminder_conversion_cannot_overflow(self):
        with self.assertRaises(ValueError):
            self.load(REMINDER_INTERVAL_HOURS="1e308")

    def test_discord_ids_must_be_positive_snowflakes(self):
        for key in ("DISCORD_GUILD_ID", "DISCORD_MEMBER_ROLE_ID"):
            for value in ("0", "-1", str(2 ** 64)):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.load(**{key: value})


class CacheLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_refresh_restart_reuse_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(ConfigTest().load(), state_file=Path(tmp) / "state.db",
                             cache_file=Path(tmp) / "members.db")
            client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(
                200, request=httpx.Request("POST", "https://api.cloudflare.com"),
                json={"success": True, "result": [{"success": True, "results": [
                    {"full_name": "Jane Doe", "uw_email": "jane@example.com", "cs_email": None}]}]})),
                aclose=AsyncMock())
            with patch("bot.__main__.httpx.AsyncClient", return_value=client):
                for _ in range(2):
                    bot = VerificationBot(config)
                    try:
                        await bot.setup_hook()
                        self.assertTrue(bot.refresh_membership.is_running())
                        self.assertTrue(await bot.d1.matches("Jane Doe"))
                        client.post.assert_awaited_once()
                    finally:
                        await bot.close()
                    self.assertTrue(bot.refresh_membership.get_task().done())
                    with self.assertRaises(sqlite3.ProgrammingError):
                        bot.membership_cache.db.execute("SELECT 1")


class StateTest(unittest.TestCase):
    def test_reminder_reservation_is_shared_and_enforces_daily_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            first, second = State(path), State(path)
            try:
                self.assertTrue(first.reserve_reminder(123, 1000, 60))
                self.assertFalse(second.reserve_reminder(123, 1000, 60))
                self.assertFalse(second.reserve_reminder(123, 87399, 60))
                self.assertTrue(second.reserve_reminder(123, 87400, 60))
                self.assertTrue(first.reserve_reminder(456, 1000, 86400))
                first.update(789, opted_out=1)
                self.assertFalse(second.reserve_reminder(789, 1000, 86400))
            finally:
                first.close()
                second.close()

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
        self.role = SimpleNamespace(id=2)
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
        self.bot.state.update(3, opted_out=1, next_reminder=0, next_check=0)
        self.bot.state.close()
        self.bot.state = State(self.config.state_file)
        await self.bot.check_member(self.member)
        self.member.send.assert_awaited_once()

    async def test_reminder_boundary_survives_restart_and_name_changes(self):
        with patch("bot.__main__.time.time", return_value=1000) as now:
            await self.bot.check_member(self.member)
            self.bot.state.close()
            self.bot.state = State(self.config.state_file)
            self.member.nick = "Changed Name"
            now.return_value = 1000 + 86399
            await self.bot.check_member(self.member)
            self.member.send.assert_awaited_once()
            # Clear only the API cooldown so it cannot mask the reminder boundary.
            self.bot.state.update(3, next_check=0)
            now.return_value = 1000 + 86400
            await self.bot.check_member(self.member)
            self.assertEqual(self.member.send.await_count, 2)

    async def test_failed_reminder_is_not_retried_before_next_day(self):
        self.member.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50007, "message": "Cannot send messages to this user"},
        )
        with patch("bot.__main__.time.time", return_value=1000) as now:
            await self.bot.check_member(self.member)
            self.bot.state.close()
            self.bot.state = State(self.config.state_file)
            now.return_value = 87400 - 61
            await self.bot.check_member(self.member)
            self.member.send.assert_awaited_once()
            now.return_value = 87400
            await self.bot.check_member(self.member)
            self.assertEqual(self.member.send.await_count, 2)

    async def test_stop_during_name_lookup_prevents_reminder(self):
        async def matches(name):
            self.bot.state.update(3, opted_out=1)
            return False

        self.bot.d1.matches.side_effect = matches
        await self.bot.check_member(self.member)
        self.member.send.assert_not_awaited()

    async def test_daily_reminder_does_not_block_requested_verification_reply(self):
        await self.bot.check_member(self.member)
        self.bot.d1.matches.return_value = True
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Jane Doe jane@example.com",
                              channel=SimpleNamespace(send=AsyncMock()))
        await self.bot.handle_dm(msg)
        self.member.add_roles.assert_awaited_once()
        msg.channel.send.assert_awaited_once_with("You're verified! The members role has been added.")

    async def test_dm_http_error_logs_service_and_status_without_secrets(self):
        request = httpx.Request("POST", "https://generativelanguage.googleapis.com/private?key=secret",
                                content="private member details")
        response = httpx.Response(429, request=request, text="sensitive response")
        error = httpx.HTTPStatusError("sensitive exception", request=request, response=response)
        self.bot.handle_dm = AsyncMock(side_effect=error)
        channel = MagicMock(spec=discord.DMChannel)
        channel.send = AsyncMock()
        msg = SimpleNamespace(author=SimpleNamespace(id=3, bot=False), channel=channel)
        with self.assertLogs("verification", level="WARNING") as logs:
            await self.bot.on_message(msg)
        self.assertEqual(logs.output, ["WARNING:verification:DM handling failed (Gemini HTTP 429)"])
        channel.send.assert_awaited_once()
        request = httpx.Request("POST", "https://api.cloudflare.com/private")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("secret", request=request, response=response)
        self.assertEqual(error_summary(error), "Cloudflare D1 HTTP 403")

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

    async def test_verified_member_dm_preserves_budget_for_unverified_member(self):
        self.config.max_api_per_minute = 1
        self.member.roles = [self.role]
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Hello",
                              channel=SimpleNamespace(send=AsyncMock()))
        await self.bot.handle_dm(msg)
        self.member.roles = []
        self.bot.state.update(3, next_reply=0)
        msg.content = "Jane Doe jane@example.com"
        await self.bot.handle_dm(msg)
        self.bot.parser.parse.assert_awaited_once()

    async def test_name_match_removed_during_discord_fetch_cannot_grant(self):
        self.bot.d1.matches.return_value = True

        async def fetch(user_id):
            self.bot.d1.matches.return_value = False
            return self.member

        self.guild.fetch_member.side_effect = fetch
        await self.bot.check_member(self.member)
        self.member.add_roles.assert_not_awaited()

    async def test_dm_match_expired_during_final_discord_fetch_cannot_grant(self):
        self.bot.d1.matches.return_value = True
        fetches = 0

        async def fetch(user_id):
            nonlocal fetches
            fetches += 1
            if fetches == 2:
                self.bot.d1.matches.side_effect = CacheUnavailable("Expired")
            return self.member

        self.guild.fetch_member.side_effect = fetch
        msg = SimpleNamespace(author=SimpleNamespace(id=3), content="Jane Doe jane@example.com",
                              channel=SimpleNamespace(send=AsyncMock()))
        with self.assertRaises(CacheUnavailable):
            await self.bot.handle_dm(msg)
        self.member.add_roles.assert_not_awaited()

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
