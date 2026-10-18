import asyncio
import io
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bot as module
from ai_engine import AIEngine, AIError
from articles import Article
from config import settings
from storage import Store


class FakeTransport:
    def __init__(self):
        self.texts = []
        self.cards = []
        self.fail = False

    async def send_text(self, text, recipient):
        self.texts.append((text, recipient))

    async def send_card(self, title, markdown, recipient, article_id):
        self.cards.append((title, markdown, recipient, article_id))
        if self.fail:
            raise TimeoutError("upstream payload secret must not be displayed")


class FakeAI:
    def __init__(self, answer="原文明示：[1] 基于该段可以看出应用示例。"):
        self.calls = []
        self.answer = answer

    async def complete(self, system, prompt):
        self.calls.append((system, prompt))
        return self.answer


def fake_article(*args):
    content = "1. " + "公开内容解释了 AI 模型与学习流程。" * 38 + "\n2. " + "示例说明了如何合理使用模型。" * 15
    article = Article(
        title="介绍 AI 模型", url="https://news.mit.edu/2026/example", source="news.mit.edu",
        summary="简述 AI", content=content, topics=["大模型"], content_status="full",
        fetched_at="2026-10-18T01:00:00+00:00",
    )
    article._body_text = content
    return article


class BotOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = replace(
            settings, allowed_open_id="ou_learner", data_dir=Path(self.directory.name),
            daily_send_time="09:00", timezone="Asia/Shanghai", article_max_chars=18000,
            daily_catchup_minutes=30, rss_feeds="",
        )
        self.config_patch = patch.object(module, "settings", self.settings)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.bot = module.LearningBot()
        self.bot.transport = FakeTransport()
        self.bot.ai = FakeAI()

    async def test_binding_and_private_recipient_before_recommendation(self):
        with patch.object(module, "pick_article", fake_article):
            await self.bot.handle("/next", "ou_learner")
            self.assertFalse(self.bot.transport.cards)
            await self.bot.handle("/start", "ou_stranger")
            self.assertEqual(self.bot.store.recipient(), "")
            await self.bot.handle("/start", "ou_learner")
            self.assertEqual(self.bot.store.recipient(), "ou_learner")
            await self.bot.handle("/next", "ou_learner")
        self.assertEqual(len(self.bot.transport.cards), 1)
        self.assertEqual(self.bot.transport.cards[0][2], "ou_learner")
        self.assertIn("https://news.mit.edu/2026/example", self.bot.transport.cards[0][1])
        self.assertEqual(self.bot.store.current_article()["content_status"], "full")

    async def test_question_uses_only_cached_numbered_body_and_verifies_citation(self):
        self.bot.store.bind_recipient("ou_learner")
        with patch.object(module, "pick_article", fake_article):
            await self.bot.handle("/next", "ou_learner")
        await self.bot.handle("请解释文中的模型", "ou_learner")
        self.assertEqual(self.bot.store.profile()["question_count"], 1)
        self.assertIn("<UNTRUSTED_ARTICLE>", self.bot.ai.calls[-1][1])
        self.bot.ai.answer = "原文明示：[888] 文章未提过的事实。"
        await self.bot.handle("再问一次", "ou_learner")
        self.assertEqual(self.bot.store.profile()["question_count"], 1)
        self.assertIn("无法对应", self.bot.transport.texts[-1][0])

    async def test_old_card_feedback_does_not_change_profile(self):
        self.bot.store.bind_recipient("ou_learner")
        article_id = self.bot.store.save_article(fake_article().dict(), "合法导读 [1]")
        await self.bot.handle("/feedback hard", "ou_learner", article_id=article_id + 1)
        self.assertEqual(self.bot.store.profile()["feedback"]["hard"], 0)
        await self.bot.handle("/feedback hard", "ou_learner", article_id=article_id)
        self.assertEqual(self.bot.store.profile()["feedback"]["hard"], 1)

    async def test_daily_window_restart_and_unknown_send(self):
        self.bot.store.bind_recipient("ou_learner")
        tz = ZoneInfo("Asia/Shanghai")
        day = datetime(2026, 10, 18, 9, 0, tzinfo=tz)
        with patch.object(module, "pick_article", fake_article):
            await self.bot.daily_once(datetime(2026, 10, 18, 8, 59, tzinfo=tz))
            self.assertFalse(self.bot.transport.cards)
            await self.bot.daily_once(day)
            await self.bot.daily_once(datetime(2026, 10, 18, 9, 15, tzinfo=tz))
            self.assertEqual(len(self.bot.transport.cards), 1)
            self.assertEqual(self.bot.store.daily_status("ou_learner", "2026-10-18"), "sent")
            self.bot.transport.fail = True
            await self.bot.daily_once(datetime(2026, 10, 19, 9, 0, tzinfo=tz))
            self.assertEqual(self.bot.store.daily_status("ou_learner", "2026-10-19"), "unknown")
            self.bot.transport.fail = False
            await self.bot.daily_once(datetime(2026, 10, 19, 9, 5, tzinfo=tz))
            self.assertEqual(len(self.bot.transport.cards), 2)
        reopened = Store(Path(self.directory.name) / "learning.db")
        self.assertEqual(reopened.daily_status("ou_learner", "2026-10-19"), "unknown")

    async def test_manual_next_does_not_reserve_daily(self):
        self.bot.store.bind_recipient("ou_learner")
        with patch.object(module, "pick_article", fake_article):
            await self.bot.handle("/next", "ou_learner")
        self.assertFalse(self.bot.store.daily_status("ou_learner", "2026-10-18"))


class StorageTests(unittest.TestCase):
    def test_event_and_daily_claims_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.db"
            first, second = Store(path), Store(path)
            self.assertTrue(first.claim_event("msg123"))
            self.assertFalse(second.claim_event("msg123"))
            self.assertTrue(first.reserve_daily("ou_learner", "2026-10-18", 1))
            self.assertFalse(second.reserve_daily("ou_learner", "2026-10-18", 2))
            self.assertEqual(second.daily_status("ou_learner", "2026-10-18"), "sending")
            first.finish_daily("ou_learner", "2026-10-18", "unknown")
            self.assertEqual(second.daily_status("ou_learner", "2026-10-18"), "unknown")
            self.assertTrue(first.bind_recipient("ou_learner"))
            self.assertFalse(second.bind_recipient("ou_someone_else"))

    def test_ai_daily_budget_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.db"
            first, second = Store(path), Store(path)
            self.assertTrue(first.reserve_ai_call("2026-10-18", 2))
            self.assertTrue(second.reserve_ai_call("2026-10-18", 2))
            self.assertFalse(first.reserve_ai_call("2026-10-18", 2))
            self.assertTrue(first.reserve_ai_call("2026-10-19", 2))


class ApiOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_does_not_echo_secret_or_response(self):
        values = replace(settings, ai_provider="api", api_key="fake-key", api_model="fake-model")
        engine = AIEngine(values, Path("."))
        error = urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", 401, "sensitive detail", {},
            io.BytesIO(b"fake-key private prompt; secret from upstream"),
        )
        with patch("ai_engine.open_with_fallback", side_effect=error):
            with self.assertRaises(AIError) as caught:
                await engine.complete("system", "prompt")
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("fake-key", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    async def test_api_post_is_single_attempt_and_bounded_output(self):
        values = replace(settings, ai_provider="api", api_key="fake-key", api_model="fake-model")
        engine = AIEngine(values, Path("."))
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def read(self, count):
                self.count = count
                return b'{"choices":[{"message":{"content":"hello"}}]}'
        response = Response()
        with patch("ai_engine.open_with_fallback", return_value=response) as sender:
            self.assertEqual(await engine.complete("system", "prompt"), "hello")
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(sender.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(response.count, 1_000_001)


if __name__ == "__main__":
    unittest.main()
