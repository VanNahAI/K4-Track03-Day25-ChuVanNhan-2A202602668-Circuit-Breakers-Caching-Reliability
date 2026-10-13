"""Part E — Multi-window, multi-burn-rate alerting (Google SRE Workbook, ch. 5).

Input: per-minute ``total`` and ``bad`` request counts for a 30-day window.

burn_rate(window) = error_ratio(window) / (1 - SLO)

A rule fires at minute i when BOTH its long and its short window (ending at i,
inclusive) burn at least ``burn_rate``. The long window says "this is
significant"; the short window says "it is still happening" (so the alert
resets quickly after recovery).

Tests: tests/test_alerting.py, then `make slo`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str  # "page" or "ticket"
    long_min: int
    short_min: int
    burn_rate: float


# SRE Workbook defaults for a 30-day SLO window
DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("fast_burn_2pct_1h", "page", 60, 5, 14.4),
    AlertRule("slow_burn_5pct_6h", "page", 360, 30, 6.0),
    AlertRule("budget_leak_10pct_3d", "ticket", 4320, 360, 1.0),
)


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    severity: str
    start_min: int  # first minute the rule fired
    end_min: int  # first minute it stopped firing (exclusive)


class WindowSums:
    """O(1) sum of any window, via prefix sums. Build once, query many times.

    43,200 minutes x 6 windows: recomputing every window with sum() is far too slow.
    """

    def __init__(self, values: list[int]) -> None:
        """Build the prefix-sum list (prefix[0] = 0)."""
        prefix = [0]
        for value in values:
            prefix.append(prefix[-1] + value)
        self._prefix = prefix

    def sum(self, end_inclusive: int, length: int) -> int:
        """Sum of values[end_inclusive - length + 1 .. end_inclusive], clipped at index 0."""
        start = end_inclusive - length + 1
        if start < 0:
            start = 0
        return self._prefix[end_inclusive + 1] - self._prefix[start]


class BurnRateAlerter:
    def __init__(self, slo: float = 0.995, rules: tuple[AlertRule, ...] = DEFAULT_RULES) -> None:
        if not 0 < slo < 1:
            raise ValueError("slo must be in (0, 1)")
        self.slo = slo
        self.rules = rules

    def burn_rate(self, total: WindowSums, bad: WindowSums, end: int, length: int) -> float:
        """(bad / total over the window) / (1 - slo); 0.0 if total is 0."""
        window_total = total.sum(end, length)
        if window_total == 0:
            return 0.0
        return (bad.sum(end, length) / window_total) / (1.0 - self.slo)

    def evaluate(self, total: list[int], bad: list[int]) -> list[AlertEvent]:
        """One AlertEvent per contiguous firing episode of each rule.

        A rule fires at minute i when both its long and short windows burn at
        least ``rule.burn_rate``. Episodes are sorted by start minute.
        """
        total_sums = WindowSums(total)
        bad_sums = WindowSums(bad)
        events: list[AlertEvent] = []
        n = len(total)
        for rule in self.rules:
            start: int | None = None
            for minute in range(n):
                firing = (
                    self.burn_rate(total_sums, bad_sums, minute, rule.long_min) >= rule.burn_rate
                    and self.burn_rate(total_sums, bad_sums, minute, rule.short_min) >= rule.burn_rate
                )
                if firing and start is None:
                    start = minute
                elif not firing and start is not None:
                    events.append(AlertEvent(rule.name, rule.severity, start, minute))
                    start = None
            if start is not None:
                events.append(AlertEvent(rule.name, rule.severity, start, n))
        events.sort(key=lambda event: (event.start_min, event.rule))
        return events


def error_budget_report(total: list[int], bad: list[int], slo: float = 0.995) -> dict[str, float | bool]:
    """How much of the 30-day error budget is spent."""
    requests = float(sum(total))
    bad_count = float(sum(bad))
    allowed_bad = round((1.0 - slo) * requests, 1)
    availability = round(1.0 - bad_count / requests, 6) if requests else 1.0
    consumed = round(bad_count / allowed_bad, 4) if allowed_bad else 0.0
    remaining = round(1.0 - consumed, 4)
    return {
        "requests": requests,
        "allowed_bad": allowed_bad,
        "bad": bad_count,
        "availability": availability,
        "budget_consumed": consumed,
        "budget_remaining": remaining,
        "freeze_releases": remaining <= 0,
    }
