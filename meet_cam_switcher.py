#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websocket-client>=1.6"]
# ///
"""Camera follows the microphone: switch OBS scenes based on which mic is talking.

OBS sends the program output to Google Meet as "OBS Virtual Camera". Mic levels come from
OBS's own audio meters over obs-websocket, so the Focusrite is never opened a second time
and Meet keeps using it directly as its microphone.

    uv run meet_cam_switcher.py --list     # show OBS inputs and scenes
    uv run meet_cam_switcher.py --meter    # live levels, for tuning --gate / --margin
    uv run meet_cam_switcher.py            # switch automatically
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
import json
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass

import websocket

SUB_SCENES = 1 << 2
SUB_INPUT_VOLUME_METERS = 1 << 16
FLOOR_DB = -100.0


class SetupError(Exception):
    """OBS is reachable but not configured the way the arguments say."""


class Disconnected(Exception):
    """Transient: retry after reconnecting."""


class NotReady(Disconnected):
    """OBS accepts connections a few seconds before it can answer requests."""


OBS_NOT_READY = 207


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def to_db(mul: float) -> float:
    return 20 * math.log10(mul) if mul > 1e-5 else FLOOR_DB


@dataclass(frozen=True)
class MicRef:
    input_name: str
    channel: int  # 0-based

    @classmethod
    def parse(cls, text: str) -> MicRef:
        name, sep, ch = text.rpartition(":")
        if sep and ch.isdigit() and int(ch) >= 1:
            return cls(name, int(ch) - 1)
        return cls(text, 0)

    def __str__(self) -> str:
        return f"{self.input_name}:{self.channel + 1}"


@dataclass
class Tuning:
    gate_db: float
    margin_db: float
    window_s: float
    hold_s: float
    idle_s: float


class Director:
    """Turns two mic levels into the role whose camera should be live.

    Every ~50 ms meter block is labelled: a mic dominates when it is above the gate AND louder
    than the other mic by a margin (each mic also hears the other person). Speech dominates
    roughly half to most blocks despite the gaps between words; a cough or a dropped pen fills
    only a few. So the mic that dominates most of the last window_s gets the camera, provided
    that is at least SPEAKER_SHARE. After any switch the picture stays put for hold_s, so
    one-word replies don't make it flap.
    """

    SPEAKER_SHARE = 0.35
    BOTH_SHARE = 0.25  # both mics talking at once, not just taking turns

    def __init__(self, tuning: Tuning, has_both: bool):
        self.t = tuning
        self.has_both = has_both
        self.history: deque[tuple[float, str | None, float, float]] = deque()  # dt, label, p_db, a_db
        self.span_s = 0.0
        self.candidate: str | None = None
        self.quiet_s = 0.0
        self.since_switch_s = math.inf

    def hold(self) -> None:
        """Someone switched by hand: respect it for at least hold_s."""
        self.since_switch_s = 0.0

    def levels(self) -> tuple[float, float]:
        """Average level per mic over the window, for the log."""
        def avg(i: int) -> float:
            power = sum(d * 10 ** (lv[i] / 10) for d, _, *lv in self.history)
            return 10 * math.log10(power / self.span_s) if power > 0 else FLOOR_DB
        return avg(0), avg(1)

    def update(self, presenter_db: float, audience_db: float, dt: float, live: str | None) -> str | None:
        t = self.t
        p, a = presenter_db, audience_db
        p_on, a_on = p > t.gate_db, a > t.gate_db
        if p_on and p - a >= t.margin_db:
            label = "presenter"
        elif a_on and a - p >= t.margin_db:
            label = "audience"
        elif p_on and a_on:
            label = "both"
        else:
            label = None
        self.history.append((dt, label, p, a))
        self.span_s += dt
        while self.span_s - self.history[0][0] >= t.window_s:
            self.span_s -= self.history.popleft()[0]

        span = max(self.span_s, t.window_s)  # no snap decisions right after start-up
        share = {k: sum(d for d, lbl, *_ in self.history if lbl == k) / span for k in ("presenter", "audience", "both")}
        speaker = max(("presenter", "audience"), key=share.get)
        if self.has_both and share["both"] >= self.BOTH_SHARE:
            self.candidate = "both"
        elif share[speaker] >= self.SPEAKER_SHARE:
            self.candidate = speaker
        else:
            self.candidate = None
        self.quiet_s = 0.0 if (p_on or a_on) else self.quiet_s + dt
        self.since_switch_s += dt

        target = self.candidate
        if target is None and t.idle_s > 0 and self.quiet_s >= t.idle_s:
            target = "both" if self.has_both else "presenter"
        # live is None while a scene we don't manage is on air (slides, manual override).
        if live is None or target is None or target == live or self.since_switch_s < t.hold_s:
            return None
        self.since_switch_s = 0.0
        return target


def _b64sha(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


class ObsConnection:
    """Minimal obs-websocket v5 client: one socket, requests and events interleaved."""

    def __init__(self, host: str, port: int, password: str, subscriptions: int):
        self.ws = websocket.create_connection(f"ws://{host}:{port}", timeout=5)
        self.ids = itertools.count(1)
        self.backlog: list[dict] = []
        hello = self._recv()
        identify: dict = {"rpcVersion": 1, "eventSubscriptions": subscriptions}
        auth = hello["d"].get("authentication")
        if auth:
            if not password:
                raise SetupError("OBS WebSocket needs a password: pass --password or set OBS_PASSWORD")
            identify["authentication"] = _b64sha(_b64sha(password + auth["salt"]) + auth["challenge"])
        self._send(1, identify)
        try:
            if self._recv()["op"] != 2:
                raise SetupError("unexpected reply to Identify from OBS")
        except Disconnected:
            raise SetupError("OBS refused the login: wrong WebSocket password?") from None

    def close(self) -> None:
        self.ws.close()

    def _send(self, op: int, d: dict) -> None:
        self.ws.send(json.dumps({"op": op, "d": d}))

    def _recv(self) -> dict:
        raw = self.ws.recv()
        if not raw:
            raise Disconnected("OBS closed the connection")
        return json.loads(raw)

    def request(self, request_type: str, data: dict | None = None) -> dict:
        rid = str(next(self.ids))
        self._send(6, {"requestType": request_type, "requestId": rid, "requestData": data or {}})
        while True:
            msg = self._recv()
            if msg["op"] == 7 and msg["d"]["requestId"] == rid:
                status = msg["d"]["requestStatus"]
                if status["code"] == OBS_NOT_READY:
                    raise NotReady("OBS is still starting")
                if not status["result"]:
                    raise SetupError(f"{request_type} failed: {status.get('comment') or status['code']}")
                return msg["d"].get("responseData", {})
            if msg["op"] == 5:
                self.backlog.append(msg["d"])

    def next_event(self) -> dict | None:
        if self.backlog:
            return self.backlog.pop(0)
        try:
            msg = self._recv()
        except websocket.WebSocketTimeoutException:
            return None
        return msg["d"] if msg["op"] == 5 else None


def bar(db: float, width: int = 24) -> str:
    filled = round(width * min(max((db + 60) / 60, 0), 1))
    return "#" * filled + "-" * (width - filled)


def session(obs: ObsConnection, args: argparse.Namespace) -> None:
    inputs = [i["inputName"] for i in obs.request("GetInputList")["inputs"]]
    scenes = [s["sceneName"] for s in obs.request("GetSceneList")["scenes"]]
    if args.list:
        print("OBS inputs:", *(f"  {n}" for n in inputs), "OBS scenes:", *(f"  {n}" for n in scenes), sep="\n")
        raise SystemExit(0)

    mics = (args.presenter_mic, args.audience_mic)
    for mic in mics:
        if mic.input_name not in inputs:
            raise SetupError(f"no OBS input named {mic.input_name!r}; inputs are: {inputs}")
    role_scene = {"presenter": args.presenter_scene, "audience": args.audience_scene}
    if args.both_scene:
        if args.both_scene in scenes:
            role_scene["both"] = args.both_scene
        else:
            log(f"no scene {args.both_scene!r}: silence falls back to {args.presenter_scene!r}")
    if not args.meter:
        missing = [s for s in role_scene.values() if s not in scenes]
        if missing:
            raise SetupError(f"scene(s) {missing} not found in OBS; scenes are: {scenes}")
    scene_role = {scene: role for role, scene in role_scene.items()}

    data = obs.request("GetCurrentProgramScene")
    live_scene = data.get("sceneName") or data.get("currentProgramSceneName")
    switched_to = live_scene
    director = Director(args.tuning, has_both="both" in role_scene)
    warned: set[MicRef] = set()
    last = time.monotonic()
    ticks = 0
    if not args.meter:
        state = "auto" if live_scene in scene_role else "paused until a managed scene is live"
        log(f"connected; live scene {live_scene!r} ({state}); presenter={mics[0]} audience={mics[1]}")

    silent: set[str] = set()

    def mic_db(levels: dict, mic: MicRef) -> float:
        chans = levels.get(mic.input_name)
        # OBS only meters inputs that are active, i.e. global audio devices or sources in the live scene.
        if mic.input_name not in levels and mic.input_name not in silent:
            silent.add(mic.input_name)
            log(f"no level from {mic.input_name!r} while {live_scene!r} is live: make it a global audio "
                "device (Settings > Audio > Mic/Auxiliary Audio) or add it to every scene")
        elif mic.input_name in levels and mic.input_name in silent:
            silent.discard(mic.input_name)
            log(f"level from {mic.input_name!r} is back")
        if not chans:
            return FLOOR_DB
        if mic.channel >= len(chans):
            if mic not in warned:
                warned.add(mic)
                log(f"{mic.input_name!r} has {len(chans)} channel(s), not {mic.channel + 1}: untick Mono in OBS")
            return FLOOR_DB
        # [magnitude, peak, peak before OBS fader/mute]: the last ignores the OBS fader and mute.
        return to_db(chans[mic.channel][2])

    while True:
        ev = obs.next_event()
        if ev is None:
            continue
        kind, data = ev.get("eventType"), ev.get("eventData") or {}
        if kind == "CurrentProgramSceneChanged":
            live_scene = data.get("sceneName")
            if live_scene != switched_to and not args.meter:
                director.hold()
                state = "auto" if live_scene in scene_role else "auto paused"
                log(f"manual switch to {live_scene!r} ({state})")
            switched_to = None
        elif kind == "InputVolumeMeters":
            levels = {i["inputName"]: i["inputLevelsMul"] for i in data.get("inputs", [])}
            p_db, a_db = mic_db(levels, mics[0]), mic_db(levels, mics[1])
            now = time.monotonic()
            dt, last = min(now - last, 0.5), now
            target = director.update(p_db, a_db, dt, scene_role.get(live_scene))
            if args.meter:
                ticks += 1
                if ticks % 2 == 0:
                    pick = director.candidate or "-"
                    sys.stdout.write(
                        f"\rpresenter {p_db:6.1f} dB [{bar(p_db)}]  audience {a_db:6.1f} dB [{bar(a_db)}]  winner: {pick:<9}"
                    )
                    sys.stdout.flush()
                continue
            if target:
                scene = role_scene[target]
                p_avg, a_avg = director.levels()
                log(f"-> {scene:<12} presenter {p_avg:5.0f} dB, audience {a_avg:5.0f} dB")
                if not args.dry_run:
                    switched_to = scene
                    obs.request("SetCurrentProgramScene", {"sceneName": scene})
                live_scene = scene


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=4455)
    ap.add_argument("--password", default=os.environ.get("OBS_PASSWORD", ""), help="default: $OBS_PASSWORD")
    ap.add_argument("--presenter-mic", type=MicRef.parse, default=MicRef("Mic/Aux", 0),
                    metavar="INPUT:CH", help="OBS audio input and 1-based channel (default Mic/Aux:1)")
    ap.add_argument("--audience-mic", type=MicRef.parse, default=MicRef("Mic/Aux", 1),
                    metavar="INPUT:CH", help="default Mic/Aux:2")
    ap.add_argument("--presenter-scene", default="Presenter")
    ap.add_argument("--audience-scene", default="Audience")
    ap.add_argument("--both-scene", default="Both",
                    help="optional scene for crosstalk and silence; skipped if it doesn't exist; '' disables")
    ap.add_argument("--gate", type=float, default=-40, help="dBFS peak a mic must exceed to count as talking")
    ap.add_argument("--margin", type=float, default=8, help="dB the talking mic must be louder than the other")
    ap.add_argument("--window", type=float, default=1.5,
                    help="seconds of history; the mic dominating most of it (at least 35%%) gets the camera")
    ap.add_argument("--hold", type=float, default=4, help="minimum seconds between switches")
    ap.add_argument("--idle", type=float, default=20, help="seconds of silence before going to Both/Presenter; 0 = never")
    ap.add_argument("--list", action="store_true", help="print OBS inputs and scenes, then exit")
    ap.add_argument("--meter", action="store_true", help="show live mic levels, don't switch")
    ap.add_argument("--dry-run", action="store_true", help="log the switches without doing them")
    args = ap.parse_args()
    args.tuning = Tuning(args.gate, args.margin, args.window, args.hold, args.idle)

    waiting = False
    while True:
        try:
            obs = ObsConnection(args.host, args.port, args.password, SUB_SCENES | SUB_INPUT_VOLUME_METERS)
        except (OSError, websocket.WebSocketException, Disconnected) as e:
            if not waiting:
                log(f"waiting for OBS WebSocket on {args.host}:{args.port} ({type(e).__name__}); is OBS running?")
                waiting = True
            time.sleep(3)
            continue
        waiting = False
        try:
            session(obs, args)
        except NotReady as e:
            log(f"{e}, retrying")
        except (OSError, websocket.WebSocketException, Disconnected):
            print()
            log("lost connection to OBS, reconnecting")
        finally:
            obs.close()
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except SetupError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print()
