"""Screenshots and ESC-closed popups: the game-state methods against a scripted bridge, the files the
MCP tool may hand over, and the Slack side that uploads them. No test touches the real game window
(conftest replaces civ_mcp.screen's window calls)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import claude_runs  # noqa: E402
from civ_mcp import game_state, screen  # noqa: E402
from civ_mcp.game_state import GameState  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Conn:
    """Scripted bridge: each Lua chunk is answered by the first rule whose key it contains."""

    def __init__(self):
        self.rules: list[tuple[str, object]] = []
        self.sent: list[str] = []

    def on(self, key: str, lines):
        self.rules.append((key, lines))

    async def execute(self, code, timeout=None):
        self.sent.append(code)
        for key, lines in self.rules:
            if key in code:
                return lines() if callable(lines) else lines
        return []


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(game_state, "_ESCAPE_SETTLE_S", 0)
    monkeypatch.setattr(game_state, "_CAMERA_SETTLE_S", 0)


def _popups(conn: _Conn, *answers: list[str]) -> None:
    """What the popup query answers, call by call; the last answer repeats."""
    left = list(answers)
    conn.on("DISMISSED|", lambda: left.pop(0) if len(left) > 1 else left[0])


# --- dismiss_popup ---------------------------------------------------------


def test_escape_is_pressed_once_per_queued_popup(game_window):
    conn = _Conn()
    _popups(
        conn,
        ["VISIBLE|TechCivicCompletedPopup"],
        ["VISIBLE|TechCivicCompletedPopup"],
        ["VISIBLE|EraCompletePopup"],
        [],
    )
    text = _run(GameState(conn).dismiss_popup())
    assert game_window.keys == [screen.VK_ESCAPE] * 3
    assert text == "Dismissed: TechCivicCompletedPopup, EraCompletePopup."


def test_no_escape_without_a_popup(game_window):
    conn = _Conn()
    _popups(conn, ["DISMISSED|BoostUnlockedPopup"])
    assert _run(GameState(conn).dismiss_popup()) == "Dismissed: BoostUnlockedPopup."
    assert game_window.keys == []


def test_a_diplomacy_screen_is_never_escaped(game_window):
    conn = _Conn()
    _popups(conn, ["VISIBLE|NaturalWonderPopup", "PENDING|DiplomacyActionView"])
    text = _run(GameState(conn).dismiss_popup())
    assert game_window.keys == []
    assert "NaturalWonderPopup" in text and "respond_to_diplomacy" in text


def test_the_pause_menu_an_escape_raised_is_put_away(game_window):
    # The era popup closed by itself (its animation ended) just before the key arrived.
    conn = _Conn()
    _popups(conn, ["VISIBLE|EraCompletePopup"], ["MENU"])
    text = _run(GameState(conn).dismiss_popup())
    assert game_window.keys == [screen.VK_ESCAPE] * 2
    assert text == "Dismissed: EraCompletePopup."


def test_a_popup_that_ignores_escape_is_reported(game_window):
    conn = _Conn()
    _popups(conn, ["VISIBLE|WonderBuiltPopup"])
    text = _run(GameState(conn).dismiss_popup())
    assert len(game_window.keys) == game_state._MAX_ESCAPES
    assert "Still showing" in text and "WonderBuiltPopup" in text and "at the PC" in text


def test_no_game_window_is_reported_not_raised(game_window):
    game_window.error = screen.ScreenError("No Civilization VI window found. Is the game running?")
    conn = _Conn()
    _popups(conn, ["VISIBLE|EraReviewPopup"])
    text = _run(GameState(conn).dismiss_popup())
    assert "No Civilization VI window found" in text and "EraReviewPopup" in text


# --- take_screenshot -------------------------------------------------------


def test_screenshot_moves_the_camera_hides_the_hud_and_puts_it_back(game_window, tmp_path):
    conn = _Conn()
    conn.on("UI.LookAtPlot", ["CAMERA"])
    conn.on("MCP_HUD = nil", ["RESTORED"])
    text = _run(GameState(conn).take_screenshot(tmp_path / "a.jpg", 5, 17, zoom=0.25))
    camera = next(c for c in conn.sent if "UI.LookAtPlot" in c)
    assert "local x, y = 5, 17 " in camera and "UI.LookAtPlot(x, y)" in camera
    assert "UI.SetMapZoom(0.250, 0.0, 0.0)" in camera
    assert "MCP_HUD[n] = c:IsHidden() c:SetHide(true)" in camera
    assert game_window.captures == [tmp_path / "a.jpg"]
    assert "MCP_HUD = nil" in conn.sent[-1]  # restored after the picture
    assert str(tmp_path / "a.jpg") in text and "(5,17)" in text and "panels hidden" in text


def test_screenshot_with_the_hud_hides_nothing(game_window, tmp_path):
    conn = _Conn()
    conn.on("UI.SetMapZoom", ["CAMERA"])
    text = _run(GameState(conn).take_screenshot(tmp_path / "a.jpg", show_hud=True))
    assert not any("MCP_HUD" in c for c in conn.sent)
    assert not any("UI.LookAtPlot" in c for c in conn.sent)
    assert "with the interface panels" in text and "as the camera stood" in text


def test_the_hud_comes_back_when_the_picture_fails(monkeypatch, tmp_path):
    conn = _Conn()
    conn.on("UI.LookAtPlot", ["CAMERA"])

    def no_picture(path, window=None, quality=0):
        raise screen.ScreenError("The picture of the game window came out blank.")

    monkeypatch.setattr(screen, "capture", no_picture)
    with pytest.raises(screen.ScreenError):
        _run(GameState(conn).take_screenshot(tmp_path / "a.jpg", 5, 17))
    assert "MCP_HUD = nil" in conn.sent[-1]


def test_screenshot_names_what_still_covers_the_map(game_window, tmp_path):
    conn = _Conn()
    conn.on("UI.LookAtPlot", ["COVER|PlayerChange", "CAMERA"])
    text = _run(GameState(conn).take_screenshot(tmp_path / "a.jpg", 5, 17))
    assert "Still covering the map in the picture: PlayerChange." in text


def test_screenshot_wants_both_coordinates(game_window, tmp_path):
    with pytest.raises(ValueError):
        _run(GameState(_Conn()).take_screenshot(tmp_path / "a.jpg", x=5))
    assert game_window.captures == []


# --- the files send_screenshot may hand over -------------------------------


def test_only_a_jpg_in_the_screenshots_folder_is_a_screenshot(screenshots):
    shot = screenshots / "amanitore_turn56_132500.jpg"
    shot.write_bytes(b"jpg")
    (screenshots / "notes.txt").write_text("x")
    outside = screenshots.parent / "secret.jpg"
    outside.write_bytes(b"jpg")
    assert screen.screenshot_named(shot.name) == shot
    assert screen.screenshot_named(str(shot)) == shot
    assert screen.screenshot_named("notes.txt") is None
    assert screen.screenshot_named("missing.jpg") is None
    # A path elsewhere is cut down to its name, which is not in the folder.
    assert screen.screenshot_named(str(outside)) is None
    assert screen.screenshot_named(r"..\secret.jpg") is None


def test_sent_screenshot_reads_only_the_servers_own_answer(screenshots):
    shot = screenshots / "a.jpg"
    shot.write_bytes(b"jpg")
    assert screen.sent_screenshot(f"{screen.SENT_PREFIX}{shot}\nThe Slack bot uploads it.") == shot
    assert screen.sent_screenshot(f"Not sent: this session was not started from Slack. The file is {shot}.") is None
    assert screen.sent_screenshot(f"{screen.SENT_PREFIX}C:\\Users\\x\\.env") is None


def test_clearing_removes_pictures_and_the_delivery_marker(screenshots):
    (screenshots / "a.jpg").write_bytes(b"jpg")
    screen.set_delivery(True)
    assert screen.delivery_on()
    screen.clear_screenshots()
    assert list(screenshots.iterdir()) == []
    assert not screen.delivery_on()


# --- the Slack side --------------------------------------------------------


def _send(tool_id: str, file: str, caption: str) -> dict:
    block = {
        "type": "tool_use",
        "id": tool_id,
        "name": "mcp__civ6__send_screenshot",
        "input": {"file": file, "caption": caption},
    }
    return {"type": "assistant", "message": {"content": [block]}}


def _result(tool_id: str, text: str) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": [{"type": "text", "text": text}]}
    return {"type": "user", "message": {"content": [block]}}


def test_a_sent_screenshot_becomes_an_upload_step(screenshots):
    shot = screenshots / "a.jpg"
    shot.write_bytes(b"jpg")
    steps = claude_runs.Steps()
    list(steps.feed(_send("t1", "a.jpg", "Meroe  and\nthe river")))
    out = list(steps.feed(_result("t1", f"{screen.SENT_PREFIX}{shot}\nThe Slack bot uploads it.")))
    assert out == [(claude_runs.UPLOAD, f"{shot}\nMeroe and the river")]


def test_a_refused_send_uploads_nothing(screenshots):
    steps = claude_runs.Steps()
    list(steps.feed(_send("t1", "a.jpg", "")))
    out = list(steps.feed(_result("t1", "Not sent: this session was not started from Slack, so there is no DM.")))
    assert [kind for kind, _ in out] == ["error"]


def test_what_claude_asked_to_send_is_not_what_gets_uploaded(screenshots):
    # The tool call names a file outside the folder; only the server's answer counts, and it refused.
    steps = claude_runs.Steps()
    list(steps.feed(_send("t1", r"C:\repo\.env", "")))
    out = list(steps.feed(_result("t1", "Error: no screenshot named 'C:\\repo\\.env'.")))
    assert [kind for kind, _ in out] == ["error"]


def test_screenshot_steps_read_well():
    steps = claude_runs.Steps()
    assert steps._action("take_screenshot", {"x": 5, "y": 17, "zoom": 0.3}, "") == "Screenshot of (5,17)"
    assert steps._action("take_screenshot", {}, "") == "Screenshot of the current view"


def test_upload_goes_to_the_runs_conversation(screenshots):
    calls = []

    class Client:
        def files_upload_v2(self, **kwargs):
            calls.append(kwargs)

    shot = screenshots / "a.jpg"
    claude_runs.upload_screenshot(Client(), "D123", None, shot, "Meroe")
    assert calls == [
        {
            "channel": "D123",
            "thread_ts": None,
            "file": str(shot),
            "filename": "a.jpg",
            "title": "Meroe",
            "initial_comment": "Meroe",
        }
    ]
