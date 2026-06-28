"""Tests for execution.epic_detection config block (issue #455).

Coverage:
- Dispatcher _resolve_epic_config: parsing, defaults, validation, soft warnings
- is_epic(): backward compat (no config = module constants) + config overrides
- _planner_body(): reads config thresholds for reason string
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.providers.base import is_epic
from conftest import _load_dispatch

disp = _load_dispatch()


def _make_issue(body: str = "", labels=None, title: str = "T", number: int = 1) -> dict:
    return {"number": number, "title": title, "body": body,
            "labels": labels if labels is not None else [],
            "url": "https://example.com/issues/1"}


# ── _resolve_epic_config: parsing & defaults ───────────────────────────────────


class TestResolveEpicConfig:
    def test_empty_execution_returns_defaults(self):
        out = disp._resolve_epic_config({})
        # dispatcher config defaults differ from module constants (6/1000 vs 4/2000)
        assert out == {
            "enabled": True,
            "min_deliverables": 6,
            "size_threshold": 1000,
            "epic_label": "epic",
            "child_label": "subtask",
        }

    def test_none_execution_returns_defaults(self):
        out = disp._resolve_epic_config(None)
        assert out["enabled"] is True
        assert out["min_deliverables"] == 6

    def test_missing_epic_detection_returns_defaults(self):
        out = disp._resolve_epic_config({"epic_detection": None})
        assert out["min_deliverables"] == 6

    def test_missing_block_returns_defaults(self):
        out = disp._resolve_epic_config({"max_dispatch": 5})
        assert out == disp._resolve_epic_config({})

    def test_partial_override_preserves_other_defaults(self):
        out = disp._resolve_epic_config({"epic_detection": {"min_deliverables": 10}})
        assert out["min_deliverables"] == 10
        assert out["size_threshold"] == 1000
        assert out["epic_label"] == "epic"
        assert out["child_label"] == "subtask"

    def test_full_override(self):
        out = disp._resolve_epic_config({"epic_detection": {
            "enabled": False,
            "min_deliverables": 12,
            "size_threshold": 5000,
            "epic_label": "Mega",
            "child_label": "ChildTask",
        }})
        assert out == {
            "enabled": False,
            "min_deliverables": 12,
            "size_threshold": 5000,
            "epic_label": "mega",
            "child_label": "childtask",
        }

    def test_enabled_coerced_from_string(self):
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "false"}})["enabled"] is False
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "False"}})["enabled"] is False
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "yes"}})["enabled"] is True
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "1"}})["enabled"] is True
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "on"}})["enabled"] is True
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "0"}})["enabled"] is False
        assert disp._resolve_epic_config({"epic_detection": {"enabled": "no"}})["enabled"] is False

    def test_enabled_coerced_from_int(self):
        assert disp._resolve_epic_config({"epic_detection": {"enabled": 0}})["enabled"] is False
        assert disp._resolve_epic_config({"epic_detection": {"enabled": 1}})["enabled"] is True

    def test_labels_lowercased(self):
        out = disp._resolve_epic_config(
            {"epic_detection": {"epic_label": "EPIC-ISH", "child_label": "SubTask"}}
        )
        assert out["epic_label"] == "epic-ish"
        assert out["child_label"] == "subtask"

    def test_unknown_keys_ignored(self):
        out = disp._resolve_epic_config(
            {"epic_detection": {"unknown_thing": 123, "min_deliverables": 7}}
        )
        assert "unknown_thing" not in out
        assert out["min_deliverables"] == 7


# ── validation: soft (log warning + fall back to default) ─────────────────────


class TestValidationWarnings:
    def test_min_deliverables_below_1_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"min_deliverables": 0}})
        assert out["min_deliverables"] == 6
        assert any("min_deliverables" in r.message for r in caplog.records)

    def test_min_deliverables_negative_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"min_deliverables": -5}})
        assert out["min_deliverables"] == 6

    def test_min_deliverables_non_int_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"min_deliverables": "not-a-number"}})
        assert out["min_deliverables"] == 6

    def test_size_threshold_below_100_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"size_threshold": 50}})
        assert out["size_threshold"] == 1000
        assert any("size_threshold" in r.message for r in caplog.records)

    def test_size_threshold_non_int_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"size_threshold": None}})
        assert out["size_threshold"] == 1000

    def test_empty_label_string_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"epic_label": "   "}})
        assert out["epic_label"] == "epic"

    def test_non_string_label_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = disp._resolve_epic_config({"epic_detection": {"child_label": 123}})
        assert out["child_label"] == "subtask"


# ── is_epic: backward compat (no config = module constants) ───────────────────


class TestIsEpicBackwardCompat:
    """No epic_config passed — is_epic must use module constants (4 / 2000) and
    preserve existing behavior exactly."""

    def test_four_checklist_items_is_epic(self):
        body = "\n".join("- [ ] t" + str(i) for i in range(4))
        assert is_epic(_make_issue(body=body)) is True

    def test_three_checklist_items_not_epic(self):
        body = "\n".join("- [ ] t" + str(i) for i in range(3))
        assert is_epic(_make_issue(body=body)) is False

    def test_2000_char_body_is_epic(self):
        assert is_epic(_make_issue(body="x" * 2000)) is True

    def test_1999_char_body_not_epic(self):
        assert is_epic(_make_issue(body="x" * 1999)) is False

    def test_epic_label_still_matches(self):
        assert is_epic(_make_issue(labels=[{"name": "epic"}])) is True
        assert is_epic(_make_issue(labels=[{"name": "EPIC"}])) is True

    def test_subtask_label_excludes(self):
        assert is_epic(_make_issue(body="x" * 5000, labels=[{"name": "subtask"}])) is False


# ── is_epic: config overrides ─────────────────────────────────────────────────


class TestIsEpicConfigOverrides:
    def test_config_raises_checklist_threshold(self):
        # 4 items = epic under module constant, NOT epic when min_deliverables=10
        body = "\n".join("- [ ] t" + str(i) for i in range(4))
        cfg = {"enabled": True, "min_deliverables": 10, "size_threshold": 1000,
               "epic_label": "epic", "child_label": "subtask"}
        assert is_epic(_make_issue(body=body), epic_config=cfg) is False

    def test_config_lowered_checklist_threshold(self):
        # 2 items not epic under module constant, epic when min_deliverables=2
        body = "\n".join("- [ ] t" + str(i) for i in range(2))
        cfg = {"enabled": True, "min_deliverables": 2, "size_threshold": 100,
               "epic_label": "epic", "child_label": "subtask"}
        assert is_epic(_make_issue(body=body), epic_config=cfg) is True

    def test_config_raises_body_size_threshold(self):
        # 1500 chars not epic under module constant, epic under config threshold=1200
        cfg = {"enabled": True, "min_deliverables": 6, "size_threshold": 1200,
               "epic_label": "epic", "child_label": "subtask"}
        assert is_epic(_make_issue(body="x" * 1500), epic_config=cfg) is True

    def test_config_raises_size_to_exclude_body(self):
        # 1500 chars normally epic under module constant (2000), not epic with higher threshold
        cfg = {"enabled": True, "min_deliverables": 6, "size_threshold": 5000,
               "epic_label": "epic", "child_label": "subtask"}
        assert is_epic(_make_issue(body="x" * 1500), epic_config=cfg) is False

    def test_custom_epic_label(self):
        cfg = {"enabled": True, "min_deliverables": 6, "size_threshold": 1000,
               "epic_label": "mega", "child_label": "subtask"}
        # 'epic' label doesn't match when reconfigured
        assert is_epic(_make_issue(labels=[{"name": "epic"}]), epic_config=cfg) is False
        assert is_epic(_make_issue(labels=[{"name": "mega"}]), epic_config=cfg) is True
        assert is_epic(_make_issue(labels=[{"name": "MEGA"}]), epic_config=cfg) is True

    def test_custom_child_label_excludes(self):
        cfg = {"enabled": True, "min_deliverables": 6, "size_threshold": 100,
               "epic_label": "epic", "child_label": "child-task"}
        # custom child_label excludes, old 'subtask' no longer guards
        assert is_epic(
            _make_issue(body="x" * 5000, labels=[{"name": "child-task"}]),
            epic_config=cfg,
        ) is False
        assert is_epic(
            _make_issue(body="x" * 5000, labels=[{"name": "subtask"}]),
            epic_config=cfg,
        ) is True  # old guard label no longer effective

    def test_enabled_false_short_circuits_all_heuristics(self):
        cfg = {"enabled": False, "min_deliverables": 1, "size_threshold": 100,
               "epic_label": "epic", "child_label": "subtask"}
        # Every other heuristic would individually fire
        big_body = "x" * 5000
        big_checklist = "\n".join("- [ ] t" + str(i) for i in range(20))
        assert is_epic(_make_issue(body=big_body), epic_config=cfg) is False
        assert is_epic(_make_issue(body=big_checklist), epic_config=cfg) is False
        assert is_epic(_make_issue(labels=[{"name": "epic"}]), epic_config=cfg) is False
        assert is_epic(
            _make_issue(body=big_body, labels=[{"name": "epic"}]),
            epic_config=cfg,
        ) is False

    def test_enabled_none_uses_defaults_for_other_keys(self):
        # enabled absent → treated as True, other values honored
        cfg = {"min_deliverables": 2, "size_threshold": 100,
               "epic_label": "epic", "child_label": "subtask"}
        body = "\n".join("- [ ] t" + str(i) for i in range(2))
        assert is_epic(_make_issue(body=body), epic_config=cfg) is True


# ── _planner_body reads config ────────────────────────────────────────────────


class TestPlannerBodyReadsConfig:
    def test_default_threshold_5_legacy_when_no_config(self):
        # No epic_config → legacy threshold 5, so 4 items is not "checklist"
        issue = _make_issue(body="\n".join("- [ ] t" + str(i) for i in range(4)))
        body = disp._planner_body("o/r", issue, "/w", "main", "github")
        assert "checklist" not in body.lower()

    def test_default_threshold_5_legacy_when_5_items(self):
        # 5 items with legacy threshold → reason includes checklist
        issue = _make_issue(body="\n".join("- [ ] t" + str(i) for i in range(5)))
        body = disp._planner_body("o/r", issue, "/w", "main", "github")
        assert "checklist" in body.lower()

    def test_config_custom_checklist_threshold(self):
        # 4 items — not enough at default (5), enough when config min_deliverables=4
        issue = _make_issue(body="\n".join("- [ ] t" + str(i) for i in range(4)))
        cfg = {"enabled": True, "min_deliverables": 4, "size_threshold": 1000,
               "epic_label": "epic", "child_label": "subtask"}
        body = disp._planner_body("o/r", issue, "/w", "main", "github",
                                  epic_config=cfg)
        assert "checklist" in body.lower()

    def test_config_custom_epic_label_in_reason(self):
        issue = _make_issue(labels=[{"name": "mega"}])
        cfg = {"enabled": True, "min_deliverables": 6, "size_threshold": 1000,
               "epic_label": "mega", "child_label": "subtask"}
        body = disp._planner_body("o/r", issue, "/w", "main", "github",
                                  epic_config=cfg)
        assert "epic-label" in body.lower()

    def test_config_custom_epic_label_standard_name_does_not_match(self):
        issue = _make_issue(labels=[{"name": "epic"}])
        cfg = {"enabled": True, "min_deliverables": 6, "size_threshold": 1000,
               "epic_label": "mega", "child_label": "subtask"}
        body = disp._planner_body("o/r", issue, "/w", "main", "github",
                                  epic_config=cfg)
        # "epic" label doesn't fire when configured epic_label is "mega"
        assert "epic-label" not in body.lower()
