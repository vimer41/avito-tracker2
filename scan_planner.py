"""Offline-testable, event-loop-owned heap deadline scheduling for unique sources."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import config


def get_capacity_mode_intervals() -> dict[str, tuple[float, float]]:
    base_low = float(config.SCAN_INTERVAL_MIN)
    base_high = float(config.SCAN_INTERVAL_MAX)
    spread = max(0.5, base_high - base_low)
    return {
        "normal": (base_low, base_high),
        "full_capacity": (min(base_high, round(base_low + spread * 0.25, 1)), base_high),
        "boundary": (min(base_high, round(base_low + spread * 0.50, 1)), base_high),
        "deficit": (min(base_high, round(base_low + spread * 0.75, 1)), base_high),
    }


CAPACITY_MODE_INTERVALS: dict[str, tuple[float, float]] = get_capacity_mode_intervals()
BULK_NEW_SOURCES = 5  # more new links than this in one pass start spread over their interval


@dataclass
class IntervalPlan:
    intervals: dict[str, float]
    requested_rps: float
    planned_rps: float
    available_rps: float
    overloaded: bool


def calculate_interval_plan(
    base_intervals: dict[str, float],
    requests_per_second: float,
    max_interval: float = 120,
    utilization: float = 0.8,
    requests_per_scan: float = 1,
) -> IntervalPlan:
    values = [requests_per_second, max_interval, utilization, requests_per_scan, *base_intervals.values()]
    if any(not math.isfinite(value) or value <= 0 for value in values) or utilization > 1:
        raise ValueError("Intervals, request cost and budget must be positive and finite")
    available = requests_per_second * utilization
    demand = sum(requests_per_scan / interval for interval in base_intervals.values())
    scale = max(1.0, demand / available)
    intervals = {key: min(max(max_interval, base), base * scale) for key, base in base_intervals.items()}
    planned = sum(requests_per_scan / interval for interval in intervals.values())
    return IntervalPlan(intervals, demand, planned, available, planned > available * (1 + 1e-9))


def stretch_factor(demand_rps: float, available_rps: float, *, utilization: float, max_factor: float) -> float:
    """How much longer every interval must be so the planned load fits the measured capacity; 1.0 while it fits.
    Every link stretches by the same factor, so the tariffs keep their proportion (20/30/40 s stay 1 : 1.5 : 2),
    and nothing piles up in the queue: without it an overload left every link minutes late at random."""
    values = (demand_rps, available_rps, utilization, max_factor)
    if any(not math.isfinite(value) for value in values) or demand_rps <= 0 or available_rps <= 0 or utilization <= 0:
        return 1.0
    return round(min(max(1.0, max_factor), max(1.0, demand_rps / (available_rps * utilization))), 2)


def compute_group_revision(radars: Iterable[dict]) -> str:
    """Compute a deterministic revision hash for the active radars of a URL group."""
    normalized = []
    for r in sorted(radars, key=lambda item: int(item.get("id") or 0)):
        stop_words = r.get("stop_words") or []
        if isinstance(stop_words, str):
            try:
                stop_words = json.loads(stop_words)
            except Exception:
                stop_words = [stop_words]
        normalized.append(
            {
                "id": int(r.get("id") or 0),
                "user_id": int(r.get("user_id") or 0),
                "initialized": r.get("initialized_at") is not None,
                "max_age": int(r.get("max_age_minutes") or 5),
                "title_only": int(bool(r.get("search_in_title_only") or 0)),
                "stop_words": sorted(str(w).strip().lower() for w in stop_words if str(w).strip()),
            }
        )
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class ScheduledScan:
    key: str
    radars: tuple[dict, ...]
    scheduled_at: float
    started_at: float
    retry_attempt: int
    generation: int
    can_fast_skip: bool = False
    last_top_ids: tuple[str, ...] = ()
    group_revision: str = ""
    previous_processed_at: float = 0.0


@dataclass
class SourceState:
    key: str
    radars: list[dict]
    interval: float
    due_at: float
    generation: int
    retry_attempt: int = 0
    last_started: Optional[float] = None
    last_success: Optional[float] = None
    created_at: float = 0.0
    http_in_flight: bool = False
    fanout_pending: bool = False
    pending_rescan: bool = False
    last_top_ids: tuple[str, ...] = ()
    group_revision: str = ""
    last_full_scan_at: float = 0.0
    processed_at: float = 0.0
    source_failure_streak: int = 0
    source_recovery_streak: int = 0
    backoff_until: float = 0.0
    fixed_interval: Optional[float] = None


class DeadlineScheduler:
    """Min-heap deadline scheduler with lazy invalidation, two-phase in-flight
    state (`http_in_flight` + `fanout_pending`), and hysteresis source backoff.
    """

    def __init__(
        self,
        max_retries: int = 2,
        retry_base_delay: float = 5,
        retry_max_delay: float = 60,
        max_retry_burst: int = 1,
        *,
        interval_low: float = config.SCAN_INTERVAL_MIN,
        interval_high: float = config.SCAN_INTERVAL_MAX,
        rng: Optional[random.Random] = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        use_source_backoff: bool = False,
    ):
        self.sources: dict[str, SourceState] = {}
        self.running: dict[str, ScheduledScan] = {}
        self._heap: list[tuple[float, int, str]] = []
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.max_retry_burst = max_retry_burst
        self.interval_low = float(interval_low)
        self.interval_high = float(interval_high)
        self.rng = rng or random.Random()
        self.monotonic_fn = monotonic_fn
        self.use_source_backoff = use_source_backoff
        self._generation = 0
        self._retry_burst = 0
        self.completed = 0
        self.failures = 0

        # Capacity mode state
        self.capacity_mode: str = "normal"
        self._candidate_mode: Optional[str] = None
        self._candidate_windows: int = 0
        self._last_mode_eval_at: float = 0.0

    def sample_interval(self) -> float:
        low, high = self.interval_low, self.interval_high
        if high <= low:
            return low
        return self.rng.uniform(low, high)

    def _push_heap(self, state: SourceState) -> None:
        if not state.http_in_flight and not state.fanout_pending and state.key not in self.running:
            heapq.heappush(self._heap, (state.due_at, state.generation, state.key))

    def _is_heap_entry_valid(self, entry: tuple[float, int, str]) -> bool:
        due_at, generation, key = entry
        if key in self.running:
            return False
        state = self.sources.get(key)
        if state is None or state.generation != generation:
            return False
        if state.http_in_flight or state.fanout_pending:
            return False
        return abs(state.due_at - due_at) <= 1e-9

    def _clean_heap_top(self) -> None:
        while self._heap and not self._is_heap_entry_valid(self._heap[0]):
            heapq.heappop(self._heap)

    def update_capacity_mode(self, available_rps: float, required_rps: float, now: Optional[float] = None) -> str:
        """Switch interval bounds at most once per minute after 2 consecutive windows."""
        now_val = self.monotonic_fn() if now is None else now
        if self._last_mode_eval_at and (now_val - self._last_mode_eval_at) < 55.0:
            return self.capacity_mode
        self._last_mode_eval_at = now_val

        if required_rps <= 0:
            target = "normal"
        elif available_rps >= required_rps * 1.15:
            target = "normal"
        elif available_rps >= required_rps:
            target = "full_capacity"
        elif available_rps >= required_rps * 0.90:
            target = "boundary"
        else:
            target = "deficit"

        if target == self.capacity_mode:
            self._candidate_mode = None
            self._candidate_windows = 0
            return self.capacity_mode

        if self._candidate_mode == target:
            self._candidate_windows += 1
        else:
            self._candidate_mode = target
            self._candidate_windows = 1

        if self._candidate_windows >= config.SCAN_CAPACITY_MODE_WINDOWS:
            self.capacity_mode = target
            intervals = get_capacity_mode_intervals()
            self.interval_low, self.interval_high = intervals.get(
                target, (float(config.SCAN_INTERVAL_MIN), float(config.SCAN_INTERVAL_MAX))
            )
            self._candidate_mode = None
            self._candidate_windows = 0

        return self.capacity_mode

    def sync(
        self,
        groups: dict[str, list[dict]],
        intervals: Optional[dict[str, float]] = None,
        now: Optional[float] = None,
        startup: bool = False,
        startup_spread: float = config.SCAN_STARTUP_SPREAD,
        persisted_states: Optional[dict[str, dict]] = None,
    ) -> None:
        now_val = self.monotonic_fn() if now is None else now
        for key in set(self.sources) - set(groups):
            del self.sources[key]

        total_groups = max(1, len(groups))
        # Many links added at once (an admin's bulk add) start spread over their interval. All due at once kept
        # every channel flat out for minutes and Avito answered with a wave of 403s (130 links, 06.11).
        # A link added on its own is still scanned at once.
        new_keys = [key for key in sorted(groups) if key not in self.sources]
        new_rank = {key: index for index, key in enumerate(new_keys)} if len(new_keys) > BULK_NEW_SOURCES else {}
        for rank, key in enumerate(sorted(groups)):
            radars = groups[key]
            revision = compute_group_revision(radars)
            fixed_iv = intervals[key] if (intervals is not None and key in intervals) else None
            interval = fixed_iv if fixed_iv is not None else self.sample_interval()
            state = self.sources.get(key)
            if state is None:
                self._generation += 1
                if startup:
                    offset = (rank / total_groups) * min(startup_spread, interval)
                else:
                    offset = (new_rank[key] / len(new_keys)) * interval if key in new_rank else 0.0
                persisted = (persisted_states or {}).get(key) or {}
                top_ids = tuple(persisted.get("last_top_ids") or ())
                processed_at = float(persisted.get("processed_at") or 0.0)
                failure_streak = int(persisted.get("failure_streak") or 0)
                state = SourceState(
                    key=key,
                    radars=radars,
                    interval=interval,
                    due_at=now_val + offset,
                    generation=self._generation,
                    created_at=now_val,
                    last_top_ids=top_ids,
                    group_revision=revision,
                    processed_at=processed_at,
                    source_failure_streak=failure_streak,
                    fixed_interval=fixed_iv,
                )
                self.sources[key] = state
                self._push_heap(state)
            else:
                old_revision = state.group_revision
                state.radars = radars
                state.group_revision = revision
                state.fixed_interval = fixed_iv
                due_changed = False
                if fixed_iv is not None:
                    if fixed_iv < state.interval and state.retry_attempt == 0 and key not in self.running and not state.http_in_flight and not state.fanout_pending:
                        anchor = state.last_started if state.last_started is not None else now_val
                        new_due = min(state.due_at, anchor + fixed_iv)
                        if new_due != state.due_at:
                            state.due_at = new_due
                            due_changed = True
                    state.interval = fixed_iv
                if old_revision != revision:
                    # Invalidate fast-skip when radar set or filters change
                    state.last_full_scan_at = 0.0
                if due_changed:
                    self._push_heap(state)

    def force_due_now(self, keys: Iterable[str], now: Optional[float] = None) -> None:
        now_val = self.monotonic_fn() if now is None else now
        for key in keys:
            state = self.sources.get(key)
            if state is None:
                continue
            state.last_full_scan_at = 0.0
            state.backoff_until = 0.0
            if state.http_in_flight or state.fanout_pending or key in self.running:
                state.pending_rescan = True
            else:
                state.due_at = now_val
                self._push_heap(state)

    def can_fast_skip(self, state: SourceState, now: float, now_epoch: Optional[float] = None) -> bool:
        if not state.last_top_ids:
            return False
        if any(r.get("initialized_at") is None for r in state.radars):
            return False
        if state.last_full_scan_at <= 0 or (now - state.last_full_scan_at) >= config.FAST_SKIP_FULL_SCAN_INTERVAL:
            return False
        if state.processed_at > 0 and now_epoch is not None:
            max_age_sec = max((min(30, max(1, int(r.get("max_age_minutes") or 5))) for r in state.radars), default=5) * 60
            if (now_epoch - state.processed_at) > max_age_sec:
                return False
        return True

    def take_due(self, now: Optional[float] = None, now_epoch: Optional[float] = None) -> Optional[ScheduledScan]:
        now_val = self.monotonic_fn() if now is None else now
        self._clean_heap_top()
        if not self._heap or self._heap[0][0] > now_val:
            return None

        # Pop the earliest due candidate; if retry burst limit is reached, check if a regular due item exists.
        skipped_retries: list[SourceState] = []
        chosen: Optional[SourceState] = None

        while self._heap and self._heap[0][0] <= now_val:
            due_at, generation, key = heapq.heappop(self._heap)
            if not self._is_heap_entry_valid((due_at, generation, key)):
                continue
            state = self.sources[key]
            if state.retry_attempt > 0 and self._retry_burst >= self.max_retry_burst:
                skipped_retries.append(state)
                continue
            chosen = state
            break

        if chosen is None:
            if not skipped_retries:
                return None
            # No regular due item available; take the earliest retry
            chosen = skipped_retries.pop(0)

        for retry_state in skipped_retries:
            self._push_heap(retry_state)

        self._retry_burst = self._retry_burst + 1 if chosen.retry_attempt else 0
        chosen.http_in_flight = True
        chosen.pending_rescan = False
        chosen.last_started = now_val
        fast_skip_allowed = self.can_fast_skip(chosen, now_val, now_epoch=now_epoch)
        task = ScheduledScan(
            key=chosen.key,
            radars=tuple(chosen.radars),
            scheduled_at=chosen.due_at,
            started_at=now_val,
            retry_attempt=chosen.retry_attempt,
            generation=chosen.generation,
            can_fast_skip=fast_skip_allowed,
            last_top_ids=chosen.last_top_ids,
            group_revision=chosen.group_revision,
            previous_processed_at=chosen.processed_at,
        )
        self.running[chosen.key] = task
        return task

    def mark_http_finished_fanout_pending(self, task: ScheduledScan, now: Optional[float] = None) -> bool:
        """Transition a running scan from HTTP phase to DB fanout phase."""
        now_val = self.monotonic_fn() if now is None else now
        if self.running.get(task.key) is not task:
            return False
        state = self.sources.get(task.key)
        if state is None or state.generation != task.generation:
            self.running.pop(task.key, None)
            return False
        state.http_in_flight = False
        state.fanout_pending = True
        if now_val >= task.started_at + state.interval:
            state.pending_rescan = True
        return True

    def _apply_success(
        self,
        state: SourceState,
        task: ScheduledScan,
        now_val: float,
        *,
        top_ids: Optional[tuple[str, ...]] = None,
        fast_skipped: bool = False,
        processed_epoch: Optional[float] = None,
    ) -> None:
        state.last_success = now_val
        state.retry_attempt = 0
        state.backoff_until = 0.0
        if processed_epoch is not None:
            state.processed_at = processed_epoch
        if top_ids is not None:
            state.last_top_ids = tuple(top_ids[:3])
        if not fast_skipped:
            state.last_full_scan_at = now_val

        if state.source_failure_streak > 0:
            state.source_recovery_streak += 1
            if state.source_recovery_streak >= config.SOURCE_RECOVERY_SUCCESSES:
                # A healthy scan clears the whole streak: a source must not stay slow after one bad minute.
                state.source_failure_streak = 0
                state.source_recovery_streak = 0
        else:
            state.source_recovery_streak = 0

        if state.fixed_interval is None:
            state.interval = self.sample_interval()
        if state.pending_rescan:
            state.pending_rescan = False
            state.due_at = now_val
        else:
            # Anchor to the start, not completion; never replay missed ticks.
            state.due_at = max(now_val, task.started_at + state.interval)

    def _apply_failure(
        self,
        state: SourceState,
        task: ScheduledScan,
        now_val: float,
        *,
        retry_after: float = 0.0,
        retryable: bool = True,
        exhausted_proxies: Optional[bool] = None,
    ) -> None:
        self.failures += 1
        state.source_recovery_streak = 0
        should_source_backoff = exhausted_proxies if exhausted_proxies is not None else self.use_source_backoff
        if should_source_backoff:
            state.source_failure_streak += 1
            state.retry_attempt = task.retry_attempt + 1 if retryable else 0
            delay = min(
                float(config.SOURCE_BACKOFF_MAX),
                float(config.SOURCE_BACKOFF_BASE) * (2 ** min(state.source_failure_streak - 1, 16)),
            )
            if state.source_failure_streak >= config.SOURCE_SLOW_AFTER:
                # A link failing in a row must not hit Avito more often than a healthy link of its tariff.
                factor = min(state.source_failure_streak - 1, config.SOURCE_SLOW_MAX_FACTOR)
                delay = max(delay, state.interval * factor)
            state.backoff_until = now_val + max(delay, retry_after)
            state.due_at = state.backoff_until
            return

        if retryable and task.retry_attempt < self.max_retries:
            state.retry_attempt = task.retry_attempt + 1
            delay = min(self.retry_max_delay, self.retry_base_delay * 2 ** task.retry_attempt)
        else:
            state.retry_attempt = 0
            delay = state.interval
        state.due_at = now_val + max(delay, retry_after)

    def complete(
        self,
        task: ScheduledScan,
        now: Optional[float] = None,
        success: bool = True,
        retry_after: float = 0,
        retryable: bool = True,
        *,
        top_ids: Optional[tuple[str, ...]] = None,
        fast_skipped: bool = False,
        processed_epoch: Optional[float] = None,
        exhausted_proxies: Optional[bool] = None,
    ) -> None:
        now_val = self.monotonic_fn() if now is None else now
        if self.running.get(task.key) is not task:
            return
        state = self.sources.get(task.key)
        if state is None:
            del self.running[task.key]
            return
        if state.generation != task.generation:
            del self.running[task.key]
            self._push_heap(state)
            return
        self.completed += 1
        if success:
            self._apply_success(
                state,
                task,
                now_val,
                top_ids=top_ids,
                fast_skipped=fast_skipped,
                processed_epoch=processed_epoch,
            )
        else:
            self._apply_failure(
                state,
                task,
                now_val,
                retry_after=retry_after,
                retryable=retryable,
                exhausted_proxies=exhausted_proxies,
            )
        # Released last: a crash above leaves the task running, so abandon() can still recover the source.
        del self.running[task.key]
        state.http_in_flight = False
        state.fanout_pending = False
        self._push_heap(state)

    def abandon(self, task: ScheduledScan, now: Optional[float] = None) -> None:
        """Releases a scan whose worker crashed, so the source is retried later instead of lost until a restart."""
        now_val = self.monotonic_fn() if now is None else now
        if self.running.get(task.key) is not task:
            return
        del self.running[task.key]
        state = self.sources.get(task.key)
        if state is None:
            return
        state.http_in_flight = False
        state.fanout_pending = False
        if state.generation == task.generation:
            state.retry_attempt = 0
            state.due_at = now_val + float(config.SOURCE_BACKOFF_MAX)
        self._push_heap(state)

    def seconds_until_next(self, now: Optional[float] = None) -> float:
        now_val = self.monotonic_fn() if now is None else now
        self._clean_heap_top()
        if not self._heap:
            return 1.0
        return max(0.0, self._heap[0][0] - now_val)

    def full_coverage_age_seconds(self, now: Optional[float] = None) -> float:
        now_val = self.monotonic_fn() if now is None else now
        worst = self._least_covered()
        return max(0.0, now_val - self._covered_at(worst)) if worst else 0.0

    @staticmethod
    def _covered_at(state: SourceState) -> float:
        return state.last_success if state.last_success is not None else state.created_at

    def _least_covered(self) -> Optional[SourceState]:
        return min(self.sources.values(), key=self._covered_at, default=None)

    def least_covered_radar(self) -> Optional[int]:
        """The radar behind the coverage age: a blind zone is useless without knowing whose it is."""
        worst = self._least_covered()
        return worst.radars[0].get("id") if worst and worst.radars else None

    def snapshot(self, now: Optional[float] = None) -> dict:
        now_val = self.monotonic_fn() if now is None else now
        pending = [
            state
            for key, state in self.sources.items()
            if key not in self.running and not state.http_in_flight and not state.fanout_pending
        ]
        due = [state for state in pending if state.due_at <= now_val]
        http_running = sum(1 for s in self.sources.values() if s.http_in_flight)
        fanout_running = sum(1 for s in self.sources.values() if s.fanout_pending)
        backed_off = sum(1 for s in self.sources.values() if s.backoff_until > now_val)
        return {
            "sources": len(self.sources),
            "subscriptions": sum(len(state.radars) for state in self.sources.values()),
            "running": len(self.running),
            "http_in_flight": http_running,
            "fanout_pending": fanout_running,
            "due": len(due),
            "deferred_retries": sum(state.retry_attempt > 0 for state in pending),
            "backed_off_sources": backed_off,
            "oldest_due_seconds": max((now_val - state.due_at for state in due), default=0.0),
            "max_revisit_seconds": max(
                (now_val - state.last_started for state in self.sources.values() if state.last_started is not None),
                default=0.0,
            ),
            "full_coverage_age_seconds": self.full_coverage_age_seconds(now_val),
            "least_covered_radar": self.least_covered_radar(),
            "capacity_mode": self.capacity_mode,
            "interval_bounds": [self.interval_low, self.interval_high],
            "completed": self.completed,
            "failures": self.failures,
        }
