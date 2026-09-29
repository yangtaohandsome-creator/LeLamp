"""Small persistent todo store; scheduling remains in Timer and Alarm."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class TodoSnapshot:
    todo_id: str
    text: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "todo_id": self.todo_id,
            "text": self.text,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


class TodoManager:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path(__file__).resolve().parents[2]
        self._lock = asyncio.Lock()
        self._items: dict[str, TodoSnapshot] = {}
        self._next_id = 1
        self._loaded = False

    def _path(self) -> Path:
        return self._root / os.getenv("TODO_STATE_FILE", "runtime_state/todos.json")

    def _load(self) -> None:
        if self._loaded:
            return
        path = self._path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items = {}
                next_id = max(1, int(data["next_id"]))
                for raw in data["todos"]:
                    item = TodoSnapshot(
                        todo_id=str(raw["todo_id"]),
                        text=str(raw["text"]),
                        status=str(raw["status"]),
                        created_at=str(raw["created_at"]),
                        updated_at=str(raw["updated_at"]),
                        completed_at=raw.get("completed_at"),
                    )
                    if item.status not in {"open", "completed"} or item.todo_id in items:
                        raise ValueError("待办状态或 ID 无效")
                    items[item.todo_id] = item
                self._items = items
                self._next_id = max(next_id, *(int(key.removeprefix("todo-")) + 1 for key in items)) if items else next_id
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"待办数据读取失败: {exc}") from exc
        self._loaded = True

    def _save(self, items: dict[str, TodoSnapshot], next_id: int) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        data = {"next_id": next_id, "todos": [item.as_dict() for item in items.values()]}
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _text(value: str) -> str:
        text = " ".join(str(value).split())
        if not text or len(text) > 500:
            raise ValueError("待办内容必须为 1～500 字")
        return text

    def _get(self, todo_id: str) -> TodoSnapshot:
        try:
            return self._items[todo_id]
        except KeyError as exc:
            raise ValueError(f"找不到待办: {todo_id}") from exc

    async def create_todo(self, text: str) -> TodoSnapshot:
        content = self._text(text)
        async with self._lock:
            self._load()
            now = datetime.now().astimezone().isoformat()
            item = TodoSnapshot(f"todo-{self._next_id}", content, "open", now, now)
            items = {**self._items, item.todo_id: item}
            self._save(items, self._next_id + 1)
            self._items = items
            self._next_id += 1
            return item

    async def list_todos(self, include_completed: bool = False) -> list[TodoSnapshot]:
        async with self._lock:
            self._load()
            return [item for item in self._items.values() if include_completed or item.status == "open"]

    async def update_todo(self, todo_id: str, text: str) -> TodoSnapshot:
        content = self._text(text)
        async with self._lock:
            self._load()
            old = self._get(todo_id)
            if old.status != "open":
                raise ValueError(f"待办 {todo_id} 已完成，不能修改")
            item = TodoSnapshot(old.todo_id, content, old.status, old.created_at,
                                datetime.now().astimezone().isoformat())
            items = {**self._items, todo_id: item}
            self._save(items, self._next_id)
            self._items = items
            return item

    async def complete_todo(self, todo_id: str) -> TodoSnapshot:
        async with self._lock:
            self._load()
            old = self._get(todo_id)
            if old.status != "open":
                raise ValueError(f"待办 {todo_id} 已完成")
            now = datetime.now().astimezone().isoformat()
            item = TodoSnapshot(old.todo_id, old.text, "completed", old.created_at, now, now)
            items = {**self._items, todo_id: item}
            self._save(items, self._next_id)
            self._items = items
            return item

    async def delete_todo(self, todo_id: str) -> TodoSnapshot:
        async with self._lock:
            self._load()
            item = self._get(todo_id)
            items = dict(self._items)
            del items[todo_id]
            self._save(items, self._next_id)
            self._items = items
            return item
