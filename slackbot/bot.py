"""The bot's logic. Slack, the game and the agent come in through small ports."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from slackbot import messages as msg
from slackbot.capture import Capture
from slackbot.config import Player, Settings
from slackbot.game import GameChecker
from slackbot.rules import GameStatus, approval_met, authorize, check_game_ready, due_actions
from slackbot.runner import DEFAULT_NOTE, AgentRunner, RunResult, split_for_slack
from slackbot.state import Request, Store
from slackbot.turn_tracker import ChannelMessage, TurnState, current_turn, next_in_order

log = logging.getLogger(__name__)

BUSY = "Claude is already playing a turn. Wait for it to end."


class SlackPort(Protocol):
    async def history(self, oldest: str) -> list[ChannelMessage]: ...
    async def post(
        self, text: str, *, thread_ts: str | None = None, blocks: list[dict[str, Any]] | None = None
    ) -> str: ...
    async def update(
        self, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None: ...
    async def dm(self, user_id: str, text: str) -> None: ...
    async def upload(self, path: Path, *, thread_ts: str, title: str) -> None: ...


@dataclass
class Approval:
    """Private reply for the clicker, plus the turn run to start (if any)."""

    reply: str
    job: Awaitable[None] | None = None


class CivBot:
    def __init__(
        self,
        settings: Settings,
        players: list[Player],
        store: Store,
        slack: SlackPort,
        game: GameChecker,
        runner: AgentRunner,
        capture: Capture,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.players = players
        self.order = [p.slack_id for p in players]
        self.by_id = {p.slack_id: p for p in players}
        self.store = store
        self.slack = slack
        self.game = game
        self.runner = runner
        self.capture = capture
        self.clock = clock
        self._messages: dict[str, ChannelMessage] = {}
        self._turn = TurnState()
        self._run_lock = asyncio.Lock()

    # -- turn tracking ------------------------------------------------------

    async def refresh_turn(self) -> TurnState:
        oldest = max(self._messages, key=float) if self._messages else self.settings.history_start_ts
        for m in await self.slack.history(oldest):
            self._messages[m.ts] = m
        self._turn = current_turn(
            list(self._messages.values()), self.order, self.settings.bare_mention_max_chars
        )
        for note in self._turn.anomalies[-3:]:
            log.warning("turn tracker: %s", note)
        if self._turn.player_id and self._turn.since_ts:
            self.store.sync_turn(self._turn.player_id, self._turn.since_ts)
        return self._turn

    # -- DMs ---------------------------------------------------------------

    async def handle_dm(self, user_id: str, text: str) -> None:
        text = text.strip()
        low = text.lower()
        if user_id not in self.by_id:
            await self.slack.dm(user_id, "You are not in the civ game.")
            return
        if not text or low in ("help", "?"):
            await self.slack.dm(user_id, msg.HELP)
            return
        turn = await self.refresh_turn()
        if low == "status":
            await self.slack.dm(user_id, await self._status_text(turn))
            return
        if low.split()[0] == "takeover":
            await self._set_takeover_prompt(user_id, text[len("takeover"):].strip())
            return
        game = await self._probe_game()
        if game is None:
            await self.slack.dm(user_id, BUSY)
            return
        decision = authorize(user_id, turn, self.players, game)
        if not decision.allowed:
            log.info("refused DM from %s: %s", user_id, decision.reason)
            await self.slack.dm(user_id, decision.reason)
            return
        await self.slack.dm(user_id, "Prompt accepted. Claude is starting your turn. I reply when it ends.")
        await self._run(decision.player, text, requested_by=user_id, source="dm", game=game)

    async def _probe_game(self) -> GameStatus | None:
        """Game status, or None while a run is on.

        A new bridge connection starts a higher command number, so the mod
        then skips every command from the agent's older connection.
        """
        if self._run_lock.locked():
            return None
        return await self.game.status()

    async def _status_text(self, turn: TurnState) -> str:
        game = await self._probe_game()
        who = self.by_id[turn.player_id].name if turn.player_id else "nobody"
        if game is None:
            return f"Slack turn: {who}.\nGame: not checked during a run.\nClaude is playing a turn now."
        return f"Slack turn: {who}.\nGame: {game.description or 'no answer'}\nNo run in progress."

    # -- running a turn -------------------------------------------------------

    async def _run(
        self, player: Player, note: str, *, requested_by: str, source: str, game: GameStatus
    ) -> None:
        if self._run_lock.locked():
            await self.slack.dm(requested_by.split(",")[0], BUSY)
            return
        async with self._run_lock:
            root = await self.slack.post(
                f"Claude is taking {msg.mention(player.slack_id)}'s turn ({player.leader}, {source}). "
                "Please keep off the civ machine until it ends."
            )
            shots: list[Path] = []
            await self._shot(shots, "pre")
            mid = asyncio.create_task(self._mid_shots(shots))
            try:
                result = await self.runner.run(game.leader, note)
            except Exception as e:  # noqa: BLE001 - never leave the lock or the channel hanging
                log.exception("run failed")
                result = RunResult(False, f"Internal error: {e}")
            finally:
                mid.cancel()
            await self._shot(shots, "post")
            self.store.log_run(player.slack_id, requested_by, source, note, result.ok, result.text)
            await self._report(player, result, root, shots)

    async def _shot(self, shots: list[Path], label: str) -> None:
        path = await self.capture.screenshot(label)
        if path is not None:
            shots.append(path)

    async def _mid_shots(self, shots: list[Path]) -> None:
        """A few screenshots while the turn runs, spaced by shot_interval_s."""
        for i in range(self.settings.max_mid_shots):
            await asyncio.sleep(self.settings.shot_interval_s)
            await self._shot(shots, f"mid{i + 1}")

    async def _report(
        self, player: Player, result: RunResult, root: str, shots: list[Path]
    ) -> None:
        pid = player.slack_id
        head = (
            f"Claude finished your turn ({player.leader})."
            if result.ok
            else f"Claude could not finish your turn ({player.leader})."
        )
        for i, chunk in enumerate(split_for_slack(msg.safe(result.text))):
            await self.slack.dm(pid, f"{head}\n\n{chunk}" if i == 0 else chunk)

        if result.ok:
            nxt = next_in_order(self.order, pid)
            await self.slack.post(f"{msg.mention(nxt)} your turn.")
            saves = ", ".join(s for s in (result.pre_turn_save, result.finished_save) if s)
            await self.slack.post(
                f"Claude played {player.name}'s turn. Saves: {saves or 'none reported'}.",
                thread_ts=root,
            )
        else:
            await self.slack.post(
                f"Claude did not finish {msg.mention(pid)}'s turn. A person must check the "
                f"civ machine.\n{msg.safe(result.text)[:500]}",
                thread_ts=root,
            )
        for path in shots:
            await self._upload(path, root, path.stem)
        if result.ok:
            await self.refresh_turn()

    async def _upload(self, path: Path, root: str, title: str) -> None:
        try:
            await self.slack.upload(path, thread_ts=root, title=title)
        except Exception as e:  # noqa: BLE001 - a lost image must not fail the report
            log.warning("upload of %s failed: %s", path, e)

    # -- timers and takeover ---------------------------------------------------

    async def tick(self) -> None:
        turn = await self.refresh_turn()
        if not turn.player_id or not turn.since_ts:
            return
        row = self.store.sync_turn(turn.player_id, turn.since_ts)
        player = self.by_id[turn.player_id]
        hours = int(self.settings.ping_after_s // 3600) or 1
        for action in due_actions(self.clock(), row, self.settings):
            if action == "ping":
                self.store.mark_turn(row.id, "pinged")
                await self.slack.post(
                    f"{msg.mention(player.slack_id)} your turn has been open for {hours} office hour. "
                    "After 2 office hours Claude can take it. DM me a prompt to start it now."
                )
            elif action == "warn":
                self.store.mark_turn(row.id, "warned")
                await self._open_request(row.id, player)
        req = self.store.open_request_for_turn(row.id)
        if req is not None and self._expired(req):
            await self._expire_request(req, player)

    async def _open_request(self, turn_id: int, player: Player, prompt: str = DEFAULT_NOTE) -> Request:
        req = self.store.create_request(
            uuid.uuid4().hex[:12], turn_id, prompt, self.clock()
        )
        ts = await self.slack.post(
            f"Takeover request for {player.name}",
            blocks=msg.approval_blocks(req, player, set(), self.settings.approvals_needed),
        )
        self.store.set_request_message(req.id, ts)
        return req

    async def _refresh_request_message(self, req: Request, player: Player) -> None:
        blocks = msg.approval_blocks(
            req, player, self.store.approvers(req.id), self.settings.approvals_needed
        )
        await self.slack.update(req.message_ts, f"Takeover request for {player.name}", blocks)

    async def _close_request_message(self, req: Request, player: Player, note: str) -> None:
        await self.slack.update(req.message_ts, msg.closed_text(req, player, note), [])

    def _expired(self, req: Request) -> bool:
        return self.settings.working_elapsed(req.created_at, self.clock()) > self.settings.approval_ttl_s

    async def _expire_request(self, req: Request, player: Player) -> None:
        if self.store.close_request(req.id, "expired"):
            await self._close_request_message(
                req, player, "expired. DM me `takeover <prompt>` to open a new request."
            )

    async def _request_context(self, req: Request) -> tuple[Player, TurnState] | None:
        """The request's player, if the turn is still theirs."""
        row = self.store.get_turn(req.turn_id)
        turn = await self.refresh_turn()
        if row is None or turn.player_id != row.player_id or turn.since_ts != row.since_ts:
            return None
        return self.by_id[row.player_id], turn

    async def _set_takeover_prompt(self, user_id: str, prompt: str) -> None:
        turn = await self.refresh_turn()
        if not turn.player_id or not turn.since_ts:
            await self.slack.dm(user_id, "No turn found in the channel yet.")
            return
        row = self.store.sync_turn(turn.player_id, turn.since_ts)
        req = self.store.open_request_for_turn(row.id)
        player = self.by_id[turn.player_id]
        if req is not None and self._expired(req):
            await self._expire_request(req, player)
            req = None
        if req is None:
            if not row.warned:
                await self.slack.dm(user_id, "No takeover request is open. One opens after 2 office hours.")
                return
            # The first request expired or closed: open a new one.
            await self._open_request(row.id, player, prompt or DEFAULT_NOTE)
            await self.slack.dm(user_id, "Opened a new takeover request in the channel.")
            return
        if not prompt:
            await self.slack.dm(user_id, f"Current prompt: {msg.safe(req.prompt)}")
            return
        self.store.set_request_prompt(req.id, prompt)
        req = self.store.get_request(req.id)
        await self._refresh_request_message(req, player)
        await self.slack.dm(user_id, "Prompt changed. All earlier approvals are cleared.")

    async def handle_approve(self, user_id: str, request_id: str) -> Approval:
        """Count one Approve click. The caller schedules `job`; it runs for minutes."""
        req = self.store.get_request(request_id)
        if req is None or req.status != "open":
            return Approval("This request is closed.")
        if user_id not in self.by_id:
            return Approval("Only players in the game can approve.")
        ctx = await self._request_context(req)
        if ctx is None:
            self.store.close_request(req.id, "stale")
            return Approval("The turn already moved on. This request is closed.")
        player, turn = ctx
        if self._expired(req):
            await self._expire_request(req, player)
            return Approval("This request expired. DM me `takeover <prompt>` to open a new one.")
        approvers = self.store.add_approval(req.id, user_id)
        if not approval_met(approvers, player.slack_id, self.order, self.settings.approvals_needed):
            await self._refresh_request_message(req, player)
            return Approval(f"Approval recorded ({len(approvers)}/{self.settings.approvals_needed}).")
        game = await self._probe_game()
        if game is None:
            return Approval("Enough approvals, but Claude is playing another turn. Click Approve again when it ends.")
        why_not = check_game_ready(player, game)
        if why_not:
            return Approval(f"Enough approvals, but the game is not ready: {why_not} Click Approve again when it is.")
        if not self.store.close_request(req.id, "approved"):
            return Approval("This request is already handled.")
        names = ", ".join(self.by_id[u].name for u in sorted(approvers))
        await self._close_request_message(req, player, f"approved by {names}. Running now.")
        job = self._run(
            player, req.prompt, requested_by=",".join(sorted(approvers)), source="takeover", game=game
        )
        return Approval("Approved. Claude is starting the turn.", job)
