"""Krishna -- Streamlit UI. Same local pipeline as krishna.py (push-to-talk
console version), wrapped in a proper window with chat history instead of a
bare terminal. Still fully local -- brain/voice never leave this machine."""
import streamlit as st

from krishna import (MODEL, SAMPLE_RATE, SYSTEM_PROMPT, VOICE_OPTIONS, _load_tts,
                     _load_whisper, load_history, maybe_take_note, record_vad,
                     save_history, set_voice, speak, synthesize, think,
                     think_and_speak, transcribe, word_timing)
import avatar_widget

st.set_page_config(page_title="Krishna", page_icon="\U0001FA94", layout="centered")


@st.cache_resource
def get_whisper():
    return _load_whisper()


@st.cache_resource
def get_tts():
    return _load_tts()


whisper_model = get_whisper()
tts = get_tts()

st.title("Krishna")
st.caption("Fully local voice companion. Nothing you say leaves this machine — "
           "no Anthropic, no OpenAI, no cloud API of any kind.")

_voice_label = st.selectbox("🔊 Voice", list(VOICE_OPTIONS.keys()), key="voice_choice")
set_voice(VOICE_OPTIONS[_voice_label])

if "history" not in st.session_state:
    st.session_state.history = load_history()
    _remembered = len(st.session_state.history) - 1
    if _remembered:
        st.caption(f"Loaded {_remembered} message(s) from past sessions.")

for msg in st.session_state.history:
    if msg["role"] == "system":
        continue
    with st.chat_message(msg["role"]):
        st.write(msg["content"])


def _reply_and_speak(user_text: str) -> bool:
    """Returns True if this was a real conversational turn (and got saved to
    memory), False if it was a note (handled separately, never enters the
    conversation history -- same no-LLM-involved note path as krishna.py)."""
    note_reply = maybe_take_note(user_text)
    if note_reply is not None:
        st.info(f"📝 {note_reply}")
        speak(tts, note_reply)
        return False
    st.session_state.history.append({"role": "user", "content": user_text})
    if st.session_state.get("avatar_on"):
        # Avatar handles its own audio playback in-browser (Web Audio API
        # inside the iframe) -- must NOT also call speak()/sounddevice or
        # the reply would play twice. Non-streaming: the avatar needs the
        # complete text before it can synthesize+time it as one piece, so
        # this trades away think_and_speak()'s first-sentence-latency win.
        reply = think(MODEL, st.session_state.history)
        audio = synthesize(tts, reply)
        timing = word_timing(whisper_model, audio)
        st.session_state["_avatar_turn"] = st.session_state.get("_avatar_turn", 0) + 1
        st.session_state["_avatar_payload"] = (audio, timing, st.session_state["_avatar_turn"])
    else:
        reply = think_and_speak(tts, MODEL, st.session_state.history)
    st.session_state.history.append({"role": "assistant", "content": reply})
    save_history(st.session_state.history)
    return True


c1, c2, c3, c4 = st.columns([2.2, 1, 1, 1])
with c1:
    talk = st.button("🎤 Push to talk (stops on its own once you go quiet)",
                     width='stretch', type="primary")
with c2:
    st.toggle("🧑 Avatar", key="avatar_on",
             help="Shows a talking-head avatar instead of just playing audio. "
                  "Reloads each reply (brief flicker) -- no persistent "
                  "avatar yet, that needs a proper custom component.")
with c3:
    if st.button("🗑 Clear", width='stretch'):
        st.session_state.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        save_history(st.session_state.history)   # else a reload brings the old history back
        st.rerun()
with c4:
    if st.button("🔄 Restart", width='stretch',
                 help="Picks up code changes to krishna.py/avatar_widget.py -- a "
                      "normal page refresh only reloads app.py; Python caches the "
                      "other files on import, so this restarts the whole server."):
        st.warning("Restarting server to load new code — this page reloads itself "
                   "in ~6 seconds.")
        st.components.v1.html(
            "<script>setTimeout(() => window.parent.location.reload(), 6000);</script>",
            height=0)

        def _delayed_exit():
            import os
            import time
            time.sleep(1)   # let this response finish sending before the process dies
            os._exit(0)     # supervisor.py notices and starts a fresh server

        import threading
        threading.Thread(target=_delayed_exit, daemon=True).start()

if st.session_state.get("avatar_on"):
    if "_avatar_payload" in st.session_state:
        audio, timing, turn_id = st.session_state["_avatar_payload"]
        # Only actually speak the FIRST time this exact reply is rendered.
        # Streamlit reruns the whole script on ANY widget interaction
        # anywhere on the page, not just a new reply -- without this check,
        # every unrelated click re-triggered speakAudio() on the same old
        # audio, which is exactly the "mouth moving when no one is
        # speaking" bug reported live. Idle-render (face only, no
        # speakAudio call) on every rerun after the first.
        already_spoken = st.session_state.get("_avatar_spoken_turn") == turn_id
        if not already_spoken:
            st.session_state["_avatar_spoken_turn"] = turn_id
        avatar_widget.render_speaking(audio, timing["words"], timing["wtimes"],
                                      timing["wdurations"], speak=not already_spoken)
    else:
        avatar_widget.render_idle()

if talk:
    with st.status("Listening…", expanded=True) as s:
        # No fixed recording window -- stops automatically once you stop
        # talking (voice-activity detection), not after a set N seconds
        # regardless of how long you actually spoke.
        audio = record_vad()
        if len(audio) == 0:
            s.update(label="Didn't catch that — try again", state="error")
        else:
            s.write("Transcribing…")
            text = transcribe(whisper_model, audio)
            if not text:
                s.update(label="Didn't catch that — try again", state="error")
            else:
                s.write(f'You said: "{text}"')
                s.write("Thinking + speaking (starts on the first sentence, "
                        "doesn't wait for the whole reply)…")
                _reply_and_speak(text)
                s.update(label="Done", state="complete")
    st.rerun()

typed = st.chat_input("...or type instead of talking")
if typed:
    with st.spinner("Thinking…"):
        _reply_and_speak(typed)
    st.rerun()
