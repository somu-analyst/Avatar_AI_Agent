"""Krishna's talking-head avatar, embedded in Streamlit via components.html().

Serves the actual TalkingHead library from static/avatar/ (Streamlit's
static-file serving, enabled in .streamlit/config.toml) -- verified working
end to end in avatar/test.html before this file existed: real 3D rendering,
real Kokoro audio, real Whisper-derived word timing, zero cloud dependency.

Honest limitation: components.html() creates a fresh iframe every time it's
called, so the avatar reloads (a brief flicker) each time it speaks, rather
than staying persistently mounted across turns. A smooth, no-reload version
would need a proper custom Streamlit component with iframe messaging --
real, separate engineering, not built here.
"""
from __future__ import annotations
import base64

import numpy as np
import streamlit.components.v1 as components

AVATAR_URL = "app/static/avatar/avatars/brunette.glb"
MODULE_URL = "app/static/avatar/talkinghead.mjs"


def _audio_to_pcm16_b64(audio_f32: np.ndarray) -> str:
    """Kokoro's float32 -> base64 int16 PCM. TalkingHead's pcmToAudioBuffer()
    only supports signed 16-bit little-endian (verified from their source),
    not float32 -- passing float32 directly crashed with "Failed to convert
    value to 'AudioBuffer'" (caught live before this was fixed)."""
    clipped = np.clip(audio_f32, -1.0, 1.0)
    audio_i16 = (clipped * 32767).astype("<i2")
    return base64.b64encode(audio_i16.tobytes()).decode("ascii")


def _base_script(speak_js: str) -> str:
    return f"""
<script type="importmap">
{{
  "imports": {{
    "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
  }}
}}
</script>
<script type="module">
  const {{ TalkingHead }} = await import('/{MODULE_URL}');
  const container = document.getElementById('avatar');
  const head = new TalkingHead(container, {{
    ttsEndpoint: "",
    lipsyncModules: ["en"],
    cameraView: "upper",
    pcmSampleRate: 24000
  }});
  await head.showAvatar({{ url: '/{AVATAR_URL}', body: 'F', lipsyncLang: 'en' }});
  {speak_js}
</script>
"""


def render_speaking(audio_f32: np.ndarray, words: list[str], wtimes: list[int],
                    wdurations: list[int], height: int = 420, speak: bool = True) -> None:
    """Loads the avatar. Only calls speakAudio() when speak=True -- when
    False, this renders the SAME reply's face on a page rerun that isn't a
    genuinely new turn (Streamlit reruns the whole script on ANY widget
    interaction, not just a new reply) without re-triggering the mouth
    animation on stale audio. That mismatch -- speak() firing on every
    unrelated rerun -- was reported live as "mouth moving when no one is
    speaking"; the caller (app.py) is responsible for only passing
    speak=True once per new reply via a turn-id check."""
    if not speak:
        components.html(
            f'<div id="avatar" style="width:100%;height:{height}px;'
            f'background:#1a1a1a;border-radius:12px"></div>' + _base_script(""),
            height=height)
        return
    audio_b64 = _audio_to_pcm16_b64(audio_f32)
    speak_js = (
        f'const bytes = Uint8Array.from(atob("{audio_b64}"), c => c.charCodeAt(0));\n'
        f'  head.speakAudio(\n'
        f'    {{ audio: [bytes.buffer], words: {words}, wtimes: {wtimes}, '
        f'wdurations: {wdurations} }},\n'
        f'    {{}}, () => {{}}\n'
        f'  );'
    )
    components.html(
        f'<div id="avatar" style="width:100%;height:{height}px;'
        f'background:#1a1a1a;border-radius:12px"></div>' + _base_script(speak_js),
        height=height)


def render_idle(height: int = 420) -> None:
    """Just the face, no speech -- shown before the first reply."""
    components.html(
        f'<div id="avatar" style="width:100%;height:{height}px;'
        f'background:#1a1a1a;border-radius:12px"></div>' + _base_script(""),
        height=height)
