"""Session affinity must work through the real production call path.

Existing affinity tests hand-write the canonical three-part key, which hides
the fact that request handling records ``Candidate.key`` (two parts) while the
authoritative RouterEngine reads ``ResourceRef.key`` (three parts).  This test
drives the same sequence production uses: order candidates, remember the
selected candidate exactly as the handlers do, then order again.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from router import SmartRouter


def _router() -> SmartRouter:
    return SmartRouter({
        "smart_scheduler": {
            "enabled": True,
            "mode": "active",
            "weights": {"session_affinity_factor": 1.0},
        },
        "routes": {
            "chat": {
                "strategy": "priority",
                "candidates": [
                    {"upstream": "a", "model": "m1"},
                    {"upstream": "b", "model": "m2"},
                    {"upstream": "c", "model": "m3"},
                ],
                "fallback": [],
            }
        },
        "logging": {"level": "CRITICAL"},
    })


async def test_affinity_recorded_by_handlers_pins_next_selection():
    router = _router()
    router._ensure_scoring()

    first = await router._candidate_order("chat", conversation_thread="s1")
    assert [c.key for c in first] == ["a:m1", "b:m2", "c:m3"]

    # Production pins the last candidate exactly as the handlers do now.
    chosen = first[-1]
    router._remember_session_affinity("s1", chosen)

    second = await router._candidate_order("chat", conversation_thread="s1")

    assert second[0].key == "c:m3", (
        "session affinity lost: handler-recorded key and engine lookup key "
        f"disagree (ordered {[c.key for c in second]})"
    )

    # Other threads are unaffected.
    other = await router._candidate_order("chat", conversation_thread="s2")
    assert [c.key for c in other] == ["a:m1", "b:m2", "c:m3"]
