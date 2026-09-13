# Krishna — decisions & backlog

## Decided: no Open Interpreter (for now)
Tested Docker-sandboxed Open Interpreter on the Oracle VM (2026-09-12).
- **Isolation worked**: verified live, the container genuinely could not see
  `/home/ubuntu/nyse` or `/home/ubuntu/job-search-copilot` -- the safety
  concern was real and the fix held.
- **Capability didn't**: the VM's 2 CPU cores are too slow to process Open
  Interpreter's own ~1,900-token system/tool-definition prompt in reasonable
  time (~80-90s+ before any real generation starts) -- confirmed a hardware
  ceiling, not a model-quality issue, by testing a second model
  (qwen2.5-coder:3b) which was *worse*, not better.
- Decision: drop Open Interpreter as a component. Staying with the simpler
  custom build (Ollama + Whisper + Kokoro, no shell/file access) since it
  already runs at usable speed and carries none of the sandboxing risk.
- VM sandbox artifacts cleaned up (image + container removed).

## Done
- **Interrupt/barge-in support** (console) -- press Enter mid-reply, Krishna
  stops talking immediately. Verified live (cut off at 7.5s vs. the natural
  12.1s length). Can't yet interrupt during the brief per-sentence synthesis
  window before playback starts. Streamlit UI can't do this cleanly (its
  single-threaded rerun model doesn't support true concurrent interrupt).
- **Persistent memory across sessions** -- `memory.json`, loaded on start,
  saved after every turn, both console and Streamlit.
- **Safe notes capability** -- say "note ..." or "remember ...", appended to
  `notes.md` with a timestamp, no LLM involved in deciding what gets saved
  (plain string match + file append, on purpose).
- **Talking-head avatar** -- [TalkingHead (3D)](https://github.com/met4citizen/talkinghead),
  fully working end to end and verified live with screenshots: real 3D
  rendering, real Kokoro audio, real word-level timing from Whisper (already
  in the stack -- transcribes Krishna's OWN generated speech back to get
  lip-sync timing, avoiding their default Google-Cloud-TTS-based timing
  path entirely). Toggle in the Streamlit UI ("🧑 Avatar"). Two real bugs
  found and fixed by reading their actual source, not guessing: (1) audio
  must be int16 PCM wrapped in a real JS Array, not a raw Float32Array --
  their `Array.isArray()` check decides whether PCM conversion runs at all;
  (2) `pcmSampleRate` defaults to 22050, Kokoro outputs 24000, needed an
  explicit override or audio would play pitch/speed-distorted. Served via
  Streamlit's static-file serving (`.streamlit/config.toml`,
  `enableStaticServing`), files in `static/avatar/`.
  Honest limitation: `components.html()` makes a fresh iframe each call, so
  the avatar reloads (brief flicker) every reply rather than staying
  persistently mounted -- a smooth version needs a proper custom Streamlit
  component, not built.

## Backlog (not being built now)
- **Open Interpreter on the laptop instead of the VM** -- 6 cores vs the
  VM's 2, likely fast enough. Would need its own sandboxing (Docker Desktop
  on Windows) since it'd be running where your real files live.
- **Cloud LLM behind the sandbox** -- solves the speed problem, reopens the
  "does data leave the machine" question.
- **Telugu / Indian-accent voice** -- AI4Bharat's IndicF5 or Indic-Parler-TTS
  (Telugu-capable) vs. MeloTTS (Indian-accented English) vs. Kokoro (no
  Telugu, English/Hindi only) -- still waiting on which matters more.
- **Always-listening / wake-word mode** -- explicitly paused pending
  confirmation, given the autonomy/control concern raised earlier. Off by
  default if ever built; push-to-talk remains the norm until decided
  otherwise.
- **Telegram integration** -- explicitly NOT to be bolted onto the live
  trading bot without its own separate, careful conversation given real
  financial stakes. job-search-copilot has no Telegram bot yet either
  (separate, unbuilt backlog item there).
- **Persistent avatar (no reload-per-reply flicker)** -- needs a proper
  custom Streamlit component with iframe messaging, not components.html().
- **Interrupt support in the Streamlit UI** -- structurally hard given
  Streamlit's single-threaded rerun model; console-only for now.
