#!/usr/bin/env python
"""Krishna -- a private voice companion, local-first with an optional fast
cloud mode.

Push-to-talk loop: press Enter, speak, and it stops recording once you go
quiet. Transcribes (Whisper) -> thinks (LLM) -> speaks (Kokoro / MMS-TTS).

Two brains, chosen by CLOUD_MODE:
  - local : faster-whisper + Ollama llama3.2:3b. Nothing leaves the machine,
            but a reply takes seconds on this CPU, and the 3B model can't
            follow Telugu or Telugu typed in English letters ("ela unnav").
  - cloud : Groq -- whisper-large-v3-turbo + gpt-oss-120b. The voice
            recording and conversation text go to Groq; memory, notes and
            Krishna's own voice stay here. Measured ~0.4s to first word
            (vs 3-20s local), proper Telugu script, and correct tool calls.
Cloud mode was switched on at the user's request (2026-09-14) for speed. Any
cloud error falls back to the local path rather than failing the turn.

Usage:
    python krishna.py
"""
from __future__ import annotations
import difflib
import io
import json
import queue
import re
import sys
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import ollama
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel

import tools as _tools

ROOT = Path(__file__).resolve().parent

# Kokoro's non-English voices need espeak-ng for phonemization. misaki
# (Kokoro's G2P layer) only checks the standard admin-install path
# (C:\Program Files\eSpeak NG\...), which needs an elevated installer we
# deliberately avoided -- espeak-ng was extracted instead via `msiexec /a`
# into vendor/espeak-ng/. Setting the library AND data path directly here,
# before `kokoro` is ever imported, makes phonemizer use this copy; calling
# misaki's own lookup with the wrong path caused a native access violation.
_ESPEAK_DIR = ROOT / "vendor" / "espeak-ng"
if (_ESPEAK_DIR / "libespeak-ng.dll").exists():
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    EspeakWrapper.set_library(str(_ESPEAK_DIR / "libespeak-ng.dll"))
    EspeakWrapper.set_data_path(str(_ESPEAK_DIR / "espeak-ng-data"))

MODEL = "llama3.2:3b"
SAMPLE_RATE = 16000
# Ollama unloads an idle model by default, which is exactly why the first
# reply in a session pays a multi-second reload cost.
KEEP_ALIVE = "30m"

MEMORY_PATH = ROOT / "memory.json"
NOTES_PATH = ROOT / "notes.md"
MAX_HISTORY_TURNS = 40   # trimmed so an old conversation can't grow the prompt forever

HOME_LOCATION = "Iselin, New Jersey, USA"

SYSTEM_PROMPT = f"""You are Krishna, a warm, direct, knowledgeable personal companion and
advisor. You speak naturally and conversationally, like a trusted friend who has real
expertise -- not like a corporate assistant. Keep responses short and spoken-natural
(1-3 sentences unless genuinely more is needed) since this is a VOICE conversation, not
a chat window. Be honest and specific rather than generically encouraging.
When a tool result appears in the conversation, that is REAL, current data you just
looked up -- state it directly and specifically (the actual number/fact), in your own
natural voice. Never deflect, joke about not knowing, or hedge with "check elsewhere"
when you were just given the real answer -- that's actively wrong, not humble.
You cannot know live facts yourself -- weather, prices, scores, news. Only state them
from a tool result; if you have none, use a tool or say you'll check. Never invent numbers.
The user lives in {HOME_LOCATION}. If they ask about weather or anything local without
naming a place, use {HOME_LOCATION}. Treat short follow-ups (just a place name, "and
tomorrow?") as continuing the previous question -- never ask them to repeat it.
The user may write Telugu or Hindi in English letters -- for example "ela unnav" is
Telugu for "how are you". Understand it naturally."""


def load_history() -> list[dict]:
    """Loads past conversation turns from disk so Krishna remembers previous
    sessions. Starts fresh if the file is missing or unreadable rather than
    crashing on a corrupt/partial file."""
    if MEMORY_PATH.exists():
        try:
            saved = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if saved:
                return [{"role": "system", "content": SYSTEM_PROMPT}] + saved[-MAX_HISTORY_TURNS:]
        except Exception:
            pass
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def save_history(history: list[dict]) -> None:
    """Everything except the system prompt -- that's reconstructed fresh each
    load so a SYSTEM_PROMPT edit takes effect on old saved sessions too."""
    to_save = [m for m in history if m["role"] != "system"][-MAX_HISTORY_TURNS:]
    MEMORY_PATH.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")


_NOTE_TRIGGERS = ("note ", "note:", "remember ", "remember that ", "remember this")


def maybe_take_note(text: str) -> str | None:
    """If you say something starting with 'note' or 'remember', this appends
    it straight to notes.md -- no LLM involved in deciding what gets written,
    on purpose. Returns None for anything else."""
    low = text.strip().lower()
    # Longest trigger first -- "remember that " must be checked before the
    # shorter "remember " or it never gets a chance to match.
    for trigger in sorted(_NOTE_TRIGGERS, key=len, reverse=True):
        if low.startswith(trigger):
            content = text.strip()[len(trigger):].strip(" :,")
            if not content:
                return "Note what, exactly?"
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(NOTES_PATH, "a", encoding="utf-8") as f:
                f.write(f"- [{stamp}] {content}\n")
            return "Noted."
    return None


def _load_whisper() -> WhisperModel:
    """'base' (multilingual), not 'base.en' -- the .en variant can't
    transcribe anything but English. Locally it's used for language
    detection and as the fallback transcriber; fast mode transcribes on Groq."""
    print("[krishna] loading speech recognition (first run downloads the model)...")
    return WhisperModel("base", device="cpu", compute_type="int8")


def _load_tts() -> dict:
    """Returns an empty model CACHE, not a model -- Kokoro pipelines are bound
    to one lang_code and MMS models to one language, so models are built
    lazily as each language is actually used."""
    print("[krishna] voice ready (each language's model loads on first use)...")
    return {}


def _evict(tts_cache: dict, engine: str, keep: int) -> None:
    """Holding ~5 Kokoro pipelines at once corrupted the process (verified:
    a trivial 256KB allocation started failing -- native-state corruption,
    not a real shortage), so Kokoro keeps ONE pipeline. MMS models are plain
    transformers VITS and are allowed two, so Auto mode can hold Telugu and
    Hindi together instead of reloading on every language switch."""
    same = [key for key in tts_cache if key.startswith("mms:") == (engine == "mms")]
    while len(same) >= keep:
        tts_cache.pop(same.pop(0), None)


def _get_tts_pipeline(tts_cache: dict):
    from kokoro import KPipeline
    kokoro_code = LANGUAGES[CURRENT_LANGUAGE]["kokoro"]
    if kokoro_code not in tts_cache:
        _evict(tts_cache, "kokoro", keep=1)
        tts_cache[kokoro_code] = KPipeline(lang_code=kokoro_code)
    return tts_cache[kokoro_code]


_SILERO = None


def _silero_vad():
    """Silero VAD, loaded once -- a small neural model, markedly better than
    webrtcvad at telling speech from room noise, which decides whether you
    get cut off mid-sentence. Returns None if unavailable (webrtcvad fallback)."""
    global _SILERO
    if _SILERO is None:
        try:
            from silero_vad import load_silero_vad
            _SILERO = (load_silero_vad(), True)
        except Exception as e:
            print(f"[krishna] Silero VAD unavailable, using webrtcvad: {e}")
            _SILERO = (None, False)
    return _SILERO[0]


def record_vad(max_seconds: int = 20, silence_ms: int = 900,
               aggressiveness: int = 2, speech_threshold: float = 0.5) -> np.ndarray:
    """Records until you've actually stopped talking (not a fixed duration).
    Stops after `silence_ms` of quiet following detected speech, or
    `max_seconds` as a hard cap. Silero wants 512-sample frames at 16kHz;
    webrtcvad wants 10/20/30ms, so the fallback uses 30ms frames."""
    model = _silero_vad()
    frame_size = 512 if model is not None else int(SAMPLE_RATE * 30 / 1000)
    frame_ms = frame_size * 1000 / SAMPLE_RATE
    silence_frames_needed = max(1, int(silence_ms / frame_ms))
    vad = None if model is not None else webrtcvad.Vad(aggressiveness)
    if model is not None:
        import torch
        model.reset_states()

    q: queue.Queue = queue.Queue()

    def callback(indata, frames_count, time_info, status):
        q.put(indata.copy())

    frames = []
    triggered = False
    silence_count = 0
    start = time.time()
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            callback=callback, blocksize=frame_size)
    with stream:
        while time.time() - start < max_seconds:
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                continue
            frames.append(chunk)
            flat = chunk.flatten()
            if model is not None:
                if len(flat) != frame_size:
                    continue
                with torch.no_grad():
                    prob = model(torch.from_numpy(
                        flat.astype(np.float32) / 32768.0), SAMPLE_RATE).item()
                is_speech = prob >= speech_threshold
            else:
                is_speech = vad.is_speech(chunk.tobytes(), SAMPLE_RATE)
            if is_speech:
                triggered = True
                silence_count = 0
            elif triggered:
                silence_count += 1
                if silence_count >= silence_frames_needed:
                    break
    if not frames or not triggered:
        return np.array([], dtype=np.float32)
    audio_i16 = np.concatenate(frames, axis=0).flatten()
    return audio_i16.astype(np.float32) / 32768.0


# ---------------------------------------------------------------- cloud (Groq)

GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_CHAT_MODEL = "openai/gpt-oss-120b"
GROQ_STT_MODEL = "whisper-large-v3-turbo"


def _groq_key() -> str | None:
    return _tools._api_key("GROQ_API_KEY")


def cloud_available() -> bool:
    return bool(_groq_key())


CLOUD_MODE = cloud_available()


def set_cloud_mode(on: bool) -> None:
    global CLOUD_MODE
    CLOUD_MODE = bool(on) and cloud_available()


def _groq_post(path: str, **kwargs) -> dict:
    import requests
    r = requests.post(f"{GROQ_BASE}/{path}", timeout=45,
                      headers={"Authorization": f"Bearer {_groq_key()}"}, **kwargs)
    r.raise_for_status()
    return r.json()


_TOOL_SCHEMAS: list | None = None


def _tool_schemas() -> list:
    """OpenAI-format schemas for the same functions Ollama gets, produced by
    Ollama's own docstring->schema converter so the two paths can't drift."""
    global _TOOL_SCHEMAS
    if _TOOL_SCHEMAS is None:
        from ollama._utils import convert_function_to_tool
        _TOOL_SCHEMAS = [convert_function_to_tool(f).model_dump(exclude_none=True)
                         for f in _tools.ALL_TOOLS]
    return _TOOL_SCHEMAS


def _groq_think(messages: list) -> str:
    """Tools are offered on every turn -- no keyword gate, since the whole
    round trip is ~0.3-0.6s. That's also what makes a bare follow-up like
    "iselin nj" work: the model sees the conversation and calls the weather
    tool itself instead of a keyword list deciding it isn't a weather question."""
    body = {"model": GROQ_CHAT_MODEL, "reasoning_effort": "low",
            "include_reasoning": False, "max_tokens": 700}
    first = _groq_post("chat/completions",
                       json={**body, "messages": messages, "tools": _tool_schemas()})
    msg = first["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        return (msg.get("content") or "").strip()
    working = messages + [{"role": "assistant", "content": msg.get("content") or "",
                           "tool_calls": calls}]
    for call in calls:
        name = call["function"]["name"]
        fn = _TOOL_FUNCS.get(name)
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            result = fn(**args) if fn else f"Unknown tool: {name}"
        except Exception as e:
            result = f"Tool call failed: {e}"
        working.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
    second = _groq_post("chat/completions", json={**body, "messages": working})
    return (second["choices"][0]["message"].get("content") or "").strip()


def _groq_transcribe(audio: np.ndarray, language_code: str) -> str:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
    out = _groq_post("audio/transcriptions",
                     files={"file": ("speech.wav", buf.getvalue(), "audio/wav")},
                     data={"model": GROQ_STT_MODEL, "language": language_code,
                           "response_format": "json"})
    return (out.get("text") or "").strip()


# ------------------------------------------------------------ speech to text

# Auto mode deliberately chooses only between the languages actually spoken
# here. Unrestricted detection misfires between neighbours -- measured: both
# local Whisper and Groq's labelled a Telugu clip "Tamil", which switched
# Krishna to a voice that then began a silent first-time download. Restricted
# to these three, the same clip picked Telugu (0.78 vs Kannada 0.15).
_AUTO_CANDIDATES = {"en": "English (US)", "hi": "Hindi (MMS voice)", "te": "Telugu"}
AUTO_LANGUAGE_LABEL = "Auto (English / Hindi / Telugu)"
AUTO_LANGUAGE = True


def set_auto_language(on: bool) -> None:
    global AUTO_LANGUAGE
    AUTO_LANGUAGE = bool(on)


def _detect_language(whisper_model: WhisperModel, audio: np.ndarray) -> str:
    current = LANGUAGES[CURRENT_LANGUAGE]["whisper"]
    fallback = current if current in _AUTO_CANDIDATES else "en"
    if len(audio) < SAMPLE_RATE * 1.0:   # too short to detect reliably
        return fallback
    try:
        _, info = whisper_model.transcribe(audio[:SAMPLE_RATE * 30], language=None,
                                           without_timestamps=True)
        probs = dict(info.all_language_probs or [])
        return max(_AUTO_CANDIDATES, key=lambda c: probs.get(c, 0.0))
    except Exception as e:
        print(f"[krishna] language detection failed: {e}")
        return fallback


def transcribe(whisper_model: WhisperModel, audio: np.ndarray) -> str:
    """An explicitly chosen language is forced, never guessed. In Auto mode
    the language is detected (restricted to English/Hindi/Telugu) and
    Krishna switches to it. Fast mode transcribes on Groq with the language
    pinned; local Whisper is the fallback."""
    if len(audio) < SAMPLE_RATE * 0.3:   # too short to be real speech
        return ""
    if AUTO_LANGUAGE:
        code = _detect_language(whisper_model, audio)
        if _AUTO_CANDIDATES[code] != CURRENT_LANGUAGE:
            print(f"[krishna] heard {_AUTO_CANDIDATES[code]} -- switching")
            set_language(_AUTO_CANDIDATES[code])
    else:
        code = LANGUAGES[CURRENT_LANGUAGE]["whisper"]
    if CLOUD_MODE:
        try:
            return _groq_transcribe(audio, code)
        except Exception as e:
            print(f"[krishna] cloud transcription failed, using local: {e}")
    segments, _ = whisper_model.transcribe(audio, language=code)
    return " ".join(s.text.strip() for s in segments).strip()


# ------------------------------------------------------------------- voices

_TTS_DROP_CHARS = set("*#_~`|<>[]{}^\\@&+=")
_TTS_SAFE_PUNCT = set(".,!?;:'\"()-¿¡«»।॥")
_TTS_REPLACE = {"—": ", ", "–": ", ", "…": ".", "“": '"', "”": '"', "‘": "'",
                "’": "'", " ": " ", " ": " "}
# Spoken forms for what the cloud model writes but Kokoro would read as a
# symbol or drop: "67 °F" came out as "67 F", and "$332.27" lost the dollars.
_TTS_SPOKEN = [
    (re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)"), r"\1 dollars"),
    (re.compile(r"₹\s?(\d[\d,]*(?:\.\d+)?)"), r"\1 rupees"),
    (re.compile(r"\s*°\s*F\b"), " degrees Fahrenheit"),
    (re.compile(r"\s*°\s*C\b"), " degrees Celsius"),
    (re.compile(r"\bkm/h\b"), " kilometers per hour"),
    (re.compile(r"\bmph\b"), " miles per hour"),
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
]


def _sanitize_for_tts(text: str) -> str:
    """Strips what trips Kokoro's G2P -- emoji, symbols, markdown -- while
    keeping every letter and combining mark. The previous ASCII-only
    whitelist deleted accented and non-Latin letters outright: French
    "réel" became "r el", and a full Hindi sentence came out as 0.6s of
    audio because the Devanagari itself was being erased."""
    for old, new in _TTS_REPLACE.items():
        text = text.replace(old, new)
    for pattern, spoken in _TTS_SPOKEN:
        text = pattern.sub(spoken, text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if (ch in _TTS_DROP_CHARS or cat[0] in "SC"
                or (cat[0] == "P" and ch not in _TTS_SAFE_PUNCT)):
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


# Each language: Whisper's language code, Kokoro's lang_code, and a curated
# set of REAL Kokoro voice IDs -- checked against the hexgrad/Kokoro-82M
# voices/ listing on Hugging Face, not assumed from a naming convention.
LANGUAGES: dict[str, dict] = {
    "English (US)": {
        "whisper": "en", "kokoro": "a",
        "voices": {
            "Heart (US female, default)": "af_heart",
            "Bella (US female)": "af_bella",
            "Nicole (US female)": "af_nicole",
            "Adam (US male)": "am_adam",
            "Michael (US male)": "am_michael",
        },
    },
    "English (UK)": {
        "whisper": "en", "kokoro": "b",
        "voices": {
            "Emma (British female)": "bf_emma",
            "George (British male)": "bm_george",
        },
    },
    "Spanish": {
        "whisper": "es", "kokoro": "e",
        "voices": {"Dora (female)": "ef_dora", "Alex (male)": "em_alex"},
    },
    "French": {
        "whisper": "fr", "kokoro": "f",
        "voices": {"Siwis (female)": "ff_siwis"},
    },
    "Hindi": {
        "whisper": "hi", "kokoro": "h",
        "voices": {
            "Alpha (female)": "hf_alpha", "Beta (female)": "hf_beta",
            "Omega (male)": "hm_omega", "Psi (male)": "hm_psi",
        },
    },
    "Italian": {
        "whisper": "it", "kokoro": "i",
        "voices": {"Sara (female)": "if_sara", "Nicola (male)": "im_nicola"},
    },
    "Portuguese (Brazil)": {
        "whisper": "pt", "kokoro": "p",
        "voices": {"Dora (female)": "pf_dora", "Alex (male)": "pm_alex"},
    },
    # Japanese ('j') and Mandarin ('z') deliberately left out -- the installed
    # kokoro==0.2.2 never had them in its LANG_CODES map. Real fix is a kokoro
    # upgrade, held for its own tested pass -- see BACKLOG.md.
}

# Indian languages via Meta's MMS-TTS -- Kokoro has no voices for any of
# these except Hindi. One ~145MB VITS model per language, CPU-only, verified
# on Telugu at 2.8x realtime. Takes native script directly (no G2P/espeak).
# Honest limit: ONE voice per language. Multiple Telugu voices need
# AI4Bharat's IndicF5, a gated model -- see BACKLOG.md.
_MMS_LANGUAGES = {
    "Telugu":    ("te", "tel"),
    "Tamil":     ("ta", "tam"),
    "Kannada":   ("kn", "kan"),
    "Malayalam": ("ml", "mal"),
    "Marathi":   ("mr", "mar"),
    "Bengali":   ("bn", "ben"),
    "Gujarati":  ("gu", "guj"),
    "Punjabi":   ("pa", "pan"),
    "Odia":      ("or", "ory"),
    "Assamese":  ("as", "asm"),
}
for _name, (_whisper_code, _mms_code) in _MMS_LANGUAGES.items():
    LANGUAGES[_name] = {
        "whisper": _whisper_code,
        "engine": "mms",
        "mms": _mms_code,
        "voices": {f"{_name} (MMS)": f"mms_{_mms_code}"},
    }

# Hindi is the one overlap: Kokoro has 4 Hindi voices and MMS has one. Auto
# mode uses the MMS voice -- it's the one verified to speak full sentences.
LANGUAGES["Hindi (MMS voice)"] = {
    "whisper": "hi", "engine": "mms", "mms": "hin",
    "voices": {"Hindi (MMS)": "mms_hin"},
}

# TalkingHead's lip-sync viseme modules exist only for these of the languages
# above; elsewhere the mouth timing is right but the shapes are approximate.
AVATAR_CAPABLE_LANGUAGES = {"English (US)", "English (UK)", "French"}

DEFAULT_LANGUAGE = "English (US)"
CURRENT_LANGUAGE = DEFAULT_LANGUAGE
CURRENT_VOICE = next(iter(LANGUAGES[DEFAULT_LANGUAGE]["voices"].values()))
_VOICE_MEMORY: dict[str, str] = {}


def set_language(name: str) -> None:
    """Switches language and restores the voice last chosen for it (or its
    first voice) -- a voice ID is only valid inside its own language's model."""
    global CURRENT_LANGUAGE, CURRENT_VOICE
    if name not in LANGUAGES:
        return
    CURRENT_LANGUAGE = name
    voices = list(LANGUAGES[name]["voices"].values())
    remembered = _VOICE_MEMORY.get(name)
    CURRENT_VOICE = remembered if remembered in voices else voices[0]


def set_voice(voice_id: str) -> None:
    global CURRENT_VOICE
    CURRENT_VOICE = voice_id
    _VOICE_MEMORY[CURRENT_LANGUAGE] = voice_id


def set_preferred_voice(language: str, voice_id: str) -> None:
    """Remembers a voice for a language even when it isn't the current one --
    Auto mode switches languages under you, and your English voice choice
    shouldn't reset every time it does."""
    global CURRENT_VOICE
    _VOICE_MEMORY[language] = voice_id
    if CURRENT_LANGUAGE == language:
        CURRENT_VOICE = voice_id


@contextmanager
def _speaking_as(name: str):
    """Temporarily switch the speaking voice without losing the user's own
    language/voice choice."""
    global CURRENT_LANGUAGE, CURRENT_VOICE
    saved = (CURRENT_LANGUAGE, CURRENT_VOICE)
    set_language(name)
    try:
        yield
    finally:
        CURRENT_LANGUAGE, CURRENT_VOICE = saved


# Unicode blocks per MMS language.
_SCRIPT_RANGES = {
    "tel": [(0x0C00, 0x0C7F)],   "tam": [(0x0B80, 0x0BFF)],
    "kan": [(0x0C80, 0x0CFF)],   "mal": [(0x0D00, 0x0D7F)],
    "ben": [(0x0980, 0x09FF)],   "guj": [(0x0A80, 0x0AFF)],
    "pan": [(0x0A00, 0x0A7F)],   "ory": [(0x0B00, 0x0B7F)],
    "asm": [(0x0980, 0x09FF)],
    "hin": [(0x0900, 0x097F)],   "mar": [(0x0900, 0x097F)],
}

_SCRIPT_VOICES = [   # (voice language, first code point, last code point)
    ("Telugu", 0x0C00, 0x0C7F), ("Hindi (MMS voice)", 0x0900, 0x097F),
    ("Tamil", 0x0B80, 0x0BFF), ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F), ("Bengali", 0x0980, 0x09FF),
    ("Gujarati", 0x0A80, 0x0AFF), ("Punjabi", 0x0A00, 0x0A7F),
    ("Odia", 0x0B00, 0x0B7F),
]


def _is_native_script(text: str) -> bool:
    """Does this text contain characters the current MMS voice can pronounce?"""
    ranges = _SCRIPT_RANGES.get(LANGUAGES[CURRENT_LANGUAGE].get("mms"))
    if not ranges:
        return True
    return any(any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in text)


def _voice_for_text(text: str) -> str | None:
    """Which voice should say this text? None means the current one is right.
    Chosen by script: a Telugu reply is spoken by the Telugu voice even if the
    language was left on English, and an English reply is never handed to an
    MMS model (which can't pronounce Latin letters at all -- that used to
    produce silence, which looks exactly like Krishna ignoring you)."""
    count, best = max((sum(lo <= ord(c) <= hi for c in text), name)
                      for name, lo, hi in _SCRIPT_VOICES)
    engine = LANGUAGES[CURRENT_LANGUAGE].get("engine")
    if count == 0:
        return "English (US)" if engine == "mms" else None
    if engine == "mms" and _is_native_script(text):
        return None
    if CURRENT_LANGUAGE == "Hindi" and best == "Hindi (MMS voice)":
        return None   # Kokoro's Hindi voices read Devanagari themselves
    return best


def _mms_load(tts_cache: dict):
    """Loads (and caches) the current language's MMS model without inference."""
    from transformers import AutoTokenizer, VitsModel

    code = LANGUAGES[CURRENT_LANGUAGE]["mms"]
    key = f"mms:{code}"
    if key not in tts_cache:
        _evict(tts_cache, "mms", keep=2)
        model_id = f"facebook/mms-tts-{code}"
        tts_cache[key] = (AutoTokenizer.from_pretrained(model_id),
                          VitsModel.from_pretrained(model_id))
    return tts_cache[key]


def _mms_synthesize(tts_cache: dict, text: str) -> np.ndarray:
    import torch

    tok, model = _mms_load(tts_cache)
    inputs = tok(text, return_tensors="pt")
    # An MMS tokenizer only knows its own script; Latin text yields zero
    # tokens and the model then fails. Skip rather than crash.
    if inputs["input_ids"].shape[-1] == 0:
        print(f"[krishna] {CURRENT_LANGUAGE} voice can't pronounce "
              f"{text[:40]!r} (wrong script) -- skipping speech.")
        return np.array([], dtype=np.float32)
    with torch.no_grad():
        wave = model(**inputs).waveform
    return wave.squeeze().cpu().numpy().astype(np.float32)


def prepare_voice(tts_cache: dict) -> None:
    """Loads the current language's MMS model now rather than mid-reply,
    where a first-time ~145MB download looks like an unexplained hang."""
    if LANGUAGES[CURRENT_LANGUAGE].get("engine") == "mms":
        _mms_load(tts_cache)


def prepare_voices(tts_cache: dict, names: list[str]) -> None:
    for name in names:
        if LANGUAGES.get(name, {}).get("engine") == "mms":
            with _speaking_as(name):
                _mms_load(tts_cache)


def current_sample_rate() -> int:
    """Kokoro is 24kHz, MMS-TTS is 16kHz."""
    return 16000 if LANGUAGES[CURRENT_LANGUAGE].get("engine") == "mms" else 24000


_LAST_RATE = 24000   # sample rate of the most recently synthesized chunk


def _safe_tts_chunks(tts_cache: dict, text: str):
    """Yields audio for text, switching to the right voice for its script.
    Kokoro's G2P can raise on odd text; any TTS failure yields nothing so a
    bad sentence goes unspoken instead of taking the app down. _LAST_RATE is
    set before every yield -- the voice can change per sentence, so callers
    must not assume the rate of whatever language is selected."""
    global _LAST_RATE
    target = _voice_for_text(text)
    if target and target != CURRENT_LANGUAGE:
        with _speaking_as(target):
            yield from _safe_tts_chunks(tts_cache, text)
        return
    rate = current_sample_rate()
    if LANGUAGES[CURRENT_LANGUAGE].get("engine") == "mms":
        try:
            audio = _mms_synthesize(tts_cache, text.strip())
            if len(audio):
                _LAST_RATE = rate
                yield None, None, audio
        except Exception as e:
            print(f"[krishna] TTS failed on this text, skipping speech: {e}")
        return
    safe_text = _sanitize_for_tts(text)
    if not safe_text:
        return
    try:
        pipeline = _get_tts_pipeline(tts_cache)
        for result in pipeline(safe_text, voice=CURRENT_VOICE):
            _LAST_RATE = rate
            yield result
    except Exception as e:
        print(f"[krishna] TTS failed on this text, skipping speech: {e}")


VOLUME = 1.0


def set_volume(level: float) -> None:
    """1.0 = the model's own level; above that amplifies (clipped at playback)."""
    global VOLUME
    VOLUME = max(0.0, float(level))


def _apply_volume(audio: np.ndarray) -> np.ndarray:
    if VOLUME == 1.0:
        return audio
    return np.clip(audio * VOLUME, -1.0, 1.0)


def synthesize_with_rate(tts_cache: dict, text: str) -> tuple[np.ndarray, int]:
    chunks = [audio for _, _, audio in _safe_tts_chunks(tts_cache, text)]
    if not chunks:
        return np.array([], dtype=np.float32), current_sample_rate()
    return np.concatenate(chunks), _LAST_RATE


def synthesize(tts_cache: dict, text: str) -> np.ndarray:
    return synthesize_with_rate(tts_cache, text)[0]


def _resample(audio: np.ndarray, src: int, dst: int = SAMPLE_RATE) -> np.ndarray:
    if src == dst or not len(audio):
        return audio
    n = int(round(len(audio) * dst / src))
    return np.interp(np.linspace(0, len(audio) - 1, n),
                     np.arange(len(audio)), audio).astype(np.float32)


def word_timing(whisper_model: WhisperModel, audio: np.ndarray,
                rate: int | None = None, text: str | None = None) -> dict:
    """Word-level timestamps for Krishna's own speech, in the shape
    TalkingHead's speakAudio() expects (ms). Audio is resampled to 16kHz
    first: faster-whisper assumes 16kHz, so feeding it Kokoro's 24kHz audio
    stretched every timestamp by 1.5x and the mouth lagged the voice."""
    lang_key = (_voice_for_text(text) if text else None) or CURRENT_LANGUAGE
    segments, _ = whisper_model.transcribe(
        _resample(audio, rate or _LAST_RATE),
        language=LANGUAGES[lang_key]["whisper"], word_timestamps=True)
    words, wtimes, wdurations = [], [], []
    for seg in segments:
        for w in seg.words:
            words.append(w.word.strip())
            wtimes.append(round(w.start * 1000))
            wdurations.append(round((w.end - w.start) * 1000))
    return {"words": words, "wtimes": wtimes, "wdurations": wdurations}


def speak(tts_cache: dict, text: str) -> None:
    for _, _, audio in _safe_tts_chunks(tts_cache, text):
        sd.play(_apply_volume(audio), samplerate=_LAST_RATE)
        sd.wait()


def _enter_pressed() -> bool:
    """Non-blocking: was Enter pressed since the last check? Windows-only."""
    import msvcrt
    hit = False
    while msvcrt.kbhit():
        msvcrt.getch()
        hit = True
    return hit


def speak_interruptible(tts_cache: dict, text: str) -> bool:
    """Same as speak(), but stops immediately on an Enter press. Returns True
    if interrupted."""
    for _, _, audio in _safe_tts_chunks(tts_cache, text):
        sd.play(_apply_volume(audio), samplerate=_LAST_RATE)
        while sd.get_stream().active:
            if _enter_pressed():
                sd.stop()
                return True
            time.sleep(0.05)
    return False


# ----------------------------------------------------------------- thinking

# Hindi and Telugu writing often ends sentences with a danda, not a period.
_SENTENCE_END = re.compile(r"[.!?।॥]+(?:\s|$)")


def _split_sentences(text: str) -> list[str]:
    parts, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        parts.append(text[start:m.end()].strip())
        start = m.end()
    parts.append(text[start:].strip())
    return [p for p in parts if p]


_SCRIPT_NAMES = {
    "Telugu": "Telugu script", "Hindi": "Devanagari script", "Tamil": "Tamil script",
    "Kannada": "Kannada script", "Malayalam": "Malayalam script",
    "Marathi": "Devanagari script", "Bengali": "Bengali script",
    "Gujarati": "Gujarati script", "Punjabi": "Gurmukhi script",
    "Odia": "Odia script", "Assamese": "Assamese script",
}


def _language_note() -> dict | None:
    """Per-turn instruction, never saved to memory.json. Without it the model
    answered in English while Telugu was selected -- nothing had ever told it
    which language to reply in."""
    if AUTO_LANGUAGE:
        text = ("Reply in the language the user just used. If they used Telugu or "
                "Hindi -- in its own script or typed in English letters -- reply in "
                "that language written in its native script (Telugu script or "
                "Devanagari), not in English letters.")
    else:
        lang = CURRENT_LANGUAGE.replace(" (MMS voice)", "")
        if lang.startswith("English"):
            return None
        script = _SCRIPT_NAMES.get(lang)
        text = (f"Always reply in {lang}"
                + (f", written in {script} -- never in English letters" if script else "")
                + f" -- even if the user writes in English or types {lang} in English letters.")
    return {"role": "system", "content": text}


def _with_language_note(history: list) -> list:
    note = _language_note()
    return history + [note] if note else history


_TOOL_FUNCS = {f.__name__: f for f in _tools.ALL_TOOLS}

# Local-mode keyword gate. Deliberately generous -- a false positive costs one
# slow pass, a false negative means answering from a stale training cutoff.
_TOOL_HINTS = (
    "weather", "temperature", "forecast", "raining", "rain", "snow", "humid",
    "hot ", "cold ", "climate",
    "convert", "currency", "exchange rate", "usd", "eur", "inr", "gbp", "yen",
    "dollar", "rupee", "euro", "pound",
    "stock", "share price", "shares", "ticker", "nasdaq", "dow", "s&p",
    "market", "trading at", "stock price",
    "score", "scored", "game", "match", "played", "beat", "won", "lost",
    "fixture", "league", "tournament",
    "define", "definition", "meaning of", "what does", "spell",
    "joke", "funny", "make me laugh",
    "how much is", "what is the price", "cost of",
    # Bare time words ("today", "now") are deliberately NOT hints: they made
    # "how are you doing today?" pay the full 22s tool pass.
)
_TOOL_HINT_WORDS = [h.strip() for h in _TOOL_HINTS if " " not in h.strip() and len(h.strip()) >= 4]


def _text_hints_tool(text: str) -> bool:
    text = text.lower()
    if any(hint in text for hint in _TOOL_HINTS):
        return True
    # Typos: "hows wheater" missed the gate, no tool ran, and the model
    # invented a forecast. Close-match single words against the hint words.
    return any(difflib.get_close_matches(w, _TOOL_HINT_WORDS, n=1, cutoff=0.8)
               for w in re.findall(r"[a-z]{4,}", text))


def _might_need_tool(history: list) -> bool:
    """Cheap gate in front of the local tool-detection pass (measured at ~18s
    of a ~20s reply). A short follow-up to a data question counts too -- "iselin
    nj" right after a weather question is still a weather question."""
    users = [str(m.get("content", "")) for m in history if m.get("role") == "user"]
    if not users:
        return False
    if _text_hints_tool(users[-1]):
        return True
    return len(users) > 1 and len(users[-1].split()) <= 4 and _text_hints_tool(users[-2])


def _resolve_tools(model: str, history: list) -> list:
    """Local mode: one non-streaming pre-pass to see if the model wants a
    live-data tool before answering. Returns history unchanged if not, or
    with the tool call and its real result appended."""
    if not _might_need_tool(history):
        return history
    check = ollama.chat(model=model, messages=history, tools=_tools.ALL_TOOLS,
                        keep_alive=KEEP_ALIVE)
    calls = check["message"].get("tool_calls")
    if not calls:
        return history
    working = history + [check["message"]]
    results = []
    for call in calls:
        name = call["function"]["name"]
        fn = _TOOL_FUNCS.get(name)
        if not fn:
            continue
        try:
            result = fn(**call["function"]["arguments"])
        except Exception as e:
            result = f"Tool call failed: {e}"
        results.append(str(result))
        # Without `name`, llama3.2:3b didn't connect the result to its own call.
        working.append({"role": "tool", "name": name, "content": str(result)})
    if results:
        # Even with a named tool message this model kept deflecting while
        # holding the real number; restating it plainly is what stopped that.
        working.append({
            "role": "user",
            "content": ("Here is the real, current data just looked up for my "
                        "question: " + " | ".join(results) +
                        "\nAnswer using these exact figures. Do not say you "
                        "lack real-time access, do not suggest I check "
                        "elsewhere, and do not call this a sample -- this is "
                        "the genuine current answer."),
        })
    return working


def _cloud_reply(history: list) -> str:
    if not CLOUD_MODE:
        return ""
    try:
        return _groq_think(_with_language_note(history))
    except Exception as e:
        print(f"[krishna] cloud reply failed, using local model: {e}")
        return ""


def think(model: str, history: list) -> str:
    """Whole reply at once -- used by the avatar path, which needs the
    complete text before it can synthesize and time it."""
    reply = _cloud_reply(history)
    if reply:
        return reply
    messages = _resolve_tools(model, _with_language_note(history))
    response = ollama.chat(model=model, messages=messages, keep_alive=KEEP_ALIVE)
    return response["message"]["content"]


def think_and_speak(tts_cache: dict, model: str, history: list) -> str:
    """Speaks the reply sentence by sentence, stopping on an Enter press.
    Fast mode gets the whole reply from Groq in well under a second, so it
    just speaks it; local mode streams tokens and starts on the first
    finished sentence, which is most of what makes the slow path bearable."""
    reply = _cloud_reply(history)
    if reply:
        for sentence in _split_sentences(reply):
            if speak_interruptible(tts_cache, sentence):
                break
        return reply
    messages = _resolve_tools(model, _with_language_note(history))
    buffer = ""
    full_reply = ""
    stream = ollama.chat(model=model, messages=messages, stream=True,
                         keep_alive=KEEP_ALIVE)
    interrupted = False
    for chunk in stream:
        if interrupted:
            break   # stop pulling more of the reply once you've cut in
        piece = chunk["message"]["content"]
        buffer += piece
        full_reply += piece
        m = _SENTENCE_END.search(buffer)
        while m and not interrupted:
            sentence = buffer[:m.end()].strip()
            buffer = buffer[m.end():]
            if sentence:
                interrupted = speak_interruptible(tts_cache, sentence)
            m = _SENTENCE_END.search(buffer)
    if buffer.strip() and not interrupted:
        speak_interruptible(tts_cache, buffer.strip())
    return full_reply


def main() -> int:
    print("=" * 60)
    print("  KRISHNA -- voice companion")
    print("  Mode: " + ("fast (Groq cloud, local fallback)" if CLOUD_MODE
                        else "fully local -- nothing leaves this machine"))
    print("=" * 60)

    whisper_model = _load_whisper()
    tts = _load_tts()
    try:
        ollama.show(MODEL)
    except Exception:
        if not CLOUD_MODE:
            print(f"[krishna] model '{MODEL}' not found -- run: ollama pull {MODEL}")
            return 1

    history = load_history()
    remembered = len(history) - 1   # minus the system prompt
    if remembered:
        print(f"[krishna] Loaded {remembered} message(s) from past sessions "
              f"({MEMORY_PATH.name}).")
    print("[krishna] Ready. Press Enter, then just talk -- it stops recording "
          "on its own once you go quiet. Say 'note ...' or 'remember ...' to "
          "jot something down without a full reply. Say 'goodbye' to exit.")

    while True:
        print("\n[krishna] Press Enter, then speak...")
        input()
        print("[krishna] Listening...")
        audio = record_vad()
        if len(audio) == 0:
            print("[krishna] (didn't catch that)")
            continue
        t0 = time.time()
        text = transcribe(whisper_model, audio)
        if not text:
            print("[krishna] (didn't catch that)")
            continue
        print(f"[you] {text}  ({round(time.time()-t0, 1)}s to transcribe)")

        if "goodbye" in text.lower():
            speak(tts, "Goodbye. Talk soon.")
            save_history(history)
            break

        note_reply = maybe_take_note(text)
        if note_reply is not None:
            print(f"[krishna] {note_reply}  (saved to {NOTES_PATH.name}, no LLM call)")
            speak(tts, note_reply)
            continue   # notes don't enter the conversation history at all

        history.append({"role": "user", "content": text})
        t1 = time.time()
        reply = think_and_speak(tts, MODEL, history)
        print(f"[krishna] {reply}  ({round(time.time()-t1, 1)}s total)")
        history.append({"role": "assistant", "content": reply})
        save_history(history)

    return 0


if __name__ == "__main__":
    sys.exit(main())
