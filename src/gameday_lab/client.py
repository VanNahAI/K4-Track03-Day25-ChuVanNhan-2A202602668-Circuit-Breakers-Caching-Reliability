"""Part C — ResilientClient: compose classification, retry, breaker and fallback.

The simulator calls ``start`` for every logical LLM call, then ``on_success``
or ``on_error`` for every attempt. You answer with ``Send`` (try a provider,
maybe after a delay) or ``GiveUp`` (user gets a static degraded answer).

Tests: tests/test_client.py, then `make compare` (6 incident tapes).

Per-call scratch space lives in ``call.state`` (a dict). Suggested keys:
    "ladder": list[str]  providers this call may use, in order
    "idx":    int        position of the provider we are currently on
    "tries":  int        attempts already made on that provider

Decision table for ``on_error`` (implement it!):

| ErrorKind        | breaker              | then                                                    |
|------------------|----------------------|---------------------------------------------------------|
| CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
| QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
| RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
| OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
| SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
| TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |

Cooldowns are per provider and shared by all calls (other calls must avoid a
rate-limited provider too). When every provider in the ladder is unusable,
you may wait for a *short* (<= 5 s) rate-limit cooldown if it fits the deadline.
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker  # noqa: F401
from gameday_lab.errors import ErrorKind, classify
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

RATE_LIMIT_WAIT_MAX_S = 5.0  # only wait for short (rate-limit) cooldowns, never for quota


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """PROVIDED: reads the `client:` block of configs/gameday.yaml."""
        self.providers = providers
        self.order = list(fallback_order)
        self.timeout_s = float(settings.get("attempt_timeout_s", 8.0))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))
        self.backoff = FullJitterBackoff(
            float(settings.get("backoff_base_s", 0.25)),
            float(settings.get("backoff_cap_s", 4.0)),
            random.Random(seed),
        )
        self.budget = RetryBudget(
            float(settings.get("retry_budget_ratio", 0.1)),
            float(settings.get("retry_budget_min_per_s", 0.5)),
            float(settings.get("retry_budget_max_tokens", 20.0)),
        )
        breaker_cfg = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_cfg) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}

    # ------------------------------------------------------------------ provided helper
    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """PROVIDED. Build a Send for ``provider`` (after ``delay``), or None if its breaker refuses.

        Takes a breaker permit (carried in ``tag``), and caps the attempt timeout
        so it never runs past the call's deadline. Returns GiveUp("deadline") if
        there is not enough time left for a meaningful attempt.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        if timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")
        return Send(provider, timeout_s=timeout, delay_s=delay, tag=permit)

    def ladder(self, task: str) -> list[str]:
        """``self.order`` filtered by capability — a tool call only uses providers that support tools."""
        if task == "tool":
            return [name for name in self.order if self.providers[name].supports_tools]
        return list(self.order)

    def _fits(self, call: CallContext, now: float, delay: float) -> bool:
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        return timeout >= self.min_time_left_s

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        """Pick the provider for the next attempt, walking the ladder from ``idx``."""
        ladder: list[str] = call.state["ladder"]
        idx = int(call.state["idx"])
        while idx < len(ladder):
            provider = ladder[idx]
            if now + delay < self.cooldown_until[provider]:
                idx += 1
                call.state["idx"] = idx
                call.state["tries"] = 0
                delay = 0.0
                continue
            action = self._send(call, provider, now, delay)
            if action is None:
                idx += 1
                call.state["idx"] = idx
                call.state["tries"] = 0
                delay = 0.0
                continue
            if isinstance(action, GiveUp):
                return action
            call.state["idx"] = idx
            call.state["tries"] = int(call.state.get("tries", 0)) + 1
            return action
        return self._wait_for_short_cooldown(call, now)

    def _wait_for_short_cooldown(self, call: CallContext, now: float) -> Action:
        """Wait for the earliest short rate-limit cooldown, or give up."""
        ladder: list[str] = call.state["ladder"]
        best: str | None = None
        best_wait: float | None = None
        for provider in ladder:
            wait = self.cooldown_until[provider] - now
            if wait <= 0.0 or wait > RATE_LIMIT_WAIT_MAX_S:
                continue
            if self.breakers[provider].state is BreakerState.OPEN:
                continue
            if not self._fits(call, now, wait):
                continue
            if best_wait is None or wait < best_wait:
                best, best_wait = provider, wait
        if best is None or best_wait is None:
            return GiveUp("no_provider_available")
        call.state["idx"] = ladder.index(best)
        call.state["tries"] = 0
        action = self._send(call, best, now, best_wait)
        if isinstance(action, Send):
            call.state["tries"] = 1
            return action
        if isinstance(action, GiveUp):
            return action
        return GiveUp("no_provider_available")

    def _failover(self, call: CallContext, now: float) -> Action:
        call.state["idx"] = int(call.state["idx"]) + 1
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def _release(self, provider: str, tag: Any, now: float) -> None:
        if isinstance(tag, Permit):
            self.breakers[provider].release(tag, now)

    def start(self, call: CallContext, now: float) -> Action:
        """Record the request in the retry budget, then route the first attempt."""
        self.budget.record_request(now)
        call.state["ladder"] = self.ladder(call.task)
        call.state["idx"] = 0
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Report success and latency to the provider's breaker."""
        permit = attempt.tag
        if isinstance(permit, Permit):
            self.breakers[attempt.provider].on_success(permit, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Apply the error decision table: release or record, then retry, fail over, or give up."""
        call = attempt.call
        provider = attempt.provider
        kind = classify(err)
        breaker = self.breakers[provider]

        if kind is ErrorKind.CLIENT_ERROR:
            self._release(provider, attempt.tag, now)
            return GiveUp("client_error")

        if kind is ErrorKind.QUOTA_EXHAUSTED:
            self._release(provider, attempt.tag, now)
            self.cooldown_until[provider] = max(self.cooldown_until[provider], now + self.quota_cooldown_s)
            return self._failover(call, now)

        if kind is ErrorKind.RATE_LIMITED:
            self._release(provider, attempt.tag, now)
            pause = float(err.retry_after or 0.0)
            self.cooldown_until[provider] = max(self.cooldown_until[provider], now + pause)
            return self._failover(call, now)

        if isinstance(attempt.tag, Permit):
            breaker.on_failure(attempt.tag, now)

        tries = int(call.state.get("tries", 0))
        if tries < self.max_attempts_per_provider:
            delay = self.backoff.delay(max(1, tries))
            if self._fits(call, now, delay) and self.budget.try_acquire_retry(now):
                action = self._send(call, provider, now, delay)
                if isinstance(action, Send):
                    call.state["tries"] = tries + 1
                    return action
                if isinstance(action, GiveUp):
                    return action
        return self._failover(call, now)

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """A delayed attempt was dropped (deadline) — release its permit."""
        if isinstance(tag, Permit):
            self.breakers[tag.breaker].release(tag, now)
