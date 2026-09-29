import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lelamp.todo import TodoManager
from lelamp.tools import ToolExecutor, ToolSource


class TodoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    async def test_persists_multiple_items_and_mutations(self):
        manager = TodoManager(self.root)
        first = await manager.create_todo("  明天带充电器  ")
        second = await manager.create_todo("周末固定灯罩")
        self.assertEqual([x.todo_id for x in await manager.list_todos()], ["todo-1", "todo-2"])
        self.assertEqual(first.text, "明天带充电器")
        await manager.update_todo(first.todo_id, "带充电器和线")
        await manager.complete_todo(second.todo_id)
        self.assertEqual([x.text for x in await manager.list_todos()], ["带充电器和线"])
        restored = TodoManager(self.root)
        self.assertEqual([x.status for x in await restored.list_todos(True)], ["open", "completed"])
        await restored.delete_todo(first.todo_id)
        third = await restored.create_todo("检查麦克风")
        self.assertEqual(third.todo_id, "todo-3")
        self.assertEqual(len(json.loads((self.root / "runtime_state/todos.json").read_text())["todos"]), 2)

    async def test_invalid_targets_and_contents_do_not_change_data(self):
        manager = TodoManager(self.root)
        with self.assertRaises(ValueError):
            await manager.create_todo("  ")
        item = await manager.create_todo("记事")
        await manager.complete_todo(item.todo_id)
        for operation in (
            manager.update_todo(item.todo_id, "修改"),
            manager.complete_todo(item.todo_id),
            manager.delete_todo("todo-999"),
        ):
            with self.assertRaises(ValueError):
                await operation
        self.assertEqual((await manager.list_todos(True))[0].text, "记事")

    async def test_shared_tool_path(self):
        app = SimpleNamespace(todos=TodoManager(self.root))
        tools = ToolExecutor(app)
        created = await tools.execute("create_todo", {"text": "买电池"}, source=ToolSource.AGENT)
        self.assertTrue(created.as_dict()["ok"])
        listed = await tools.execute("list_todos", source=ToolSource.AGENT)
        self.assertEqual(listed.data["todos"][0]["text"], "买电池")
        completed = await tools.execute("complete_todo", {"todo_id": created.data["todo_id"]}, source=ToolSource.AGENT)
        self.assertEqual(completed.data["status"], "completed")
