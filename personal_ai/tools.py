from .files import Workspace, execute_file
from .models import ToolCall, ToolResult
from .runtime import OperationError, run_bounded


SCHEMAS = [
    {"name": "search_notes", "description": "Search allowed UTF-8 notes by name/content",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"], "additionalProperties": False}},
    {"name": "read_note", "description": "Read an allowed UTF-8 note",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"], "additionalProperties": False}},
    {"name": "create_note", "description": "Create a new note; never overwrite",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"], "additionalProperties": False}},
]


def permitted(call):
    if (not isinstance(call, ToolCall) or not isinstance(call.name, str)
            or not isinstance(call.arguments, dict)):
        return False
    schema = next((item for item in SCHEMAS if item["name"] == call.name), None)
    if schema is None or set(call.arguments) != set(schema["parameters"]["required"]):
        return False
    for key, value in call.arguments.items():
        if not isinstance(value, str):
            return False
        limit = 65536 if key == "content" else 1024
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            return False
        if size > limit or (key != "content" and not value.strip()):
            return False
    return True


class Tools:
    def __init__(self, root, store, timeout):
        self.root, self.store, self.timeout = str(root), store, timeout
        workspace = Workspace(root)
        try:
            self.identity = workspace.identity
        finally:
            workspace.close()

    def execute(self, call, timeout=None):
        allowed = permitted(call)
        # Record the file target, but never note contents or search terms.
        name = call.name if isinstance(call, ToolCall) and isinstance(call.name, str) and call.name in {
            item["name"] for item in SCHEMAS
        } else "unknown_tool"
        metadata = {"path": call.arguments["path"]} if allowed and "path" in call.arguments else {}
        operation = self.store.start_operation(name, metadata)
        if not allowed:
            self.store.finish_operation(operation, "denied", "tool_or_arguments_denied")
            return ToolResult(name, False, error="tool_or_arguments_denied")
        try:
            ok, data, error = run_bounded(
                execute_file, (self.root, self.identity, call.name, call.arguments),
                self.timeout if timeout is None else min(timeout, self.timeout),
            )
            self.store.finish_operation(operation, "success" if ok else "failed", error)
            return ToolResult(name, ok, data, error)
        except OperationError as exc:
            self.store.finish_operation(operation, "failed", str(exc))
            return ToolResult(name, False, error=str(exc))
        except KeyboardInterrupt:
            self.store.finish_operation(operation, "cancelled", "cancelled")
            raise
