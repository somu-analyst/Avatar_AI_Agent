#!/usr/bin/env python
"""Krishna -- a fully local, private voice companion.

Push-to-talk loop: press Enter to start recording, speak, press Enter again
to stop. Transcribes (Whisper, local) -> thinks (Ollama, local) -> speaks
(Kokoro, local). Nothing here ever leaves this machine -- no Anthropic, no
OpenAI, no cloud API of any kind at runtime. That's the whole point.

Usage:
    python krishna.py
"""
from __future__ import annotations
import collections
import json
import queue
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import ollama
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel

import tools as _tools

MODEL = "llama3.2:3b"
SAMPLE_RATE = 16000
# Ollama unloads an idle model by default, which is exactly why the first
# reply in a session pays a multi-second reload cost. keep_alive holds it
# resident in memory for the length of an active conversation.
KEEP_ALIVE = "30m"

ROOT = Path(__file__).resolve().parent
MEMORY_PATH = ROOT / "memory.json"
NOTES_PATH = ROOT / "notes.md"
MAX_HISTORY_TURNS = 40   # trimmed so an old conversation can't grow the prompt forever

SYSTEM_PROMPT = """You are Krishna, a warm, direct, knowledgeable personal companion and
advisor. You speak naturally and conversationally, like a trusted friend who has real
expertise -- not like a corporate assistant. Keep responses short and spoken-natural
(1-3 sentences unless genuinely more is needed) since this is a VOICE conversation, not
a chat window. Be honest and specific rather than generically encouraging.
When a tool result appears in the conversation, that is REAL, current data you just
looked up -- state it directly and specifically (the actual number/fact), in your own
natural voice. Never deflect, joke about not knowing, or hedge with "check elsewhere"
when you were just given the real answer -- that's actively wrong, not humble."""


def load_history() -> list[dict]:
    """Loads past conversation turns from disk so Krishna remembers previous
    sessions, not just the current one. Plain local JSON -- nothing sent
    anywhere, same as everything else here. Starts fresh if the file is
    missing or unreadable rather than crashing on a corrupt/partial file."""
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
    load so a SYSTEM_PROMPT edit takes effect on old saved sessions too,
    instead of an old prompt being frozen into memory.json forever."""
    to_save = [m for m in history if m["role"] != "system"][-MAX_HISTORY_TURNS:]
    MEMORY_PATH.write_text(json.dumps(to_save, indent=2), encoding="utf-8")


_NOTE_TRIGGERS = ("note ", "note:", "remember ", "remember that ", "remember this")


def maybe_take_note(text: str) -> str | None:
    """If you say something starting with 'note' or 'remember', this appends
    it straight to notes.md and returns a confirmation string -- no LLM
    involved in deciding what gets written, on purpose. That's what keeps
    this safe: a plain string match and a file append, not an agent deciding
    for itself what's worth remembering. Returns None for anything else, so
    normal conversation is untouched."""
    low = text.strip().lower()
    # Longest trigger first -- "remember that " must be checked before the
    # shorter "remember " or it never gets a chance to match (caught live:
    # "remember that the wifi password..." stripped to "that the wifi
    # password..." because "remember " matched first and won).
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
    print("[krishna] loading speech recognition (first run downloads the model)...")
    return WhisperModel("base.en", device="cpu", compute_type="int8")


def _load_tts():
    from kokoro import KPipeline
    print("[krishna] loading voice (first run downloads the model)...")
    return KPipeline(lang_code="a")   # 'a' = American English


def record_vad(max_seconds: int = 20, silence_ms: int = 900,
              aggressiveness: int = 2) -> np.ndarray:
    """Records until it detects you've actually stopped talking (via
    webrtcvad, the same voice-activity-detection real voice assistants use)
    instead of a fixed duration -- this was the single biggest source of
    "not like a real conversation": waiting a fixed N seconds regardless of
    when you actually finished speaking. Stops after `silence_ms` of quiet
    following detected speech, or `max_seconds` as a hard cap either way."""
    vad = webrtcvad.Vad(aggressiveness)
    frame_ms = 30
    frame_size = int(SAMPLE_RATE * frame_ms / 1000)
    silence_frames_needed = max(1, silence_ms // frame_ms)

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
    return (audio_i16.astype(np.float32) / 32768.0)


def transcribe(whisper_model: WhisperModel, audio: np.ndarray) -> str:
    if len(audio) < SAMPLE_RATE * 0.3:   # too short to be real speech
        return ""
    segments, _ = whisper_model.transcribe(audio, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


_TTS_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9\s.,!?;:'\"()\-]")


def _sanitize_for_tts(text: str) -> str:
    """Strips characters known to trigger Kokoro's G2P (misaki) crash --
    emoji, markdown symbols (*, #), unusual unicode -- a defensive pass, not
    a guarantee. _safe_tts_chunks() still catches whatever gets through."""
    cleaned = _TTS_UNSAFE_CHARS.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


# Kokoro ships ~54 built-in voices; this is a curated, verified-real subset
# (naming convention: a=American/b=British, f=female/m=male) rather than the
# full list -- single-user local app, so a module-level "current voice" is
# simpler than threading a parameter through every call site (synthesize,
# speak, speak_interruptible, think_and_speak all funnel through here).
VOICE_OPTIONS = {
    "Heart (US female, default)": "af_heart",
    "Bella (US female)": "af_bella",
    "Nicole (US female)": "af_nicole",
    "Adam (US male)": "am_adam",
    "Michael (US male)": "am_michael",
    "Emma (British female)": "bf_emma",
    "George (British male)": "bm_george",
}
CURRENT_VOICE = "af_heart"


def set_voice(voice_id: str) -> None:
    global CURRENT_VOICE
    CURRENT_VOICE = voice_id


def _safe_tts_chunks(tts_pipeline, text: str):
    """Kokoro's G2P can crash with `TypeError: unsupported operand type(s)
    for +: 'NoneType' and 'str'` on certain text (a token gets no phonemes
    assigned) instead of failing gracefully -- caught live, it took the
    whole Streamlit app down mid-reply. Sanitizes first; if it STILL fails,
    yields nothing rather than crashing, so a bad reply just goes unspoken
    instead of taking the app down."""
    safe_text = _sanitize_for_tts(text)
    if not safe_text:
        return
    try:
        yield from tts_pipeline(safe_text, voice=CURRENT_VOICE)
    except Exception as e:
        print(f"[krishna] TTS failed on this text, skipping speech: {e}")
        return


def synthesize(tts_pipeline, text: str) -> np.ndarray:
    """Text -> Kokoro's raw float32 audio at 24kHz, no playback. Split out
    from speak() so the avatar path can get the same audio Whisper will
    analyze for lip-sync timing, instead of duplicating synthesis."""
    chunks = [audio for _, _, audio in _safe_tts_chunks(tts_pipeline, text)]
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)


def word_timing(whisper_model: WhisperModel, audio: np.ndarray) -> dict:
    """Runs Whisper (already in this stack for speech-IN) on Krishna's own
    generated speech-OUT to get real word-level timestamps -- this is the
    local, no-cloud alternative to TalkingHead's default Google-Cloud-TTS-
    based lip-sync timing. Returns {words, wtimes, wdurations} in the exact
    shape TalkingHead's speakAudio() expects (times/durations in ms)."""
    segments, _ = whisper_model.transcribe(audio, language="en", word_timestamps=True)
    words, wtimes, wdurations = [], [], []
    for seg in segments:
        for w in seg.words:
            words.append(w.word.strip())
            wtimes.append(round(w.start * 1000))
            wdurations.append(round((w.end - w.start) * 1000))
    return {"words": words, "wtimes": wtimes, "wdurations": wdurations}


def speak(tts_pipeline, text: str) -> None:
    for _, _, audio in _safe_tts_chunks(tts_pipeline, text):
        sd.play(audio, samplerate=24000)
        sd.wait()


def _enter_pressed() -> bool:
    """Non-blocking check: was Enter pressed since the last check? Windows-
    only (msvcrt) -- fine here since this whole project targets your Windows
    laptop. Used to let you cut Krishna off mid-sentence instead of having to
    sit through the whole reply once it starts talking, same as a real
    conversation lets you interrupt."""
    import msvcrt
    hit = False
    while msvcrt.kbhit():
        msvcrt.getch()
        hit = True
    return hit


def speak_interruptible(tts_pipeline, text: str) -> bool:
    """Same as speak(), but stops immediately if you press Enter while it's
    talking. Returns True if you interrupted it, False if it finished on its
    own -- the caller uses this to stop queuing more sentences once you've
    clearly indicated you want to cut in."""
    for _, _, audio in _safe_tts_chunks(tts_pipeline, text):
        sd.play(audio, samplerate=24000)
        while sd.get_stream().active:
            if _enter_pressed():
                sd.stop()
                return True
            time.sleep(0.05)
    return False


_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")


_TOOL_FUNCS = {f.__name__: f for f in _tools.ALL_TOOLS}


def _resolve_tools(model: str, history: list) -> list:
    """One non-streaming pre-pass to check if the model wants a live-data
    tool (weather/currency/dictionary/jokes -- see tools.py) before actually
    answering. This is THE fix for "my knowledge cutoff is December 2023":
    no local model can know anything past its training cutoff no matter how
    it's prompted -- the only real fix is fetching live data and handing it
    to the model as context for THIS answer, which is what this does.
    Returns history unchanged if no tool was needed (the common case), or
    history + the tool call + its real result appended, ready for the
    caller to get a final answer grounded in that real data."""
    check = ollama.chat(model=model, messages=history, tools=_tools.ALL_TOOLS,
                        keep_alive=KEEP_ALIVE)
    calls = check["message"].get("tool_calls")
    if not calls:
        return history
    working = history + [check["message"]]
    for call in calls:
        fn = _TOOL_FUNCS.get(call["function"]["name"])
        if not fn:
            continue
        try:
            result = fn(**call["function"]["arguments"])
        except Exception as e:
            result = f"Tool call failed: {e}"
        working.append({"role": "tool", "content": str(result)})
    return working


def think(model: str, history: list) -> str:
    """Non-streaming reply, whole thing at once -- used by the avatar path,
    which needs the complete text before it can synthesize+time it as one
    piece. Trades away think_and_speak()'s first-sentence-latency win in
    exchange for a simpler, correct avatar integration; the console/plain-
    voice path keeps using think_and_speak() for that speed."""
    history = _resolve_tools(model, history)
    response = ollama.chat(model=model, messages=history, keep_alive=KEEP_ALIVE)
    return response["message"]["content"]


def think_and_speak(tts_pipeline, model: str, history: list) -> str:
    """Streams the reply and speaks each completed SENTENCE as soon as it's
    ready, instead of waiting for the entire response to finish generating
    first -- first-sentence latency instead of full-reply latency, which is
    most of what actually makes ChatGPT/Grok voice feel fast. keep_alive
    keeps the model warm so only the very first turn in a session pays a
    reload cost. Checks for an Enter-press between sentences too, so a long
    reply can be cut off partway through, not just within one sentence.
    Also checks for a live-data tool need first (weather/currency/etc,
    same as think()) -- costs one extra non-streaming round trip on every
    turn, but that's the price of Krishna reliably knowing to reach for
    real data instead of guessing from a frozen training cutoff."""
    history = _resolve_tools(model, history)
    buffer = ""
    full_reply = ""
    stream = ollama.chat(model=model, messages=history, stream=True,
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
                interrupted = speak_interruptible(tts_pipeline, sentence)
            m = _SENTENCE_END.search(buffer)
    if buffer.strip() and not interrupted:
        speak_interruptible(tts_pipeline, buffer.strip())
    return full_reply


def main() -> int:
    print("=" * 60)
    print("  KRISHNA -- fully local voice companion")
    print("  Nothing you say leaves this machine.")
    print("=" * 60)

    whisper_model = _load_whisper()
    tts = _load_tts()
    try:
        ollama.show(MODEL)
    except Exception:
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
        print(f"[krishna] {reply}  (first words spoken within a sentence of "
              f"starting; {round(time.time()-t1, 1)}s total)")
        history.append({"role": "assistant", "content": reply})
        save_history(history)

    return 0


if __name__ == "__main__":
    sys.exit(main())
