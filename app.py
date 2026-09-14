"""Krishna -- Streamlit UI. Same local pipeline as krishna.py (push-to-talk
console version), wrapped in a proper window with chat history instead of a
bare terminal. Still fully local -- brain/voice never leave this machine."""
import streamlit as st

from krishna import (AVATAR_CAPABLE_LANGUAGES, DEFAULT_LANGUAGE, LANGUAGES, MODEL,
                     SAMPLE_RATE, SYSTEM_PROMPT, _load_tts, _load_whisper,
                     load_history, maybe_take_note, record_vad, save_history,
                     current_sample_rate, prepare_voice, set_language, set_voice, set_volume, speak, synthesize,
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

st.title("Krishna")
st.caption("Fully local voice companion. Nothing you say leaves this machine — "
           "no Anthropic, no OpenAI, no cloud API of any kind.")

# Every setting lives in the sidebar, always visible and grouped in one
# place. They used to be spread across the top of the page, and the avatar
# pickers were hidden until the avatar was switched on -- which meant the
# options existed but couldn't be found ("where is option to change avatar
# and voices"). Discoverability beats saving vertical space here.
with st.sidebar:
    st.header("Settings")

    st.subheader("🗣 Voice")
    _lang = st.selectbox("Language", list(LANGUAGES.keys()),
                         index=list(LANGUAGES.keys()).index(DEFAULT_LANGUAGE),
                         key="language_choice")
    set_language(_lang)
    # Keyed by language so switching language always shows THAT language's
    # voices from their default (first) option, instead of Streamlit reusing
    # a stale voice_choice value that may not exist in the new language's
    # voice set at all.
    _voice_options = LANGUAGES[_lang]["voices"]
    _voice_label = st.selectbox("Voice", list(_voice_options.keys()),
                                key=f"voice_choice_{_lang}")
    set_voice(_voice_options[_voice_label])

    _vol = st.slider("🔈 Volume", min_value=0.0, max_value=3.0, value=1.0, step=0.1,
                     help="1.0 is the voice model's own level. Above that "
                          "genuinely amplifies (clipped to avoid crackle), "
                          "below that quietens.")
    set_volume(_vol)

    # Off by default, and deliberately never auto-enabled: the mic staying
    # live between turns is exactly the "something is happening without my
    # notice" concern raised earlier, so it's opt-in, clearly labelled while
    # active, and stops the moment it's switched off.
    st.toggle("🔁 Hands-free mode", key="hands_free",
             help="Keeps the conversation going: after each reply Krishna "
                  "listens again automatically, so you never touch the Talk "
                  "button. Only listens between replies, never while it's "
                  "speaking. Switch off any time.")

    # An MMS language downloads a ~145MB model the first time it's used, and
    # that used to happen silently mid-reply -- looking like an unexplained
    # hang behind a spinner. Doing it here, with a visible message, means the
    # wait is explained and happens once, before you try to talk.
    if LANGUAGES[_lang].get("engine") == "mms":
        _mms_key = f"mms:{LANGUAGES[_lang]['mms']}"
        if _mms_key not in tts:
            with st.spinner(f"Getting the {_lang} voice ready — one-time "
                            f"~145MB download, then it's instant."):
                try:
                    prepare_voice(tts)
                except Exception as exc:
                    st.error(f"Couldn't load the {_lang} voice: {exc}")

    if LANGUAGES[_lang].get("engine") == "mms":
        st.caption("Indian-language voices use Meta's MMS-TTS — one voice per "
                   "language, so no accent choice yet. Multiple Telugu voices "
                   "would need AI4Bharat's IndicF5, which is a gated model "
                   "(needs an access request on your Hugging Face account).")

    st.divider()
    st.subheader("🧑 Avatar")
    st.toggle("Show avatar", key="avatar_on",
             help="Shows a talking-head avatar instead of just playing audio. "
                  "Reloads each reply (brief flicker) — no persistent avatar "
                  "yet, that needs a proper custom component. Outside "
                  "English/French the mouth timing is right but the shapes "
                  "are approximate.")
    # Available in EVERY language. It used to be hard-blocked outside
    # English/French because TalkingHead only ships viseme rules for 5
    # European languages -- but blocking it outright was the wrong call:
    # the word TIMING comes from Whisper and is accurate in any language, so
    # the mouth still opens and closes at the right moments; only the exact
    # shapes are approximate. An approximate talking face beats no face,
    # and it's your choice to make, not a thing to silently withhold.
    _avatar_choice = st.selectbox(
        "Face", list(avatar_widget.AVATARS.keys()),
        index=list(avatar_widget.AVATARS.keys()).index(
            avatar_widget.DEFAULT_AVATAR),
        key="avatar_choice")
    _bg_choice = st.selectbox(
        "Background", list(avatar_widget.BACKGROUNDS.keys()),
        index=list(avatar_widget.BACKGROUNDS.keys()).index(
            avatar_widget.DEFAULT_BACKGROUND),
        key="background_choice")
    avatar_widget.set_avatar(_avatar_choice)
    avatar_widget.set_background(_bg_choice)
    if _lang not in AVATAR_CAPABLE_LANGUAGES:
        st.caption(f"Note: lip-sync shapes are approximate in {_lang} — the "
                   f"mouth moves in time with the speech, but TalkingHead "
                   f"only has exact mouth-shape data for English and French.")

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


# Only three controls here now. Five was too many: at this width Streamlit
# truncated the two toggle labels to "Ha…" and "…", so the avatar switch was
# unidentifiable and looked like the avatar simply didn't work. Both toggles
# moved to the sidebar, beside the settings they actually control.
c1, c2, c3 = st.columns([2.4, 1, 1])
with c1:
    talk = st.button("🎤 Talk", width='stretch', type="primary",
                     help="Starts listening and stops on its own once you go "
                          "quiet — no need to hold anything or click again.")
with c2:
    if st.button("🗑 Clear", width='stretch'):
        st.session_state.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        save_history(st.session_state.history)   # else a reload brings the old history back
        st.rerun()
with c3:
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
                                      timing["wdurations"], speak=not already_spoken,
                                      sample_rate=current_sample_rate())
    else:
        avatar_widget.render_idle()

# Hands-free continues the loop by setting _auto_listen after a reply, so a
# turn can start without a click. It's re-checked against the live toggle on
# every rerun, so switching hands-free OFF stops the next turn immediately
# rather than after one more listen.
_auto = st.session_state.pop("_auto_listen", False) and st.session_state.get("hands_free")

if talk or _auto:
    if _auto:
        st.info("🎙 Listening — hands-free is on. Switch it off any time to stop.")
    with st.status("Listening…", expanded=True) as s:
        # No fixed recording window -- stops automatically once you stop
        # talking (voice-activity detection), not after a set N seconds
        # regardless of how long you actually spoke.
        audio = record_vad()
        heard = False
        if len(audio) == 0:
            s.update(label="Didn't catch that — try again", state="error")
        else:
            s.write("Transcribing…")
            text = transcribe(whisper_model, audio)
            if not text:
                s.update(label="Didn't catch that — try again", state="error")
            else:
                heard = True
                s.write(f'You said: "{text}"')
                s.write("Thinking + speaking (starts on the first sentence, "
                        "doesn't wait for the whole reply)…")
                _reply_and_speak(text)
                s.update(label="Done", state="complete")
    # Only continue the loop after a turn that actually happened -- otherwise
    # silence would spin listen->nothing->listen forever with no way to
    # interject.
    if heard and st.session_state.get("hands_free"):
        st.session_state["_auto_listen"] = True
    st.rerun()

typed = st.chat_input("...or type instead of talking")
if typed:
    with st.spinner("Thinking…"):
        _reply_and_speak(typed)
    st.rerun()
