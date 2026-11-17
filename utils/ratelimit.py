"""Per-user sliding-window rate limiter shared by prefix and slash commands."""
from __future__ import annotations

import time
from collections import deque

from discord import app_commands
from discord.ext import commands


class RateLimited(commands.CheckFailure):
    """Raised for prefix commands when a user is sending commands too fast."""

    def __init__(self, retry_after: float, silent: bool = False) -> None:
        self.retry_after = retry_after
        self.silent = silent  # True = don't answer again, avoids bot spamming the chat
        super().__init__(f"Rate limited for {retry_after:.1f}s")


class AppRateLimited(app_commands.CheckFailure):
    """Raised for slash commands when a user is sending commands too fast."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited for {retry_after:.1f}s")


class RateLimiter:
    def __init__(self, rate: int, per: float) -> None:
        self.rate = rate
        self.per = per
        self._hits: dict[int, deque[float]] = {}
        self._notified: dict[int, float] = {}

    def hit(self, key: int) -> tuple[float, bool]:
        """Register one command. Returns (retry_after, first_notice).

        retry_after == 0 means the command is allowed.
        """
        now = time.monotonic()
        queue = self._hits.setdefault(key, deque())
        while queue and now - queue[0] >= self.per:
            queue.popleft()

        if len(queue) < self.rate:
            queue.append(now)
            self._notified.pop(key, None)
            return 0.0, False

        retry_after = self.per - (now - queue[0])
        first = self._notified.get(key, 0.0) <= now
        if first:
            self._notified[key] = now + retry_after
        return retry_after, first

    def cleanup(self) -> None:
        now = time.monotonic()
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.per]:
            self._hits.pop(key, None)
            self._notified.pop(key, None)
