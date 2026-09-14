#!/usr/bin/env python
"""Krishna live -- FastAPI + WebSocket voice server with true barge-in.

Why this exists alongside app.py: Streamlit physically cannot interrupt
speech on the plain-voice path. There, audio plays server-side through
sounddevice and the script is BLOCKED inside that call, so a click can't
reach it until playback finishes. The fix isn't a cleverer Streamlit app,
it's a different transport -- a WebSocket that stays open in both
directions while audio is playing.

Design (pattern borrowed from open-gpt-live, which runs this same
Ollama + faster-whisper + Kokoro stack; the brain itself is krishna.py,
imported unchanged, so memory/notes/tools/18 languages all work here too):

  browser mic -> 16kHz PCM frames -> WS -> server Silero VAD
       -> end of utterance -> transcribe -> think -> synthesize
       -> PCM back over WS -> browser plays it

  ...and while the browser is playing, the mic STAYS OPEN. If you start
  talking, the server sees speech in the incoming frames, cancels the
  in-flight reply, and tells the browser to stop playing immediately.
  That's real barge-in: it stops because you spoke, not because you
  clicked something.

Run:
    python server.py          # then open http://127.0.0.1:8611
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import krishna as k

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static" / "live"
SAMPLE_RATE = 16000
FRAME = 512                      # Silero's required frame size at 16kHz
SILENCE_FRAMES = 28              # ~900ms of quiet ends the utterance
BARGE_IN_FRAMES = 5              # ~160ms of speech while speaking = interrupt

app = FastAPI(title="Krishna Live")

_models: dict = {}


def _load_models() -> None:
    if "whisper" not in _models:
        _models["whisper"] = k._load_whisper()
        _models["tts"] = k._load_tts()
        _models["vad"] = k._silero_vad()
        _models["history"] = k.load_history()


# Serves the TalkingHead library and avatar GLBs to the live page, the same
# assets the Streamlit app serves through its own static route.
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


class Session:
    """One browser connection. Holds the rolling mic buffer, the VAD state,
    and whatever reply is currently being generated so it can be cancelled
    the moment the user speaks over it."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.buf: list[np.ndarray] = []
        self.speech_frames = 0
        self.silence_frames = 0
        self.triggered = False
        self.speaking = False          # is the browser currently playing audio
        self.avatar = False            # browser has the avatar view enabled
        self.task: asyncio.Task | None = None
        self.vad = _models["vad"]
        self.vad.reset_states()

    def vad_prob(self, frame: np.ndarray) -> float:
        with torch.no_grad():
            return self.vad(torch.from_numpy(frame), SAMPLE_RATE).item()

    async def send(self, obj: dict) -> None:
        await self.ws.send_text(json.dumps(obj))

    async def cancel_reply(self, reason: str) -> None:
        """Barge-in: kill the in-flight generation and silence the browser."""
        if self.task and not self.task.done():
            self.task.cancel()
        self.speaking = False
        await self.send({"type": "stop", "reason": reason})

    async def on_frame(self, frame_i16: np.ndarray) -> None:
        frame = frame_i16.astype(np.float32) / 32768.0
        prob = self.vad_prob(frame)
        is_speech = prob >= 0.5

        # While Krishna is talking, incoming speech means interrupt -- not a
        # new utterance yet. Requiring several consecutive speech frames
        # stops a cough or a keyboard clack from cutting it off.
        if self.speaking:
            if is_speech:
                self.speech_frames += 1
                if self.speech_frames >= BARGE_IN_FRAMES:
                    self.speech_frames = 0
                    await self.cancel_reply("barge-in")
                    self.buf, self.triggered, self.silence_frames = [], False, 0
            else:
                self.speech_frames = 0
            return

        self.buf.append(frame_i16)
        if is_speech:
            self.triggered = True
            self.silence_frames = 0
        elif self.triggered:
            self.silence_frames += 1
            if self.silence_frames >= SILENCE_FRAMES:
                audio = np.concatenate(self.buf).astype(np.float32) / 32768.0
                self.buf, self.triggered, self.silence_frames = [], False, 0
                self.task = asyncio.create_task(self.handle_utterance(audio))
        elif len(self.buf) > 200:
            # Nothing but silence -- don't grow the buffer forever.
            self.buf = self.buf[-50:]

    async def handle_utterance(self, audio: np.ndarray) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self.send({"type": "status", "text": "transcribing"})
            text = await loop.run_in_executor(
                None, k.transcribe, _models["whisper"], audio)
            if not text.strip():
                await self.send({"type": "status", "text": "idle"})
                return
            await self.send({"type": "user", "text": text})

            # Notes never reach the LLM -- same plain-string path as the
            # console and Streamlit versions, on purpose.
            note = k.maybe_take_note(text)
            if note is not None:
                await self.send({"type": "assistant", "text": note})
                await self.speak(note)
                return

            await self.send({"type": "status", "text": "thinking"})
            history = _models["history"]
            history.append({"role": "user", "content": text})
            reply = await loop.run_in_executor(None, k.think, k.MODEL, history)
            history.append({"role": "assistant", "content": reply})
            k.save_history(history)

            await self.send({"type": "assistant", "text": reply})
            await self.speak(reply)
        except asyncio.CancelledError:
            # Expected on barge-in.
            raise
        except Exception as e:
            await self.send({"type": "error", "text": str(e)})

    async def speak(self, text: str) -> None:
        """Synthesize and stream the audio to the browser. Sent sentence by
        sentence so the first words arrive while the rest is still being
        generated, and so a barge-in can cut it off partway rather than
        only between whole replies."""
        loop = asyncio.get_running_loop()
        sentences = [s.strip() for s in k._SENTENCE_END.split(text) if s.strip()]
        self.speaking = True
        await self.send({"type": "speaking_start",
                         "sample_rate": k.current_sample_rate()})
        try:
            for sentence in sentences:
                audio = await loop.run_in_executor(
                    None, k.synthesize, _models["tts"], sentence)
                if audio is None or not len(audio):
                    continue
                # Word timings ride ahead of each chunk so the avatar can
                # lip-sync it. Only computed when the avatar is actually on:
                # it's a Whisper pass over Krishna's own speech (~0.15s per
                # sentence, measured), pure waste with the avatar off.
                if self.avatar:
                    timing = await loop.run_in_executor(
                        None, k.word_timing, _models["whisper"], audio)
                    await self.send({"type": "chunk_timing", **timing})
                pcm = (np.clip(k._apply_volume(audio), -1, 1) * 32767).astype("<i2")
                await self.ws.send_bytes(pcm.tobytes())
        except asyncio.CancelledError:
            raise
        finally:
            if self.speaking:
                await self.send({"type": "speaking_end"})
            self.speaking = False


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _load_models()
    session = Session(ws)
    await session.send({"type": "ready",
                        "languages": list(k.LANGUAGES.keys()),
                        "language": k.CURRENT_LANGUAGE})
    try:
        while True:
            msg = await ws.receive()
            # receive() RETURNS a disconnect message rather than raising, and
            # calling it again afterwards throws RuntimeError -- which killed
            # the session loop on every page reload. Handle it explicitly.
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                data = np.frombuffer(msg["bytes"], dtype="<i2")
                for i in range(0, len(data) - FRAME + 1, FRAME):
                    await session.on_frame(data[i:i + FRAME])
            elif "text" in msg and msg["text"]:
                payload = json.loads(msg["text"])
                if payload.get("type") == "set_language":
                    k.set_language(payload["language"])
                    await session.send({"type": "status",
                                        "text": f"language: {payload['language']}"})
                elif payload.get("type") == "set_avatar":
                    session.avatar = bool(payload.get("on"))
                elif payload.get("type") == "stop":
                    await session.cancel_reply("user-stop")
    except WebSocketDisconnect:
        pass
    finally:
        if session.task and not session.task.done():
            session.task.cancel()


if __name__ == "__main__":
    print("Krishna live -> http://127.0.0.1:8611")
    uvicorn.run(app, host="127.0.0.1", port=8611, log_level="warning")
