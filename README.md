# Meet camera switcher

The camera follows whoever is talking. OBS runs both cameras and sends one picture to Google Meet as **OBS Virtual Camera**. `meet_cam_switcher.py` watches the two Focusrite mic channels in OBS's own audio meter and switches between the `Presenter`, `Audience` and optional `Both` scenes. Meet keeps using the Focusrite directly as its microphone, so audio works exactly as it does now.

```
presenter cam ─┐                    ┌─ scene Presenter ─┐
audience cam  ─┼─> OBS ─────────────┼─ scene Audience  ─┼─> OBS Virtual Camera ─> Google Meet (camera)
Focusrite ─────┘   │ Mic/Aux ch1/2  └─ scene Both      ─┘
     └─────────────┼──────────────────────────────────────────────────────────> Google Meet (mic)
                   └─> meet_cam_switcher.py ── SetCurrentProgramScene ──> OBS
```

## 1. Get both cameras running at once (do this first)

Most likely the problem is USB bandwidth, not Windows. A webcam sending uncompressed YUY2 at 720p30 uses about 440 Mbit/s, close to the 480 Mbit/s a USB 2.0 bus has. The second camera then gets nothing and stays black or fails to start. Linux has the same limit, so switching OS won't fix this.

- In OBS, for each **Video Capture Device**, open Properties and set: Resolution/FPS Type = Custom, 1280x720 or 1920x1080, 30 fps, **Video Format = MJPEG**. MJPEG needs only tens of Mbit/s.
- Plug each camera straight into the PC, not through a hub, dock or monitor, and not on the same hub as the Focusrite (audio reserves USB bandwidth too). Put the cameras on different controllers, for example one on the front panel and one at the back. [UsbTreeView](https://www.uwe-sieber.de/usbtreeview_e.html) shows which controller each device is on. If every port shares one controller, a PCIe USB 3 card (about €25) adds a separate one.
- On both camera sources, untick **Deactivate when not showing**. Both cameras then stay open and switching is instant.
- Two cameras of the same model: OBS tells them apart by USB port. Keep each camera in the same port, otherwise they can swap places.
- Close the Windows Camera app and Teams. Windows gives each camera to only one app at a time. In Meet you choose OBS Virtual Camera, never the real cameras.

If the cameras still won't run together, see "No USB for the second camera" below.

## 2. OBS setup (once)

You need OBS 28 or newer, which has the WebSocket server built in.

1. **Scenes:**
   - `Presenter`: the presenter camera, full screen.
   - `Audience`: the audience camera, full screen.
   - `Both` (optional): both cameras side by side or picture-in-picture. Add the cameras with **Add Existing** so each camera is opened only once.
   - Any other scene (`Slides`, `Break`) **pauses** the automation while it is live.
2. **Audio:** in **Settings → Audio**, set Channels to Stereo and **Mic/Auxiliary Audio** to the Focusrite (for example "Analogue 1 + 2"). It appears in the mixer as `Mic/Aux`. Mic 1 is the presenter, mic 2 is the audience. Leave **Mono** unticked under Advanced Audio Properties. The OBS fader and mute button don't matter, because the script reads the level before the fader.
   - Use the global Mic/Aux device, not an Audio Input Capture source inside one scene. OBS only reports levels for audio that is active, so a source in the `Presenter` scene goes silent as soon as `Audience` is live. `--list` still shows that source, and the switcher logs "no level from …" when this happens.
3. **Tools → WebSocket Server Settings:** tick Enable, keep port 4455, open Show Connect Info and copy the password.
4. **Settings → Video:** output 1280x720 or 1920x1080 at 30 fps. In the Scene Transitions dock, a 300 ms Fade looks nicer than Cut.
5. Click **Start Virtual Camera**.

## 3. Google Meet

Set the camera to **OBS Virtual Camera** and keep the microphone on the Focusrite, as now. Your own self-view may appear mirrored. Other participants see it the right way round.

## 4. Run the switcher

One-time setup on the Windows machine:

```
winget install astral-sh.uv Git.Git GitHub.cli
gh auth login
gh repo clone Pimvandermeer/meet-cam-switcher
setx OBS_PASSWORD "<password from step 2.3>"
```

Then open a new terminal in `meet-cam-switcher`. Run `git pull` later to get updates.

```
uv run meet_cam_switcher.py --list     # OBS input and scene names, to check spelling
uv run meet_cam_switcher.py --meter    # live levels per mic: talk into each one
uv run meet_cam_switcher.py --dry-run  # logs the switches it would make, without doing them
uv run meet_cam_switcher.py            # the real thing
```

Use different names with `--presenter-mic "Name:CH"`, `--audience-mic`, `--presenter-scene`, `--audience-scene` and `--both-scene`. Pass `--both-scene ""` to turn off the Both scene.

Tuning: check the levels in `--meter` first.

| flag | default | meaning |
|---|---|---|
| `--gate` | -40 | dBFS peak a mic must exceed to count as talking. Set it between the room-noise level and the speaking level. |
| `--margin` | 8 | dB the talking mic must be louder than the other one. This ignores the sound each mic picks up from the other person. |
| `--window` | 1.5 | seconds of history. The mic that dominates most of it, with at least 35%, gets the camera. A cough, a laugh or a dropped pen isn't enough. |
| `--hold` | 4 | minimum seconds between switches. |
| `--idle` | 20 | seconds of silence before going to Both (or to Presenter if there is no Both scene). 0 = never. |

- **Manual override:** click `Slides` or any other unmanaged scene and the automation pauses. Click Presenter, Audience or Both and it resumes. A scene you pick by hand stays on for at least `--hold` seconds.
- **Remote people talking through the room speakers** can trigger the audience camera if the speakers are near the audience mic. Move the speakers or raise `--gate`.
- The script waits for OBS when OBS isn't running yet, and reconnects after OBS restarts.

## 5. Autostart

Put a shortcut to `start-meeting-room.bat` in `shell:startup` (press Win+R and type `shell:startup`). The script starts OBS minimised with the virtual camera on, then starts the switcher. Extra arguments are passed through to the switcher.

## Alternatives

- **No code:** the OBS plugin *Advanced Scene Switcher* has an "Audio" condition that switches the scene when a source's volume stays above a level for some seconds. It needs each mic as a separate OBS source, but the Focusrite gives both mics as one stereo source. It also has no "louder than the other mic" rule, so bleed between the mics causes false switches. That is why this script exists.
- **Linux:** the same OBS setup and the same script work unchanged. The virtual camera needs `sudo apt install v4l2loopback-dkms`. It only makes sense if you want a dedicated meeting-room box, because it doesn't solve the USB problem. For uncompressed formats, `options uvcvideo quirks=128` in `/etc/modprobe.d/uvcvideo.conf` makes the kernel reserve only the bandwidth the camera actually needs.
- **No USB for the second camera:** use a network camera, which needs no USB bandwidth. An IP or PTZ camera works through RTSP as an OBS *Media Source*, or a phone with an NDI or DroidCam app works through the matching OBS plugin. With a PTZ camera you can also give each scene its own preset, such as a wide shot of the audience versus a close-up of the presenter.
