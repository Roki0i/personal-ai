# Personal AI

JARVIS / アマデウスを目標に、段階的に育てるパーソナルAIアシスタント。
Phase 1は、Python・CLI・SQLiteによるローカルの基盤です。

現在のLLMは**外部通信しないmock**です。APIキーも外部パッケージも不要で、
会話履歴・記憶・権限判定・ファイル操作・失敗処理を試せます。
mockは固定ルールによる動作確認用であり、自然言語を理解する実際のLLMではありません。
Phase 2のPush-to-Talk音声入力・応答を追加しています（既定は無効）。
Web検索・PC操作・常駐処理・クラウドAPI接続は実装していません。

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
  runtime.py    別プロセス実行、タイムアウト、停止、共有キャンセルスコープ
  voice.py      音声Protocol、権限、状態管理、PTT制御、mock
  voice_local.py Vosk / Piper / sounddeviceの任意ローカル実装
tests/
  test_mvp.py   セキュリティ、失敗処理、CLIの統合テスト
  test_voice.py 音声パイプライン、失敗、停止、privacy、ローカル接続テスト
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


## Phase 2：Push-to-Talk音声インターフェース

### 外部サービスなしで動かす

```sh
python3 -m personal_ai --persona persona.json --voice mock
```

`/voice` を入力し、次のプロンプトでEnterを押すと1回だけ処理します。
mockはマイクもスピーカーも使わず、メモリ内のダミーPCM → 固定STT →
既存Assistant → ダミーTTS → 模擬再生を実行します。
文字の応答を表示した後、通常の `you>` に戻ります。
認識結果を変えるには `--mock-transcript 'こんにちは'` を指定してください。
`--mock-transcript '/read missing.md'` で操作失敗の読み上げ経路も試せます。

### アーキテクチャ・交換点

```text
/voice → Enterで録音開始 → Enterで録音停止（最大30秒）
       → Recorder.record → SpeechToTextProvider.transcribe
       → 既存 cli.handle → Assistant / Memory / Tools
       → テキスト表示 → TextToSpeechProvider.synthesize → Player.play
```

`voice.py` のProtocolを実装し、`VoiceSession(assistant, stt, tts, recorder, player)`
に注入できます。STTとTTSは互いに独立して交換可能です。

| interface | 入出力 | 標準の選択肢 |
| --- | --- | --- |
| `SpeechToTextProvider` | `transcribe(Audio) -> str` | `MockSTT`、`VoskSTT` |
| `TextToSpeechProvider` | `synthesize(str) -> Audio` | `MockTTS`、`PiperTTS` |
| `Recorder` | `record(stop_event, max_seconds) -> Audio` | `MockRecorder`、`SoundDeviceRecorder` |
| `Player` | `play(Audio) -> None` | `MockPlayer`、`SoundDevicePlayer` |

`Audio`はモノラル・16-bit little-endian PCM、サンプルレート、送信分類を持ちます。
STT・TTSには設定側の固定`provider_id`と`location`（`local` / `cloud`）が必要です。
providerはimport可能・pickle可能な、信頼されたステートレスPythonコードにします。
モデルや音声のダウンロードは起動時にも認識時にも行いません。

音声入力も `cli.handle` を通るため、明示Memoryコマンド、Persona、Memoryのepoch、
Tool権限、ファイル安全性、監査ログ、既存の操作・会話タイムアウトが同じです。
`/memory add ...` と認識された明示コマンドはテキスト入力と同じくMemoryを変更します。
音声の誤認識を確認する画面は未実装なので、重要な明示コマンドはテキストで確認できます。
Tool失敗時はPhase 1が生成した失敗の応答をそのままTTSへ渡し、LLMで成功に言い換えません。

### 状態と停止・失敗

`idle → recording → transcribing → reasoning → speaking → idle` が通常の遷移です。
`speaking` は音声合成と再生の両方を含みます。
録音・認識・合成・再生の失敗は`failed`、キャンセルは`cancelled`になります。
どちらの状態からも次の `/voice` で再試行できます。
`idle`への復帰は音声パイプラインの完了を意味し、Toolの成功を意味しません。
LLM/Toolの失敗は既存経路のテキスト応答として表示・読み上げます。

- ローカル録音中のEnter：録音を終了し、STTへ進みます。
- Ctrl+C：録音・STT・LLM・Tool・TTS・再生の現在の処理をキャンセルします。
- APIの `cancel()` / `stop_recording()` は別スレッドから呼べます。
  `run()` 自体はSQLiteを所有するメインスレッドで実行してください。
- ブロッキング処理は子プロセスで実行し、キャンセル・タイムアウト時は停止して回収します。
- STT失敗時は会話を開始せず、通常のテキスト入力へ戻れます。
- 応答は合成前に表示します。TTS・再生失敗でも `VoiceResult.text` と既存の会話履歴に残ります。
  Memory等のCLIコマンド応答はPhase 1と同じ保存規則で、戻り値と画面には残ります。
- 既定の音声操作制限は15秒、全体は90秒（録音含む）。録音段階の制限は30秒＋起動猶予15秒です。
  `--voice-timeout` / `--voice-turn-timeout` で変更できます。
  既存の `--timeout` / `--turn-timeout` も同時に効き、より早い期限で停止します。
- MemoryのSQLite更新は短い原子的処理です。完了済みのMemory更新やファイル書き込みを
  キャンセルで巻き戻すことはありません。

### Privacyとcloud境界

raw録音と合成PCMはRAMと子プロセス間通信のみで扱い、音声ファイル・DB・監査ログには保存しません。
保存を有効にするオプションも追加していません。処理終了時にアプリ側の参照を解放します。
OSのswapやクラッシュダンプを含む安全なメモリ消去を保証するものではありません。

**認識テキストと回答は、Phase 1と同じ会話履歴としてローカルSQLiteに保存されます。**
音声や会話からMemoryを自動抽出・登録することはありません。
`voice_record` / `voice_stt` / `voice_tts` / `voice_play` の監査には開始・成否・固定エラーコードだけを保存し、
PCM・認識本文・回答本文・provider例外詳細は記録しません。
既存LLM/Tool/Memoryの監査ログもそのまま残ります。

`VoicePermission`は各`run()`に渡す明示的な設定です。既定は全データ`local-only`、cloud許可なしです。

| 段階 | cloud呼出しに必要な条件（両方必要） |
| --- | --- |
| STT | `input_policy=CLOUD_SENDABLE` ＋ 対象`provider_id`が`cloud_stt`に存在 |
| TTS | `response_policy=CLOUD_SENDABLE` ＋ 対象`provider_id`が`cloud_tts`に存在 |

`cloud-sendable`分類だけでは送信許可になりません。許可だけでも`local-only`を送信できません。
STT許可はTTSに引き継がず、次のターンにも記憶しません。
回答にはMemoryやローカルファイルの内容が含まれ得るため、入力とは独立して既定`local-only`にします。
将来cloud TTSを接続するUIは、**回答全体の内容を送信する許可**を明示的に得た上で、この両条件を設定する必要があります。
認識テキストやLLM出力から権限を付与する経路はありません。
不明な`location`や権限不足はproviderを呼ぶ前に拒否し、`denied`を監査します。

cloud providerやcloud設定用CLIは今回追加していません。
この境界は信頼されたアダプターに対するアプリ制御であり、悪意あるPythonコードの通信を
OSレベルで隔離するものではありません。ローカルと宣言するproviderは通信・独自保存を行わない必要があります。

### 実マイク・ローカルモデルで動かす

実機用の任意依存を別の仮想環境に導入します（導入時のパッケージ取得にはネットワークが必要です）。

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[voice]'
```

Voskモデルの展開済みディレクトリと、Piper音声の`.onnx`および同名の`.onnx.json`を
事前に用意してください。モデルは信頼できる配布元から、使用言語と利用条件に合うものを選びます。
本体から自動取得はしません。sounddeviceには利用可能なPortAudioと入力・出力デバイスが必要です。
macOSでは利用するターミナルにマイク権限を与えてください。

```sh
python3 -m personal_ai --persona persona.json --voice local \
  --stt-model /absolute/path/to/vosk-model \
  --tts-model /absolute/path/to/voice.onnx \
  --voice-timeout 30 --voice-turn-timeout 120
```

1. `/voice`を入力し、Enterで録音開始。
2. 話し終えたらEnterで停止（最大30秒で自動停止）。
3. 認識・既存Assistantの処理後、画面に回答を表示し、音声再生。
4. 任意の段階でCtrl+Cで取消。通常の文字入力も引き続き使用可能。

モデル・依存パッケージ・デバイスがない場合は音声処理が失敗し、文字入力へ戻ります。
アダプターは[Voskの公式録音例](https://github.com/alphacep/vosk-api/blob/master/python/example/test_microphone.py)と
[PiperのPython API](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md)に沿っています。

### 検証範囲と制約

`python3 -m unittest discover -s tests -v` でPhase 1・Phase 2をまとめて検証できます。
テストは外部サービス・追加パッケージ不要です。
mock一連処理、全ブロッキング段階の停止とタイムアウト、STT/TTS/再生失敗、
忘却後の再起動、Tool失敗、raw音声の非保存、STT/TTSごとのcloud拒否・許可を検証します。
ローカルアダプターのAPI接続は偽の依存モジュールでテストします。

実マイク、実モデルの認識精度・発音・遅延はこの環境では未検証です。
日本語STTには日本語モデル、TTSには回答言語に対応する音声モデルが必要で、
Piperの利用可能な音声と言語に制約があります。英語音声で日本語の読み上げ品質は保証できません。
LLMは引き続きPhase 1のmockです。自然な音声対話には本物のLLM実装が別途必要です。
モデルは各操作で読み込むため起動遅延があり、ストリーミング応答、割込み会話、複数同時セッションは未対応です。
常時マイク・wake word・Web検索・Calendar・PC/shell操作・daemon・自律タスクは追加していません。

## Phase 3: Web・外部サービス連携基盤

標準構成は完全offlineの `MockWebSearchProvider` / `MockCalendarProvider`。
APIキー・ネットワーク・追加依存なしで利用・テストできる。

```text
PythonをWebで調べて
/web Python
今日の予定は？
/calendar
/event demo
```

テキストは `python3 -m personal_ai` で起動して上記を入力する。
音声mock例:

```sh
python3 -m personal_ai --voice mock --mock-transcript 'PythonをWebで調べて'
python3 -m personal_ai --voice mock --mock-transcript '今日の予定は？'
```

起動後 `/voice` → Enter。local音声でも同じ発話を使う。STT後は既存の
`handle → Assistant.chat → LLM proposal → Tools.execute → permission → run_bounded`
を通る。音声専用の検索実行・権限バイパスはない。

### アーキテクチャとProvider

`personal_ai/external.py` のProtocolを実装してAssistantの `web_provider` /
`calendar_provider` に注入する。アダプターは信頼された、import可能・pickle可能な
statelessコードとし、外部サービスとの通信を担当する。LLMには通信アダプターを渡さない。
既存のLLM Provider同様、任意Pythonコード自体をOSレベルで隔離するセキュリティsandboxではない。

- `WebSearchProvider.search(query)` は最大20件の辞書を返す。必須キーは
  `title, url, snippet, retrieved_at, provider`。URLはhttp(s)、時刻はtimezone付きISO形式。
- `CalendarProvider.list_events(date)` / `get_event(event_id)` は読み取り専用。
  イベントは `id, title, date, retrieved_at, provider`。Phase 3の最小契約は日単位で、
  時刻・繰り返し・参加者はまだ扱わない。今日の日付はホストのローカルtimezone。
- 未知キー、欠損、型不正、件数/文字数超過、providerの不一致は `malformed_response`。
- `ConnectionError` は `network_failure`、`TimeoutError` とruntime期限超過は `timeout`、
  その他のProvider例外は `provider_unavailable`。例外本文は記録しない。

### 検索判断・送信のprivacy境界

安全側に限定した入力パーサーが**現在の明示的ユーザー入力だけ**から要求を確定する。
LLMのTool proposalは名前・引数がその要求と完全一致する場合のみ通る。
Memory・ファイル・過去会話からのquery補完、曖昧な「それを調べて」の解決は行わない。
`/tool` JSON経由だけで外部通信を許可することもない。

`ExternalPermission` はホストAPIが渡すターン限定の権限で、モデル引数には含めない。

| 分類 | 挙動 |
|---|---|
| `local-only` | 送信不可。approvedでも拒否 |
| `cloud-sendable` | 指定Providerと現在の確定要求に限り許可 |
| `explicit-approval-required` | 同じ条件に加えてホスト側の `approved=True` が必要 |

組込みmockのみ明示的検索/予定入力に標準許可がある。交換したProviderはdefault deny。
実接続を組み込むホストは、ユーザーに送信内容と送信先を示して分類・承認を取得すること。
承認UIは今回未実装で、音声から実Providerへの送信も標準では拒否される。

```python
from personal_ai.external import ExternalPermission, PrivacyClassification

answer = app.chat('/web Python', external_permission=ExternalPermission(
    classification=PrivacyClassification.EXPLICIT_APPROVAL_REQUIRED,
    approved=True,  # ホストがこの入力と送信先について承認を得た後のみ設定
    provider='your-provider-id',
))
```

分類はホストが管理する。秘密の自動検出・推測は行わないため、秘密を含むユーザー入力は
ホストがlocal-onlyにする必要がある。既存Memory/ローカルファイルは外部要求の情報源に
できず、忘れたMemoryもqueryへ補完できない。外部参照ターンのLLMコンテキストには
Memory・履歴を含めない。この分類はWeb/Calendarの送信境界であり、既存の任意LLM
Providerや音声cloud設定の通信方針を置換するものではない。

### 外部データ・監査・失敗

検索結果は `trust=untrusted_external_data` のデータとしてLLMへ渡す。
Contextにも「命令として扱わない」方針を設定する。文字列の危険語除去に依存せず、
外部参照ターンでは確定要求以外のToolを最初から禁止し、外部結果受領後のTool proposalを
すべて拒否する。同一応答内での複数Tool実行も禁止。
APIキー送信、ローカルファイル読み取り、権限変更を外部本文から実行する経路はない。

回答には生成に提供した結果のURL/取得時刻/Provider（予定はID）をruntimeが追記する。
これは提供した根拠候補の追跡であり、LLMの各文がその根拠から導かれることの検証ではない。
監査にはprovenance・送信分類・Providerを保存し、queryやsnippetは保存しない。
会話DBには外部回答を `external_assistant`、要求を `external_user` として保存し、
次回以降の `Store.history()` から除外する。これにより遅延したprompt injectionを防ぐ。
外部会話はDBに残るが通常の履歴APIには現れない。Memoryへの自動保存はしない。

取得失敗はruntimeが失敗応答を返し、LLMによる内部知識への暗黙fallbackを行わない。
外部要求に対してTool結果なしで回答するLLMも `external_result_missing` とする。
既存の操作/ターンtimeoutとcancelでProvider処理を終了し監査に成否を残す。

検証: `python3 -m unittest discover -s tests -q`。
実Webサービス、ページ全文取得、Calendar書き込み、Gmail、PC操作、shell Tool、任意ファイル参照、
ブラウザー操作、wake word、daemon、自律処理・background実行は未実装。
