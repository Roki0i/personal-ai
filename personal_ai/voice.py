"""Push-to-talk orchestration. Audio is ephemeral; text uses the existing CLI route."""
from dataclasses import dataclass, field
from enum import Enum
import math
import multiprocessing
import sqlite3
import time
from typing import FrozenSet, Protocol

from .cli import handle
from .runtime import (CancellationToken, OperationError, check_pending,
                      execution_scope, run_bounded)


class DataPolicy(str, Enum):
    LOCAL_ONLY = "local-only"
    CLOUD_SENDABLE = "cloud-sendable"


class VoiceState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    REASONING = "reasoning"
    SPEAKING = "speaking"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Audio:
    pcm: bytes
    sample_rate: int = 16000
    # Mono signed 16-bit little-endian PCM only.
    policy: DataPolicy = DataPolicy.LOCAL_ONLY

    def validate(self):
        if (not isinstance(self.pcm, bytes) or not self.pcm or len(self.pcm) % 2
                or len(self.pcm) > 16_000_000
                or type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 48000
                or not isinstance(self.policy, DataPolicy)):
            raise OperationError("invalid_audio")
        return self


class SpeechToTextProvider(Protocol):
    provider_id: str
    location: str  # "local" or "cloud"; unrecognized values are denied.

    def transcribe(self, audio: Audio) -> str: ...


class TextToSpeechProvider(Protocol):
    provider_id: str
    location: str

    def synthesize(self, text: str) -> Audio: ...


class Recorder(Protocol):
    def record(self, stop, max_seconds: float) -> Audio: ...


class Player(Protocol):
    def play(self, audio: Audio) -> None: ...


@dataclass(frozen=True)
class VoicePermission:
    # Explicit per-turn grants to specific configured providers, never inferred
    # from the recognized words or from the LLM's reply.
    cloud_stt: FrozenSet[str] = field(default_factory=frozenset)
    cloud_tts: FrozenSet[str] = field(default_factory=frozenset)
    input_policy: DataPolicy = DataPolicy.LOCAL_ONLY
    response_policy: DataPolicy = DataPolicy.LOCAL_ONLY


@dataclass(frozen=True)
class VoiceResult:
    state: VoiceState
    text: str = ""
    error: str = ""


def invoke(target, method, args):
    return getattr(target, method)(*args)


class VoiceSession:
    def __init__(self, assistant, stt, tts, recorder, player, timeout=15,
                 turn_timeout=90, max_record_seconds=30):
        if not all(math.isfinite(x) and x > 0 for x in
                   (timeout, turn_timeout, max_record_seconds)) or max_record_seconds > 30:
            raise ValueError("invalid voice time limits")
        self.assistant = assistant
        self.stt, self.tts, self.recorder, self.player = stt, tts, recorder, player
        self.timeout, self.turn_timeout = timeout, turn_timeout
        self.max_record_seconds = max_record_seconds
        self.state = VoiceState.IDLE
        self.states = [self.state]
        self.token = CancellationToken()
        self._stop = None
        self._active = False

    def cancel(self):
        self.token.cancel()

    def stop_recording(self):
        if self._stop is not None:
            self._stop.set()

    def _state(self, state):
        self.state = state
        self.states.append(state)

    def _stage(self, name, target, method, args, validate=None, permission=None,
               policy=None, budget=None):
        store = self.assistant.store
        operation = store.start_operation("voice_" + name)
        try:
            check_pending()
            if permission is not None:
                location = getattr(target, "location", None)
                provider_id = getattr(target, "provider_id", None)
                grants = permission.cloud_stt if name == "stt" else permission.cloud_tts
                if (location not in ("local", "cloud")
                        or not isinstance(provider_id, str) or not provider_id
                        or (location == "cloud" and
                            (policy != DataPolicy.CLOUD_SENDABLE or provider_id not in grants))):
                    store.finish_operation(operation, "denied", "cloud_permission_denied")
                    raise PermissionError
            value = run_bounded(invoke, (target, method, args),
                                self.timeout if budget is None else budget)
            if validate:
                validate(value)
            store.finish_operation(operation, "success")
            return value
        except PermissionError:
            raise OperationError("cloud_permission_denied") from None
        except KeyboardInterrupt:
            self.token.cancel()
            store.finish_operation(operation, "cancelled", "cancelled")
            raise
        except OperationError as exc:
            store.finish_operation(operation, "failed", str(exc))
            raise

    def run(self, permission=None, on_text=None, poll=None):
        """Run on the Assistant's owning thread. cancel/stop_recording are thread-safe.

        poll runs on that same thread while child operations are pending. on_text
        receives the answer BEFORE any synthesis/playback, including on TTS failure.
        """
        if self._active:
            raise ValueError("voice session already active")
        self._active = True
        self.token = CancellationToken()
        self._stop = multiprocessing.get_context("spawn").Event()
        self.states = []
        permission = permission or VoicePermission()
        audio = speech = None
        transcript = ""
        answer = ""
        try:
            with execution_scope(self.token, time.monotonic() + self.turn_timeout, poll):
                self._state(VoiceState.RECORDING)
                audio = self._stage("record", self.recorder, "record",
                                    (self._stop, self.max_record_seconds), self._audio,
                                    budget=self.max_record_seconds + self.timeout)
                # Only application configuration may relax the recording's label.
                audio = Audio(audio.pcm, audio.sample_rate, permission.input_policy)
                audio.validate()
                self._state(VoiceState.TRANSCRIBING)
                transcript = self._stage("stt", self.stt, "transcribe", (audio,),
                                         self._text, permission, audio.policy)
                audio = None
                self._state(VoiceState.REASONING)
                check_pending()
                # Exactly the text CLI route: Persona, Memory, tool permissions,
                # file checks, audit, operation and turn timeouts remain in force.
                answer = handle(self.assistant, transcript)
                transcript = ""
                if on_text:
                    on_text(answer)
                check_pending()
                self._state(VoiceState.SPEAKING)
                speech = self._stage("tts", self.tts, "synthesize", (answer,),
                                     self._audio, permission, permission.response_policy)
                self._stage("play", self.player, "play", (speech,))
                self._state(VoiceState.IDLE)
                return VoiceResult(self.state, answer)
        except KeyboardInterrupt:
            self.token.cancel()
            self._state(VoiceState.CANCELLED)
            return VoiceResult(self.state, answer, "cancelled")
        except (OperationError, ValueError, OSError, sqlite3.Error) as exc:
            # ValueError may include transcript/command contents; do not expose it.
            if isinstance(exc, OperationError):
                error = str(exc)
            elif isinstance(exc, ValueError):
                error = "invalid_voice_input"
            else:
                error = "voice_unavailable"
            self._state(VoiceState.FAILED)
            return VoiceResult(self.state, answer, error)
        finally:
            audio = speech = None
            transcript = ""
            self._stop = None
            self._active = False

    @staticmethod
    def _audio(value):
        if not isinstance(value, Audio):
            raise OperationError("invalid_audio")
        value.validate()

    @staticmethod
    def _text(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 16000:
            raise OperationError("invalid_transcript")


@dataclass
class MockSTT:
    transcript: str = "こんにちは"
    provider_id: str = "mock-stt"
    location: str = "local"

    def transcribe(self, audio):
        return self.transcript


class MockTTS:
    provider_id = "mock-tts"
    location = "local"

    def synthesize(self, text):
        return Audio(b"\x00\x00" * 160)


class MockRecorder:
    def record(self, stop, max_seconds):
        return Audio(b"\x01\x00" * 160)


class MockPlayer:
    def play(self, audio):
        audio.validate()
