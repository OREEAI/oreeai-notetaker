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
container (the WAV lands there), plus `bot/debug` as `/debug` for
troubleshooting screenshots. Both are gitignored local dirs. Stop the bot
with `Ctrl-C` or `docker stop oreeai-bot-spike` — SIGTERM is handled
gracefully, the recording is finalized before exit.

`make bot-build` stamps the image with the building commit (`ENV GIT_SHA`
via `--build-arg`, placed just above the `COPY bot/...` tail so rebuilds
stay cached); a `-dirty` suffix marks builds from an uncommitted tree. The
bot and the probe log it as `image_sha=` at startup. `make bot-run`
rebuilds from HEAD first (cached: seconds when unchanged), so a gate log
always identifies the exact code under test.

## Launch probe

`make bot-probe` launches the browser inside the container exactly as the bot
does (same launch call, imported verbatim from `bot.join_meet`, so the
probe cannot drift from what the bot ships) and checks it comes up headful
under Xvfb with `navigator.webdriver` false. It also dumps a bounded
fingerprint (`fingerprint=<json>`) plus the real launch argv
(`browser_argv=<...>`); diff those field-by-field against the same host's
Chrome incognito output to rank what still distinguishes the automated
client. Re-run it after any Dockerfile
or launch-config change, before spending a manual gate run on it.

## Env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `MEETING_URL` | yes | — | `https://meet.google.com/xxx-xxxx-xxx` |
| `BOT_NAME` | no | `Oree Spike` | **Spike only.** PR 3 hard-codes the name to `Oree Notetaker`. The bot types it into Meet's "Your name" field before joining. |
| `CONSENT_ACK` | no | — | Logged pass-through in the spike; refusing to start without it lands in PR 3. |
| `CALL_ID` | no | `spike` | Names the WAV: `/audio/<CALL_ID>.wav`. The runner later passes the real call id. |
| `LOG_LEVEL` | no | `INFO` | stdlib level name |
| `DEBUG_DIR` | no | `/tmp` | Where stall screenshots land. The Makefile sets it to `/debug` (mounted as `bot/debug`) so screenshots survive the `--rm` container. |

The container runs as uid `1000`. If your host uid differs, make the audio
mount writable: `chmod 777 bot/audio` (the Makefile target does this for you).

## Audio format (pinned — never changes)

Container WAV: **16-bit PCM, 16 kHz, mono** — recorded via
`parec --device=virtual_speaker.monitor --rate=16000 --channels=1
--format=s16le --file-format=wav` (recording from a sink captures its
monitor; `parec` has no `--monitor-source` option). ≈ 115 MB/hour. Both Deepgram and
AssemblyAI accept it directly; no downstream transcode is ever needed.

Verify with:

```bash
ffprobe bot/audio/*.wav
# Stream #0: Audio: pcm_s16le, 16000 Hz, mono, s16, 256 kb/s
```

## Trap flags (why these exist — do not remove)

1. **`--autoplay-policy=no-user-gesture-required`** — Chromium blocks
   autoplay without a user gesture; without this flag you get a
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

Chromium runs **headful** (`headless=False`) under the entrypoint's Xvfb —
the standard meet-bot setup. Headful Chromium doesn't advertise
`HeadlessChrome` in its user agent, which is the most common cause of Meet
serving a verify/captcha/error page to an automated visitor.

6. **`--disable-blink-features=AutomationControlled`** — Playwright
   attaches via CDP, which sets `navigator.webdriver=true`, and Meet serves
   its "You can't join this video call" block screen to clients it detects
   as automated (even when anonymous guests are allowed — an incognito
   window with the same link works fine). This flag suppresses the
   automation fingerprint (`navigator.webdriver` reads `false` again).

7. **`channel="chrome"`** — Playwright's bundled Chromium build is detected
   by Meet's server-side anti-bot check: the bot cleared the green room and
   clicked "Join now", and Meet served its "You can't join this video call"
   wall at the moment of joining (an incognito window with the same link
   joined directly, ruling out meeting policy and network). The image
   installs branded Google Chrome (`playwright install chrome --with-deps`;
   ~300–500 MB larger) and `join_meet.py` launches it via
   `channel="chrome"`; the bundled Chromium install stays as an A/B baseline.

8. **Fingerprint realism (option-B stealth pass).** Meet scores the join
   request against the client environment, so the bot closes every measured
   gap vs a real Chrome install (verified with `make bot-probe` against the
   same host's incognito dump — see Launch probe):
   - `--force-device-scale-factor=1.25` + Xvfb `2400x1350x24` + viewport
     `1920x1080` reproduce the host's screen (1920x1080 @ dpr 1.25).
   - `TZ` env (Makefile `TZ ?= Africa/Lagos`, overridable) — the Intl
     timezone is fingerprint-visible.
   - `fonts-noto-core` + a `fontconfig` `local.conf` preferring Noto Sans /
     Noto Serif, so default-family text metrics match a desktop install.
   - `--accept-lang=en-US,en` aligns the Accept-Language header (JS
     `navigator.languages` stays `["en-US"]` without a persistent profile —
     accepted, documented).
   - `ignore_default_args` (`_BROWSER_IGNORED_ARGS` in `join_meet.py`) drops
     Playwright's automation-flavored defaults (metrics, sync, phishing,
     component updater, backgrounding, extensions, updater). The
     `--disable/enable-features` pair is Playwright-forced residue that
     `ignore_default_args` cannot strip (exact-match semantics) — accepted,
     along with deliberately kept `--no-default-browser-check` (avoids a
     default-browser modal) and argv-only `--no-sandbox` /
     `--enable-unsafe-swiftshader` (not page-visible). Kept: `--no-first-run`,
     `--password-store=basic`/`--use-mock-keychain`, search-engine-choice
     (all avoid first-run modals in the temp profile). Playwright re-adds
     `--disable-features`/`--enable-features` unconditionally — accepted,
     low-signal residue.
   - `bot/humanize.py` — dwell 4–8 s, mouse paths, per-key typing jitter,
     click holds, inter-action pauses. Same flow order; selectors untouched.
   - `bot/stealth.py` — masks the software WebGL renderer string with the
     exact host values. Rationale: `--device /dev/dri` passthrough (Makefile
     `GPU_FLAGS`, no-op without `/dev/dri`) plus ANGLE-on-GL still left
     SwiftShader — and on the retry broke context creation entirely — so the
     container renders in software and the string is spoofed instead. A
     `module-remap-source` (`virtual_mic`) in the entrypoint gives Chromium a
     real audio input device from the null-sink monitor (no fake-device flag
     ever — that would break capture and add a tell).
   - Launch config (channel, flags, ignored args) lives behind
     `launch_browser()` in `join_meet.py`, shared by the bot and the probe,
     so the probe cannot drift from what ships.

Also note: `--no-sandbox` is required because the container runs Chromium
as a non-root user without `SYS_ADMIN`.

## Selectors

Every Meet DOM selector lives in `bot/selectors.py` (aria-label / role
based, `en-US` forced). A Meet UI change must be a one-file fix. When state
detection stalls, the bot saves a debug screenshot to
`$DEBUG_DIR/oreeai-debug-<ts>.png` (`bot/debug/` in spike runs) and logs a
warning with the page URL and a snippet of visible page text.

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
| 5 | Unexpected bot error (Playwright crash, exception, capture failure, the meeting blocks anonymous guests, or Meet blocks the join attempt — see Gate criteria) | |

The full contract table (3 removed, 4 timeouts, 6 consent refused, 7 silent
recording) is wired in PR 2.

## Gate criteria (definition of done)

A container started with a Meet URL produces a WAV that contains your
voice, recorded off a real Meet call you joined from a second device:

1. Host a call from device A, grab the guest link. In the meeting's host
   controls, make sure **Quick access is ON** — otherwise the bot hits
   Meet's "You can't join this video call" block screen. Sanity check:
   open the link in an incognito window; you must see the pre-join screen
   (name field + "Ask to join"), not an error.
2. `make bot-run MEETING_URL=<link> BOT_NAME=Spike CONSENT_ACK=true`
3. `Spike` appears in the participant list on device A.
4. Talk for ~20 s, stop the bot.
5. `ffprobe` confirms 16 kHz / mono / 16-bit PCM.
6. Play the WAV — **your voice must be clearly audible.**
7. Logs show the state transitions (join → admitted → recording → ended)
   and no audio bytes or recording paths with identifiers.

If the meeting blocks anonymous guests, the bot fails fast (~4 s) with
"meeting blocks anonymous guests — host must enable Quick access" and a
screenshot in `bot/debug/`. That is a host-settings problem, not a bot
problem — a human in an incognito window would hit the same screen.

If the WAV is silent, one of the trap flags above was lost — debug before
proceeding. This gate is the whole point of the PR.
