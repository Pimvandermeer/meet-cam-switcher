#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websocket-client>=1.6", "numpy"]
# ///
"""Linux test rig for meet_cam_switcher without a Focusrite.

Builds a fake Focusrite: a PipeWire null sink "fake_focusrite" whose left channel is the
laptop mic (you = presenter) and whose right channel gets generated audience babble.
OBS captures its monitor as Mic/Aux, exactly like the Focusrite on the Windows box.

    rig.py audio-up | audio-down     fake Focusrite on/off
    rig.py levels [-s 5]             per-channel levels of the fake Focusrite (no OBS needed)
    rig.py audience                  play an audience question on channel 2
    rig.py meeting                   play a scripted 34 s meeting on both channels
    rig.py obs-setup                 create scenes, cameras and labels in a running OBS
    rig.py shot SOURCE FILE.png      screenshot of an OBS source, to check a camera works
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from meet_cam_switcher import ObsConnection, SetupError  # noqa: E402

SINK = "fake_focusrite"
LOOPBACK = "fake-focusrite-presenter"
SR = 48000
CLIPS = HERE / "clips"


def sh(*cmd: str, check: bool = True) -> str:
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout.strip()


def module_id() -> str | None:
    for line in sh("pactl", "list", "short", "modules").splitlines():
        if f"sink_name={SINK}" in line:
            return line.split()[0]
    return None


def audio_up(args: argparse.Namespace) -> None:
    mic = args.mic or sh("pactl", "get-default-source")
    if mic.startswith(SINK):
        sys.exit("default source is the fake Focusrite itself; pass --mic <laptop mic source name>")
    default_sink = sh("pactl", "get-default-sink")
    if not module_id():
        sh("pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
           "sink_properties=device.description=Fake-Focusrite", "channel_map=front-left,front-right")
    if sh("pactl", "get-default-sink") != default_sink:  # never steal the user's speakers
        sh("pactl", "set-default-sink", default_sink)
    if not sh("pgrep", "-f", f"pw-loopback -n {LOOPBACK}", check=False):
        # Mono + dont-remix, otherwise PipeWire copies the mic into the right channel too.
        props = "audio.channels=1 audio.position=[ FL ] stream.dont-remix=true"
        subprocess.Popen(["pw-loopback", "-n", LOOPBACK, "-C", mic, "-P", SINK,
                          f"--capture-props={props}", f"--playback-props={props}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"fake Focusrite up: ch1 = {mic}, ch2 = 'rig.py audience'; OBS device: {SINK}.monitor")


def audio_down(_: argparse.Namespace) -> None:
    sh("pkill", "-f", f"pw-loopback -n {LOOPBACK}", check=False)
    if mid := module_id():
        sh("pactl", "unload-module", mid)
    print("fake Focusrite removed")


def babble(seconds: float, f0: float, rng: np.random.Generator) -> np.ndarray:
    """Speech-shaped noise: syllables of 120-300 ms with short gaps and the odd pause."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    phase = 2 * np.pi * np.cumsum(f0 * (1 + 0.06 * np.sin(2 * np.pi * 3 * t))) / SR
    carrier = 0.5 * np.sign(np.sin(phase)) + rng.normal(0, 0.3, n)
    env = np.zeros(n)
    i = 0
    while i < n:
        syl = int(rng.uniform(0.12, 0.30) * SR)
        seg = np.hanning(syl)[: n - i]
        env[i : i + len(seg)] = seg
        gap = rng.uniform(0.3, 0.5) if rng.random() < 0.15 else rng.uniform(0.03, 0.12)
        i += syl + int(gap * SR)
    x = carrier * env
    return x / np.max(np.abs(x))


def write_wav(path: Path, left: np.ndarray, right: np.ndarray) -> None:
    path.parent.mkdir(exist_ok=True)
    pcm = (np.clip(np.stack([left, right], axis=1), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def db(x: float) -> float:
    return 10 ** (x / 20)


def make_clips() -> None:
    rng = np.random.default_rng(7)
    q = babble(4.0, 210, rng) * db(-10)
    pad = np.zeros(int(0.5 * SR))
    write_wav(CLIPS / "audience.wav", np.zeros(len(q) + 2 * len(pad)), np.concatenate([pad, q, pad]))

    n = int(34 * SR)
    left, right = rng.normal(0, db(-75), n), rng.normal(0, db(-75), n)

    def put(start: float, sig: np.ndarray, own: np.ndarray, other: np.ndarray, own_db: float, bleed_db: float) -> None:
        a = int(start * SR)
        own[a : a + len(sig)] += sig * db(own_db)
        other[a : a + len(sig)] += sig * db(bleed_db)  # the other mic hears it too, quieter

    put(0.0, babble(6.0, 130, rng), left, right, -12, -32)                  # presenter talks
    put(6.0, rng.normal(0, 0.4, int(0.25 * SR)), right, left, -6, -30)      # audience cough: ignored
    put(6.4, babble(3.6, 130, rng), left, right, -12, -32)                  # presenter continues
    put(10.0, babble(5.0, 210, rng), right, left, -12, -30)                 # audience question
    put(15.0, babble(5.0, 130, rng), left, right, -12, -32)                 # presenter answers
    put(20.0, babble(4.0, 130, rng), left, right, -14, -32)                 # both talk at once
    put(20.0, babble(4.0, 210, rng), right, left, -14, -30)
    write_wav(CLIPS / "meeting.wav", left, right)                           # 24-34 s: silence


def play(name: str) -> None:
    make_clips()
    print(f"playing {name}.wav into {SINK}")
    subprocess.run(["pw-play", "--target", SINK, str(CLIPS / f"{name}.wav")], check=True)


def levels(args: argparse.Namespace) -> None:
    """Record the fake Focusrite and print peak dBFS per channel every 0.5 s."""
    cmd = ["parecord", "--raw", "--format=s16le", f"--rate={SR}", "--channels=2", f"--device={SINK}.monitor"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        raw = proc.stdout.read(int(args.seconds * SR) * 4)
    finally:
        proc.kill()
    x = np.frombuffer(raw, "<i2").reshape(-1, 2) / 32768
    block = SR // 2
    for i in range(0, len(x) - block + 1, block):
        peak = np.max(np.abs(x[i : i + block]), axis=0)
        ch1, ch2 = (20 * np.log10(max(p, 1e-5)) for p in peak)
        print(f"t={i / SR:4.1f}s  ch1 (presenter) {ch1:6.1f} dB   ch2 (audience) {ch2:6.1f} dB")


def connect(args: argparse.Namespace) -> ObsConnection:
    return ObsConnection(args.host, args.port, args.password, 0)


def try_request(obs: ObsConnection, req: str, data: dict) -> dict | None:
    try:
        return obs.request(req, data)
    except SetupError as e:
        if "already exists" not in str(e):
            raise
        return None


def obs_setup(args: argparse.Namespace) -> None:
    obs = connect(args)
    W, H = 1280, 720
    obs.request("SetVideoSettings", {"baseWidth": W, "baseHeight": H, "outputWidth": W, "outputHeight": H,
                                     "fpsNumerator": 30, "fpsDenominator": 1})
    special = obs.request("GetSpecialInputs")
    mic = special.get("mic1")
    if not mic:
        sys.exit("OBS has no global Mic/Aux input; set one in Settings > Audio, then rerun")
    obs.request("SetInputSettings", {"inputName": mic, "inputSettings": {"device_id": f"{SINK}.monitor"}})
    obs.request("SetInputMute", {"inputName": mic, "inputMuted": False})
    if desk := special.get("desktop1"):
        obs.request("SetInputMute", {"inputName": desk, "inputMuted": True})

    scenes = ["Presenter", "Audience", "Both", "Slides"]
    for s in scenes:
        try_request(obs, "CreateScene", {"sceneName": s})

    def add(scene: str, name: str, kind: str, settings: dict) -> int:
        """Create the input once, then reuse it in other scenes so a camera is opened only once."""
        created = try_request(obs, "CreateInput", {"sceneName": scene, "inputName": name,
                                                    "inputKind": kind, "inputSettings": settings})
        if created:
            return created["sceneItemId"]
        items = obs.request("GetSceneItemList", {"sceneName": scene})["sceneItems"]
        for it in items:
            if it["sourceName"] == name:
                return it["sceneItemId"]
        return obs.request("CreateSceneItem", {"sceneName": scene, "sourceName": name})["sceneItemId"]

    def place(scene: str, item: int, x: float, y: float, w: float, h: float) -> None:
        obs.request("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item, "sceneItemTransform": {
            "positionX": x, "positionY": y, "alignment": 5, "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsWidth": w, "boundsHeight": h, "boundsAlignment": 0}})

    def label(scene: str, text: str) -> None:
        item = add(scene, f"label {text}", "text_ft2_source_v2", {
            "text": text, "font": {"face": "Sans", "size": 64, "style": "Bold", "flags": 1},
            "outline": True, "drop_shadow": True})
        obs.request("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item, "sceneItemTransform": {
            "positionX": 32, "positionY": H - 110, "alignment": 5}})

    def camera(scene: str, name: str, device: str | None, color: int) -> int:
        if device:
            return add(scene, name, "v4l2_input", {"device_id": device})
        return add(scene, name, "color_source_v3", {"color": color, "width": W, "height": H})

    for scene, cams in {
        "Presenter": [("Presenter cam", args.presenter_device, 0xFF5A3A1E, (0, 0, W, H))],
        "Audience": [("Audience cam", args.audience_device, 0xFF1E5A2E, (0, 0, W, H))],
        "Both": [("Presenter cam", args.presenter_device, 0xFF5A3A1E, (0, 0, W / 2, H)),
                 ("Audience cam", args.audience_device, 0xFF1E5A2E, (W / 2, 0, W / 2, H))],
    }.items():
        for name, device, color, (x, y, w, h) in cams:
            place(scene, camera(scene, name, device, color), x, y, w, h)
        label(scene, scene.upper())
    place("Slides", add("Slides", "slides bg", "color_source_v3", {"color": 0xFF202020, "width": W, "height": H}),
          0, 0, W, H)
    label("Slides", "SLIDES (auto paused)")

    obs.request("SetCurrentProgramScene", {"sceneName": "Presenter"})
    if "Scene" in [s["sceneName"] for s in obs.request("GetSceneList")["scenes"]]:
        obs.request("RemoveScene", {"sceneName": "Scene"})  # OBS's empty default scene
    obs.close()
    print(f"OBS ready: scenes {scenes}, Mic/Aux = {SINK}.monitor, "
          f"presenter cam = {args.presenter_device or 'placeholder'}, audience cam = {args.audience_device or 'placeholder'}")


def shot(args: argparse.Namespace) -> None:
    obs = connect(args)
    img = obs.request("GetSourceScreenshot", {"sourceName": args.source, "imageFormat": "png", "imageWidth": 640})
    obs.close()
    Path(args.file).write_bytes(base64.b64decode(img["imageData"].split(",", 1)[1]))
    print(args.file)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=4455)
    ap.add_argument("--password", default=os.environ.get("OBS_PASSWORD", "meettest"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("audio-up")
    up.add_argument("--mic", help="PipeWire source for channel 1 (default: current default source)")
    sub.add_parser("audio-down")
    lv = sub.add_parser("levels")
    lv.add_argument("-s", "--seconds", type=float, default=5)
    sub.add_parser("audience")
    sub.add_parser("meeting")
    st = sub.add_parser("obs-setup")
    st.add_argument("--presenter-device", default="/dev/video0", help="'' for a coloured placeholder")
    st.add_argument("--audience-device", default="", help="e.g. /dev/video2; '' for a coloured placeholder")
    sh_ = sub.add_parser("shot")
    sh_.add_argument("source")
    sh_.add_argument("file")
    args = ap.parse_args()
    {"audio-up": audio_up, "audio-down": audio_down, "levels": levels, "audience": lambda a: play("audience"),
     "meeting": lambda a: play("meeting"), "obs-setup": obs_setup, "shot": shot}[args.cmd](args)


if __name__ == "__main__":
    main()
