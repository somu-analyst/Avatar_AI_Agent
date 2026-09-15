"""Krishna -- Streamlit UI over krishna.py: chat history, voice and avatar
settings, and the same brain as the console version. Fast mode (Groq cloud)
or fully local, switchable in the sidebar."""
import streamlit as st

import krishna as k
from krishna import (AUTO_LANGUAGE_LABEL, AVATAR_CAPABLE_LANGUAGES, LANGUAGES, MODEL,
                     SYSTEM_PROMPT, _load_tts, _load_whisper, cloud_available,
                     load_history, maybe_take_note, prepare_voices, record_vad,
                     save_history, set_auto_language, set_cloud_mode, set_language,
                     set_preferred_voice, set_volume, speak, synthesize_with_rate,
                     think, think_and_speak, transcribe, word_timing)
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

# Every setting lives in the sidebar, always visible and grouped in one place.
with st.sidebar:
    st.header("Settings")

    st.subheader("⚡ Speed")
    if cloud_available():
        _cloud = st.toggle("Fast mode (cloud)", value=True, key="cloud_mode",
                           help="Groq does the speech recognition and the thinking: "
                                "first words in well under a second instead of 3-20s "
                                "on this CPU, and far better Telugu. Switch off to keep "
                                "everything on this machine.")
    else:
        _cloud = False
        st.caption("Fast mode needs GROQ_API_KEY in krishna/api_keys.env.")
    set_cloud_mode(_cloud)
    if _cloud:
        st.caption("Sent to Groq: your voice recording and the conversation text. "
                   "Stays here: memory, notes, and Krishna's voice.")

    st.divider()
    st.subheader("🗣 Voice")
    _choice = st.selectbox("Language", [AUTO_LANGUAGE_LABEL] + list(LANGUAGES.keys()),
                           index=0, key="language_choice",
                           help="Auto listens for English, Hindi or Telugu and answers "
                                "in whichever you used -- including Telugu or Hindi "
                                "typed in English letters.")
    _auto = _choice == AUTO_LANGUAGE_LABEL
    set_auto_language(_auto)
    if not _auto:
        set_language(_choice)
    _voice_lang = "English (US)" if _auto else _choice
    _voice_options = LANGUAGES[_voice_lang]["voices"]
    # Keyed by language so switching language never reuses a stale voice that
    # doesn't exist in the new language's voice set.
    _voice_label = st.selectbox("Voice" + (" (for English replies)" if _auto else ""),
                                list(_voice_options.keys()),
                                key=f"voice_choice_{_voice_lang}")
    set_preferred_voice(_voice_lang, _voice_options[_voice_label])

    _vol = st.slider("🔈 Volume", min_value=0.0, max_value=3.0, value=1.0, step=0.1,
                     help="1.0 is the voice model's own level. Above that genuinely "
                          "amplifies (clipped to avoid crackle), below that quietens.")
    set_volume(_vol)

    # Off by default, and never auto-enabled: the mic staying live between
    # turns is exactly the "something is happening without my notice" concern.
    st.toggle("🔁 Hands-free mode", key="hands_free",
              help="After each reply Krishna listens again automatically. Only "
                   "listens between replies, never while it's speaking.")

    # Each MMS voice downloads ~145MB on first use. Doing it here, visibly,
    # beats a silent download mid-reply that looks like a hang.
    if _auto:
        _needed = ["Telugu", "Hindi (MMS voice)"]
    elif LANGUAGES[_choice].get("engine") == "mms":
        _needed = [_choice]
    else:
        _needed = []
    _missing = [n for n in _needed if f"mms:{LANGUAGES[n]['mms']}" not in tts]
    if _missing:
        with st.spinner(f"Getting the {' and '.join(_missing)} voice ready -- "
                        f"one-time download, then it's instant."):
            try:
                prepare_voices(tts, _missing)
            except Exception as exc:
                st.error(f"Couldn't load a voice: {exc}")
    if _needed:
        st.caption("Indian-language voices use Meta's MMS-TTS -- one voice per "
                   "language, so no accent choice yet.")

    st.divider()
    st.subheader("🧑 Avatar")
    st.toggle("Show avatar", key="avatar_on",
              help="Shows a talking-head avatar instead of just playing audio. "
                   "Reloads each reply (brief flicker) in this app; Krishna Live "
                   "keeps it mounted.")
    _avatar_choice = st.selectbox(
        "Face", list(avatar_widget.AVATARS.keys()),
        index=list(avatar_widget.AVATARS.keys()).index(avatar_widget.DEFAULT_AVATAR),
        key="avatar_choice")
    _bg_choice = st.selectbox(
        "Background", list(avatar_widget.BACKGROUNDS.keys()),
        index=list(avatar_widget.BACKGROUNDS.keys()).index(avatar_widget.DEFAULT_BACKGROUND),
        key="background_choice")
    avatar_widget.set_avatar(_avatar_choice)
    avatar_widget.set_background(_bg_choice)
    _credit = avatar_widget.background_credit(_bg_choice)
    if _credit:
        st.caption(_credit)
    if not _auto and _choice not in AVATAR_CAPABLE_LANGUAGES:
        st.caption(f"Lip-sync shapes are approximate in {_choice}: the mouth moves "
                   f"in time with the speech, but exact mouth shapes exist only "
                   f"for English and French.")

st.title("Krishna")
if k.CLOUD_MODE:
    st.caption("Voice companion -- fast mode: speech recognition and replies run on "
               "Groq. Memory and notes stay on this machine.")
else:
    st.caption("Fully local voice companion. Nothing you say leaves this machine.")

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
    """True for a real conversational turn (saved to memory), False for a note
    (never enters the conversation history)."""
    note_reply = maybe_take_note(user_text)
    if note_reply is not None:
        st.info(f"📝 {note_reply}")
        speak(tts, note_reply)
        return False
    st.session_state.history.append({"role": "user", "content": user_text})
    if st.session_state.get("avatar_on"):
        # The avatar plays audio in the browser, so speak() must not also play
        # it. It needs the complete reply to synthesize and time as one piece.
        reply = think(MODEL, st.session_state.history)
        audio, rate = synthesize_with_rate(tts, reply)
        timing = word_timing(whisper_model, audio, rate=rate, text=reply)
        st.session_state["_avatar_turn"] = st.session_state.get("_avatar_turn", 0) + 1
        st.session_state["_avatar_payload"] = (audio, timing,
                                               st.session_state["_avatar_turn"], rate)
    else:
        reply = think_and_speak(tts, MODEL, st.session_state.history)
    st.session_state.history.append({"role": "assistant", "content": reply})
    save_history(st.session_state.history)
    return True


# Only three controls here: with five, Streamlit truncated the toggle labels to
# "Ha…" and "…" and the avatar switch was unidentifiable.
c1, c2, c3 = st.columns([2.4, 1, 1])
with c1:
    talk = st.button("🎤 Talk", width='stretch', type="primary",
                     help="Starts listening and stops on its own once you go quiet.")
with c2:
    if st.button("🗑 Clear", width='stretch'):
        st.session_state.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        save_history(st.session_state.history)   # else a reload brings the old history back
        st.rerun()
with c3:
    if st.button("🔄 Restart", width='stretch',
                 help="Picks up code changes to krishna.py/avatar_widget.py -- Python "
                      "caches them on import, so this restarts the whole server."):
        st.warning("Restarting server to load new code -- this page reloads itself "
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
        payload = st.session_state["_avatar_payload"]
        audio, timing, turn_id = payload[:3]
        rate = payload[3] if len(payload) > 3 else 24000
        # Only speak the FIRST time this reply renders -- Streamlit reruns the
        # whole script on any widget interaction, and re-speaking stale audio
        # was the "mouth moving when no one is speaking" bug.
        already_spoken = st.session_state.get("_avatar_spoken_turn") == turn_id
        if not already_spoken:
            st.session_state["_avatar_spoken_turn"] = turn_id
        avatar_widget.render_speaking(audio, timing["words"], timing["wtimes"],
                                      timing["wdurations"], speak=not already_spoken,
                                      sample_rate=rate)
    else:
        avatar_widget.render_idle()

# Hands-free continues the loop by setting _auto_listen after a reply. It's
# re-checked against the live toggle on every rerun, so switching it off stops
# the next turn immediately.
_auto_listen = st.session_state.pop("_auto_listen", False) and st.session_state.get("hands_free")

if talk or _auto_listen:
    if _auto_listen:
        st.info("🎙 Listening -- hands-free is on. Switch it off any time to stop.")
    with st.status("Listening…", expanded=True) as s:
        audio = record_vad()
        heard = False
        if len(audio) == 0:
            s.update(label="Didn't catch that -- try again", state="error")
        else:
            s.write("Transcribing…")
            text = transcribe(whisper_model, audio)
            if not text:
                s.update(label="Didn't catch that -- try again", state="error")
            else:
                heard = True
                s.write(f'You said: "{text}"')
                s.write("Thinking + speaking…")
                _reply_and_speak(text)
                s.update(label="Done", state="complete")
    # Only continue after a turn that actually happened -- otherwise silence
    # would spin listen->nothing->listen forever.
    if heard and st.session_state.get("hands_free"):
        st.session_state["_auto_listen"] = True
    st.rerun()

typed = st.chat_input("...or type instead of talking")
if typed:
    with st.spinner("Thinking…"):
        _reply_and_speak(typed)
    st.rerun()
