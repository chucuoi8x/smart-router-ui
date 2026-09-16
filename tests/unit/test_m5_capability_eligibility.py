"""RED tests cho capability eligibility M5 §18.2 / Step 93.

Yêu cầu:
- constraints.require_capabilities:
    - "none"   -> không lọc gì (compat)
    - "auto"   -> chỉ lọc khi candidate có field capability* rõ ràng là False/missing
    - "strict" -> loại candidate nếu thiếu hỗ trợ tools/vision/request cần
- filter phải provider-agnostic, chỉ đọc metadata candidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.gateway.routing.presets import PolicyConstraints, PolicyPreset, filter_candidates_for_policy


@dataclass(frozen=True)
class FakeCandidate:
    name: str
    metadata: dict = field(default_factory=dict)


def _preset(**overrides) -> PolicyPreset:
    return PolicyPreset(name="test", constraints=PolicyConstraints(**overrides))


def test_strict_requires_tools_support():
    candidates = [
        FakeCandidate("no-tools", {"capabilities": {"tools": False, "vision": True}}),
        FakeCandidate("has-tools", {"capabilities": {"tools": True, "vision": True}}),
        FakeCandidate("unknown", {}),  # thiếu metadata -> strict vẫn loại
    ]
    accepted = filter_candidates_for_policy(
        candidates,
        _preset(require_capabilities="strict"),
        required_capabilities={"tools": True},
    )
    assert [c.name for c in accepted] == ["has-tools"]


def test_strict_requires_vision_support():
    candidates = [
        FakeCandidate("no-vision", {"capabilities": {"vision": False}}),
        FakeCandidate("vision", {"capabilities": {"vision": True}}),
        FakeCandidate("unknown", {}),
    ]
    accepted = filter_candidates_for_policy(
        candidates,
        _preset(require_capabilities="strict"),
        required_capabilities={"vision": True},
    )
    assert [c.name for c in accepted] == ["vision"]


def test_auto_does_not_block_when_required_false():
    candidates = [
        FakeCandidate("no-vision", {"capabilities": {"vision": False}}),
        FakeCandidate("empty", {}),
    ]
    accepted = filter_candidates_for_policy(
        candidates,
        _preset(require_capabilities="auto"),
        required_capabilities={"vision": False},
    )
    # không yêu cầu vision -> không lọc gì
    assert {c.name for c in accepted} == {"no-vision", "empty"}


def test_none_mode_never_filters_capability():
    candidates = [
        FakeCandidate("no-tools", {"capabilities": {"tools": False}}),
        FakeCandidate("unknown", {}),
    ]
    accepted = filter_candidates_for_policy(
        candidates,
        _preset(require_capabilities="none"),
        required_capabilities={"tools": True},
    )
    assert {c.name for c in accepted} == {"no-tools", "unknown"}


def test_context_window_eligibility_strict():
    candidates = [
        FakeCandidate("small", {"capabilities": {"max_context_tokens": 8000}}),
        FakeCandidate("large", {"capabilities": {"max_context_tokens": 128000}}),
        FakeCandidate("unknown", {}),
    ]
    accepted = filter_candidates_for_policy(
        candidates,
        _preset(require_capabilities="strict"),
        required_capabilities={"min_context_tokens": 50000},
    )
    assert [c.name for c in accepted] == ["large"]
