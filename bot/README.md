# OreeAI Meet bot (PR 1 — spike)

A container that joins a real Google Meet call as a guest and records the
meeting audio to a WAV file. This spike is the **go/no-go gate** for the
whole self-hosting approach: if the bot can't hear a real Meet call, stop
and reassess.

The bot is a **separate deployable**. It must never import `oreeai_nt`, and
the service must never import `bot/`. The only contract between them is the
container boundary and the exit-code table below.

## Build & run

```bash
make bot-build
make bot-run MEETING_URL=https://meet.google.com/xxx-xxxx-xxx BOT_NAME=Spike CONSENT_ACK=true
```

`make bot-run` creates and mounts `bot/audio` as `/audio` inside the
container (the WAV lands there). Stop the bot with `Ctrl-C` or
`docker stop oreeai-bot-spike` — SIGTERM is handled gracefully, the
recording is finalized before exit.

## Env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `MEETING_URL` | yes | — | `https://meet.google.com/xxx-xxxx-xxx` |
| `BOT_NAME` | no | `Oree Spike` | **Spike only.** PR 3 hard-codes the name to `Oree Notetaker`. |
| `CONSENT_ACK` | no | — | Logged pass-through in the spike; refusing to start without it lands in PR 3. |
| `CALL_ID` | no | `spike` | Names the WAV: `/audio/<CALL_ID>.wav`. The runner later passes the real call id. |
| `LOG_LEVEL` | no | `INFO` | stdlib level name |

The container runs as uid `1000`. If your host uid differs, make the audio
mount writable: `chmod 777 bot/audio` (the Makefile target does this for you).

## Audio format (pinned — never changes)

Container WAV: **16-bit PCM, 16 kHz, mono** — recorded via
`parec --monitor-source=virtual_speaker.monitor --rate=16000 --channels=1
--format=s16le --file-format=wav`. ≈ 115 MB/hour. Both Deepgram and
AssemblyAI accept it directly; no downstream transcode is ever needed.

Verify with:

```bash
ffprobe bot/audio/*.wav
# Stream #0: Audio: pcm_s16le, 16000 Hz, mono, s16, 256 kb/s
```

## Trap flags (why these exist — do not remove)

1. **`--autoplay-policy=no-user-gesture-required`** — headless Chromium
   blocks autoplay without a user gesture; without this flag you get a
   silent recording that "works" (a vacuous pass).
2. **`--disable-dev-shm-usage`** (+ `--shm-size=1g` on docker run) —
   Chromium crashes in Docker on the default 64 MB `/dev/shm`.
3. **`--lang=en-US`** + context `locale="en-US"` + `LANG=en_US.UTF-8` (image
   generates the locale) — Meet UI selectors break on localized strings.
4. **`--use-fake-ui-for-media-stream`** + context permissions
   `microphone`/`camera` (Playwright's API names; they map to Chromium's
   `audioCapture`/`videoCapture`) — auto-grants media capture; the bot
   joins with mic muted and camera off (toggles clicked before "ask to join").
5. **`pulseaudio --exit-idle-time=-1`** — without it the daemon quits after
   30 s of silence (which there will be, in waiting rooms) and capture dies
   mid-call. Plus `module-null-sink sink_name=virtual_speaker`: the
   container has no real audio device — Chromium "plays" into the null sink
   and `parec` records its monitor. Setting the default sink ensures
   Chromium's audio goes there.

Also note: `--no-sandbox` is required because the container runs Chromium
as a non-root user without `SYS_ADMIN`.

## Selectors

Every Meet DOM selector lives in `bot/selectors.py` (aria-label / role
based, `en-US` forced). A Meet UI change must be a one-file fix. When state
detection stalls, the bot saves a debug screenshot to
`/tmp/oreeai-debug-<ts>.png` and logs a warning.

`bot/selectors.py` is imported only as part of the `bot` package
(`python -m bot.join_meet`): a flat script import from inside `bot/` would
shadow Python's stdlib `selectors` module, which Playwright/asyncio need.

## Logging

stdlib `logging`, logger `oreeai.bot` (and `oreeai.bot.record`). Every state
transition is logged. Audio bytes are never logged, and recording paths are
never logged with identifiers.

## Exit codes

| Code | Meaning | Notes |
|---|---|---|
| 0 | Clean end (call ended or graceful SIGTERM/stop) | |
| 2 | Never admitted (waiting-room timeout, 600 s in the spike) | |
| 5 | Unexpected bot error (Playwright crash, exception, capture failure) | |

The full contract table (3 removed, 4 timeouts, 6 consent refused, 7 silent
recording) is wired in PR 2.

## Gate criteria (definition of done)

A container started with a Meet URL produces a WAV that contains your
voice, recorded off a real Meet call you joined from a second device:

1. Host a call from device A, grab the guest link.
2. `make bot-run MEETING_URL=<link> BOT_NAME=Spike CONSENT_ACK=true`
3. `Spike` appears in the participant list on device A.
4. Talk for ~20 s, stop the bot.
5. `ffprobe` confirms 16 kHz / mono / 16-bit PCM.
6. Play the WAV — **your voice must be clearly audible.**
7. Logs show the state transitions (join → admitted → recording → ended)
   and no audio bytes or recording paths with identifiers.

If the WAV is silent, one of the trap flags above was lost — debug before
proceeding. This gate is the whole point of the PR.
