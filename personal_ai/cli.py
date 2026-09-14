import argparse
import json
import shlex
import sqlite3

from .app import Assistant
from .models import ToolCall


HELP = """通常の入力: mock LLMと会話（外部通信なし）
/memory add 内容             明示的な記憶を登録
/memory list                 記憶一覧
/memory update ID 内容       記憶を更新、旧会話コンテキストを無効化
/memory forget ID|all        記憶を削除、旧会話コンテキストを無効化
忘れて ID|all                /memory forget と同じ
/search 検索語               許可フォルダのメモ検索
/read 相対パス               UTF-8の .md/.txt を読み取り
/note 相対パス 内容          新規メモ作成（上書き禁止）
/tool JSON                   mockのLLM→ツール実行ループを試す
/logs                        最近の操作ログ（本文は含まない）
/voice                       Push-to-Talk（--voice mock|local 指定時）
/help                        このヘルプ
/quit                        終了
空白を含むパスは引用符で囲んでください。"""


def render_tool(result):
    if not result.ok:
        detail = "操作は成功していません: {} ({})".format(result.name, result.error)
        if result.name == "create_note":
            detail += "。再試行前に対象ファイルの状態を確認してください。"
        return detail
    return "成功: " + json.dumps(result.data, ensure_ascii=False)


def handle(app, line):
    if line.startswith("忘れて "):
        line = "/memory forget " + line[len("忘れて "):]
    if line.strip() == "忘れて":
        return "削除対象を指定してください: 忘れて ID または 忘れて all"
    if not line.startswith("/") or line.startswith("/tool "):
        return app.chat(line)
    parts = shlex.split(line)
    command = parts[0]
    if command == "/help" and len(parts) == 1:
        return HELP
    if command == "/logs" and len(parts) == 1:
        return json.dumps(app.store.operations(), ensure_ascii=False, indent=2)
    if command == "/memory" and len(parts) >= 2:
        action = parts[1]
        if action == "list" and len(parts) == 2:
            return json.dumps(app.memory("list"), ensure_ascii=False, indent=2)
        if action == "add" and len(parts) >= 3:
            return "記憶を登録しました: ID={}".format(app.memory("add", " ".join(parts[2:])))
        if action == "update" and len(parts) >= 4:
            memory_id = int(parts[2])
            app.memory("update", " ".join(parts[3:]), memory_id)
            return "記憶を更新し、過去の会話をLLMの参照対象から外しました。"
        if action == "forget" and len(parts) == 3:
            memory_id = "all" if parts[2] == "all" else int(parts[2])
            app.memory("forget", memory_id=memory_id)
            return "記憶を削除し、過去の会話をLLMの参照対象から外しました。"
    if command == "/search" and len(parts) >= 2:
        return render_tool(app.tools.execute(ToolCall("search_notes", {"query": " ".join(parts[1:])})))
    if command == "/read" and len(parts) == 2:
        return render_tool(app.tools.execute(ToolCall("read_note", {"path": parts[1]})))
    if command == "/note" and len(parts) >= 3:
        return render_tool(app.tools.execute(ToolCall("create_note", {
            "path": parts[1], "content": " ".join(parts[2:])
        })))
    raise ValueError("コマンドの形式が正しくありません。/help を参照してください。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Personal AI — offline MVP")
    parser.add_argument("--data-dir", default=".personal-ai")
    parser.add_argument("--notes-dir", default="notes")
    parser.add_argument("--persona", help="JSON persona file (name, instructions)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-operation seconds")
    parser.add_argument("--turn-timeout", type=float, default=30.0)
    parser.add_argument("--voice", choices=("mock", "local"))
    parser.add_argument("--stt-model", help="existing local Vosk model directory")
    parser.add_argument("--tts-model", help="existing local Piper .onnx file")
    parser.add_argument("--mock-transcript", default="こんにちは")
    parser.add_argument("--voice-timeout", type=float, default=15)
    parser.add_argument("--voice-turn-timeout", type=float, default=90)
    args = parser.parse_args(argv)
    if args.voice == "local" and (not args.stt_model or not args.tts_model):
        parser.error("--voice local requires --stt-model and --tts-model")
    try:
        app = Assistant(args.data_dir, args.notes_dir, args.persona,
                        timeout=args.timeout, turn_timeout=args.turn_timeout)
    except (OSError, ValueError, sqlite3.Error):
        print("起動できませんでした。設定ファイル・保存先・権限を確認してください。")
        return 1
    try:
        voice = make_voice(app, args) if args.voice else None
    except ValueError:
        app.close()
        print("音声設定の時間制限が不正です。")
        return 1
    print("{} / mockモード・外部通信なし。/help でコマンド一覧。".format(app.persona.name))
    try:
        while True:
            try:
                line = input("you> ").strip()
                if line == "/quit":
                    break
                if line == "/voice":
                    if voice is None:
                        print("--voice mock または --voice local で起動してください。")
                    else:
                        run_voice_cli(voice, args.voice)
                elif line:
                    print(handle(app, line))
            except EOFError:
                break
            except KeyboardInterrupt:
                print("キャンセルしました。終了は /quit。")
            except ValueError as exc:
                print("入力エラー: {}".format(exc))
            except (OSError, sqlite3.Error):
                print("処理に失敗しました。保存先・権限・DBのロック状態を確認してください。")
    finally:
        app.close()
    return 0


def make_voice(app, args):
    from .voice import VoiceSession, MockSTT, MockTTS, MockRecorder, MockPlayer
    if args.voice == "mock":
        adapters = (MockSTT(args.mock_transcript), MockTTS(), MockRecorder(), MockPlayer())
    else:
        from .voice_local import VoskSTT, PiperTTS, SoundDeviceRecorder, SoundDevicePlayer
        adapters = (VoskSTT(args.stt_model), PiperTTS(args.tts_model),
                    SoundDeviceRecorder(), SoundDevicePlayer())
    return VoiceSession(app, *adapters, timeout=args.voice_timeout,
                        turn_timeout=args.voice_turn_timeout)


def run_voice_cli(voice, mode):
    import select
    import sys
    from .voice import VoiceState
    input("Enterで録音開始（Ctrl+Cで取消）> ")
    print("録音中: Enterで停止、最大30秒。Ctrl+Cは全工程で取消。" if mode == "local"
          else "mock音声を処理します（マイク・スピーカーは使用しません）。")

    def poll():
        if mode == "local" and voice.state == VoiceState.RECORDING:
            if select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                voice.stop_recording()

    result = voice.run(on_text=print, poll=poll)
    if result.error:
        print("音声処理: {}。テキスト入力を続けられます。".format(result.error))
