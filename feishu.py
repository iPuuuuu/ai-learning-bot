from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable

log = logging.getLogger("feishu")


def _field(value: Any, *path: str) -> Any:
    """Read either an SDK event object or an event-shaped mapping."""
    for name in path:
        value = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if value is None:
            break
    return value


def _event_key(data: Any, *paths: tuple[str, ...]) -> str:
    for path in paths:
        candidate = _field(data, *path)
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _p2p_chat_key(open_id: str) -> str:
    # Used only to validate card callbacks; never used as a sending destination.
    return f"feishu:p2p_chat:{open_id}"


class FeishuTransport:
    def __init__(self, client: Any, allowed_open_id: str):
        self.client = client
        self.allowed_open_id = allowed_open_id

    def _require_recipient(self, recipient: str) -> None:
        if not recipient or recipient != self.allowed_open_id:
            raise ValueError("飞书发送目标必须是显式指定的授权 open_id")

    async def send_text(self, text: str, recipient: str) -> None:
        self._require_recipient(recipient)
        for start in range(0, len(text), 12000):
            await asyncio.to_thread(
                self._create, recipient, "text",
                json.dumps({"text": text[start:start + 12000]}, ensure_ascii=False),
            )

    async def send_card(
        self, title: str, markdown: str, recipient: str, article_id: int | str | None = None,
    ) -> None:
        self._require_recipient(recipient)

        def button(label: str, command: str, primary: bool = False) -> dict:
            value = {"callback_data": command}
            if article_id is not None:
                value["article_id"] = str(article_id)
            action = {"tag": "button", "text": {"tag": "plain_text", "content": label}, "value": value}
            if primary:
                action["type"] = "primary"
            return action

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": title[:100]}},
            "elements": [
                {"tag": "markdown", "content": markdown[:18000]},
                {"tag": "action", "actions": [
                    button("换一篇", "/next", primary=True),
                    button("适合我", "/feedback ok"),
                    button("太难了", "/feedback hard"),
                ]},
            ],
        }
        await asyncio.to_thread(self._create, recipient, "interactive", json.dumps(card, ensure_ascii=False))

    def _create(self, recipient: str, msg_type: str, content: str) -> None:
        import lark_oapi as lark

        request = (lark.im.v1.CreateMessageRequest.builder().receive_id_type("open_id").request_body(
            lark.im.v1.CreateMessageRequestBody.builder().receive_id(recipient).msg_type(msg_type).content(content).build()
        ).build())
        response = self.client.im.v1.message.create(request)
        if not response.success():
            code = response.code
            safe_code = code if type(code) is int else "unknown"
            raise RuntimeError(f"飞书发送失败：code={safe_code}")


def _allowed(app: Any, open_id: Any, allowed_open_id: str, *, first_start: bool = False) -> bool:
    if not isinstance(open_id, str) or not allowed_open_id or open_id != allowed_open_id:
        return False
    bound = app.store.recipient()
    return bound == open_id or (first_start and not bound)


def dispatch_message(
    app: Any, data: Any, allowed_open_id: str, schedule: Callable[[Awaitable[Any]], None],
) -> bool:
    """Accept only authorized p2p text; dispatch with the sender's explicit open_id."""
    event = _field(data, "event")
    message = _field(event, "message")
    open_id = _field(event, "sender", "sender_id", "open_id")
    if _field(message, "chat_type") != "p2p" or _field(message, "message_type") != "text":
        return False
    try:
        content = json.loads(_field(message, "content"))
    except (TypeError, ValueError):
        return False
    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        return False
    text = content["text"].strip()
    chat_id = _field(message, "chat_id")
    if not text or not isinstance(chat_id, str) or not chat_id:
        return False
    if not _allowed(app, open_id, allowed_open_id, first_start=text.lower() == "/start"):
        return False
    key = _event_key(data, ("event", "message", "message_id"), ("header", "event_id"),
                     ("event", "event_id"), ("event_id",))
    if not key or not app.store.claim_event(f"feishu:message:{key}"):
        return False
    app.store.set_json(_p2p_chat_key(open_id), chat_id)
    schedule(app.handle(text, open_id))
    return True


def dispatch_action(
    app: Any, data: Any, allowed_open_id: str, schedule: Callable[[Awaitable[Any]], None],
) -> dict:
    """Only a bound user's card in their known p2p chat can trigger a command."""
    event = _field(data, "event")
    open_id = _field(event, "operator", "open_id")
    if not _allowed(app, open_id, allowed_open_id):
        return {"toast": {"type": "error", "content": "无权限"}}
    # The SDK callback has no chat_type field. Match its chat to a p2p chat
    # observed in a previously accepted message, not to any recent chat.
    chat_id = _field(event, "context", "open_chat_id")
    known_chat = app.store.get_json(_p2p_chat_key(open_id), "")
    if not isinstance(chat_id, str) or not chat_id or chat_id != known_chat:
        return {"toast": {"type": "error", "content": "仅支持私聊卡片"}}
    for owner in (event, _field(event, "context")):
        chat_type = _field(owner, "chat_type")
        if chat_type is not None and chat_type != "p2p":
            return {"toast": {"type": "error", "content": "仅支持私聊卡片"}}
    value = _field(event, "action", "value")
    if not isinstance(value, dict):
        return {"toast": {"type": "error", "content": "无效操作"}}
    command = value.get("callback_data")
    if command not in ("/next", "/feedback ok", "/feedback hard", "/feedback easy"):
        return {"toast": {"type": "error", "content": "无效操作"}}
    article_id = value.get("article_id")
    if article_id is not None:
        if (not isinstance(article_id, (str, int)) or isinstance(article_id, bool)
                or not str(article_id).isdecimal() or len(str(article_id)) > 19
                or not 0 < int(article_id) <= 9223372036854775807):
            return {"toast": {"type": "error", "content": "无效文章"}}
    if command.startswith("/feedback ") and article_id is None:
        return {"toast": {"type": "error", "content": "无法验证文章"}}
    key = _event_key(data, ("header", "event_id"), ("event", "event_id"),
                     ("event", "action", "event_id"), ("event_id",), ("uuid",),
                     ("event", "token"))
    if not key:
        return {"toast": {"type": "error", "content": "缺少事件 ID"}}
    if not app.store.claim_event(f"feishu:action:{key}"):
        return {"toast": {"type": "info", "content": "已处理"}}
    if command.startswith("/feedback "):
        schedule(app.handle(command, open_id, article_id=int(article_id)))
    else:
        schedule(app.handle(command, open_id))
    return {"toast": {"type": "info", "content": "正在处理"}}


def run(app: Any, app_id: str, app_secret: str, allowed_open_id: str) -> None:
    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise SystemExit("缺少依赖，请先运行：python -m pip install -r requirements.txt") from exc
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    app.transport = FeishuTransport(client, allowed_open_id)

    def schedule(coro: Awaitable[Any]) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def report_error(result: Any) -> None:
            try:
                result.result()
            except Exception as exc:
                log.error("事件处理失败: %s", type(exc).__name__)

        future.add_done_callback(report_error)

    def on_message(data: Any) -> None:
        if not dispatch_message(app, data, allowed_open_id, schedule):
            log.info("忽略未授权、非私聊、重复或无效消息")

    def on_action(data: Any) -> dict:
        return dispatch_action(app, data, allowed_open_id, schedule)

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_action).build())

    def ws_worker() -> None:
        lark.ws.Client(app_id, app_secret, event_handler=handler).start()

    async def serve() -> None:
        asyncio.create_task(app.daily_loop())
        while True:
            worker = threading.Thread(target=ws_worker, daemon=True, name="feishu-ws")
            worker.start()
            while worker.is_alive():
                await asyncio.sleep(1)
            log.error("飞书长连接已退出，5 秒后重连")
            await asyncio.sleep(5)

    loop.run_until_complete(serve())
