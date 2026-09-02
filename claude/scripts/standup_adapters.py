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
from typing import Protocol, runtime_checkable


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


@runtime_checkable
class IssueTrackerAdapter(Protocol):
    def get_assigned_items(self) -> list[Item]: ...


@runtime_checkable
class ChatAdapter(Protocol):
    def get_relevant_messages(self, since: date) -> list[Message]: ...

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]: ...


@runtime_checkable
class EmailAdapter(Protocol):
    def get_correspondence(self, since: date) -> list[Message]: ...

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]: ...


@runtime_checkable
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


class OutlookEmailAdapter:
    """Email adapter communicating with Outlook on Windows host via PowerShell COM."""

    def get_correspondence(self, since: date) -> list[Message]:
        try:
            import outlook_email

            emails = outlook_email.get_recent_correspondence(since=since)
            messages = []
            for item in emails:
                sender_name = str(item.get("sender_name") or "")
                sender_email = str(item.get("sender_email") or "")
                author = (
                    f"{sender_name} <{sender_email}>".strip()
                    if sender_email
                    else sender_name
                )
                subject = str(item.get("subject") or "")
                preview = str(item.get("body_preview") or "")
                text = f"{subject}: {preview}".strip(": ")
                entry_id = str(item.get("entry_id") or "")
                timestamp = str(item.get("received_time") or "")
                messages.append(
                    Message(
                        author=author,
                        text=text,
                        permalink=f"outlook:{entry_id}" if entry_id else "",
                        timestamp=timestamp,
                        channel_or_thread=subject,
                    )
                )
        except Exception as exc:
            raise NotConfiguredError(f"Outlook email adapter error: {exc}") from exc
        else:
            return messages

    def get_thread_updates(
        self, pending_items: list[dict[str, object]]
    ) -> list[Message]:
        try:
            import outlook_email

            updates: list[Message] = []
            for item in pending_items:
                ref = item.get("source_ref")
                subject = ""
                if isinstance(ref, dict):
                    subject = str(ref.get("subject") or "")
                if not subject:
                    continue
                emails = outlook_email.search_emails(query=subject, limit=5)
                for email in emails:
                    sender_name = str(email.get("sender_name") or "")
                    sender_email = str(email.get("sender_email") or "")
                    author = (
                        f"{sender_name} <{sender_email}>".strip()
                        if sender_email
                        else sender_name
                    )
                    email_subject = str(email.get("subject") or "")
                    preview = str(email.get("body_preview") or "")
                    text = f"{email_subject}: {preview}".strip(": ")
                    entry_id = str(email.get("entry_id") or "")
                    timestamp = str(email.get("received_time") or "")
                    updates.append(
                        Message(
                            author=author,
                            text=text,
                            permalink=f"outlook:{entry_id}" if entry_id else "",
                            timestamp=timestamp,
                            channel_or_thread=email_subject,
                        )
                    )
        except Exception as exc:
            raise NotConfiguredError(f"Outlook email adapter error: {exc}") from exc
        else:
            return updates


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
    "email": OutlookEmailAdapter(),
    "calendar": StubCalendarAdapter(),
}
