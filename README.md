# Personal AI

JARVIS / アマデウスを目標に、段階的に育てるパーソナルAIアシスタント。
Phase 1は、Python・CLI・SQLiteによるローカルの基盤です。

現在のLLMは**外部通信しないmock**です。APIキーも外部パッケージも不要で、
会話履歴・記憶・権限判定・ファイル操作・失敗処理を試せます。
mockは固定ルールによる動作確認用であり、自然言語を理解する実際のLLMではありません。
音声・Web検索・PC操作・常駐処理・クラウドAPI接続は実装していません。

## 起動

Python 3.9以上、macOS / Linux（POSIXの `dir_fd` / `O_NOFOLLOW` が必要）。
Windowsは対象外です。リポジトリ直下から実行します。

```sh
python3 -m personal_ai --persona persona.json
```

初回起動で、次のディレクトリを作ります。

- `.personal-ai/assistant.sqlite3`：会話履歴、明示的Memory、操作ログ。
- `notes/`：ファイル操作を許可する専用フォルダ。

両方ともGit管理対象外です。保存先は重複・入れ子にできません。
パスは起動時のカレントディレクトリを基準にします。

```sh
python3 -m personal_ai --data-dir .personal-ai --notes-dir notes \
  --persona persona.json --timeout 10 --turn-timeout 30
```

パッケージとして利用する場合は、仮想環境で `python3 -m pip install -e .` の後、
`personal-ai --persona persona.json` でも起動できます。
このインストール方法ではビルド用のsetuptools取得が発生する場合があります。
上記の `python3 -m personal_ai` とテストにはインストール不要です。

## 操作例

```text
こんにちは
/memory add 回答は簡潔な日本語が好み
/memory list
私の回答の好みは？
/memory update 1 回答は日本語で、必要なら詳しく説明してほしい
/note meeting.md 次にやることはCLIの動作確認
/search CLI
/read meeting.md
/memory forget 1
/memory list
/logs
/quit
```

`/memory add` の表示するIDを更新・削除に使います。
`忘れて 1` は `/memory forget 1` と同じです。
`/memory forget all` または `忘れて all` は全Memoryを削除します。
`忘れて` だけでは対象を案内し、推測して削除しません。

Memoryを変更するのはこれらの明示コマンドだけです。
通常の会話から自動抽出・自動登録はしません。
`/search` / `/read` / `/note` はアプリの権限判定を通して直接ツールを実行します。
空白を含むパスは `/note "daily memo.md" 今日のメモ` のように引用符で囲みます。
親ディレクトリは事前にユーザーが作成してください。CLIは新規ファイルだけを作ります。

mockを介した **LLM → ツール → 結果 → 回答** のループも確認できます。

```text
/tool {"name":"create_note","arguments":{"path":"demo.md","content":"動作確認"}}
/tool {"name":"read_note","arguments":{"path":"demo.md"}}
/tool {"name":"read_note","arguments":{"path":"../secret.md"}}
```

最後の操作は失敗になります。`/help` でコマンド一覧を表示します。
Ctrl+Cは処理を中断し、`/quit` またはEOFで終了します。

## Persona

`persona.json` の `name` と `instructions` を編集し、`--persona persona.json` で読み込みます。
指定しない場合は組み込みの既定Personaを使います。
人格設定は毎回LLMのContextへ渡しますが、mockが反映するのは表示名のみです。
口調・応答方針の実際の反映には、本物のLLM実装が必要です。
会話やファイルの内容から設定を自動変更することはありません。

## 構成

```text
personal_ai/
  cli.py        CLI、明示Memoryコマンド
  app.py        会話制御、Context作成、ツール回数・時間の制限
  models.py     LLM Protocol、Persona、Context、Reply、ToolCall/Result
  llm.py        APIキー不要のMockLLM
  storage.py    SQLiteの履歴・Memory・操作ログ
  tools.py      ツール登録、引数スキーマ、許可判定
  files.py      許可フォルダ内のファイル操作
  runtime.py    別プロセス実行、タイムアウト、停止
tests/
  test_mvp.py   セキュリティ、失敗処理、CLIの統合テスト
persona.json    任意に読み込む人格設定
pyproject.toml  パッケージ情報とCLIエントリポイント
```

### LLMの交換

`models.LLM` は `generate(context: Context) -> Reply` のProtocolです。
`Assistant(..., provider=YourProvider())` で実装を注入します。
`Context`には人格、現在有効なMemory、現在の区切りの直近12発言、今回の入力、
ツールスキーマ、今回のツール結果を渡します。

- 返すものはテキスト、または `ToolCall(name, arguments)` のリスト。
- 許可ツールは `search_notes` / `read_note` / `create_note` の3つのみ。
- ツール実行はLLM実装内では行わず、アプリに返します。
- テキストとツール要求が同時に来た場合、そのテキストは表示せず実行結果を待ちます。
- Providerは別プロセスへ渡すため、モジュールからimport可能なクラスで、pickle可能である必要があります。
- Providerは**ステートレス**にします。過去の会話やMemoryを独自保存したり、
  クラウドの永続スレッドを再利用したりすると「忘れて」の保証を壊します。
- Pythonから起動するスクリプトには `if __name__ == "__main__":` ガードが必要です。

クラウドProviderを追加する際は、送信する記憶・本文の可否判定と認証情報管理も追加してください。
現在はmockのみのためクラウドへのデータ送信はありません。

### Memoryと「忘れて」

SQLiteには、本文、ID、出典（明示ユーザーコマンド）、登録・更新日時を保存します。
Memoryの更新・削除と同じトランザクション内で、会話の `epoch` を進めます。
LLMが参照できる履歴は現在のepochのみなので、削除済みの内容が古いユーザー発言や
アシスタント回答から復活しません。再起動後もこの区切りは保持されます。
削除後もIDを再利用しません。

この方式では、1件の更新・削除でも**それ以前の会話全体がLLMの参照対象から外れます**。
残っている明示Memoryは引き続き参照できます。

「忘れて」は、Memoryを削除して会話コンテキストから除外する機能です。
過去の会話原文はローカルSQLiteに履歴として残ります。
既存メモ、Persona、バックアップ、ユーザーが再入力した情報の削除は対象外です。
メモに同じ事実を保存してあれば、ユーザーの読み取り依頼で再取得できます。
履歴・ツール結果の自動検索によるMemory再登録は実装していません。

### ファイル権限

- 相対パスのUTF-8 `.md` / `.txt` のみ。
- 絶対パス、`..`、`.`、空パス要素、バックスラッシュ、NULを拒否。
- 起動時に許可ルートを確定し、デバイス・inodeで同じルートか確認。
- 各パス要素をディレクトリFD相対で開き、`O_NOFOLLOW` でsymlinkを拒否。
- 許可範囲内を指すsymlinkも拒否。検索はsymlinkを辿りません。
- 読み取りは通常ファイルのみ。複数のハードリンクやFIFOも拒否。
- 新規作成は `O_EXCL` を使用し、存在するファイルを上書きしません。
- 1ファイル64 KiB、検索は最大1,000エントリ・深さ16・結果30件。
  検索結果にはスキップ数と打ち切り有無を付けます。

この境界はLLMからの引数を制限するものです。
Provider自体は信頼するPythonコードであり、OSのサンドボックスではありません。
同じOSユーザーによる保存先の悪意ある改変を隔離する構成ではなく、
専用フォルダは自分だけが管理する前提です。

### ログ・失敗・タイムアウト

- 操作開始を先に保存し、`success` / `failed` / `denied` / `cancelled` を記録。
- 操作ログには日時、操作名、ファイル対象やMemory ID、エラーコードを保存。
  Memory本文・メモ本文・検索語・Provider例外の詳細は保存しません。
  会話履歴にはユーザー入力と回答が保存されるため、本文が含まれます。
- 起動中断やDB障害で完了記録ができなかった操作は `started` が残ります。
  `started` は成功を意味しません。
- LLM・ファイル処理は別プロセスで実行。既定で1操作10秒、1会話30秒、
  1会話のツール呼び出し4回まで。SQLiteのロック待ちは1秒です。
- ツールが失敗したらそのターンを終了し、LLMによる成功への言い換えを防ぎます。
- タイムアウト・Ctrl+Cで子プロセスを停止し、CLIで次の入力を受けられます。
- 書き込み開始後の失敗・停止では、作成済みまたは途中のファイルが残る可能性があります。
  自動再試行・自動削除は行いません。成功は報告せず、対象を確認するよう案内します。
- 一人で1つのCLIを使う前提です。同時に複数のCLIから会話を進める運用は対象外です。

## テスト

APIキー・ネットワーク接続・追加パッケージなしで実行できます。

```sh
python3 -m unittest discover -s tests -v
```

テストでは一時ディレクトリだけを使い、通常の保存先には書き込みません。
主な検証内容は以下です。

- パストラバーサルと絶対パスによる読み取り・書き込みの拒否。
- ファイル・ディレクトリ・dangling symlink経由のアクセス拒否。
- Workspaceを開いた後のsymlink差し替え、許可ルートの差し替えの拒否。
- 「忘れて」後、再起動・複数ターンを通して古い会話から記憶が再利用されないこと。
- Memory更新時も古い情報が再利用されず、未削除のMemoryは残ること。
- ツール失敗後に成功を返そうとするProviderでも、成功表示しないこと。
- LLMタイムアウト、異常応答、例外、ツール回数制限、操作ログ。
- タイムアウトした子プロセスが停止し、後から書き込みを行わないこと。
- APIキーを除いた環境で、実際のCLIとmockのツールループが動くこと。
