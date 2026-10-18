from __future__ import annotations

import html
import http.client
import ipaddress
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from config import settings

DEFAULT_FEEDS = [
    "https://news.mit.edu/rss/topic/artificial-intelligence2",
    "https://huggingface.co/blog/feed.xml",
    "https://bair.berkeley.edu/blog/feed.xml",
]

TOPIC_WORDS = {
    "大模型": ["llm", "language model", "大模型", "gpt"],
    "机器学习": ["machine learning", "机器学习", "neural", "神经网络"],
    "Agent": ["agent", "智能体", "tool use"],
    "RAG": ["rag", "retrieval", "检索增强"],
    "提示词": ["prompt", "提示词"],
    "视觉": ["vision", "image", "视觉", "multimodal"],
    "AI 安全": ["safety", "alignment", "安全", "对齐"],
    "训练": ["training", "fine-tun", "训练", "微调"],
}

FEED_MAX_BYTES = 1_000_000
ARTICLE_MAX_BYTES = 2_000_000
FETCH_TIMEOUT = 12
MIN_ARTICLE_CHARS = 500
FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"})
ARTICLE_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "li", "blockquote"})
_SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "header", "aside", "svg", "form", "noscript", "iframe", "template", "button"})


@dataclass
class Article:
    title: str
    url: str
    source: str
    summary: str = ""
    published: str = ""
    content: str = ""
    difficulty: str = "beginner"
    topics: list[str] | None = None
    content_status: str = "unfetched"
    content_truncated: bool = False
    fetched_at: str = ""

    def dict(self) -> dict:
        result = asdict(self)
        result["topics"] = self.topics or []
        return result


class TextExtractor(HTMLParser):
    """Keep paragraph boundaries; prefer the main/article body over surrounding page chrome."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, int]] = []
        self.plain_parts: list[str] = []
        self._part: list[str] = []
        self._skipped: list[str] = []
        self._main_depth = 0
        self._article_depth = 0

    def _flush(self):
        text = re.sub(r"\s+", " ", html.unescape("".join(self._part))).strip()
        if text:
            self.blocks.append((text, 2 if self._article_depth else 1 if self._main_depth else 0))
        self._part = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skipped.append(tag)
        if self._skipped:
            return
        if tag in {"main", "article"}:
            self._flush()
            if tag == "main":
                self._main_depth += 1
            else:
                self._article_depth += 1
        elif tag in _BLOCK_TAGS:
            self._flush()
        elif tag == "br" and self._part:
            self._part.append(" ")

    def handle_endtag(self, tag):
        if self._skipped:
            if tag == self._skipped[-1]:
                self._skipped.pop()
            return
        if tag in _BLOCK_TAGS or tag in {"main", "article"}:
            self._flush()
        if tag == "main" and self._main_depth:
            self._main_depth -= 1
        elif tag == "article" and self._article_depth:
            self._article_depth -= 1

    def handle_data(self, data):
        if not self._skipped:
            self._part.append(data)
            self.plain_parts.append(data)

    def paragraphs(self) -> list[str]:
        self._flush()
        for scope in (2, 1, 0):
            selected = [text for text, level in self.blocks if level == scope]
            if selected:
                return selected
        return []


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect only to prevalidated IPs, preserving the original hostname for TLS."""

    def __init__(self, *args, addresses: tuple[str, ...], **kwargs):
        super().__init__(*args, **kwargs)
        self._addresses = addresses

    def connect(self):
        if self._tunnel_host:
            raise ValueError("不允许代理隧道")
        last_error = None
        for address in self._addresses:
            try:
                self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
                try:
                    self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
                except Exception:
                    self.sock.close()
                    raise
                return
            except OSError as exc:
                last_error = exc
        raise last_error or OSError("无法连接到来源")


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, hosts: frozenset[str]):
        super().__init__()
        self.hosts = hosts

    def https_open(self, request):
        host = _validate_url(request.full_url, self.hosts)
        addresses = _public_addresses(host)
        return self.do_open(_PinnedHTTPSConnection, request, context=self._context,
                            addresses=addresses)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _host(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\\]", url):
        raise ValueError("无效的来源地址")
    try:
        parsed = urlsplit(url)
        name = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("无效的来源地址") from exc
    if (parsed.scheme.lower() != "https" or not name or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.fragment or not re.fullmatch(r"[a-z0-9.-]+", name.lower())
            or name.endswith(".") or ".." in name or "." not in name):
        raise ValueError("仅允许可信来源的 HTTPS 地址")
    name = name.lower()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError("不允许 IP 地址作为来源")
    if name.endswith((".localhost", ".local", ".internal")):
        raise ValueError("不允许内网域名")
    return name


def _approved_hosts() -> frozenset[str]:
    approved = {_host(url) for url in DEFAULT_FEEDS}
    configured = [url.strip() for url in settings.rss_feeds.split(",") if url.strip()]
    for feed in configured:
        try:
            approved.add(_host(feed))
        except ValueError:
            # Invalid feeds never grant trust to the host or links they contain.
            continue
    return frozenset(approved)


def _validate_url(url: str, hosts: frozenset[str]) -> str:
    host = _host(url)
    if host not in hosts:
        raise ValueError("文章来源未获批准")
    return host


def _public_addresses(host: str) -> tuple[str, ...]:
    # Reject mixed public/private answers too. Pin the checked addresses at connect time
    # so a second DNS lookup cannot silently route to a loopback/private address.
    addresses = tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("文章来源解析到非公网地址")
    return addresses


def _get(url: str, timeout: int = FETCH_TIMEOUT, *, kind: str = "article",
         hosts: frozenset[str] | None = None) -> bytes:
    allowed = hosts if hosts is not None else _approved_hosts()
    _validate_url(url, allowed)
    limit = FEED_MAX_BYTES if kind == "feed" else ARTICLE_MAX_BYTES
    types = FEED_TYPES if kind == "feed" else ARTICLE_TYPES
    request = urllib.request.Request(url, headers={"User-Agent": "AI-Learning-Bot/1.0 (+personal study assistant)"})
    # No ambient/system proxies: they can re-resolve a checked hostname internally.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         _SafeHTTPSHandler(allowed), _SafeRedirectHandler(allowed))
    with opener.open(request, timeout=min(max(timeout, 1), FETCH_TIMEOUT)) as response:
        _validate_url(response.geturl(), allowed)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in types:
            raise ValueError("文章来源的响应类型不符合预期")
        if response.headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
            raise ValueError("不支持压缩后的文章来源")
        length = response.headers.get("Content-Length", "")
        if length:
            try:
                if int(length) > limit:
                    raise ValueError("文章来源响应超过大小限制")
            except ValueError as exc:
                raise ValueError("文章来源响应长度无效或超过限制") from exc
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("文章来源响应超过大小限制")
        return body


def _text(node, names: list[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _clean_markup(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.plain_parts))).strip()[:1200]


def fetch_feed(url: str, allowed_hosts: frozenset[str] | None = None) -> list[Article]:
    hosts = allowed_hosts if allowed_hosts is not None else _approved_hosts()
    _validate_url(url, hosts)
    raw = _get(url, kind="feed", hosts=hosts)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("不接受包含 DTD 的文章来源")
    root = ET.fromstring(raw)
    items = root.findall(".//item")
    output: list[Article] = []
    if items:
        for item in items[:20]:
            link = _text(item, ["link"])
            try:
                article_host = _validate_url(link, hosts)
            except ValueError:
                continue
            article = Article(
                title=_text(item, ["title"]), url=link, source=article_host,
                summary=_clean_markup(_text(item, ["description", "{http://purl.org/rss/1.0/modules/content/}encoded"])),
                published=_text(item, ["pubDate", "{http://purl.org/dc/elements/1.1/}date"]),
            )
            article._approved_hosts = hosts
            output.append(article)
        return output
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for item in root.findall(".//a:entry", ns)[:20]:
        links = item.findall("a:link", ns)
        link = next((node.attrib.get("href", "") for node in links
                     if node.attrib.get("rel", "alternate") == "alternate"), "")
        try:
            article_host = _validate_url(link, hosts)
        except ValueError:
            continue
        article = Article(
            title=_text(item, ["{http://www.w3.org/2005/Atom}title"]), url=link, source=article_host,
            summary=_clean_markup(_text(item, ["{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"])),
            published=_text(item, ["{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"]),
        )
        article._approved_hosts = hosts
        output.append(article)
    return output


def infer_topics(article: Article) -> list[str]:
    haystack = f"{article.title} {article.summary}".lower()
    return [topic for topic, words in TOPIC_WORDS.items() if any(word in haystack for word in words)] or ["AI 应用"]


def infer_difficulty(article: Article) -> str:
    text = f"{article.title} {article.summary}".lower()
    advanced = ["paper", "benchmark", "architecture", "gradient", "训练", "research"]
    beginner = ["introduction", "beginner", "guide", "explained", "入门", "科普"]
    if sum(word in text for word in advanced) >= 2:
        return "advanced"
    if any(word in text for word in beginner):
        return "beginner"
    return "intermediate"


def score_article(article: Article, profile: dict, seen: set[str]) -> float:
    if article.url in seen:
        return float("-inf")
    article.topics = infer_topics(article)
    article.difficulty = infer_difficulty(article)
    wanted = {topic.lower() for topic in profile.get("topics", [])}
    overlap = sum(topic.lower() in wanted for topic in article.topics)
    levels = {"beginner": 0, "intermediate": 1, "advanced": 2}
    distance = abs(levels.get(profile.get("level", "beginner"), 0) - levels[article.difficulty])
    freshness = 0.0
    try:
        dt = parsedate_to_datetime(article.published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - dt).days)
        freshness = max(0.0, 5.0 - age_days / 30)
    except Exception:
        pass
    return overlap * 8 - distance * 5 + min(len(article.summary) / 300, 3) + freshness


def pick_article(feeds: list[str], profile: dict, seen: set[str]) -> Article:
    hosts = _approved_hosts()
    candidates: list[Article] = []
    for feed in feeds:
        try:
            candidates.extend(fetch_feed(feed, hosts))
        except Exception:
            # Don't include remote URLs or exception text (potential credentials) in errors.
            continue
    if not candidates:
        raise RuntimeError("暂时没有可用的文章来源，请稍后再试。")
    ranked = sorted((item for item in candidates if item.url not in seen),
                    key=lambda item: score_article(item, profile, seen), reverse=True)
    for article in ranked:
        try:
            content = fetch_article_text(article, settings.article_max_chars)
            if len(content) >= MIN_ARTICLE_CHARS and article.content_status in {"full", "truncated"}:
                return article
        except Exception:
            continue
    raise RuntimeError("暂时没有可阅读的文章正文，请稍后再试。")


def fetch_article_text(article: Article, max_chars: int) -> str:
    """Return verified article body, never the RSS summary as a substitute."""
    limit = max(0, int(max_chars))
    cached = getattr(article, "_body_text", None)
    if cached is None:
        article.fetched_at = datetime.now(timezone.utc).isoformat()
        article.content = ""
        article.content_truncated = False
        article.content_status = "unavailable"
        try:
            hosts = getattr(article, "_approved_hosts", _approved_hosts())
            _validate_url(article.url, hosts)
            raw = _get(article.url, kind="article", hosts=hosts)
            parser = TextExtractor()
            parser.feed(raw.decode("utf-8", errors="replace"))
            paragraphs = parser.paragraphs()
            body = "\n".join(f"{index}. {paragraph}" for index, paragraph in enumerate(paragraphs, 1))
            if len("".join(paragraphs)) < MIN_ARTICLE_CHARS:
                article.content_status = "insufficient"
                return ""
            article._body_text = body
            cached = body
        except Exception:
            return ""
    if limit == 0:
        article.content = ""
        article.content_status = "insufficient"
        article.content_truncated = bool(cached)
        return ""
    article.content = cached[:limit]
    article.content_truncated = len(cached) > limit
    article.content_status = "truncated" if article.content_truncated else "full"
    return article.content
