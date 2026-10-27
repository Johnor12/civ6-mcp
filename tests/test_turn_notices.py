"""Timer notices, takeover approvals and screenshots around a Slack-started run."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import screenshots  # noqa: E402
import slack_setup  # noqa: E402
import turn_notices as tn  # noqa: E402

LIMIT = 2 * 3600


class TestDueActions:
    def test_nothing_before_the_first_hour(self):
        assert tn.due_actions(tn.Notice("k"), 3599, LIMIT) == []

    def test_ping_then_warn(self):
        n = tn.Notice("k")
        assert tn.due_actions(n, 3600, LIMIT) == ["ping"]
        assert tn.due_actions(n, LIMIT, LIMIT) == ["ping", "warn"]

    def test_done_actions_do_not_repeat(self):
        n = tn.Notice("k", pinged=True, warned=True)
        assert tn.due_actions(n, LIMIT * 2, LIMIT) == []


class TestApprovals:
    def test_owner_alone_is_enough(self):
        assert tn.approval_met({"UOWNER"}, "UOWNER", 3)

    def test_three_other_players(self):
        assert not tn.approval_met({"UA", "UB"}, "UOWNER", 3)
        assert tn.approval_met({"UA", "UB", "UC"}, "UOWNER", 3)

    def test_one_needed_restores_a_single_player_takeover(self):
        assert tn.approval_met({"UA"}, "UOWNER", 1)

    def test_unknown_owner(self):
        assert not tn.approval_met({"UA"}, None, 3)


class TestStore:
    def test_state_survives_a_restart(self, tmp_path):
        path = tmp_path / "notices.json"
        key = tn.turn_key(64, "LEADER_X", "Ray")
        a = tn.TurnNotices(path)
        a.update(key, pinged=True, request_ts="123.4")
        a.add_approval(key, "UA")
        a.add_approval(key, "UA")  # the same person twice counts once
        a.add_approval(key, "UB")
        b = tn.TurnNotices(path).get(key)
        assert b.pinged and not b.warned and b.request_ts == "123.4"
        assert b.approvers == ["UA", "UB"]

    def test_a_new_turn_starts_clean(self, tmp_path):
        store = tn.TurnNotices(tmp_path / "notices.json")
        store.update("old", pinged=True)
        store.add_approval("old", "UA")
        fresh = store.get("new")
        assert fresh == tn.Notice("new")

    def test_clear_approvals(self, tmp_path):
        store = tn.TurnNotices(tmp_path / "notices.json")
        store.add_approval("k", "UA")
        store.clear_approvals("k")
        assert store.get("k").approvers == []

    def test_bad_file_is_ignored(self, tmp_path):
        path = tmp_path / "notices.json"
        path.write_text("not json")
        assert tn.TurnNotices(path).get("k") == tn.Notice("k")


class TestShotSession:
    @staticmethod
    def grabber(taken, fail=()):
        def grab(label):
            taken.append(label)
            return None if label in fail else Path(f"/shots/{label}.png")

        return grab

    def test_pre_mid_and_post_are_capped(self):
        taken: list[str] = []
        s = screenshots.ShotSession(self.grabber(taken), interval_s=0.01, max_mid=2)
        s.start()
        time.sleep(0.2)
        s.finish()
        assert taken == ["pre", "mid1", "mid2", "post"]
        assert [p.stem for p in s.all()] == ["pre", "mid1", "mid2", "post"]

    def test_a_fast_run_gets_only_the_first_and_last(self):
        taken: list[str] = []
        s = screenshots.ShotSession(self.grabber(taken), interval_s=60, max_mid=2)
        s.start()
        s.finish()
        assert taken == ["pre", "post"] and s.mid == []

    def test_a_failed_grab_is_skipped(self):
        s = screenshots.ShotSession(self.grabber([], fail={"pre"}), interval_s=60)
        s.start()
        s.finish()
        assert s.pre is None and [p.stem for p in s.all()] == ["post"]

    def test_grab_screen_without_mss_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "mss", None)  # import fails
        assert screenshots.grab_screen("x", tmp_path) is None


class TestScopes:
    def test_all_present(self):
        have = slack_setup.REQUIRED_SCOPES | set(slack_setup.OPTIONAL_SCOPES)
        assert slack_setup.scope_problems(have) == ([], [])

    def test_missing_required_is_fatal(self):
        fatal, warnings = slack_setup.scope_problems({"chat:write", "files:write"})
        assert any("im:history" in f for f in fatal) and not warnings

    def test_missing_files_write_only_warns(self):
        fatal, warnings = slack_setup.scope_problems(set(slack_setup.REQUIRED_SCOPES))
        assert not fatal and "files:write" in warnings[0]

    def test_parse_header(self):
        assert slack_setup.parse_scopes("a, b,,c") == {"a", "b", "c"}
        assert slack_setup.parse_scopes(None) == set()
