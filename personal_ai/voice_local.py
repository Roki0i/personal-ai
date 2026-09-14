"""Optional offline adapters. No model download, shell invocation or audio files."""
from dataclasses import dataclass
import json
from pathlib import Path
import time

from .voice import Audio


@dataclass
class VoskSTT:
    model_path: str
    provider_id = "local-vosk"
    location = "local"

    def transcribe(self, audio):
        from vosk import Model, KaldiRecognizer
        audio.validate()
        if not Path(self.model_path).is_dir():
            raise ValueError("missing local model")
        recognizer = KaldiRecognizer(Model(model_path=self.model_path), audio.sample_rate)
        texts = []
        for offset in range(0, len(audio.pcm), 8000):
            if recognizer.AcceptWaveform(audio.pcm[offset:offset + 8000]):
                texts.append(json.loads(recognizer.Result()).get("text", ""))
        texts.append(json.loads(recognizer.FinalResult()).get("text", ""))
        return " ".join(texts).strip()


@dataclass
class PiperTTS:
    model_path: str
    provider_id = "local-piper"
    location = "local"

    def synthesize(self, text):
        from piper import PiperVoice
        # Explicit local files only. Never invoke the download helper.
        if not Path(self.model_path).is_file() or not Path(self.model_path + ".json").is_file():
            raise ValueError("missing local voice")
        voice = PiperVoice.load(self.model_path, use_cuda=False)
        pcm = bytearray()
        rate = None
        for chunk in voice.synthesize(text):
            if chunk.sample_width != 2 or chunk.sample_channels != 1:
                raise ValueError("unsupported audio format")
            if rate is not None and rate != chunk.sample_rate:
                raise ValueError("inconsistent sample rate")
            rate = chunk.sample_rate
            pcm.extend(chunk.audio_int16_bytes)
            if len(pcm) > 16_000_000:
                raise ValueError("speech too large")
        return Audio(bytes(pcm), rate).validate()


class SoundDeviceRecorder:
    def record(self, stop, max_seconds):
        import sounddevice as sd
        pcm = bytearray()
        rate, frames = 16000, 800
        deadline = time.monotonic() + max_seconds
        with sd.RawInputStream(samplerate=rate, channels=1, dtype="int16", blocksize=frames) as stream:
            while not stop.is_set() and time.monotonic() < deadline:
                data, overflow = stream.read(frames)
                if overflow:
                    raise ValueError("recording overflow")
                pcm.extend(data)
                if len(pcm) >= int(max_seconds * rate) * 2:
                    break
        return Audio(bytes(pcm), rate).validate()


class SoundDevicePlayer:
    def play(self, audio):
        import sounddevice as sd
        audio.validate()
        with sd.RawOutputStream(samplerate=audio.sample_rate, channels=1, dtype="int16") as stream:
            for offset in range(0, len(audio.pcm), 8192):
                if stream.write(audio.pcm[offset:offset + 8192]):
                    raise ValueError("playback underflow")
