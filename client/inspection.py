"""Read-only inspection (state, source, facts, positional queries) and checkpoints."""
from __future__ import annotations

from typing import Any

from ._base import BASE_URL, ClientBase


class InspectionMixin(ClientBase):
    # --- proof state / source (read-only) ------------------------------------
    async def get_proof_state(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Current goal: ``{subgoals, proof_finished, current_theory}``."""
        return await self._get_json(f"{BASE_URL}/{session_id}/state", lease_id=lease_id)

    async def get_subgoals(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Open subgoals: ``{subgoals, count, proof_finished}``."""
        return await self._get_json(f"{BASE_URL}/{session_id}/subgoals", lease_id=lease_id)

    async def get_source(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Theory source as the prover sees it: ``{source, theory}``."""
        return await self._get_json(f"{BASE_URL}/{session_id}/source", lease_id=lease_id)

    async def get_local_facts(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Facts in the current local proof context: ``{facts, count}``.
        Read-only transient probe — the proof script is untouched."""
        return await self._get_json(f"{BASE_URL}/{session_id}/facts/local", lease_id=lease_id)

    async def get_global_facts(
        self, session_id: str, *, limit: int = 100, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Theory-level facts, sorted by name, capped at ``limit``: ``{facts, count}``.
        Read-only transient probe — the proof script is untouched."""
        return await self._get_json(
            f"{BASE_URL}/{session_id}/facts/global?limit={int(limit)}", lease_id=lease_id)

    # --- positional (jEdit/LSP-style) queries ---------------------------------
    async def command_at_line(
        self, session_id: str, line: int, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Command containing the 1-based ``line``: ``{found, kind, source, range}``.
        Read-only snapshot query (no ML probes) — safe past a trailing theory end."""
        return await self._get_json(
            f"{BASE_URL}/{session_id}/command_at_line?line={int(line)}", lease_id=lease_id)

    async def goals_at_line(
        self, session_id: str, line: int, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Goal state before/after the command containing the 1-based ``line``:
        ``{found, command, goals_before, goals_after}``. Read-only snapshot query;
        goal lists are empty unless show_states is on (default true)."""
        return await self._get_json(f"{BASE_URL}/{session_id}/goals?line={int(line)}", lease_id=lease_id)

    async def hover_at(
        self, session_id: str, line: int, col: int, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Hover info at a 1-based line/col (UTF-16 columns):
        ``{found, range, contents}``. Snapshot + Rendering; no evaluation."""
        return await self._get_json(
            f"{BASE_URL}/{session_id}/hover?line={int(line)}&col={int(col)}", lease_id=lease_id)

    async def definition_at(
        self, session_id: str, line: int, col: int, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Go-to-definition at a 1-based line/col: ``{found, targets}`` — file
        targets for heap/source entities, node targets for entry-document ones."""
        return await self._get_json(
            f"{BASE_URL}/{session_id}/definition?line={int(line)}&col={int(col)}", lease_id=lease_id)

    # --- checkpoints / rollback ----------------------------------------------
    async def save_checkpoint(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a checkpoint; returns ``{checkpoint_id, timestamp}``."""
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/checkpoints",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        return response.json()

    async def restore_checkpoint(
        self, session_id: str, checkpoint_id: int, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a previously saved checkpoint; returns ``{success, checkpoint_id, message}``."""
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/checkpoints/{checkpoint_id}/restore",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        return response.json()

    async def rollback(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Roll back the most recent command/edit; returns ``{success, output}``."""
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/rollback",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        return response.json()
