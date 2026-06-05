"""Budget enforcement — daily token/recall caps + per-session throttling.

Pure stdlib. Stored as JSON under <kos-dir>/budget.json.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_DAILY_TOKEN_CAP = 50_000     # ~$0.50/day at Sonnet rates
DEFAULT_DAILY_RECALL_CAP = 50
DEFAULT_PER_SESSION_RECALL_CAP = 5
DEFAULT_PER_TURN_RECALL_CAP = 1


class Budget:
    def __init__(self, budget_file: str | Path):
        self.path = Path(budget_file)
        self._state: dict[str, Any] = {}
        self._load()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}
        else:
            self._state = {}

        today = self._today()
        if self._state.get("date") != today:
            self._state = {
                "date": today,
                "tokens_used": 0,
                "recalls_today": 0,
                "sessions": {},
            }
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            pass

    def can_recall(
        self,
        session_id: str | None = None,
        token_cap: int = DEFAULT_DAILY_TOKEN_CAP,
        recall_cap: int = DEFAULT_DAILY_RECALL_CAP,
        session_cap: int = DEFAULT_PER_SESSION_RECALL_CAP,
        estimated_tokens: int = 0,
    ) -> tuple[bool, str]:
        """Return (allowed, reason_if_denied).

        If estimated_tokens > 0, the projected post-call total is checked
        too — protects against blowing the cap on a single large call.
        """
        self._load()
        cur = int(self._state.get("tokens_used", 0))
        if cur >= token_cap:
            return False, (
                f"Daily token budget exceeded "
                f"({cur}/{token_cap})"
            )
        if estimated_tokens > 0 and (cur + estimated_tokens) > token_cap:
            return False, (
                f"Estimated call would exceed daily token budget "
                f"({cur}+{estimated_tokens} > {token_cap})"
            )
        if self._state.get("recalls_today", 0) >= recall_cap:
            return False, (
                f"Daily recall count exceeded "
                f"({self._state['recalls_today']}/{recall_cap})"
            )
        if session_id:
            sess = self._state.setdefault("sessions", {}).get(
                session_id, {"recalls": 0}
            )
            if sess["recalls"] >= session_cap:
                return False, (
                    f"Session recall limit ({session_cap}) "
                    f"reached for {session_id}"
                )
        return True, ""

    def record_recall(
        self,
        session_id: str | None = None,
        tokens_used: int = 0,
        *,
        tokens: int | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        """Increment counters. Accepts both `tokens_used` (positional, original)
        and `tokens` (keyword, callers prefer this name). cost_usd recorded
        but not enforced (token cap is the practical knob).
        """
        if tokens is not None:
            tokens_used = tokens
        self._load()
        self._state["tokens_used"] = int(
            self._state.get("tokens_used", 0)
        ) + int(tokens_used or 0)
        self._state["recalls_today"] = int(
            self._state.get("recalls_today", 0)
        ) + 1
        self._state["cost_usd_today"] = float(
            self._state.get("cost_usd_today", 0.0)
        ) + float(cost_usd or 0.0)
        if session_id:
            sess = self._state.setdefault("sessions", {}).setdefault(
                session_id, {"recalls": 0}
            )
            sess["recalls"] += 1
        self._save()

    def status(self) -> dict[str, Any]:
        self._load()
        return dict(self._state)
