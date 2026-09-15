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

Memoryの更新・削除は、本文を持たないsuppressionと参照元の依存関係を使って、
影響するMemory・summary・会話を同じSQLiteトランザクションで無効化します。
安全な会話と無関係なMemoryは維持します。詳細と保証範囲は末尾のPhase 4.1を参照してください。
削除後もIDを再利用しません。履歴の物理消去、既存メモ・Persona・バックアップの削除は対象外です。

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

## Phase 4: Memory高度化

Phase 1〜3のPersona、明示Memory、Conversation history、Tool permission、Audit、Voice、
Web/Calendar読取を維持し、SQLite内のMemory管理を拡張した。外部サービス・追加依存は不要。
PC操作、shell Tool、Gmail、Calendar書込み、常駐化、自律実行は追加していない。

### Architecture / schema

`Assistant → Store(MemoryStore) → SQLite` が唯一の保存経路。
CLIと音声は共通の `handle()` を通り、LLM向けTool schemaにはMemory書込みを公開しない。
通常チャットは関連Memoryと直近12発言だけをContextへ渡す。Web/Calendar要求は従来どおり
Memory・履歴を渡さず、外部結果由来の会話も後続Contextとsummaryから除外する。

既存DBは起動時に不足列を追加し、元のID・本文・日時を保持してexplicit memoryとして移行する。
`memories` は以下を保持する。

| 列 | 意味 |
| --- | --- |
| id | 永続ID、削除後も再利用しない |
| type | explicit_memory / user_preference / fact / project_context / temporary_context / conversation_summary |
| content | 最大4,000文字の本文 |
| source | explicit_user_command / user_conversation / conversation_summary |
| created_at / updated_at | UTC日時 |
| last_accessed_at | 実際にretrievalで選択された最終日時、未使用はNULL |
| confidence / importance | 0〜1、確信度と重要度。confidenceは真偽保証ではない |
| expires_at | optional。temporary_contextではtimezone付き日時が必須 |
| status | active / conflict / expired / superseded / forgotten |
| provenance | JSON。origin、epoch、conversation_ids。任意の外部metadataは保存しない |
| confirmed | 明示ユーザー操作で確認済みか |
| claim_key | 同一論点の競合検出用キー。例: preferred_language |
| fingerprint | NFKC・casefold・連続空白正規化後のSHA-256 |

補助テーブルは `memory_conflicts`（双方のID・状態・日時）、`memory_blocks`（削除本文のhash）、
`memory_policy`（自動保存停止フラグ）。操作監査は既存の `operations` を使用する。

### Retrieval / scoring

SQLiteからactive/conflict候補を読み、英数字単語・日本語の文字bigramでlexical検索する。
語の重なりがないMemoryはimportanceが高くても除外する。互換性のため「記憶を教えて」
「覚えていることは？」「確認」は限定的な記憶確認意図として扱う。この場合も取得上限は適用する。

```text
relevance = queryと本文の共通token数 / queryのtoken数
recency = 1 / (1 + 更新からの日数 / 30)
score = 5*relevance + importance + 0.5*recency + 0.5*type_weight + 2*confirmed
```

type_weightはexplicit=1、preference=.9、temporary=.8、project=.7、fact=.6、summary=.3。
関連候補のうちconfirmedを第一優先、次にscore降順、同点はID順。
既定最大6件・本文合計8,000文字。全件をLLMへ渡さない。
`retrieval_reason` に各得点要素、score、重なり数、取得理由、確認要否を記録する。
監査にはIDと数値理由だけを残し、query・本文・一致語は残さない。候補がゼロのDBでは
取得処理はno-opになり、retrieveログも作らない。期限切れは取得前に除外しexpireを一度記録する。

### Deduplication / conflict

正規化完全一致のみ同一IDへまとめる。自動候補は明示Memoryの本文・importanceを上書きしない。
自動候補をユーザーが明示追加した場合はconfirmedへ昇格する。
意味が似ているだけの文章は自動統合しない。検索時にもfingerprint単位で重複を除外する。

同一claim_keyで本文が違えば、sourceや日時が新しくても両方をconflictとして保持する。
回答への投入を保留し、理由にconfirmation_requiredを付ける。CLI登録時にも確認方法を案内する。
`/memory show ID` で双方の参照元・時刻・明示性を確認する。
`/memory update ID 内容` はユーザーによる採用値指定として扱い、競合相手をsupersededにする。
片方をforgetすることでも競合を解除できる。claim_keyのない自由文間の意味的矛盾は検出しない。

### Summary / forget保証

`/memory summarize` は古い安全なユーザー発言を最大4,000文字の抽出型summaryにまとめる。
直近12発言、assistant、tool、Web/Calendar、secret検出対象、コマンド入力は対象外。
元のconversation IDsとepochを保持し、同じ参照元を繰り返しsummary化しない。
LLMで文章を再生成せず、検証可能な発言の連結を採用している。元会話のローカル保存は維持する。

Phase 4時点のDB全体停止方式は、Phase 4.1のscoped invalidationへ置き換えています。
更新・forget後も安全な元発言からsummaryを作成できます。下記Phase 4.1を参照してください。

### Privacy / audit

通常のチャットを自動的に恒久Memoryへ保存する抽出器は有効にしていない。
ホスト内部の非明示登録APIは、現epochのuser発言の実在参照と本文の完全な抽出一致を必須にする。
secret/password/API key/token/private keyのラベル・代表的な鍵形式・保存禁止表現を拒否する。
保存禁止のユーザー発言は、後続の自動保存も停止する。Web/Calendar/assistantを参照元にした
登録、外部source、自動confirmed指定、存在しない参照元、改変された抽出本文は拒否する。
音声も同じポリシーを通る。文字列検出で無印のあらゆる秘密を判別できるわけではない。
明示保存と既存Conversation historyはこの自動Memory禁止とは別の保存経路であり、暗号化は未実装。

add/update/retrieve/conflict/expire/forgetを既存監査へ記録する。本文・query・claim_keyは入れず、
エラーも固定コードとする。`/memory why` はこのプロセスの直近取得理由、`/logs` は永続監査。
明示コマンドの開始/完了ログと、保存トランザクション内のイベントが別々に記録される。

### CLI例

```text
覚えて 日本語で簡潔に回答する
/memory add --type user_preference --importance 0.9 回答には具体例が欲しい
/memory add --type project_context --claim-key current_project Personal AIのPhase 4を実装中
/memory add --type temporary_context --expires-at 2026-09-15T18:00:00+09:00 今日の作業はMemory検証
/memory list
/memory search 回答の好み
/memory show 1
/memory why
/memory update 1 日本語で要点と理由を回答する
/memory forget 1
/memory summarize
```

`--type / --importance / --claim-key / --expires-at` は本文の前に指定する。
更新・forget後も、安全な未要約の元発言があればsummaryを作成できます。

## Phase 4.1: Memoryの安全な再開

### forget / update architecture

- `source_edges` は `(parent_kind, parent_id) → (child_kind, child_id)` の依存関係。
  conversation → Memory/summary、Memory → assistant回答、履歴 → 後続会話を記録する。
  チャットでは実際に渡した直近履歴と取得Memoryを記録し、外部参照は別roleのまま保持する。
- forget/updateは `BEGIN IMMEDIATE` 内でsuppression保存、依存先の推移的無効化、
  既知の派生表現のコピーの無効化、競合解消、epoch更新、監査をまとめて確定する。
- `conversations.status=stale` は履歴・抽出・summaryの対象外。
  派生Memory/summaryは `stale` にし本文とclaim_keyを消す。数値の出典は再生成の追跡用に残す。
  forget対象は本文・provenance・claim_keyを消した `forgotten` tombstoneにする。
- epochは進行中の処理を拒否する世代番号として維持する。安全な会話だけ新epochへ移す。
  古いepochで到着した発言はstaleになり、自動抽出には最新epochの要求を必須とする。
- updateは旧値と競合の不採用値をsuppressionに追加し、対象IDを新しいcanonicalな
  `explicit_memory` に置き換える。confirmed=1、旧期限を解除し、旧revisionの依存辺を切り離す。
  旧値・不採用値・その派生物はretrievalから除外する。別の競合を無関係なforgetで解消しない。
- forget/updateは `automatic_disabled` を設定しない。「保存禁止」という明示指示の停止は維持する。
  `forget all` はMemoryが空でも既存会話を無効化するが、その後の安全な新規保存を停止しない。

### Tombstone / suppression

本文を保持する代わりに、NFKC・casefold・空白正規化後のSHA-256を保存する。

| テーブル | 保存情報・用途 |
| --- | --- |
| memory_blocks | 対象と既知の派生本文のdigest。正規化完全一致を拒否 |
| suppression_phrases | digestと正規化文字数。文章中に埋め込まれた既知の本文も拒否 |
| suppression_terms | 対象本文の英数字単語・日本語bigramのdigest。部分引用や語順変更を保守的に拒否 |
| source_edges | 種別と数値IDのみ。語句が異なっていても既知の派生経路を遮断 |

派生summaryの全単語を新たな禁止語にすると、その中の無関係な元発言まで使えなくなるため、
派生本文はphrase digestを保存する。自動登録は候補だけでなく各元発言についても検証する。
監査・tombstone・suppression・依存辺へsecret本文や任意metadataを入れない。
ハッシュは暗号化ではなく、低エントロピーの語句は辞書照合で推測され得る。

### Summary regeneration

summaryは引き続きLLMを使わない、最大4,000文字の元発言の連結。
activeなuser発言のうち、suppression・secret・保存禁止・コマンドを除き、
activeなsummaryに未収録の発言から再生成する。assistant/Web/Calendarは参照元として拒否する。

例えば `Python Alpha` と `gardening roses` を含むsummaryで前者をforgetすると、
旧summaryをstaleにし、独立した安全な元発言 `gardening roses` から新summaryを作れる。
無効な元発言の一部分を切り出して安全だと推測する処理は行わない。
有効なsummaryの収録済みIDはepochをまたいで維持し、再生成の重複を防ぐ。

### 既存DBの移行

列・テーブル追加は再実行可能。Phase 4以前の会話は依存情報が不足するため、同epochの
直前12発言と、assistantについては既存Memoryを保守的な参照候補として補完する。
旧epochの会話を復活させない。

旧方式で停止済みのDBは、保存された会話からすべての削除fingerprintの元表現を照合でき、
「保存禁止」の指示がない場合に限りsuppressionを構築して再開する。
元表現が失われている場合や停止理由を安全に解除できない場合は、旧停止を維持する。
新方式で行ったforget/updateではこの移行上の停止は発生しない。

### 保証範囲と残る制約

- **保証する範囲**：記録された元発言・依存グラフからの復活、既知の本文の正規化一致・
  埋め込みコピー、保持した語句digestと一致する再入力。音声も同じ保存経路を使う。
- **任意の意味的な言い換えまでは保証しない**：依存関係のない新規ユーザー発言で、
  既知の表現・語句と一致しない同義語や別言語を使われると、ハッシュでは同一事実と識別できない。
  この意味で「派生した内容を一切再保存しない」という無条件の保証は未達。
  LLMによる要約・自動言い換え抽出は導入していない。
- 語句の一致は安全側に判定するため、共通語を持つ無関係な自動候補も除外することがある。
  例えば旧値と新値が同じ単語を含むと、新値を含む自動summaryも除外し得る。
  canonicalな明示Memoryのretrievalにはこの語句フィルタを適用しない。
- 履歴の文脈に依存した後続発言は、内容が無関係に見えても保守的にstaleになる。
  無効化単位は発言全体。独立した安全な元発言の保存・summaryは継続できる。
- 通常チャットでの自動抽出器は引き続き無効。今回継続可能にしたのはホスト内部の
  検証付き自動保存APIとsummary生成。明示的な再学習は従来どおり可能。
- 元会話はローカルDBに残る。物理削除、暗号化、バックアップ消去、外部Provider独自の記憶は対象外。
  suppressionの本文走査と依存グラフの無効化は、大量データ向けの最適化をしていない。

### 検証

`python3 -m unittest discover -s tests -q`

実行結果: **127件すべて成功**（約35秒、外部サービス・音声機器なし）。
Phase 4の104テストに23件を追加。旧仕様を検証していた1件は、無関係なforgetで別の競合を
解消しない期待値へ更新した。その他の既存103件は維持している。
追加テストは継続保存、混在summary再生成、正規化・部分引用・既知の派生コピー、更新、
再起動、voice、Web/Calendar境界、世代競合、トランザクションrollback、privacy、DB移行を検証する。

Phase 5のPC操作、shell Tool、daemon、自律実行は追加していない。

## Phase 5: Safe local task execution / PC操作・タスク委譲

### Task architecture

Phase 5のPC操作は **現在の明示的なユーザー入力**からのみ開始する。
`User request → Task proposal → Risk classification → Permission → Execution → Verification → Result`
を `TaskManager` が管理する。LLMにPC操作Toolのschemaを渡さず、LLMの出力・履歴・Memory・
Web/Calendar結果・読んだfile本文からTaskを生成しない。

`Task` は `task_id`, `requested_at`, `intent`, `proposed_actions`, `risk_level`,
`permission_state`, `started_at`, `finished_at`, `status`, `result`, `error`,
`audit_reference`, `allowed_root` を持つ。計画はdeep copyし、承認後は最大8stepを順に実行。
呼出元が返却された計画を変更しても実行対象は変わらない。Provider・許可フォルダ・repository設定が
提案後に変わった場合も承認を拒否する。Taskの承認は一回限りで、再実行・再起動後の引継ぎはない。

| 状態 | 意味 |
| --- | --- |
| proposed | 計画表示済み、承認待ち |
| running | 承認済み、実行中 |
| completed | 全stepの実行後検証が成功 |
| unverified | 実行要求は処理されたが、OS状態などを確認できないstepがある |
| failed | 完了を確認できた先行stepなし。errorが副作用不明を示す場合は対象の確認が必要 |
| partial | 先行stepまたは検証前の実行結果あり。後続stepは停止 |
| denied / cancelled | 未実行の計画を拒否 / 取消 |
| expired / interrupted | 別セッションの未実行計画 / 終了を記録できなかった実行。自動再開しない |

`--timeout` が各実行・検証、`--turn-timeout` がTask全体の上限。
Ctrl+C・音声取消・timeoutではworkerとそのsubprocess groupを停止する。
実行workerのtimeout・応答喪失は `*_effects_unknown` として表示する。
書込み済みの可能性があるため、自動再実行せず対象を確認する。各stepの開始前にもTask全体の期限を検査する。
`/task cancel ID` は承認待ちTask用。同期実行中の取消にはCtrl+Cを使用する。

### Risk / Permission

| Risk | 操作 | Policy |
| --- | --- | --- |
| LOW | list/read、対応app一覧、system info、許可されたread-only command、listのfilter | 明示依頼した計画だけに権限を付与 |
| MEDIUM | create file/directory、rename/copy/move、file/app open、Finder reveal | 計画・対象root・引数を表示し、`/task approve ID` 後だけ実行 |
| HIGH | delete、overwrite、process termination、shell、external upload、未知の操作 | 今回はすべて無効。承認しても実行可能にしない |

HIGH操作の実装を追加していないため、HIGHを確認なしで実行する経路はない。
未知のcommand・追加引数・`overwrite: true`・任意app名を拒否する。
Providerでもschema、固定risk、操作名と全引数のdigestに結び付いたホスト発行の権限を再検査する。
文字列の `permission=granted`、別Taskの操作用権限、riskをLOWに変えた権限は通らない。

従来の `/note` と明示的な `/tool create_note` はPhase 1の単発メモ作成契約を維持する。
Phase 5の汎用ファイル作成は `/task` 経由で承認が必要。LLMが独自にメモ書込みを提案する経路は拒否し、
`read_note` の本文を受け取った後のTool連鎖も拒否する。`search_notes` の既存read-onlyループ上限は維持。

### Local Tool / macOS Provider

`LocalProvider` は `execute` / `verify` と設定情報を持つ、ホストが選ぶ信頼済みadapter。
`FileLocalProvider` が既存 `Workspace` を拡張したPOSIXファイル処理を共有し、
`MockLocalProvider` と `MacOSLocalProvider` がOS依存処理を別々に実装する。
新しいOSではProviderを追加する。現時点の共通ファイル層・process group制御はPOSIX用で、Windows対応済みではない。

| Tool | 現在の範囲 |
| --- | --- |
| list_directory | 指定した1階層、最大1,000entry。symlinkはblockedとして表示 |
| read_file | 最大64 KiBのUTF-8通常file。拡張子は限定しない |
| create_file / create_directory | 新規作成のみ、親directoryは既存であること |
| copy / move / rename | 最大64 KiBのUTF-8通常file。directoryの移動・再帰copyは対象外 |
| open_file | 許可フォルダ内の.md/.txtを既定appで開く |
| reveal_in_finder | 同じ.md/.txtをFinderで表示 |
| open_application | 固定allowlistのTextEdit / Calculatorを開く |
| list_applications | 上記の対応allowlistを表示。全インストール済みappの走査はしない |
| get_system_info | OS・release・architecture。mockでは固定値 |
| command | 下記の構造化された固定commandのみ |
| filter_files | 直前のdirectory listingを拡張子で絞り込む純粋処理 |

既定は `--local-provider mock`。mockも許可フォルダ内のファイルは実際に読み書きするが、
app起動・command・system infoはシミュレーションであり、GUIや外部サービスは使わない。
テストでは一時フォルダを使用する。
`--local-provider macos` で `/usr/bin/open` の引数配列によるfile/app起動・Finder表示を有効にする。
GUIクリック、キーボード操作、screen readingはない。

### File safety

- `--notes-dir` をPhase 1と同じ許可フォルダとして使用する。data directoryとの重なりも従来どおり拒否。
- file指定はrootからの相対pathのみ。directory listing / commandのroot指定は `.`。
  絶対path、`..`、余分な `.`、空component、NUL、backslashを拒否する。
- rootのdevice/inodeを検査し、各親directoryとfileをdescriptor-relativeに `O_NOFOLLOW` で開く。
  隠しdirectoryにも同じ検査を行い、copy/moveのsourceとdestination両方へ適用する。
- hardlink、FIFOなどの特殊fileを読まない。symlinkを辿らず、outsideへの書込みを行わない。
- create/copyはexclusive create。move/renameは同一filesystem上でexclusive hardlinkを作成し、
  inodeを検査してsourceをunlinkする。既存destinationを置換しない。
- 上書きは承認後も実装していない。既存fileの場合は `already_exists_no_overwrite`。
  delete / Trash / recursive deleteも未実装。
- file操作の競合・中断は原子的な一括成功を保証しない。move途中に2つのlinkが残るなどの状態を
  `partial` または副作用不明のerrorで扱い、後続処理と危険な自動rollbackを行わない。

macOSのLaunchServices/Finderはpathを受け取るため、file openでは直前に再検査しても、
**別のローカルプロセスによる検査直後の悪意あるpath差替えまで防ぐOS brokerは未実装**。
通常のtraversal/symlink escapeは拒否するが、この競合まで含む無条件のsandbox保証はしない。
既定appの動作や起動完了も保証しないため、起動結果は `verified=false` とする。

### Shell safety / read-only Git

`command` はshell文字列ではなく、次の固定識別子だけを受理する。

- `pwd`
- `git status` / `git diff` / `git log` / `git branch`
- `python --version` / `node --version`

`subprocess` は固定の絶対実行pathと引数配列。shell、eval、exec、sudo/su、任意bash/zsh、
追加option、pipe、command substitution、chmod/chown、credential操作を渡す口はない。
process起動直前にも引数配列のallowlistを検査する。stdoutとstderrの合計は64 KiB、processは5秒で制限。
継承する環境を限定し、`NODE_OPTIONS`、`PYTHONPATH`、Gitのcommand設定などを引き継がない。
Pythonは `/usr/bin/python3`、Nodeは `/opt/homebrew/bin/node` または `/usr/local/bin/node`。
見つからなければ失敗し、自動installやPATH探索はしない。

Gitは `--allowed-repository repo` のように明示設定したroot相対repository内だけ。
元repositoryに対してGitを起動せず、descriptor経由で読み取ったprivateな一時snapshot上で実行する。
元の `.git/config`・hooksを除外し、固定configを使用。symlink/hardlink、gitdir/commondir、
alternates、submodule、入れ子repository、worktreeの間接参照を拒否する。
`--no-optional-locks`、pager無効、fsmonitor/hooks無効、protocol無効、
diffの `--no-ext-diff --no-textconv` を固定する。logは最新30件。
commit/push/reset/clean/checkoutのcommandは存在しない。

snapshotは1,000entry、深さ16、1file 4 MiB、合計16 MiBまで。
大きなrepository、symlinkを含むrepository、sparse/SHA-256等の特殊repositoryは対応範囲外。
元configを省くため、filter・local ignore等に依存する結果は通常のGit表示と異なり得る。
同時更新下の一貫したtransaction snapshotではなく、結果には `snapshot=true`, `verified=false` を付ける。
親プロセスが一時領域を所有し、workerのtimeout・取消後も削除する。
アプリ全体の強制終了・OS障害後の残存一時領域の回収は今後の課題。

### Task examples / CLI

```console
python3 -m personal_ai --notes-dir notes --local-provider mock
```

```text
このフォルダのPythonファイル一覧を出して
# list_directory → filter_files → result

demoフォルダとREADMEを作って
# 計画を表示。まだ作成しない
/task approve <表示されたID>
# create_directory → verify → create_file → verify

/task propose [{"name":"copy","arguments":{"source":"hello.txt","destination":"copy.txt"}}]
/task deny <表示されたID>
/task list
/task show <ID>
/task cancel <未実行ID>
/tools
/permissions
```

macOSのDocumentsを対象にする場合は最初から許可フォルダを明示設定する。
既定のnotesをDocumentsとして黙って扱うことはない。

```console
python3 -m personal_ai --notes-dir "$HOME/Documents" --local-provider macos --allowed-repository demo-repo
```

```text
DocumentsにdemoフォルダとREADMEを作って
/task approve <表示されたID>
demo/README.mdを開いて
/task approve <表示されたID>
demo/README.mdをFinderで表示して
/task approve <表示されたID>
TextEditを開いて
/task approve <表示されたID>
demo-repoでgit statusを表示して
```

自然文は上記や `PATHを読んで`, `PATHのPythonファイル一覧を出して`,
`システム情報を表示して`, `アプリ一覧を表示して` の厳密な文型のみ。
任意の文章をLLMで実行計画に変換する機能ではない。その他の計画は `/task propose` で明示する。
file本文に `/task approve ...` 等があっても再解釈しない。

### Voice / Memory / Audit

Voiceは既存の `handle()` を通るため、Textと同じ計画表示・ID付き承認・拒否・検証を使用する。
音声の「はい」を包括承認と解釈せず、`/task approve ID` の正確なtranscriptが必要。
STT/TTSの既存cloud permissionや取消・時間制限はそのまま適用する。

Taskの依頼・計画・path・file本文・command output・system infoは会話テーブルへ追加せず、
LLM context・Memory自動抽出・summaryへ渡さない。詳細は現在のセッションのRAM内だけ。
終了時に参照を解放し、別セッションでは本文を復元せず再実行もできない。
Python文字列の物理的なメモリ消去を保証するものではない。

SQLiteの `local_tasks` は時刻・状態・risk・権限・固定操作名・stepごとの検証成否・監査IDだけを保持する。
本文、path、command識別子/出力、content hash、system infoは記録しない。
`intent` / 引数は `[not retained]`、resultは検証成否のみとして再表示する。
既存DBにはtableを追加するだけで、Memory/forget/updateのtableや移行を変更しない。

既存operationsへ `task_proposed`, `task_permission_requested`, `task_permission_granted/denied`,
`task_tool_started/completed/failed`, `task_verification`, `task_rollback` を記録する。
metadataはtask ID・固定Tool名・step番号・risk・検証booleanなどに限定し、Provider例外も安全なcodeに変換する。
不正・無効操作の提案は `task_proposal_denied`。
rollbackは `not_attempted_manual_review_required` を記録してresourceを残す。
安全性を証明できる作成resourceの識別・競合制御がないため、自動rollbackや汎用undoは追加していない。

### 検証とPhase 6前の課題

標準テストはmockと一時フォルダだけで実行でき、macOS app・実Git・音声機器・外部接続は不要。
macOS起動はprocessをmockして引数を検証する。別途macOSの一時repositoryで4種の実Gitを実行し、
期待した表示と元repositoryのfile hashが変わらないことを確認した。実GUIの起動は行っていない。

```console
python3 -m unittest discover -s tests -q
```

既存127テストを維持し、Phase 5の65テストを追加（合計192件）。承認の対象固定、
ファイル境界、構造化command、Git snapshot、複数stepの検証・失敗停止、
取消・timeout、worker応答喪失、Memory/Auditへの本文非保存を検証する。

Phase 6前に扱う課題:

- macOS file openの競合も閉じるOS brokerと、実機でのLaunchServices/起動状態の検証。
- Windows/Linuxのnative Providerと、それぞれの安全なfile・subprocess取消実装。
- 大きいrepository、特殊Git構成、directory copy/moveへの安全な対応。
- resource所有権と同時更新を検査できる限定rollback、強制終了後の一時領域回収。
- 自然文の対応文型拡充。LLMを導入する場合も、現在の承認・出典境界をホスト側で維持すること。

常駐化・自律実行・scheduled task・任意shell・GUI自動操作・browser操作・remote PC・
Gmail送信・Calendar書込み・credential管理・自己改変は追加していない。
