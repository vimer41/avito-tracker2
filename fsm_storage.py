"""Persistent SQLite-backed aiogram FSM storage (survives restarts across deploys)."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

import database


def _resolve_state(state: StateType) -> Optional[str]:
    if state is None:
        return None
    if isinstance(state, State):
        return state.state
    return str(state)


class SQLiteFSMStorage(BaseStorage):
    """Stores aiogram FSM state & data in SQLite table `fsm_states`."""

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str = _resolve_state(state)
        now = time.time()
        async with database.writer_connection() as db:
            if state_str is None:
                cursor = await db.execute(
                    "SELECT data_json FROM fsm_states WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                    (key.bot_id, key.chat_id, key.user_id),
                )
                row = await cursor.fetchone()
                if row is not None and (not row["data_json"] or row["data_json"] == "{}"):
                    await db.execute(
                        "DELETE FROM fsm_states WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                        (key.bot_id, key.chat_id, key.user_id),
                    )
                elif row is not None:
                    await db.execute(
                        "UPDATE fsm_states SET state = NULL, updated_at = ? "
                        "WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                        (now, key.bot_id, key.chat_id, key.user_id),
                    )
            else:
                await db.execute(
                    "INSERT INTO fsm_states (bot_id, chat_id, user_id, state, data_json, updated_at) "
                    "VALUES (?, ?, ?, ?, '{}', ?) "
                    "ON CONFLICT(bot_id, chat_id, user_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                    (key.bot_id, key.chat_id, key.user_id, state_str, now),
                )
            await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with database.reader_connection() as db:
            cursor = await db.execute(
                "SELECT state FROM fsm_states WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                (key.bot_id, key.chat_id, key.user_id),
            )
            row = await cursor.fetchone()
        return row["state"] if row is not None else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        clean_data = dict(data) if data else {}
        data_json = json.dumps(clean_data, ensure_ascii=False)
        now = time.time()
        async with database.writer_connection() as db:
            if not clean_data:
                cursor = await db.execute(
                    "SELECT state FROM fsm_states WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                    (key.bot_id, key.chat_id, key.user_id),
                )
                row = await cursor.fetchone()
                if row is not None and row["state"] is None:
                    await db.execute(
                        "DELETE FROM fsm_states WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                        (key.bot_id, key.chat_id, key.user_id),
                    )
                elif row is not None:
                    await db.execute(
                        "UPDATE fsm_states SET data_json = '{}', updated_at = ? "
                        "WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                        (now, key.bot_id, key.chat_id, key.user_id),
                    )
            else:
                await db.execute(
                    "INSERT INTO fsm_states (bot_id, chat_id, user_id, state, data_json, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?) "
                    "ON CONFLICT(bot_id, chat_id, user_id) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at",
                    (key.bot_id, key.chat_id, key.user_id, data_json, now),
                )
            await db.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with database.reader_connection() as db:
            cursor = await db.execute(
                "SELECT data_json FROM fsm_states WHERE bot_id = ? AND chat_id = ? AND user_id = ?",
                (key.bot_id, key.chat_id, key.user_id),
            )
            row = await cursor.fetchone()
        if row is None or not row["data_json"]:
            return {}
        try:
            loaded = json.loads(row["data_json"])
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    async def close(self) -> None:
        return None
