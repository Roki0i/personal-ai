import json
from .external import request_from_message
from .models import Context, Reply, ToolCall


def generate(provider, context):
    return provider.generate(context)


class MockLLM:
    """Deterministic offline demo. /tool JSON demonstrates the actual tool loop."""

    def generate(self, context: Context) -> Reply:
        if context.results:
            last = context.results[-1]
            if not last.ok:
                return Reply("操作に失敗しました。")
            return Reply("操作が完了しました。\n" + json.dumps(last.data, ensure_ascii=False))
        request = request_from_message(context.message)
        if request:
            return Reply(calls=[request])
        if context.message.startswith("/tool "):
            try:
                value = json.loads(context.message[6:])
                return Reply(calls=[ToolCall(value["name"], value["arguments"])])
            except (ValueError, TypeError, KeyError):
                return Reply('形式: /tool {"name":"read_note","arguments":{"path":"memo.md"}}')
        if context.message in ("記憶を教えて", "私の回答の好みは？"):
            contents = [item["content"] for item in context.memories]
            return Reply(" / ".join(contents) if contents else "保存された記憶はありません。")
        return Reply("{} [mock]: {}\n（LLM未接続の動作確認用応答です）".format(
            context.persona.name, context.message
        ))
