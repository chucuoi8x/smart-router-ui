"""Retry budget theo README §21 — quyết định tài nguyên, không chỉ error handling."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetryBudget:
    retry_policy: object
    estimated_input_tokens: int = 0
    attempts_used: int = 1
    extra_input_tokens_used: int = 0
    extra_latency_ms_used: int = 0

    def can_retry(self, kind: str | None, *, extra_input_tokens: int = 0, extra_latency_ms: int = 0) -> bool:
        if not kind:
            return False
        policy = self.retry_policy
        allowed = getattr(policy, "retryable_errors", None)
        if allowed and kind not in allowed:
            return False
        max_attempts = int(getattr(policy, "max_attempts", 0) or 0)
        if max_attempts <= 0:
            return False
        if self.attempts_used >= max_attempts:
            return False
        max_tokens = int(getattr(policy, "max_extra_input_tokens", 0) or 0)
        max_latency = int(getattr(policy, "max_extra_latency_ms", 0) or 0)
        if max_tokens and (self.extra_input_tokens_used + int(extra_input_tokens)) > max_tokens:
            return False
        if max_latency and (self.extra_latency_ms_used + int(extra_latency_ms)) > max_latency:
            return False
        return True

    def record_retry(self, *, extra_input_tokens: int = 0, extra_latency_ms: int = 0) -> None:
        self.attempts_used += 1
        self.extra_input_tokens_used += int(extra_input_tokens or 0)
        self.extra_latency_ms_used += int(extra_latency_ms or 0)
