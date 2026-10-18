from __future__ import annotations

import asyncio
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from feishu import FeishuTransport, dispatch_action, dispatch_message


class FakeStore:
    def __init__(self, bound: str = "") -> None:
        self.bound = bound
        self.values: dict[str, str] = {}
        self.claimed: set[str] = set()

    def recipient(self) -> str:
        return self.bound

    def bind_recipient(self, open_id: str) -> bool:
        if self.bound and self.bound != open_id:
            return False
        self.bound = open_id
        return True

    def claim_event(self, key: str) -> bool:
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def set_json(self, key: str, value: str) -> None:
        self.values[key] = value

    def get_json(self, key: str, default: str) -> str:
        return self.values.get(key, default)


class FakeApp:
    def __init__(self, bound: str = "") -> None:
        self.store = FakeStore(bound)
        self.calls: list[tuple[str, str, int | None]] = []

    async def handle(self, text: str, recipient: str, article_id: int | None = None) -> None:
        self.calls.append((text, recipient, article_id))
        if text == "/start":
            self.store.bind_recipient(recipient)


def schedule(coro) -> None:
    asyncio.run(coro)


def message(
    text: str = "/next", *, open_id: str = "ou_owner", chat_type: str = "p2p",
    chat_id: str = "oc_private", message_id: str = "msg-1", event_id: str = "evt-1",
    message_type: str = "text", content: str | None = None,
):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id)),
            message=SimpleNamespace(
                message_id=message_id, chat_type=chat_type, chat_id=chat_id,
                message_type=message_type,
                content=content if content is not None else json.dumps({"text": text}),
            ),
        ),
    )


def action(
    command: str = "/next", *, open_id: str = "ou_owner", chat_id: str = "oc_private",
    event_id: str = "action-1", token: str = "token-1", value: dict | None = None,
):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=open_id),
            context=SimpleNamespace(open_chat_id=chat_id),
            action=SimpleNamespace(value={"callback_data": command} if value is None else value),
            token=token,
        ),
    )


class DispatchTests(unittest.TestCase):
    def test_first_start_binds_authorized_sender_but_not_other_commands(self):
        app = FakeApp()
        self.assertFalse(dispatch_message(app, message(), "ou_owner", schedule))
        self.assertFalse(dispatch_message(app, message("/start", open_id="ou_other"), "ou_owner", schedule))
        self.assertFalse(dispatch_message(app, message("/start", chat_type="group"), "ou_owner", schedule))
        self.assertEqual(app.store.claimed, set())
        self.assertTrue(dispatch_message(app, message("/start"), "ou_owner", schedule))
        self.assertEqual(app.store.recipient(), "ou_owner")
        self.assertEqual(app.calls, [("/start", "ou_owner", None)])
        self.assertEqual(app.store.values["feishu:p2p_chat:ou_owner"], "oc_private")
        self.assertTrue(dispatch_message(app, message(message_id="msg-2"), "ou_owner", schedule))
        self.assertEqual(app.calls[-1], ("/next", "ou_owner", None))

    def test_message_rejects_unauthorized_group_and_invalid_payload_before_claim(self):
        app = FakeApp("ou_owner")
        cases = [
            message(open_id="ou_other"), message(chat_type="group"), message(chat_type=""),
            message(message_type="image"), message(content="not json"),
            message(content=json.dumps(["/next"])), message(content=json.dumps({"text": 12})),
            message(content=json.dumps({"text": "  "})), message(chat_id=""),
            message(message_id="", event_id=""),
        ]
        for event in cases:
            with self.subTest(event=event):
                self.assertFalse(dispatch_message(app, event, "ou_owner", schedule))
        self.assertEqual(app.calls, [])
        self.assertEqual(app.store.claimed, set())
        self.assertEqual(app.store.values, {})

    def test_message_prefers_stable_message_id_and_falls_back_to_event_id(self):
        app = FakeApp("ou_owner")
        event = message(text="  /today ", message_id="msg-1", event_id="evt-1")
        self.assertTrue(dispatch_message(app, event, "ou_owner", schedule))
        self.assertFalse(dispatch_message(app, message(message_id="msg-1", event_id="new-event"), "ou_owner", schedule))
        self.assertTrue(dispatch_message(app, message(message_id="", event_id="evt-1"), "ou_owner", schedule))
        self.assertFalse(dispatch_message(app, message(message_id="", event_id="evt-1"), "ou_owner", schedule))
        self.assertEqual(app.calls, [("/today", "ou_owner", None), ("/next", "ou_owner", None)])
        self.assertEqual(app.store.claimed, {"feishu:message:msg-1", "feishu:message:evt-1"})

    def test_message_mapping_event_is_supported(self):
        app = FakeApp("ou_owner")
        event = {
            "header": {"event_id": "evt-3"},
            "event": {"sender": {"sender_id": {"open_id": "ou_owner"}}, "message": {
                "chat_type": "p2p", "chat_id": "oc_private", "message_type": "text",
                "content": '{"text":"/help"}',
            }},
        }
        self.assertTrue(dispatch_message(app, event, "ou_owner", schedule))
        self.assertEqual(app.calls, [("/help", "ou_owner", None)])

    def test_action_requires_authorized_bound_user_and_observed_p2p_chat(self):
        app = FakeApp()
        self.assertEqual(dispatch_action(app, action(), "ou_owner", schedule)["toast"]["type"], "error")
        app.store.bind_recipient("ou_owner")
        self.assertEqual(dispatch_action(app, action(), "ou_owner", schedule)["toast"]["type"], "error")
        self.assertTrue(dispatch_message(app, message(), "ou_owner", schedule))
        calls = list(app.calls)
        for event in (action(open_id="ou_other"), action(chat_id="oc_group"), action(chat_id="")):
            self.assertEqual(dispatch_action(app, event, "ou_owner", schedule)["toast"]["type"], "error")
        group_event = action()
        group_event.event.context.chat_type = "group"
        self.assertEqual(dispatch_action(app, group_event, "ou_owner", schedule)["toast"]["type"], "error")
        self.assertEqual(app.calls, calls)
        self.assertTrue(dispatch_action(app, action(), "ou_owner", schedule)["toast"]["type"] == "info")
        self.assertEqual(app.calls[-1], ("/next", "ou_owner", None))
        self.assertEqual(app.store.claimed, {"feishu:message:msg-1", "feishu:action:action-1"})

    def test_action_attaches_article_id_and_deduplicates(self):
        app = FakeApp("ou_owner")
        app.store.set_json("feishu:p2p_chat:ou_owner", "oc_private")
        event = action("/feedback ok", value={"callback_data": "/feedback ok", "article_id": 42})
        self.assertEqual(dispatch_action(app, event, "ou_owner", schedule)["toast"]["content"], "正在处理")
        self.assertEqual(app.calls, [("/feedback ok", "ou_owner", 42)])
        self.assertEqual(dispatch_action(app, event, "ou_owner", schedule)["toast"]["content"], "已处理")
        self.assertEqual(len(app.calls), 1)
        next_event = action(event_id="action-2", value={"callback_data": "/next", "article_id": "12"})
        dispatch_action(app, next_event, "ou_owner", schedule)
        self.assertEqual(app.calls[-1], ("/next", "ou_owner", None))

    def test_action_rejects_unknown_command_bad_article_and_missing_identifier(self):
        app = FakeApp("ou_owner")
        app.store.set_json("feishu:p2p_chat:ou_owner", "oc_private")
        for value in ({"callback_data": "rm -rf /"}, {}, [],
                      {"callback_data": "/feedback ok"},
                      {"callback_data": "/feedback ok", "article_id": "not-an-id"},
                      {"callback_data": "/feedback hard", "article_id": False}):
            with self.subTest(value=value):
                self.assertEqual(dispatch_action(app, action(value=value), "ou_owner", schedule)["toast"]["type"], "error")
        self.assertEqual(dispatch_action(app, action(event_id="", token=""), "ou_owner", schedule)["toast"]["type"], "error")
        self.assertEqual(app.calls, [])
        self.assertEqual(app.store.claimed, set())

    def test_action_uses_callback_token_if_no_event_id(self):
        app = FakeApp("ou_owner")
        app.store.set_json("feishu:p2p_chat:ou_owner", "oc_private")
        event = action(event_id="", token="stable-callback-token")
        dispatch_action(app, event, "ou_owner", schedule)
        dispatch_action(app, event, "ou_owner", schedule)
        self.assertEqual(app.calls, [("/next", "ou_owner", None)])
        self.assertEqual(app.store.claimed, {"feishu:action:stable-callback-token"})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_requires_explicit_recipient_and_sends_every_chunk_to_it(self):
        transport = FeishuTransport(Mock(), "ou_owner")
        with self.assertRaises(ValueError):
            await transport.send_text("hello", "")
        with self.assertRaises(ValueError):
            await transport.send_text("hello", "ou_other")
        with patch.object(transport, "_create") as create:
            await transport.send_text("x" * 12001, "ou_owner")
        self.assertEqual(create.call_count, 2)
        for args, _ in (call for call in create.call_args_list):
            self.assertEqual(args[0:2], ("ou_owner", "text"))
        self.assertEqual(json.loads(create.call_args_list[0].args[2]), {"text": "x" * 12000})
        self.assertEqual(json.loads(create.call_args_list[1].args[2]), {"text": "x"})
        self.assertNotIn("chat_id", vars(transport))

    async def test_card_buttons_carry_article_id_and_require_recipient(self):
        transport = FeishuTransport(Mock(), "ou_owner")
        with self.assertRaises(ValueError):
            await transport.send_card("title", "body", "")
        with self.assertRaises(ValueError):
            await transport.send_card("title", "body", "ou_other", article_id=42)
        with patch.object(transport, "_create") as create:
            await transport.send_card("title", "body", "ou_owner", article_id=42)
        recipient, msg_type, raw = create.call_args.args
        self.assertEqual((recipient, msg_type), ("ou_owner", "interactive"))
        values = [button["value"] for button in json.loads(raw)["elements"][1]["actions"]]
        self.assertEqual(values, [
            {"callback_data": "/next", "article_id": "42"},
            {"callback_data": "/feedback ok", "article_id": "42"},
            {"callback_data": "/feedback hard", "article_id": "42"},
        ])

    async def test_sdk_builder_uses_open_id_not_chat_id(self):
        class Builder:
            def __init__(self):
                self.fields: dict[str, str | Builder] = {}

            def receive_id_type(self, value):
                self.fields["receive_id_type"] = value
                return self

            def request_body(self, value):
                self.fields["request_body"] = value
                return self

            def receive_id(self, value):
                self.fields["receive_id"] = value
                return self

            def msg_type(self, value):
                self.fields["msg_type"] = value
                return self

            def content(self, value):
                self.fields["content"] = value
                return self

            def build(self):
                return self.fields

        sdk = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
            CreateMessageRequest=SimpleNamespace(builder=Builder),
            CreateMessageRequestBody=SimpleNamespace(builder=Builder),
        )))
        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(success=lambda: True)),
        ))))
        with patch.dict(sys.modules, {"lark_oapi": sdk}):
            await FeishuTransport(client, "ou_owner").send_text("hello", "ou_owner")
        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request["receive_id_type"], "open_id")
        self.assertEqual(request["request_body"]["receive_id"], "ou_owner")

    async def test_sdk_error_never_exposes_upstream_message(self):
        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(success=lambda: False, code=403,
                                                     msg="upstream secret payload")),
        ))))
        class Builder:
            def __getattr__(self, name):
                return lambda *args: self

            def build(self):
                return self

        sdk = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
            CreateMessageRequest=SimpleNamespace(builder=Builder),
            CreateMessageRequestBody=SimpleNamespace(builder=Builder),
        )))
        with patch.dict(sys.modules, {"lark_oapi": sdk}):
            with self.assertRaisesRegex(RuntimeError, "code=403") as failure:
                await FeishuTransport(client, "ou_owner").send_text("hello", "ou_owner")
        self.assertNotIn("upstream secret payload", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
