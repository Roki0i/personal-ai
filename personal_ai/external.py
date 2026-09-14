"""Read-only adapters and host-owned, exact-payload outbound permissions.

Adapters are trusted, stateless, pickleable code. Their returned text is untrusted.
No adapter receives Context, Memory, history, credentials from prompts, or files.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Protocol
from urllib.parse import urlsplit

from .models import ToolCall
from .storage import now


class PrivacyClassification(str, Enum):
    LOCAL_ONLY = "local-only"
    CLOUD_SENDABLE = "cloud-sendable"
    EXPLICIT_APPROVAL_REQUIRED = "explicit-approval-required"


@dataclass(frozen=True)
class ExternalPermission:
    """Host API only; never deserialize this object from model/provider output."""
    classification: PrivacyClassification = PrivacyClassification.LOCAL_ONLY
    approved: bool = False
    provider: str = ""


class WebSearchProvider(Protocol):
    provider_id: str

    def search(self, query: str) -> list:
        ...


class CalendarProvider(Protocol):
    provider_id: str

    def list_events(self, date: str) -> list:
        ...

    def get_event(self, event_id: str) -> dict:
        ...


class MockWebSearchProvider:
    provider_id = "mock-web"

    def search(self, query):
        return [{"title": "Mock search: " + query, "url": "https://example.com/",
                 "snippet": "オフラインのサンプル検索結果です。", "retrieved_at": now(),
                 "provider": self.provider_id}]


class MockCalendarProvider:
    provider_id = "mock-calendar"

    def list_events(self, date):
        return [self._event("demo", date)]

    def get_event(self, event_id):
        if event_id != "demo":
            raise LookupError
        return self._event(event_id, datetime.now().astimezone().date().isoformat())

    def _event(self, event_id, date):
        return {"id": event_id, "title": "サンプル予定（mock）", "date": date,
                "retrieved_at": now(), "provider": self.provider_id}


EXTERNAL_NAMES = frozenset({"web_search", "calendar_list", "calendar_get"})


def request_from_message(message):
    """Conservative intent parser: no inferred queries or history interpolation."""
    if message.startswith("/web "):
        return ToolCall("web_search", {"query": message[5:].strip()})
    match = re.fullmatch(r"(.+)をWebで調べて[。？?]?", message, re.IGNORECASE)
    if match:
        return ToolCall("web_search", {"query": match[1].strip()})
    if message in ("今日の予定は？", "今日の予定は?", "/calendar"):
        return ToolCall("calendar_list", {"date": datetime.now().astimezone().date().isoformat()})
    if message.startswith("/event "):
        return ToolCall("calendar_get", {"event_id": message[7:].strip()})
    return None


def outbound_allowed(call, request, permission, provider):
    if request is None or call != request or not isinstance(permission, ExternalPermission):
        return False
    if not isinstance(permission.classification, PrivacyClassification):
        return False
    if permission.provider != getattr(provider, "provider_id", None):
        return False
    return (permission.classification == PrivacyClassification.CLOUD_SENDABLE or
            (permission.classification == PrivacyClassification.EXPLICIT_APPROVAL_REQUIRED
             and permission.approved is True))


def execute_external(provider, name, arguments):
    if provider is None:
        return False, None, "provider_unavailable"
    try:
        if name == "web_search":
            data = provider.search(arguments["query"])
        elif name == "calendar_list":
            datetime.strptime(arguments["date"], "%Y-%m-%d")
            data = provider.list_events(arguments["date"])
        else:
            data = provider.get_event(arguments["event_id"])
    except TimeoutError:
        return False, None, "timeout"
    except ConnectionError:
        return False, None, "network_failure"
    except LookupError:
        return False, None, "not_found"
    except Exception:
        return False, None, "provider_unavailable"
    try:
        items = [data] if name == "calendar_get" else data
        if not isinstance(items, list) or len(items) > 20:
            raise ValueError
        expected = ({"title", "url", "snippet", "retrieved_at", "provider"} if name == "web_search"
                    else {"id", "title", "date", "retrieved_at", "provider"})
        for item in items:
            if not isinstance(item, dict) or set(item) != expected:
                raise ValueError
            if any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in item.values()):
                raise ValueError
            stamp = datetime.fromisoformat(item["retrieved_at"])
            if stamp.tzinfo is None or item["provider"] != provider.provider_id:
                raise ValueError
            if name == "web_search":
                url = urlsplit(item["url"])
                if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
                    raise ValueError
                if any(ord(c) < 33 for c in item["url"]):
                    raise ValueError
            else:
                datetime.strptime(item["date"], "%Y-%m-%d")
        return True, {"trust": "untrusted_external_data", "items": items}, None
    except (ValueError, TypeError, AttributeError):
        return False, None, "malformed_response"


def provenance(data):
    return [{key: item[key] for key in ("url", "retrieved_at", "provider", "id") if key in item}
            for item in data["items"]]
