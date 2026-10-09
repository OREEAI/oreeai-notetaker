# Handover — remaining Zoom work

Where the Zoom lane stands and what comes next. The master driving doc is
`plans/zoom-teams-prs.md` (local, gitignored): strict chunk order, shared
contracts, decision log, per-chunk gate/runbook ritual. This note is the
committed pointer to it.

Status 2026-10-09: the Zoom spike and lifecycle work are merged to `main`;
the in-call consent announcement is on PR #15 awaiting merge, and its
manual gate is owed (below). Next is the multi-platform service; the
dedicated-account auth is deferred until its trigger fires. Nothing
Teams-related has begun.

## In-call consent announcement (manual gate owed)

**What:** the exact consent message is posted to the Zoom web-client chat
once recording starts — best-effort, never stopping recording — closing
the Meet consent-parity gap before the service ships Zoom to users.

**Why:** a Zoom join currently discloses only the fixed name in the
participant list; every participant should see the recording notice in
the meeting chat, exactly as on Meet.

Manual scenarios (owner):

1. **Chat usable.** `make bot-run MEETING_URL='<zoom link>'
   BOT_PLATFORM=zoom CONSENT_ACK=true CALL_ID=z3-scenario1` — the exact
   message appears in the chat ~20 s after recording starts (log line
   `consent announcement posted to chat`; screenshot the posted message;
   WAV sanity via `ffprobe` 16 kHz/mono/s16 — talk or play audio, a fully
   quiet run exits 7 via the shared silence floor).
2. **Chat restricted.** Host disables participant chat — the bot logs
   `chat not available (...)` + a debug screenshot and the recording
   completes normally.
3. **Regression.** `CONSENT_ACK=false` (or unset) — exit 6 before any
   browser work.

Full detail: `plans/handoffs/z3-zoom-consent.md` (+ PDF); live evidence in
`plans/handoffs/zoom-evidence/z3_chat/` and `.../z3_live/`.

## Multi-platform service (next)

**What:** the service accepts Zoom meeting URLs and spawns the right bot
per `Call.platform` — per-platform URL validation, the `zoom` enum value,
`BOT_PLATFORM=zoom` in the runner's spawn argv, and platform test matrices
(malformed Zoom links rejected, never a bot spawn).

**Why:** the bot side is Zoom-capable, but the API and runner are still
Meet-only — no Zoom call can be created or dispatched end to end until
this lands. Meet behavior must remain byte-for-byte unchanged.

Prompt: `plans/z4-multi-platform-service-prompt.md`.

## Zoom dedicated-account auth (deferred, trigger-gated)

**What:** `BOT_ZOOM_AUTH_MODE=anonymous|authenticated` — a signed-in,
dedicated `Oree Notetaker` account (`bot/login_zoom.py`, session gate,
profile dir, display-name verification) as the fallback for Zoom's
invisible-CAPTCHA guest walls.

**Why:** guest joins are probabilistically walled ("sign in to join");
~11 clean guest joins so far are the evidence for deferring. Build it only
when triggered: first live sign-in wall, measurable CAPTCHA flagging of the
bot account, or a production signed-in requirement. It then reorders in —
before the multi-platform chunk if that has not merged.

Prompt (draft finalized at trigger time): `plans/z5-zoom-auth-prompt.md`.

## Microsoft Teams

Nothing with Microsoft Teams has begun. Teams exists only as stubs in
`plans/zoom-teams-prs.md`; its prompts are drafted after the Zoom chain
merges, against the then-merged state.
