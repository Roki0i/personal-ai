import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from personal_ai.app import Assistant
from personal_ai.models import Reply, ToolCall
from personal_ai.voice import (Audio, DataPolicy, MockPlayer, MockRecorder, MockSTT,
                               MockTTS, VoicePermission, VoiceSession, VoiceState)
from personal_ai.voice_local import VoskSTT, PiperTTS, SoundDeviceRecorder, SoundDevicePlayer


class FailingSTT(MockSTT):
    def transcribe(self, audio):
        raise RuntimeError("SECRET_RAW_AUDIO")


class FailingTTS(MockTTS):
    def synthesize(self, text):
        raise RuntimeError("SECRET_RESPONSE")


class FailingPlayer(MockPlayer):
    def play(self, audio):
        raise RuntimeError("device failed")


class SlowSTT(MockSTT):
    def transcribe(self, audio):
        time.sleep(5)
        return "too late"


class SlowTTS(MockTTS):
    def synthesize(self, text):
        time.sleep(5)
        return super().synthesize(text)


class SlowRecorder(MockRecorder):
    def record(self, stop, max_seconds):
        time.sleep(5)
        return super().record(stop, max_seconds)


class StoppableRecorder(MockRecorder):
    def record(self, stop, max_seconds):
        stop.wait(max_seconds)
        return super().record(stop, max_seconds)


class SlowPlayer(MockPlayer):
    def play(self, audio):
        time.sleep(5)


class InspectLLM:
    def generate(self, context):
        return Reply(json.dumps({"persona": context.persona.name,
                                 "memories": context.memories,
                                 "history": context.history}, ensure_ascii=False))


class SlowLLM:
    def generate(self, context):
        time.sleep(5)
        return Reply("too late")


class FailureClaimLLM:
    def generate(self, context):
        if context.results:
            return Reply("成功しました")
        return Reply("成功しました", [ToolCall("read_note", {"path": "../secret.md"})])


class VerifyFailureTTS(MockTTS):
    def synthesize(self, text):
        if "操作は成功していません" not in text or "成功しました" in text:
            raise ValueError("incorrect spoken claim")
        return super().synthesize(text)


class VerifyTextTTS(MockTTS):
    def synthesize(self, text):
        if "mock" not in text:
            raise ValueError("assistant text missing")
        return super().synthesize(text)


class RawRecorder(MockRecorder):
    def record(self, stop, max_seconds):
        return Audio(b"PRIVATE_RAW_AUDIO" * 32)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Assistant(self.root / "data", self.root / "notes", timeout=3)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def voice(self, **kwargs):
        return VoiceSession(self.app, kwargs.pop("stt", MockSTT()),
                            kwargs.pop("tts", MockTTS()),
                            kwargs.pop("recorder", MockRecorder()),
                            kwargs.pop("player", MockPlayer()), timeout=3, **kwargs)

    def test_mock_end_to_end_and_states(self):
        voice = self.voice(tts=VerifyTextTTS())
        shown = []
        result = voice.run(on_text=lambda text: shown.append((voice.state, text)))
        self.assertEqual(result.state, VoiceState.IDLE)
        self.assertEqual(shown, [(VoiceState.REASONING, result.text)])
        self.assertEqual(voice.states, [VoiceState.RECORDING, VoiceState.TRANSCRIBING,
                                      VoiceState.REASONING, VoiceState.SPEAKING, VoiceState.IDLE])
        self.assertEqual(self.app.store.history()[-1]["content"], result.text)
        names = {row["name"] for row in self.app.store.operations()}
        self.assertTrue({"voice_record", "voice_stt", "llm_generate", "voice_tts", "voice_play"} <= names)
        self.assertEqual(self.app.memory("list"), [])

    def test_stt_failure_returns_to_text_and_can_retry(self):
        voice = self.voice(stt=FailingSTT())
        result = voice.run()
        self.assertEqual(result.state, VoiceState.FAILED)
        self.assertEqual(result.text, "")
        self.assertEqual(self.app.store.history(), [])
        self.assertNotIn("SECRET", str(self.app.store.operations()))
        self.assertIn("mock", self.app.chat("テキストで続ける"))
        voice.stt = MockSTT()
        self.assertEqual(voice.run().state, VoiceState.IDLE)

    def test_tts_and_playback_failure_preserve_answer(self):
        for adapter in ({"tts": FailingTTS()}, {"player": FailingPlayer()}):
            with self.subTest(adapter=adapter):
                shown = []
                result = self.voice(**adapter).run(on_text=shown.append)
                self.assertEqual(result.state, VoiceState.FAILED)
                self.assertEqual(shown, [result.text])
                self.assertIn("mock", result.text)
                self.assertEqual(self.app.store.history()[-1]["content"], result.text)

    def test_cancel_all_blocking_stages_and_no_worker_leaks(self):
        cases = [("record", {"recorder": SlowRecorder()}, VoiceState.RECORDING),
                 ("stt", {"stt": SlowSTT()}, VoiceState.TRANSCRIBING),
                 ("llm", {}, VoiceState.REASONING),
                 ("tts", {"tts": SlowTTS()}, VoiceState.SPEAKING),
                 ("play", {"player": SlowPlayer()}, VoiceState.SPEAKING)]
        original = self.app.provider
        before = {p.pid for p in multiprocessing.active_children()}
        for name, adapters, state in cases:
            with self.subTest(stage=name):
                self.app.provider = SlowLLM() if name == "llm" else original
                voice = self.voice(**adapters)
                timer = None

                def poll():
                    nonlocal timer
                    # Playback shares the speaking state; wait for its audit entry.
                    active = self.app.store.operations()[0]
                    target = "llm_generate" if name == "llm" else "voice_" + name
                    if voice.state == state and active["name"] == target and timer is None:
                        timer = threading.Timer(0.15, voice.cancel)
                        timer.start()

                start = time.monotonic()
                try:
                    result = voice.run(poll=poll)
                finally:
                    if timer:
                        timer.join()
                self.assertEqual(result.state, VoiceState.CANCELLED)
                self.assertLess(time.monotonic() - start, 4)
                self.assertTrue(any(row["status"] == "cancelled" for row in self.app.store.operations()))
                if name in ("tts", "play"):
                    self.assertIn("mock", result.text)
                self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_stop_recording_continues_pipeline(self):
        voice = self.voice(recorder=StoppableRecorder())
        def poll():
            if voice.state == VoiceState.RECORDING:
                voice.stop_recording()
        self.assertEqual(voice.run(poll=poll).state, VoiceState.IDLE)

    def test_timeouts_all_stages(self):
        original = self.app.provider
        for stage, adapters in [("record", {"recorder": SlowRecorder()}),
                                ("stt", {"stt": SlowSTT()}),
                                ("llm", {}), ("tts", {"tts": SlowTTS()}),
                                ("play", {"player": SlowPlayer()})]:
            with self.subTest(stage=stage):
                self.app.provider = SlowLLM() if stage == "llm" else original
                self.app.timeout = 0.6
                voice = self.voice(**adapters, max_record_seconds=0.1)
                voice.timeout = 0.6
                result = voice.run()
                if stage == "llm":
                    # Existing Assistant returns a truthful text error; it can be spoken.
                    self.assertEqual(result.state, VoiceState.IDLE)
                    self.assertIn("timeout", result.text)
                else:
                    self.assertEqual(result.state, VoiceState.FAILED)
                    self.assertEqual(result.error, "timeout")
                if stage in ("tts", "play"):
                    self.assertIn("mock", result.text)
                self.assertTrue(any(row["error"] == "timeout" and row["status"] == "failed"
                                    for row in self.app.store.operations()))

    def test_real_operation_and_turn_deadlines(self):
        voice = self.voice(stt=SlowSTT())
        voice.timeout = 0.2
        self.assertEqual(voice.run().error, "timeout")
        voice = self.voice(recorder=SlowRecorder(), turn_timeout=0.2)
        self.assertEqual(voice.run().error, "timeout")
        self.app.provider = SlowLLM()
        self.app.timeout = 0.2
        result = self.voice().run()
        self.assertIn("timeout", result.text)
        self.assertNotIn("too late", result.text)

    def test_forgotten_memory_not_reused_after_restart(self):
        self.app.provider = InspectLLM()
        key = self.app.memory("add", "FORGOTTEN_SECRET")
        self.voice().run()
        self.voice(stt=MockSTT("忘れて " + str(key))).run()
        self.app.close()
        self.app = Assistant(self.root / "data", self.root / "notes", provider=InspectLLM())
        for _ in range(2):
            result = self.voice().run()
            self.assertNotIn("FORGOTTEN_SECRET", result.text)
            self.assertIn(self.app.persona.name, result.text)

    def test_tool_failure_spoken_as_failure(self):
        self.app.provider = FailureClaimLLM()
        result = self.voice(tts=VerifyFailureTTS()).run()
        self.assertEqual(result.state, VoiceState.IDLE)
        self.assertIn("操作は成功していません", result.text)
        self.assertNotIn("成功しました", result.text)
        self.assertFalse((self.root / "secret.md").exists())

    def test_direct_tool_command_uses_permission_boundary(self):
        result = self.voice(stt=MockSTT("/read ../secret.md")).run()
        self.assertIn("操作は成功していません", result.text)
        self.assertTrue(any(row["name"] == "read_note" and row["status"] != "success"
                            for row in self.app.store.operations()))

    def test_audio_never_persisted_and_memory_not_automatically_added(self):
        before = {p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file()}
        for kwargs in ({}, {"stt": FailingSTT()}, {"tts": FailingTTS()}):
            self.voice(recorder=RawRecorder(), **kwargs).run()
        after = {p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"PRIVATE_RAW_AUDIO", path.read_bytes())
        self.assertEqual(self.app.memory("list"), [])
        self.assertNotIn("こんにちは", str(self.app.store.operations()))

    def test_cloud_stt_requires_label_and_specific_grant(self):
        stt = MockSTT()
        stt.location = "cloud"
        for policy in (DataPolicy.LOCAL_ONLY, DataPolicy.CLOUD_SENDABLE):
            for grant in (frozenset(), frozenset({"mock-stt"}), frozenset({"other"})):
                with self.subTest(policy=policy, grant=grant):
                    result = self.voice(stt=stt).run(VoicePermission(cloud_stt=grant, input_policy=policy))
                    allowed = policy == DataPolicy.CLOUD_SENDABLE and "mock-stt" in grant
                    self.assertEqual(result.state, VoiceState.IDLE if allowed else VoiceState.FAILED)
                    if not allowed:
                        self.assertEqual(result.error, "cloud_permission_denied")
                        self.assertEqual(self.app.store.operations()[0]["status"], "denied")

    def test_cloud_tts_requires_independent_response_consent(self):
        tts = MockTTS()
        tts.location = "cloud"
        for policy in (DataPolicy.LOCAL_ONLY, DataPolicy.CLOUD_SENDABLE):
            for grants in (frozenset(), frozenset({"mock-tts"})):
                result = self.voice(tts=tts).run(VoicePermission(
                    input_policy=DataPolicy.CLOUD_SENDABLE, cloud_stt=frozenset({"mock-tts"}),
                    response_policy=policy, cloud_tts=grants))
                allowed = policy == DataPolicy.CLOUD_SENDABLE and bool(grants)
                self.assertEqual(result.state, VoiceState.IDLE if allowed else VoiceState.FAILED)
                self.assertIn("mock", result.text)
        # Grants are scoped to the call, not remembered across turns.
        voice = self.voice(tts=tts)
        self.assertEqual(voice.run().error, "cloud_permission_denied")

    def test_unknown_provider_location_fails_closed(self):
        stt = MockSTT()
        stt.location = "unknown"
        self.assertEqual(self.voice(stt=stt).run().error, "cloud_permission_denied")

    def test_invalid_audio_and_transcript(self):
        for text in ("", " " * 2, "x" * 16001, None):
            self.assertEqual(self.voice(stt=MockSTT(text)).run().error, "invalid_transcript")
        with self.assertRaises(Exception):
            Audio(b"x").validate()

    def test_cli_mock_without_external_services(self):
        result = subprocess.run([sys.executable, "-m", "personal_ai", "--voice", "mock",
                                 "--data-dir", str(self.root / "cli-data"),
                                 "--notes-dir", str(self.root / "cli-notes")],
                                input="/voice\n\n普通のテキスト\n/quit\n", text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mock音声を処理", result.stdout)
        self.assertIn("普通のテキスト", result.stdout)


class LocalAdapterTests(unittest.TestCase):
    def test_vosk_uses_explicit_model_and_collects_segments(self):
        calls = []
        class Recognizer:
            def __init__(self, model, rate):
                calls.append(rate)
            def AcceptWaveform(self, data):
                calls.append(data)
                return True
            def Result(self):
                return '{"text": "segment"}'
            def FinalResult(self):
                return '{"text": "final"}'
        def model(**kwargs):
            calls.append(kwargs)
            return object()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(sys.modules, {"vosk": SimpleNamespace(Model=model, KaldiRecognizer=Recognizer)}):
                self.assertEqual(VoskSTT(directory).transcribe(Audio(b"ab" * 5000)),
                                 "segment segment final")
        self.assertEqual(calls[0], {"model_path": directory})
        self.assertEqual(calls[1], 16000)
        self.assertEqual(len(calls[2]), 8000)

    def test_piper_synthesizes_pcm_in_memory(self):
        calls = []
        class Piper:
            @staticmethod
            def load(path, use_cuda):
                calls.append((path, use_cuda))
                return Piper()
            def synthesize(self, text):
                calls.append(text)
                yield SimpleNamespace(sample_width=2, sample_channels=1,
                                      sample_rate=22050, audio_int16_bytes=b"abcd")
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "voice.onnx"
            model.touch()
            Path(str(model) + ".json").write_text("{}")
            with patch.dict(sys.modules, {"piper": SimpleNamespace(PiperVoice=Piper)}):
                result = PiperTTS(str(model)).synthesize("hello")
            self.assertEqual(len(list(Path(directory).iterdir())), 2)
        self.assertEqual(result, Audio(b"abcd", 22050))
        self.assertEqual(calls, [(str(model), False), "hello"])

    def test_record_and_play_use_memory_streams_and_close_devices(self):
        stopped = threading.Event()
        closed = []
        writes = []
        class Stream:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def __enter__(self):
                return self
            def __exit__(self, *args):
                closed.append(self.kwargs)
            def read(self, frames):
                stopped.set()
                return b"ab" * frames, False
            def write(self, data):
                writes.append(data)
                return False
        module = SimpleNamespace(RawInputStream=Stream, RawOutputStream=Stream)
        with patch.dict(sys.modules, {"sounddevice": module}):
            audio = SoundDeviceRecorder().record(stopped, 1)
            SoundDevicePlayer().play(audio)
        self.assertEqual(writes, [audio.pcm])
        self.assertEqual(len(closed), 2)
        self.assertTrue(all(item["channels"] == 1 for item in closed))

    def test_missing_models_fail_without_download(self):
        def forbidden(*args, **kwargs):
            self.fail("model loader must not run for missing model")
        with patch.dict(sys.modules, {
                "vosk": SimpleNamespace(Model=forbidden, KaldiRecognizer=forbidden),
                "piper": SimpleNamespace(PiperVoice=SimpleNamespace(load=forbidden))}):
            with self.assertRaises(ValueError):
                VoskSTT("/nonexistent/local-model").transcribe(Audio(b"ab"))
            with self.assertRaises(ValueError):
                PiperTTS("/nonexistent/voice.onnx").synthesize("hello")


if __name__ == "__main__":
    unittest.main()
