#!/usr/bin/env python3
"""standup_adapters.py — provider-agnostic adapter interfaces for /standup.

Four platforms are unknown until the workplace's actual tools are confirmed:
issue tracker, chat, email, calendar. Each gets its own single-method
Protocol (they're independent platforms, not one unified system) and a
stub implementation that raises NotConfiguredError until the real adapter
is written. Wire a concrete adapter in here once the platform is known —
select it in ADAPTERS below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class NotConfiguredError(Exception):
    """Raised by a stub adapter — no concrete implementation exists yet."""


@dataclass
class Item:
    id: str
    title: str
    url: str
    status: str
    updated_at: str


@dataclass
class Message:
    author: str
    text: str
    permalink: str
    timestamp: str
    channel_or_thread: str


@dataclass
class CalEvent:
    title: str
    start: str
    is_recurring: bool


class IssueTrackerAdapter(Protocol):
    def get_assigned_items(self) -> list[Item]: ...


class ChatAdapter(Protocol):
    def get_relevant_messages(self, since: date) -> list[Message]: ...

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]: ...


class EmailAdapter(Protocol):
    def get_correspondence(self, since: date) -> list[Message]: ...

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]: ...


class CalendarAdapter(Protocol):
    def get_calendar_events(self, days: list[date]) -> list[CalEvent]: ...


class StubIssueTrackerAdapter:
    def get_assigned_items(self) -> list[Item]:
        raise NotConfiguredError(
            "issue tracker adapter not configured — confirm GitHub vs GitLab "
            "vs Jira at work, then implement IssueTrackerAdapter here"
        )


class StubChatAdapter:
    def get_relevant_messages(self, since: date) -> list[Message]:
        raise NotConfiguredError(
            "chat adapter not configured — confirm Slack vs Teams at "
            "work, then implement ChatAdapter here"
        )

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]:
        raise NotConfiguredError(
            "chat adapter not configured — confirm Slack vs Teams at "
            "work, then implement ChatAdapter here"
        )


class StubEmailAdapter:
    def get_correspondence(self, since: date) -> list[Message]:
        raise NotConfiguredError(
            "email adapter not configured — see backlog item "
            "meta-standup-email-adapter-outlook"
        )

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]:
        raise NotConfiguredError(
            "email adapter not configured — see backlog item "
            "meta-standup-email-adapter-outlook"
        )


class StubCalendarAdapter:
    def get_calendar_events(self, days: list[date]) -> list[CalEvent]:
        raise NotConfiguredError(
            "calendar adapter not configured — confirm Outlook/Google "
            "Calendar/other at work, then implement CalendarAdapter here"
        )


# Swap a stub for a concrete adapter instance here once it's written.
ADAPTERS: dict[str, object] = {
    "issue_tracker": StubIssueTrackerAdapter(),
    "chat": StubChatAdapter(),
    "email": StubEmailAdapter(),
    "calendar": StubCalendarAdapter(),
}
