"""Part G — a semantic response cache that is safe, fresh, and keeps FAQ answers up in an outage.

* ``SemanticCache``: lookup by embedding similarity *within a scope* (prompt
  version; personal questions never), TTL + a stale window, invalidation by
  source document, bounded size (LRU).
* ``CachingClient``: wraps your ResilientClient. A fresh hit answers without
  calling any provider; a miss goes to the providers and the answer is stored;
  when the providers give up, a *stale* answer beats no answer (fallback
  ladder, rung 3: "cached response").

Tests: tests/test_cache.py, then `make cache` (tape C1).

Lifetime of an entry (age = now - stored_at):

    0 ......... ttl_s ................ ttl_s + max_stale_s ......
    |   fresh   |  stale: only if no provider answers  |  expired: drop it

Only FAQ calls carry ``call.request``; every other call has ``request=None``
and must go straight to the inner client.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from gameday_lab.sim.contracts import (  # noqa: F401
    Action,
    Answer,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    Request,
    Serve,
)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """PROVIDED. Cosine similarity: 1.0 = same direction (high = strict threshold)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Entry:
    embedding: tuple[float, ...]
    answer: Answer
    stored_at: float
    last_used: float


@dataclass(frozen=True)
class CacheHit:
    answer: Answer
    similarity: float
    age_s: float
    fresh: bool  # age_s <= ttl_s; a stale hit may only be served when no provider can answer


class SemanticCache:
    def __init__(self, similarity_threshold: float = 0.9, ttl_s: float = 60.0, max_stale_s: float = 600.0,
                 max_entries: int = 500) -> None:
        """PROVIDED: reads the `cache:` block of configs/gameday.yaml."""
        self.similarity_threshold = similarity_threshold
        self.ttl_s = ttl_s
        self.max_stale_s = max_stale_s
        self.max_entries = max_entries
        self.entries: list[Entry] = []

    def _drop_expired(self, now: float) -> None:
        horizon = self.ttl_s + self.max_stale_s
        self.entries = [entry for entry in self.entries if now - entry.stored_at <= horizon]

    def lookup(self, req: Request, now: float) -> CacheHit | None:
        """The best usable entry for ``req``, or None.

        Personal questions are not in the allowlist. Expired entries are dropped.
        Only the same ``prompt_version`` is in scope, and a hit needs cosine
        similarity at or above ``similarity_threshold``.
        """
        if req.personal:
            return None
        self._drop_expired(now)
        best: Entry | None = None
        best_sim = -1.0
        for entry in self.entries:
            if entry.answer.prompt_version != req.prompt_version:
                continue
            sim = cosine(req.embedding, entry.embedding)
            if sim > best_sim:
                best, best_sim = entry, sim
        if best is None or best_sim < self.similarity_threshold:
            return None
        best.last_used = now
        age = now - best.stored_at
        return CacheHit(best.answer, best_sim, age, fresh=age <= self.ttl_s)

    def store(self, req: Request, answer: Answer, now: float) -> None:
        """Remember ``answer`` for ``req``. Personal answers are never stored.

        A same-scope entry already at or above the similarity threshold is
        replaced. Otherwise the answer is appended, and the least recently
        used entry is evicted past ``max_entries``.
        """
        if req.personal:
            return
        for entry in self.entries:
            if entry.answer.prompt_version != req.prompt_version:
                continue
            if cosine(req.embedding, entry.embedding) >= self.similarity_threshold:
                entry.embedding = req.embedding
                entry.answer = answer
                entry.stored_at = now
                entry.last_used = now
                return
        self.entries.append(Entry(req.embedding, answer, now, now))
        if len(self.entries) > self.max_entries:
            lru = min(self.entries, key=lambda entry: entry.last_used)
            self.entries.remove(lru)

    def invalidate(self, source: str, now: float) -> int:
        """Drop every entry whose ``answer.sources`` contains ``source``; return how many."""
        kept = [entry for entry in self.entries if source not in entry.answer.sources]
        removed = len(self.entries) - len(kept)
        self.entries = kept
        return removed


class CachingClient(BaseClient):
    name = "cached"

    def __init__(self, inner: BaseClient, cache: SemanticCache) -> None:
        """PROVIDED. ``inner`` is your ResilientClient (see factory.build_client("cached"))."""
        self.inner = inner
        self.cache = cache

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """PROVIDED."""
        self.inner.on_cancel(call, tag, now)

    # ------------------------------------------------------------------ TODO
    def start(self, call: CallContext, now: float) -> Action:
        """Serve a fresh FAQ hit, otherwise ask the inner client and fall back to stale."""
        if call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None and hit.fresh:
                return Serve(hit.answer)
        return self._or_stale(call, self.inner.start(call, now), now)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Tell the inner client first, then store the FAQ answer it produced."""
        self.inner.on_success(attempt, now, latency_s)
        call = attempt.call
        if call.request is not None and call.answer is not None:
            self.cache.store(call.request, call.answer, now)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Let the inner client decide, then substitute a stale FAQ answer for a give-up."""
        return self._or_stale(attempt.call, self.inner.on_error(attempt, err, now), now)

    def on_content_update(self, source: str, now: float) -> None:
        """A document changed (webhook) — invalidate the answers built on it."""
        self.cache.invalidate(source, now)

    def _or_stale(self, call: CallContext, action: Action, now: float) -> Action:
        """If providers give up on an FAQ call, serve a cached answer even past TTL."""
        if isinstance(action, GiveUp) and call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None:
                return Serve(hit.answer, stale=not hit.fresh)
        return action
