import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from personal_ai.app import Assistant
from personal_ai.cli import handle
from personal_ai.files import Workspace
from personal_ai.models import Reply, ToolCall
from personal_ai.runtime import OperationError, run_bounded


class InspectProvider:
    def generate(self, context):
        return Reply(json.dumps({
            "persona": context.persona.name,
            "instructions": context.persona.instructions,
            "memories": context.memories,
            "history": context.history,
        }, ensure_ascii=False))


class FailureClaimProvider:
    def generate(self, context):
        if context.results:
            return Reply("成功しました！")
        return Reply(text="成功しました！", calls=[ToolCall("read_note", {"path": "missing.md"})])


class ReadProvider:
    def generate(self, context):
        if context.results:
            return Reply("読取結果: " + context.results[-1].data)
        return Reply(calls=[ToolCall("read_note", {"path": "hello.md"})])


class LoopProvider:
    def generate(self, context):
        return Reply(calls=[ToolCall("search_notes", {"query": "hello"})])


class SlowProvider:
    def generate(self, context):
        time.sleep(5)
        return Reply("too late")


class CrashProvider:
    def generate(self, context):
        raise RuntimeError("SECRET_API_KEY")


class InvalidProvider:
    def generate(self, context):
        return {"text": "success"}


def delayed_write(path):
    time.sleep(1)
    Path(path).write_text("should never happen")


class MVPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data, self.notes = self.root / "data", self.root / "notes"
        self.app = Assistant(self.data, self.notes, timeout=3)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def call(self, name, **arguments):
        return self.app.tools.execute(ToolCall(name, arguments))

    def test_chat_persists_and_survives_restart(self):
        answer = self.app.chat("こんにちは")
        self.assertIn("mock", answer)
        self.app.close()
        self.app = Assistant(self.data, self.notes)
        history = self.app.store.history()
        self.assertEqual([row["role"] for row in history], ["user", "assistant"])
        self.assertEqual(history[0]["content"], "こんにちは")

    def test_persona_reaches_provider(self):
        persona = self.root / "persona.json"
        persona.write_text(json.dumps({"name": "Jarvis", "instructions": "短く答える"}))
        self.app.close()
        self.app = Assistant(self.data, self.notes, persona, InspectProvider())
        output = json.loads(self.app.chat("test"))
        self.assertEqual(output["persona"], "Jarvis")
        self.assertEqual(output["instructions"], "短く答える")

    def test_explicit_memory_lifecycle(self):
        memory_id = self.app.memory("add", "日本語が好み")
        self.assertEqual(self.app.memory("list")[0]["content"], "日本語が好み")
        self.app.memory("update", "英語が好み", memory_id)
        self.assertEqual(self.app.memory("list")[0]["content"], "英語が好み")
        self.app.memory("forget", memory_id=memory_id)
        self.assertEqual(self.app.memory("list"), [])
        statuses = [row["status"] for row in self.app.store.operations()]
        self.assertTrue(all(status == "success" for status in statuses))

    def test_forgotten_memory_not_reused_from_user_or_assistant_history_after_restart(self):
        secret = "秘密の好きな色は紫"
        memory_id = self.app.memory("add", secret)
        self.app.store.message("user", secret)
        self.app.store.message("assistant", "覚えました: " + secret)
        keep_id = self.app.memory("add", "日本語で返答")
        handle(self.app, "忘れて " + str(memory_id))
        self.app.close()
        self.app = Assistant(self.data, self.notes, provider=InspectProvider())
        output = self.app.chat("覚えていることは？")
        self.assertNotIn(secret, output)
        payload = json.loads(output)
        self.assertEqual(payload["history"], [])
        self.assertEqual([item["id"] for item in payload["memories"]], [keep_id])
        self.assertNotIn(secret, self.app.chat("もう一度"))
        # History is retained for the local user, but excluded from all LLM context.
        self.assertGreater(self.app.store.db.execute(
            "SELECT count(*) FROM conversations WHERE content LIKE ?", ("%" + secret + "%",)
        ).fetchone()[0], 0)

    def test_update_invalidates_old_fact(self):
        memory_id = self.app.memory("add", "OLD_VALUE")
        self.app.store.message("assistant", "OLD_VALUE")
        self.app.memory("update", "NEW_VALUE", memory_id)
        self.app.provider = InspectProvider()
        output = self.app.chat("確認")
        self.assertNotIn("OLD_VALUE", output)
        self.assertIn("NEW_VALUE", output)

    def test_forget_all_also_clears_context_when_no_memories(self):
        self.app.store.message("user", "OLD_SECRET")
        handle(self.app, "/memory forget all")
        self.assertEqual(self.app.store.history(), [])

    def test_failed_memory_update_is_logged_without_changing_context(self):
        self.app.store.message("user", "keep")
        with self.assertRaises(ValueError):
            self.app.memory("update", "missing", 123)
        self.assertEqual(self.app.store.history()[0]["content"], "keep")
        self.assertEqual(self.app.store.operations()[0]["status"], "failed")

    def test_create_read_search_and_no_overwrite(self):
        (self.notes / "sub").mkdir()
        self.assertTrue(self.call("create_note", path="sub/日本語.md", content="企画のアイデア").ok)
        self.assertEqual(self.call("read_note", path="sub/日本語.md").data, "企画のアイデア")
        result = self.call("search_notes", query="アイデア")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["paths"], ["sub/日本語.md"])
        self.assertFalse(self.call("create_note", path="sub/日本語.md", content="changed").ok)
        self.assertEqual((self.notes / "sub/日本語.md").read_text(), "企画のアイデア")

    def test_traversal_and_absolute_paths_denied_for_reads_and_writes(self):
        outside = self.root / "secret.md"
        outside.write_text("SECRET")
        paths = ["../secret.md", "sub/../../secret.md", str(outside),
                 "./memo.md", "sub//memo.md", "..\\secret.md", "bad\x00.md"]
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(self.call("read_note", path=path).ok)
                self.assertFalse(self.call("create_note", path=path, content="overwrite").ok)
        self.assertEqual(outside.read_text(), "SECRET")

    def test_symlink_file_and_directory_denied_for_all_tools(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("SECRET")
        (self.notes / "link.md").symlink_to(outside / "secret.md")
        (self.notes / "escape").symlink_to(outside, target_is_directory=True)
        for path in ["link.md", "escape/secret.md"]:
            self.assertFalse(self.call("read_note", path=path).ok)
            self.assertFalse(self.call("create_note", path=path, content="bad").ok)
        self.assertFalse(self.call("create_note", path="escape/new.md", content="bad").ok)
        self.assertFalse((outside / "new.md").exists())
        search = self.call("search_notes", query="SECRET")
        self.assertTrue(search.ok)
        self.assertEqual(search.data["paths"], [])
        self.assertGreater(search.data["skipped"], 0)
        self.assertEqual((outside / "secret.md").read_text(), "SECRET")

    def test_dangling_symlink_cannot_create_external_file(self):
        destination = self.root / "new.md"
        (self.notes / "link.md").symlink_to(destination)
        self.assertFalse(self.call("create_note", path="link.md", content="bad").ok)
        self.assertFalse(destination.exists())

    def test_symlink_swap_after_workspace_open_is_denied(self):
        (self.notes / "sub").mkdir()
        (self.notes / "sub" / "note.md").write_text("original")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "note.md").write_text("SECRET")
        workspace = Workspace(self.notes)
        try:
            (self.notes / "sub").rename(self.notes / "old")
            (self.notes / "sub").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                workspace.read("sub/note.md")
        finally:
            workspace.close()

    def test_replaced_workspace_is_denied(self):
        self.notes.rename(self.root / "old_notes")
        self.notes.mkdir()
        (self.notes / "new.md").write_text("untrusted")
        result = self.call("read_note", path="new.md")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "workspace_changed")

    def test_hardlink_and_fifo_denied(self):
        outside = self.root / "secret.md"
        outside.write_text("SECRET")
        os.link(outside, self.notes / "hard.md")
        os.mkfifo(self.notes / "pipe.md")
        self.assertFalse(self.call("read_note", path="hard.md").ok)
        self.assertFalse(self.call("read_note", path="pipe.md").ok)

    def test_bad_encoding_and_size_have_explicit_failures(self):
        (self.notes / "binary.md").write_bytes(b"\xff")
        (self.notes / "large.md").write_bytes(b"x" * 65537)
        self.assertEqual(self.call("read_note", path="binary.md").error, "invalid_utf8")
        self.assertEqual(self.call("read_note", path="large.md").error, "file_too_large")
        self.assertFalse(self.call("create_note", path="large.txt", content="x" * 65537).ok)

    def test_unknown_tools_and_bad_arguments_are_denied(self):
        calls = [ToolCall("shell", {"command": "touch pwned"}),
                 ToolCall("memory_forget", {"id": "all"}),
                 ToolCall("read_note", {"path": "x.md", "extra": True}),
                 ToolCall("read_note", {"path": 123}),
                 ToolCall([], {}), ToolCall("read_note", {"path": "\ud800.md"})]
        for call in calls:
            self.assertFalse(self.app.tools.execute(call).ok)
            self.assertEqual(self.app.store.operations()[0]["status"], "denied")

    def test_tool_failure_cannot_be_reported_as_success_by_model(self):
        self.app.provider = FailureClaimProvider()
        answer = self.app.chat("read")
        self.assertIn("操作は成功していません", answer)
        self.assertNotIn("成功しました", answer)
        logs = self.app.store.operations()
        self.assertEqual(logs[0]["status"], "failed")
        self.assertEqual(len([row for row in logs if row["name"] == "llm_generate"]), 1)
        self.assertEqual(self.app.store.history()[-1]["content"], answer)

    def test_llm_tool_success_loop(self):
        (self.notes / "hello.md").write_text("hello world")
        self.app.provider = ReadProvider()
        self.assertEqual(self.app.chat("read"), "読取結果: hello world")
        self.assertEqual(len(self.app.store.operations()), 3)

    def test_tool_loop_is_bounded(self):
        self.app.provider = LoopProvider()
        self.app.max_tool_calls = 2
        self.assertIn("tool_limit", self.app.chat("loop"))
        executions = [row for row in self.app.store.operations() if row["name"] == "search_notes"]
        self.assertEqual(len(executions), 2)

    def test_llm_timeout_and_recovery(self):
        self.app.provider = SlowProvider()
        self.app.timeout = 0.2
        start = time.monotonic()
        self.assertIn("timeout", self.app.chat("slow"))
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(self.app.store.operations()[0]["status"], "failed")
        self.app.provider = InspectProvider()
        self.app.timeout = 3
        self.assertIn("persona", self.app.chat("retry"))

    def test_tool_timeout_is_failure(self):
        result = self.app.tools.execute(ToolCall("read_note", {"path": "x.md"}), timeout=0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")
        self.assertEqual(self.app.store.operations()[0]["status"], "failed")

    def test_timed_out_worker_is_stopped_before_later_side_effect(self):
        path = self.root / "late.txt"
        before = {child.pid for child in multiprocessing.active_children()}
        with self.assertRaises(OperationError):
            run_bounded(delayed_write, (str(path),), 0.2)
        time.sleep(1.1)
        self.assertFalse(path.exists())
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_provider_crash_does_not_leak_exception_and_invalid_reply_fails(self):
        self.app.provider = CrashProvider()
        answer = self.app.chat("crash")
        self.assertIn("worker_failed", answer)
        self.assertNotIn("SECRET_API_KEY", answer + str(self.app.store.operations()))
        self.app.provider = InvalidProvider()
        self.assertIn("invalid_llm_reply", self.app.chat("invalid"))

    def test_logs_do_not_contain_memory_or_file_body(self):
        self.app.memory("add", "SECRET_MEMORY")
        self.call("create_note", path="hello.md", content="SECRET_FILE")
        self.call("search_notes", query="SECRET_QUERY")
        logs = json.dumps(self.app.store.operations())
        for secret in ("SECRET_MEMORY", "SECRET_FILE", "SECRET_QUERY"):
            self.assertNotIn(secret, logs)

    def test_overlapping_directories_and_root_symlink_rejected(self):
        with self.assertRaises(ValueError):
            Assistant(self.notes / "data", self.notes)
        (self.root / "alias").symlink_to(self.notes, target_is_directory=True)
        with self.assertRaises(ValueError):
            Assistant(self.root / "other_data", self.root / "alias")

    def test_cli_end_to_end_without_api_key(self):
        environment = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}
        result = subprocess.run(
            [sys.executable, "-m", "personal_ai", "--data-dir", str(self.data),
             "--notes-dir", str(self.notes)],
            input='/memory add 日本語が好み\nこんにちは\n/note memo.md テスト\n/read memo.md\n'
                  '/tool {"name":"read_note","arguments":{"path":"memo.md"}}\n'
                  '/memory forget all\n/memory list\n/quit\n',
            text=True, capture_output=True, env=environment, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mock", result.stdout)
        self.assertIn("記憶を登録", result.stdout)
        self.assertIn("操作が完了", result.stdout)
        self.assertIn("記憶を削除", result.stdout)
        self.assertEqual((self.notes / "memo.md").read_text(), "テスト")


if __name__ == "__main__":
    unittest.main()
