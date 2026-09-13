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

- **Mouth-moving-when-idle bug** -- root cause: `_avatar_payload` never got
  cleared from session_state, so ANY Streamlit rerun (any click anywhere,
  not just a new reply) re-triggered `speakAudio()` on stale audio. Fixed
  with a turn-id check: only the first render of a given reply actually
  speaks, later reruns show the idle face.
- **TTS crash on certain text** -- Kokoro's G2P (misaki) can raise
  `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'` on
  some text instead of failing gracefully, which took the whole Streamlit
  app down mid-reply (caught live). Fixed: text sanitized before synthesis,
  and the call wrapped so a TTS failure just goes unspoken instead of
  crashing.
- **In-app restart button** -- a plain page refresh doesn't pick up edits to
  `krishna.py`/`avatar_widget.py` (Python caches them on import). Built a
  `supervisor.py` that keeps relaunching the Streamlit server whenever it
  exits, plus a "🔄 Restart" button that kills the current process on
  purpose -- the supervisor notices and starts a fresh one, which re-imports
  everything from disk.
- **Git repo created and pushed** -- https://github.com/somu-analyst/Avatar_AI_Agent,
  `main` branch, commit `1588266`.

## In progress / next up
- **Chatterbox TTS** -- installed (verified importable), not yet wired in
  to replace Kokoro. Quality reasoning: blind listening study showed 65.3%
  preferred Chatterbox Turbo over ElevenLabs.
- **Tool-calling for live data** -- researched and picked, not yet built:
  Open-Meteo (weather, no key), Frankfurter/Exchangerate.host (currency, no
  key), Free Dictionary API (no key), Nager.Date (holidays/calendar, no
  key), JokeAPI (no key), Currents API (news, free signup needed -- you'd
  need to grab a key).

## Open questions (waiting on you)
- **Gmail access** -- you asked for "all the daily used" APIs including
  Gmail; this is a different risk category than the public read-only APIs
  above (OAuth into your actual inbox, not a public lookup). Waiting on an
  explicit yes specifically for this, not folded into the general API ask.
- **"Autonomous bot" scope** -- asked whether you mean (a) Krishna looks
  things up when you ask it to (safe, what the tool-calling above is), or
  (b) something that acts without you prompting each time (the same
  always-listening-style autonomy category you were cautious about
  earlier). Not yet answered.
- **Realistic/photorealistic avatar** -- explicitly deferred ("we will do
  that realistic later"). Real finding from research: no existing hardware
  (laptop: AMD integrated graphics, no CUDA; Oracle VM: ARM CPU-only, no
  GPU at all) can run any of the real-time-capable options (LiveAvatar
  needs 80GB VRAM; MuseTalk/SadTalker-based systems assume NVIDIA GPU).
  Would need a rented cloud GPU (RunPod/Vast.ai, roughly $0.50-2 for a
  bounded test) -- real cost, needs your payment method and explicit go-
  ahead when you're ready.
- **Indian face** -- still needs you to create one at readyplayer.me
  (photo or manual customization) and send me the `.glb` -- I can't
  fabricate a correctly-rigged one myself.
- **Telugu / Indian-accent voice** -- AI4Bharat's IndicF5/Indic-Parler-TTS
  (Telugu) vs. MeloTTS (Indian-accented English) vs. Kokoro (neither) --
  still waiting on which matters more.

## Backlog (not being built now)
- **Open Interpreter on the laptop instead of the VM** -- 6 cores vs the
  VM's 2, likely fast enough. Would need its own sandboxing (Docker Desktop
  on Windows) since it'd be running where your real files live.
- **Cloud LLM behind the sandbox** -- solves the speed problem, reopens the
  "does data leave the machine" question.
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
