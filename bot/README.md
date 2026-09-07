# OreeAI Meet bot (PRs 2–3 — join lifecycle + consent)

A container that joins a real Google Meet call, tracks the call
through every real-world lifecycle state, records meeting audio to a WAV
file, and leaves cleanly on every exit path. PR 1 proved the bot can join
and capture audible audio; PR 2 made waiting rooms, late admission,
removals, empty rooms, alone grace, maximum recording duration, and
silent-capture detection explicit and testable; PR 3 makes the bot a
**visibly consenting recorder** (fixed name + in-call chat announcement)
that refuses to start without an explicit `CONSENT_ACK`.

The bot is a **separate deployable**. It must never import `oreeai_nt`, and
the service must never import `bot/`. The only contract between them is the
container boundary and the exit-code table below.

## Build & run

```bash
make bot-build
make bot-run MEETING_URL=https://meet.google.com/xxx-xxxx-xxx CONSENT_ACK=true
```

Lifecycle timeouts can be overridden per run without changing the image:

```bash
make bot-run MEETING_URL=<link> CONSENT_ACK=true CALL_ID=max-cap BOT_MAX_RECORD_DURATION=30
```

## Join modes

Meet's server-side scoring walls anonymous guest joins probabilistically
(measured: 2 admissions / 5 walls on fresh links), so the bot ships two join
identities behind `BOT_AUTH_MODE`:

| Mode | Value | Behavior |
|---|---|---|
| Anonymous (guest) | `anonymous` | Throwaway Chrome profile; joins as a guest and types the fixed name `Oree Notetaker` into the pre-join name field. Code default. |
| Authenticated (signed-in) | `authenticated` | Branded Chrome on the persistent profile at `/profile`; joins as the signed-in account, skips name typing — the account's display name **is** the consent signal. Documented production setting. |

Everything downstream is identical in both modes: the lifecycle state
machine, selectors, exit codes, audio graph, silence check, and the
`OREEAI_BOT_RESULT` line. The stealth layer (`bot/stealth.py`,
`bot/humanize.py`) stays on in both modes — signing in removes the
anonymous-scoring tier, not the automation-fingerprint tier.

The persistent profile holds the Google session (credentials). It lives in
`bot/chrome-profile/` on the host (`BOT_PROFILE` Makefile var overrides),
mounted at `/profile`. It is gitignored **and** dockerignored — it must never
be committed or baked into the image — and the Makefile keeps it `chmod 700`.
Treat it exactly like a password. Profile paths are never logged.

Use a **dedicated Google account** for the bot, with its display name set to
**`Oree Notetaker`** (the fixed consent identity — see [Consent](#consent))
and 2FA enabled. Never a personal account: if Google ever flags automated
behavior, it flags the account. In development and staging any display name
works (the bot logs a warning on a mismatch); in production
(`ENVIRONMENT=production`) a mismatched or undeterminable name is fatal
before any Meet request.

## One-time sign-in

```bash
make bot-login
```

This builds the image, creates the profile dir, and starts a login
container: Xvfb + Chrome on the profile + noVNC published on
**127.0.0.1:7900 only**. Open `http://127.0.0.1:7900` in a browser on the
host, click **Connect**, and sign in
with the dedicated bot account (complete 2FA), and wait — the script exits 0
once it detects the session (default 600 s timeout via `BOT_LOGIN_TIMEOUT`).
Future authenticated runs reuse that session. Nothing appears on the host
desktop: the bot's Chrome runs inside the container's virtual display, and
noVNC is how you see and drive it.

Every authenticated run first verifies the session against
`myaccount.google.com`. A missing profile or a signed-out session fails fast
with exit 5 (`bot_error:google_session_missing` /
`bot_error:google_session_invalid: ...`) and a "run make bot-login" hint —
no Meet request is made.

### Session persistence

The Google session lives on the **host**, not in the container:

- `make bot-login` mounts `bot/chrome-profile/` into the container at
  `/profile`, and Chrome writes every cookie/session token straight to that
  host directory.
- The container is disposable (`--rm`); the image never contains the profile
  (`chrome-profile/` is dockerignored, so `COPY bot/` cannot bake it in).
  Full rebuilds, `--no-cache` included, do not touch it.
- Every later `make bot-run` mounts the same host dir, so the session is
  picked up again.

Three caveats:

1. **Google-side expiry** — the session persists until *Google* invalidates
   it (re-auth challenge, password change, long inactivity). The session
   gate catches that: authenticated runs exit 5 with
   `google_session_invalid`; the fix is re-running `make bot-login`.
2. **`git clean -fdx` deletes it** — the profile is gitignored; treat it as
   credentials and don't nuke untracked files casually (`make clean` is
   safe).
3. **One Chrome per profile** — never run `bot-login` and a bot run at the
   same time; Chrome locks the profile dir. Sequential use is fine, and both
   use the identical branded-Chrome build/args, which keeps Google's device
   identity consistent.

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

## Consent

Every participant must be able to see, from inside the meeting, that a
recording is happening — without being told beforehand. The policy lives in
`bot/consent.py`.

- **Fixed name.** The bot's identity is hard-coded to `Oree Notetaker`.
  Anonymous mode types it into the green room; authenticated mode skips
  typing — the dedicated account's display name *is* the consent signal, and
  `ENVIRONMENT=production` enforces that name (exit 5 before any Meet
  request on a mismatch; development/staging warn only).
- **`CONSENT_ACK` gate.** The bot refuses to start (exit `6`) unless
  `CONSENT_ACK=true` is set, checked before any browser work. The runner
  (PR 5) passes it per call; the API-level requirement lands there too.
- **In-call announcement.** Right after recording starts, the bot posts this
  exact message to the meeting chat:

  > Hi, this is Oree Notetaker. This call is being recorded and transcribed
  > for note-taking. Let me know if you'd like me to leave.

  The step is best-effort: success, failure, and element-not-found are
  distinct log lines (logger `oreeai.bot.consent`), with a 10 s per-element
  finder timeout (covers the post-admit UI race). A missing or broken chat
  UI logs a warning, saves a debug screenshot, and the recording continues —
  **chat never stops recording**; the visible name remains the signal. Host
  chat restrictions are handled the same way (warn, continue).
- **`BOT_NAME` is deprecated.** Any value other than the exact `Oree
  Notetaker` logs a deprecation warning naming both values, then the
  hard-coded name is used. Person-like names ("Alex") are never honored —
  the participant list must never show a human-sounding recorder.

## Env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `MEETING_URL` | yes | — | `https://meet.google.com/xxx-xxxx-xxx` |
| `CONSENT_ACK` | yes | — | Must be `true` or the bot exits `6` before any browser work. The Makefile honors it from `.env`; override per run on the command line. |
| `BOT_NAME` | no | — | **Deprecated.** The name is hard-coded to `Oree Notetaker` (PR 3). Any other value logs a deprecation warning naming both values, then the hard-coded name is used. |
| `CALL_ID` | no | `spike` | Names the WAV: `/audio/<CALL_ID>.wav`. The runner later passes the real call id. |
| `ENVIRONMENT` | no | `local` | Deployment env. `production` makes the authenticated-mode account-name consent check fatal (dev/staging warn only). |
| `LOG_LEVEL` | no | `INFO` | stdlib level name |
| `DEBUG_DIR` | no | `/tmp` | Where stall screenshots land. The Makefile sets it to `/debug` (mounted as `bot/debug`) so screenshots survive the `--rm` container. |
| `BOT_WAITING_ROOM_TIMEOUT` | no | `600` | Seconds waiting for admission before exiting `2` (`never_admitted`). |
| `BOT_EMPTY_ROOM_TIMEOUT` | no | `300` | Seconds after admission in a confirmed-empty room before exiting `4`. The same deadline applies when participant detection remains unavailable; that path exits `5` because the bot cannot verify the room. |
| `BOT_ALONE_GRACE` | no | `60` | Seconds the bot remains after all other participants leave a previously active call before leaving with exit `0` and `end_reason=alone`. |
| `BOT_MAX_RECORD_DURATION` | no | `10800` | Maximum recording seconds before the bot leaves with exit `0` and `end_reason=give_up`. |
| `BOT_SILENCE_RMS_FLOOR` | no | `50` | Whole-WAV RMS floor in raw 16-bit units. A clean exit with a finished recording below this floor exits `7` (`silent_recording`). |
| `PAREC_DEVICE` | no | `virtual_speaker.monitor` | Diagnostic PulseAudio-device override for `parec`. Pointing it at `silent_sink.monitor` exercises the silence path with a live recorder and a silent source. |
| `BOT_AUTH_MODE` | no | `anonymous` | Join identity: `anonymous` (guest) or `authenticated` (signed-in via the persistent profile). Production runs use `authenticated`. |
| `BOT_PROFILE_DIR` | no | `/profile` | Container-side Chrome profile path for authenticated mode. The Makefile mounts the host profile dir here. |
| `BOT_ENTRY_MODE` | no | `run` | `run` starts the bot; `login` (used by `make bot-login`) starts the interactive sign-in bootstrap with noVNC. |
| `BOT_LOGIN_TIMEOUT` | no | `600` | Seconds `make bot-login` waits for the sign-in to complete. |

The container runs as uid `1000`. If your host uid differs, make the audio
mount writable: `chmod 777 bot/audio` (the Makefile target does this for you).

## Join lifecycle

`bot/join_meet.py` performs the humanized pre-join flow and owns browser
startup/shutdown, the recorder, the silence check, and the process exit code.
`bot/states.py` contains side-effect-free DOM predicates. Each predicate takes
a page and an optional selector set defaulting to `bot/selectors.py`.
 `bot/listeners.py`
polls about every two seconds, logs transitions, starts/stops recording, and
clicks Leave on the terminal paths that need a clean departure.

```text
join_clicked
  -> waiting_room
    -> in_call (admitted; recorder starts)
      -> call_ended (exit 0)
      -> removed (exit 3)
      -> alone (exit 0 after BOT_ALONE_GRACE)
      -> give_up (exit 0 after BOT_MAX_RECORD_DURATION)
      -> empty_room (exit 4 after BOT_EMPTY_ROOM_TIMEOUT)
      -> bot_error (exit 5: recorder died or room stayed undetectable)
    -> never_admitted (exit 2 after BOT_WAITING_ROOM_TIMEOUT)
    -> blocked (exit 5: Meet served the anti-bot wall)
```

Admission into a room that never had another participant is the empty-room
case, not the alone case. The alone path requires the bot to have seen at
least two participants before everyone else leaves. A denied knock has no
distinct Meet screen, so it remains in the waiting room until the 600-second
deadline and exits `2`.

Participant counts include the bot. Meet does not always put the number in
the participants control's accessible name, so the bot also reads the visible
count badge and treats Meet's "only one here" text as corroboration. An
unknown count is never treated as an empty room.

## Exit codes

| Code | Meaning | Runner outcome |
|---|---|---|
| 0 | Clean end: host ended the call, bot left after alone grace, bot reached the maximum recording duration, or an operator stopped the container | `done`; `end_reason` is `call_ended`, `alone`, `give_up`, or `null` for an operator stop |
| 2 | Never admitted after `BOT_WAITING_ROOM_TIMEOUT` | `failed`, `failure_reason=never_admitted` |
| 3 | Removed mid-call | `done`, `end_reason=removed` |
| 4 | Lifecycle timeout: waiting in an unstarted/empty room after admission | `failed`; the runner reports `join_timeout` when the bot never recorded and `no_show` when it recorded an empty room |
| 5 | Unexpected bot error: pre-join failure, blocked join attempt, dead recorder, or unavailable room detection | `failed`, `failure_reason=bot_error:<detail>` |
| 6 | `CONSENT_ACK` not true (or unset); refused before any browser launch | `failed`, `failure_reason=consent_missing` |
| 7 | A finished clean recording is below `BOT_SILENCE_RMS_FLOOR` | `failed`, `failure_reason=silent_recording` |

The bot also emits one machine-readable terminal line for the future runner:

```text
OREEAI_BOT_RESULT {"call_id": "<uuid>", "end_reason": "alone", "exit_code": 0}
```

## Silence check

On every clean exit with a finished recording, the bot measures RMS over the
entire WAV with the standard library and compares it to
`BOT_SILENCE_RMS_FLOOR`. The default floor is intentionally low: real speech,
even soft speech in a short meeting, measures far above it. Only an
effectively silent capture—such as a broken PulseAudio graph—falls below it.

To exercise exit `7` without breaking the operational audio graph, run with a
live recorder pointed at the container's always-silent diagnostic sink:

```bash
make bot-run MEETING_URL=<link> CONSENT_ACK=true CALL_ID=silence PAREC_DEVICE=silent_sink.monitor
```

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
`$DEBUG_DIR/oreeai-debug-<ts>.png` (`bot/debug/` in local runs) and logs a
warning with the page URL and a snippet of visible page text. Lifecycle
selectors include the participant-count control and Meet's alone-room text;
consent selectors (PR 3) are the in-call chat open control, message box, and
send button.

`bot/selectors.py` is imported only as part of the `bot` package
(`python -m bot.join_meet`): a flat script import from inside `bot/` would
shadow Python's stdlib `selectors` module, which Playwright/asyncio need.

## Logging

stdlib `logging`, logger `oreeai.bot` (plus `oreeai.bot.record`,
`oreeai.bot.states`, `oreeai.bot.consent`, and `oreeai.bot.result`). Every
lifecycle transition is logged as:

```text
call_id=<id> from_state=<previous> to_state=<next> reason=<why>
```

The terminal machine-readable line is:

```text
OREEAI_BOT_RESULT {"call_id": "<id>", "end_reason": "<reason>", "exit_code": 0}
```

Audio bytes are never logged. Recording paths are never logged with
identifiers; the silence check reports only numeric RMS and floor values.

## Manual lifecycle expectations

The real-Meet scenarios exercise the happy path, late admission, never
admitted, host never starts, removal, empty room, maximum duration, and
silence detection. Run them in **authenticated mode** (the anonymous path is
kept for A/B only): fresh link per scenario, a few minutes apart — the
hour-long cooldowns were an anonymous-scoring artifact and do not apply to
signed-in joins. For every run, check that the first log line's
`image_sha=` matches the commit under test, then compare the logged
transitions, final `OREEAI_BOT_RESULT` line, container exit code, and—where a
recording should exist—the WAV format and size. Note: `${PIPESTATUS[0]}`
after `make bot-run` is make's own exit code, not the container's; the `bot
finished: exit_code=N` log line is ground truth.

If the meeting blocks anonymous guests, the bot fails fast (~4 s) with
"meeting blocks anonymous guests — host must enable Quick access" and a
screenshot in `bot/debug/`. That is a host-settings problem, not a bot
problem — a human in an incognito window would hit the same screen.

If an operational WAV is silent, one of the trap flags above was lost—debug
before proceeding. A deliberately silent diagnostic source should exit `7`;
an operational silent WAV is a failure of the audio graph.
