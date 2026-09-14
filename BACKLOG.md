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

## Done (this pass)
- **Voice picker** -- `st.selectbox` in the UI, 7 curated Kokoro voices
  (verified real by actually generating audio with each, not assumed from
  a naming convention): Heart/Bella/Nicole (US female), Adam/Michael (US
  male), Emma (British female), George (British male).

## In progress / next up
- **Chatterbox TTS** -- installed (verified importable), not yet wired in
  to replace Kokoro. Quality reasoning: blind listening study showed 65.3%
  preferred Chatterbox Turbo over ElevenLabs.
- **Tool-calling for live data** -- weather/currency/dictionary/jokes DONE
  and live (Open-Meteo, Frankfurter, Free Dictionary API, JokeAPI, all
  keyless). **Sports DONE, 2026-09-13**: `get_sports_score()` via
  TheSportsDB's own published free test key ("123") -- genuinely keyless,
  no signup, verified live (real Lakers/Warriors score came back, not a
  stub). **Stocks built but needs a key, 2026-09-13**: `get_stock_price()`
  via Finnhub -- researched first (per your "search git/reddit" standing
  ask) and confirmed no reliable keyless real-time stock source exists
  anymore: Stooq's old keyless CSV endpoint is dead (tested live, 404),
  Yahoo Finance's unofficial endpoints are fragile/rate-limited (matches
  what job-search-copilot/NYSE_DATA already found). Gracefully explains
  what's missing rather than crashing if `FINNHUB_API_KEY` isn't set --
  free signup at finnhub.io, no cost, no credit card, whenever you want
  live stock prices working.
  - **Currents API (news)** still needs a key from you, unchanged from
    before.
- **Avatar picker** -- choose between multiple avatar faces. Only one
  avatar (brunette.glb) exists right now; needs more correctly-rigged
  avatars (same ARKit+Oculus+Mixamo requirement as before) before a picker
  means anything.
- **Background picker** -- currently a fixed dark color behind the avatar;
  needs a background-image/color option in avatar_widget.py.
- **Multi-language support -- DONE for 7 languages, 2026-09-13.** English
  (US/UK), French, Hindi, Spanish, Italian, Portuguese (Brazil) all verified
  live end-to-end (real synthesized audio, non-empty, through krishna.py
  itself, not a mocked test). Scope decision (asked, you picked): voice-only
  everywhere; the avatar only renders for English/French, since
  TalkingHead's lip-sync viseme modules (checked against the real repo)
  only exist for English/French/German/Finnish/Lithuanian -- every other
  language can speak, it just can't drive correct mouth shapes, so the UI
  hides the avatar toggle for those rather than showing a mismatched one.
  Two real bugs found and fixed, not assumed:
  1. Kokoro's non-English voices need espeak-ng, which misaki (Kokoro's
     G2P) only looks for at the standard ELEVATED-install path
     (`C:\Program Files\eSpeak NG\`). Installed espeak-ng WITHOUT admin
     rights via `msiexec /a` (an "administrative install" that just
     extracts files, a legitimate MSI feature, not a workaround), copied
     into the project at `vendor/espeak-ng/` (gitignored, ~25MB). Calling
     misaki's hardcoded lookup with the wrong path didn't just fail
     quietly -- it crashed with a native access violation. Fixed by setting
     `EspeakWrapper.set_library()`/`set_data_path()` directly in
     krishna.py, before `kokoro` is ever imported, so misaki's own broken
     check gets skipped entirely.
  2. Holding more than ~4 Kokoro `KPipeline` objects alive simultaneously
     in one process corrupts it (verified live: a trivially small ~256KB
     allocation starts failing with "not enough memory" -- a native-state
     corruption symptom, not a real shortage). `_get_tts_pipeline()` now
     evicts every other language's pipeline before building a new one --
     costs a few seconds to reload if you switch back to an earlier
     language, a real but minor and far safer trade.
  - **Mandarin and Japanese deliberately left out.** Our installed
    `kokoro==0.2.2` is ~15 minor versions behind latest (0.7.16) and never
    had these in its own language map at all -- confirmed by reading its
    source, not assumed. No env var or `misaki[zh]`/`misaki[ja]` extra
    fixes a package that's simply missing the feature. Japanese's own extra
    additionally needs compiling `mojimoji` from source, which needs
    Microsoft's C++ Build Tools (a real multi-GB system install). Real fix
    is upgrading `kokoro` itself -- a big enough jump to deserve its own
    tested pass, not bundled into this one, since it risks the already-
    verified English/avatar path working today.

## In progress — photorealistic avatar (SadTalker), 2026-09-13
Goal: real GPU-based talking-head video generation, for live speaking first
(no true real-time expected -- rendering takes minutes) and the backlogged
YouTube pipeline second. Explicitly want fallback redundancy across free GPU
sources, not one path.
- **Colab (camenduru/SadTalker-colab fork)**: hit a real, structural problem,
  not a one-off bug -- `requirements.txt` pins 2022-era versions
  (numpy==1.23.4, scikit-image==0.19.3, face_alignment==1.3.5, etc.) that
  have no prebuilt wheels for Colab's now-default Python 3.12, so pip tries
  to build them from source and aborts the WHOLE install the moment one
  fails (egg_info error) -- everything listed after it in the file (kornia,
  facexlib, ...) silently never installs. Explains both the kornia and
  facexlib `ModuleNotFoundError`s as the same root cause, not two bugs.
  Fix given: drop the pins, `pip install` the package names bare so pip
  picks 3.12-compatible wheels. Not yet confirmed working by you.
- **Kaggle**: built and ran an actual kernel end to end --
  `srinivasaraosomu/sadtalker-avatar-test` (private). Clones SadTalker,
  installs `requirements.txt` AS PINNED (Kaggle's Python is older, matches
  the 2022 pins natively -- no source-build problem here), downloads all
  checkpoints (~1.7GB, confirmed on disk: both safetensors + both mapping
  models), runs `inference.py` on the bundled example image/audio. Kernel
  finished with status COMPLETE, but the run log came back 0 bytes on the
  first pull -- looked like the same Windows-console UTF-8 encoding crash
  as the Kokoro TTS bug (tqdm/aria2c progress output has non-ASCII chars),
  not a Kaggle-side failure. Re-pulling with `PYTHONUTF8=1` /
  `PYTHONIOENCODING=utf-8` to get the real log and confirm whether
  `inference.py` actually produced a result video or died partway --
  **unresolved as of this entry**, no `results/*.mp4` seen in the output
  yet.
- **Hugging Face Spaces**: found a working alternative (John6666/SadTalker,
  confirmed live) -- not yet tried.

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
