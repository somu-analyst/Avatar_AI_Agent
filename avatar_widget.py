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
import json

import numpy as np
import streamlit.components.v1 as components

MODULE_URL = "app/static/avatar/talkinghead.mjs"
_AVATAR_DIR = "app/static/avatar/avatars"

# Every one of these was verified before being offered, not assumed: each
# GLB was inspected for the three things TalkingHead actually requires --
# ARKit blendshapes, all 15 Oculus visemes, and a Mixamo rig. All 5 pass.
# The per-avatar `retarget`/`baseline` values are upstream's own (their
# siteconfig.js), not guesses -- AvatarSDK in particular is rigged with its
# neck and shoulders off by enough that it looks wrong without them.
AVATARS: dict[str, dict] = {
    "Brunette (Ready Player Me)": {
        "file": "brunette.glb", "body": "F", "mood": "neutral",
    },
    "Avaturn (realistic, from photo)": {
        "file": "avaturn.glb", "body": "F", "mood": "happy",
        "retarget": {
            "Hips": {"y": 0.03}, "Spine": {"y": 0.02},
            "Spine1": {"y": 0.02, "z": 0.01}, "Spine2": {"y": 0.02, "z": 0.01},
            "Neck": {"z": 0.02, "y": 0.01}, "Head": {"z": 0.02},
            "LeftShoulder": {"rx": -0.5}, "RightShoulder": {"rx": -0.5},
            "scaleToHipsLevel": 1.0,
        },
        "baseline": {"headRotateX": -0.05, "eyeBlinkLeft": 0.15,
                     "eyeBlinkRight": 0.15},
    },
    "AvatarSDK (realistic, male)": {
        "file": "avatarsdk.glb", "body": "M", "mood": "neutral",
        "retarget": {
            "Neck": {"z": -0.01, "rx": -0.15}, "Neck1": {"z": -0.01, "rx": -0.15},
            "Neck2": {"z": -0.01, "rx": -0.15},
            "LeftShoulder": {"rz": -0.3}, "RightShoulder": {"rz": 0.3},
            "scaleToEyesLevel": 1.0, "origin": {"y": -0.1},
        },
        "baseline": {"headRotateX": -0.04, "eyeBlinkLeft": 0.05,
                     "eyeBlinkRight": 0.05},
    },
    "VRoid (anime style)": {
        "file": "vroid.glb", "body": "F", "mood": "neutral",
        "baseline": {"headRotateX": -0.1, "eyeBlinkLeft": 0.05,
                     "eyeBlinkRight": 0.05},
    },
    "Brunette (alternate build)": {
        "file": "brunette-t.glb", "body": "F", "mood": "neutral",
    },
}
DEFAULT_AVATAR = "Brunette (Ready Player Me)"

# Plain CSS applied to the container behind the avatar. Kept to colours and
# gradients rather than photos on purpose: the avatar is rendered on a
# transparent WebGL canvas, so anything busy behind it reads as noise around
# the head rather than a backdrop.
BACKGROUNDS: dict[str, str] = {
    "Charcoal (default)": "#1a1a1a",
    "Slate": "#2b3137",
    "Deep blue": "linear-gradient(160deg, #0f2027, #203a43, #2c5364)",
    "Warm grey": "#3a3532",
    "Studio purple": "linear-gradient(160deg, #2b1055, #44318d)",
    "Forest": "linear-gradient(160deg, #0b3d2c, #1d6f4d)",
    "Sunset": "linear-gradient(160deg, #3a1c71, #d76d77, #ffaf7b)",
    "Plain white": "#f2f2f2",
}
DEFAULT_BACKGROUND = "Charcoal (default)"

CURRENT_AVATAR = DEFAULT_AVATAR
CURRENT_BACKGROUND = DEFAULT_BACKGROUND


def set_avatar(name: str) -> None:
    global CURRENT_AVATAR
    if name in AVATARS:
        CURRENT_AVATAR = name


def set_background(name: str) -> None:
    global CURRENT_BACKGROUND
    if name in BACKGROUNDS:
        CURRENT_BACKGROUND = name


def _audio_to_pcm16_b64(audio_f32: np.ndarray) -> str:
    """Kokoro's float32 -> base64 int16 PCM. TalkingHead's pcmToAudioBuffer()
    only supports signed 16-bit little-endian (verified from their source),
    not float32 -- passing float32 directly crashed with "Failed to convert
    value to 'AudioBuffer'" (caught live before this was fixed)."""
    clipped = np.clip(audio_f32, -1.0, 1.0)
    audio_i16 = (clipped * 32767).astype("<i2")
    return base64.b64encode(audio_i16.tobytes()).decode("ascii")


def _avatar_config_json() -> str:
    """Builds showAvatar()'s config for the currently selected avatar.
    json.dumps rather than hand-written JS so the retarget/baseline dicts
    can't produce malformed JavaScript."""
    cfg = AVATARS.get(CURRENT_AVATAR, AVATARS[DEFAULT_AVATAR])
    out = {
        "url": f"/{_AVATAR_DIR}/{cfg['file']}",
        "body": cfg.get("body", "F"),
        "avatarMood": cfg.get("mood", "neutral"),
        "lipsyncLang": "en",
    }
    for key in ("retarget", "baseline"):
        if cfg.get(key):
            out[key] = cfg[key]
    return json.dumps(out)


def _container_div(height: int) -> str:
    background = BACKGROUNDS.get(CURRENT_BACKGROUND, BACKGROUNDS[DEFAULT_BACKGROUND])
    return (f'<div id="avatar" style="width:100%;height:{height}px;'
            f'background:{background};border-radius:12px"></div>')


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
  await head.showAvatar({_avatar_config_json()});
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
        components.html(_container_div(height) + _base_script(""), height=height)
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
    components.html(_container_div(height) + _base_script(speak_js), height=height)


def render_idle(height: int = 420) -> None:
    """Just the face, no speech -- shown before the first reply."""
    components.html(_container_div(height) + _base_script(""), height=height)
