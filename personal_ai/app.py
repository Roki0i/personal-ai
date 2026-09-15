import json
import math
import os
import sqlite3
import time
from pathlib import Path

from .external import (EXTERNAL_NAMES, ExternalPermission, PrivacyClassification,
                       MockWebSearchProvider, MockCalendarProvider, request_from_message,
                       provenance)
from .llm import MockLLM, generate
from .models import Context, Persona, Reply, ToolCall
from .runtime import OperationError, cancel_current, run_bounded
from .storage import Store
from .tools import SCHEMAS, Tools
from .tasks import TaskManager, route as route_task


class Assistant:
    def __init__(self, data_dir, notes_dir, persona_path=None, provider=None,
                 timeout=10.0, turn_timeout=30.0, max_tool_calls=4,
                 web_provider=None, calendar_provider=None, local_provider=None,
                 local_mode="auto", allowed_repositories=()):
        if not all(math.isfinite(value) and value > 0 for value in (timeout, turn_timeout)):
            raise ValueError("timeout must be finite and positive")
        if not isinstance(max_tool_calls, int) or not 1 <= max_tool_calls <= 10:
            raise ValueError("max_tool_calls must be between 1 and 10")
        data_input, notes_input = Path(data_dir).absolute(), Path(notes_dir).absolute()
        if data_input.is_symlink() or notes_input.is_symlink():
            raise ValueError("configured directories must not be symlinks")
        if os.name == 'nt':
            from .windows import check_configured_path
            for configured in (data_input, notes_input): check_configured_path(configured)
        data, notes = data_input.resolve(), notes_input.resolve()
        if data == notes or data in notes.parents or notes in data.parents:
            raise ValueError("data and notes directories must not overlap")
        self.persona = self.load_persona(persona_path)
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        notes.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data, 0o700)
        database = data / "assistant.sqlite3"
        if database.is_symlink():
            raise ValueError("database must not be a symlink")
        if os.name == 'nt':
            check_configured_path(database)
        self.store = Store(database)
        os.chmod(database, 0o600)
        self.provider = provider if provider is not None else MockLLM()
        self.timeout, self.turn_timeout, self.max_tool_calls = timeout, turn_timeout, max_tool_calls
        try:
            self.tools = Tools(notes, self.store, timeout,
                               web_provider if web_provider is not None else MockWebSearchProvider(),
                               calendar_provider if calendar_provider is not None else MockCalendarProvider())
            from .runtime import local_provider_class
            provider_class = local_provider_class(local_mode)
            local = local_provider if local_provider is not None else provider_class(
                notes, self.tools.identity, allowed_repositories)
            if local.root != str(notes) or local.identity != self.tools.identity:
                raise ValueError('local_provider_must_use_allowed_folder')
            self.tasks = TaskManager(self.store, local, timeout, turn_timeout)
        except BaseException:
            self.store.close()
            raise

    @staticmethod
    def load_persona(path):
        if path is None:
            return Persona("Amadeus", "簡潔な日本語で答える。ツールの成否は実行結果に従う。")
        raw = Path(path).read_text(encoding="utf-8")
        if len(raw) > 16000:
            raise ValueError("persona too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"name", "instructions"}:
            raise ValueError("persona requires name and instructions")
        if not all(isinstance(item, str) and item.strip() for item in value.values()):
            raise ValueError("persona fields must be nonempty strings")
        return Persona(**value)

    def close(self):
        self.tasks.close()
        self.store.close()

    def memory(self, action, content=None, memory_id=None, **attributes):
        # Only explicit commands call this API. Memory is not an LLM tool.
        operation = self.store.start_operation("memory_" + action,
                                               {"memory_id": memory_id})
        try:
            if action == "add":
                result = self.store.add_memory(content, **attributes)
            elif action == "list":
                result = self.store.memories()
            elif action == "search":
                result = self.store.retrieve_memories(content)
            elif action == "show":
                result = self.store.show_memory(memory_id)
            elif action == "why":
                result = self.store.last_retrieval
            elif action == "summarize":
                result = self.store.summarize_conversation()
            else:
                result = self.store.memory(action, content, memory_id)
            self.store.finish_operation(operation, "success")
            return result
        except ValueError as exc:
            self.store.finish_operation(operation, "failed", str(exc))
            raise
        except sqlite3.Error:
            # If the database itself is unavailable, the durable 'started' entry
            # remains evidence that completion could not be recorded.
            self.store.finish_operation(operation, "failed", "storage_failed")
            raise

    def chat(self, message, *, external_permission=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ValueError("message must contain 1 to 16000 characters")
        local_answer = route_task(self.tasks, message)
        if local_answer is not None:
            return local_answer
        deadline = time.monotonic() + self.turn_timeout
        epoch = self.store.epoch()
        request = request_from_message(message)
        if request and external_permission is None:
            adapter = self.tools.web_provider if request.name == "web_search" else self.tools.calendar_provider
            # Only the built-in offline mocks have a default grant. Real adapters fail closed.
            if type(adapter) in (MockWebSearchProvider, MockCalendarProvider):
                external_permission = ExternalPermission(PrivacyClassification.CLOUD_SENDABLE,
                                                         provider=adapter.provider_id)
        history_rows = [] if request else self.store.history_rows()
        context = Context(self.persona, [] if request else self.store.retrieve_memories(message),
                          [{k: row[k] for k in ('role', 'content')} for row in history_rows], message, SCHEMAS)
        conversation_ids = [row['id'] for row in history_rows]
        memory_ids = [row['id'] for row in context.memories]
        user_id = self.store.message("external_user" if request else "user", message, epoch,
                                     conversation_ids=conversation_ids)

        tool_count = 0
        operation = None
        try:
            while True:
                if self.store.epoch() != epoch:
                    raise OperationError("memory_changed")
                operation = self.store.start_operation("llm_generate")
                reply = run_bounded(generate, (self.provider, context),
                                    min(self.timeout, deadline - time.monotonic()))
                if (not isinstance(reply, Reply) or not isinstance(reply.text, str)
                        or len(reply.text) > 16000 or not isinstance(reply.calls, list)
                        or any(not isinstance(call, ToolCall) for call in reply.calls)):
                    raise OperationError("invalid_llm_reply")
                self.store.finish_operation(operation, "success")
                operation = None
                if self.store.epoch() != epoch:
                    raise OperationError("memory_changed")
                if not reply.calls:
                    if request and not context.results:
                        raise OperationError("external_result_missing")
                    if not reply.text.strip():
                        raise OperationError("empty_llm_reply")
                    answer = reply.text
                    sources = [source for item in context.results if item.ok and item.name in EXTERNAL_NAMES
                               for source in provenance(item.data)]
                    if sources:
                        answer += "\n参照した外部結果（回答生成に提供）: " + json.dumps(sources, ensure_ascii=False)
                    break
                if tool_count + len(reply.calls) > self.max_tool_calls:
                    raise OperationError("tool_limit")
                if request and (context.results or len(reply.calls) != 1 or reply.calls[0] != request):
                    denied = self.store.start_operation("external_tool_boundary")
                    self.store.finish_operation(denied, "denied", "external_tool_chain_denied")
                    raise OperationError("external_tool_chain_denied")
                if (any(result.name == 'read_note' for result in context.results)
                        or (context.results and any(call.name == 'create_note' for call in reply.calls))):
                    raise OperationError('untrusted_tool_chain_denied')
                for call in reply.calls:
                    if call.name == 'create_note':
                        # Preserve the legacy explicit /tool create_note command;
                        # model/history/file data cannot invent a write request.
                        try:
                            explicit = json.loads(message[6:]) if message.startswith('/tool ') else None
                        except ValueError:
                            explicit = None
                        if explicit != {'name': call.name, 'arguments': call.arguments}:
                            raise OperationError('explicit_write_request_required')
                for call in reply.calls:
                    tool_count += 1
                    result = self.tools.execute(call, deadline - time.monotonic(),
                                                request=request, permission=external_permission)
                    context.results.append(result)
                    if not result.ok:
                        # Do not let a model reinterpret failure as success.
                        answer = "操作は成功していません: {} ({})。".format(result.name, result.error)
                        if call.name == "create_note":
                            answer += " 新規作成は途中まで進んだ可能性があるため、再試行前に対象を確認してください。"
                        if any(item.ok for item in context.results):
                            answer += " 先行する操作は完了済みです。/logs で確認できます。"
                        break
                else:
                    continue
                break
        except OperationError as exc:
            if operation is not None:
                self.store.finish_operation(operation, "failed", str(exc))
            answer = "処理を完了できませんでした ({})。".format(exc)
            if any(item.ok for item in context.results):
                answer += " 一部の操作は完了済みです。/logs で確認できます。"
        except KeyboardInterrupt:
            cancel_current()
            if operation is not None:
                self.store.finish_operation(operation, "cancelled", "cancelled")
            answer = "処理をキャンセルしました。実行済みの操作は /logs で確認してください。"
        # Save into the original epoch even if another process forgot a memory.
        self.store.message("external_assistant" if request else "assistant", answer, epoch,
                           conversation_ids=conversation_ids + [user_id], memory_ids=memory_ids)
        return answer
