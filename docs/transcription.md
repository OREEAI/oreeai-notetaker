# Transcription

How a finished recording becomes a stored, speaker-separated transcript.

## Provider: Deepgram nova-3 (batch)

**Settled 2026-09-15 (CTO).** Model `nova-3`, pre-recorded (batch) API,
configured in `integrations/transcription/deepgram.py`.

### Why Deepgram over AssemblyAI

Both providers met the queue's hard constraint — **batch and realtime
on one account** (never Whisper; see below). The tiebreakers, in the
CTO's words on record:

1. **Speaker diarization is included free on Deepgram** for both batch
   and streaming. AssemblyAI charges separately for diarization, and on
   streaming that roughly **doubles the hourly cost**.
2. **Deepgram bills audio minutes.** AssemblyAI's streaming bills **per
   WebSocket session including idle time** — and our bots deliberately
   sit in waiting rooms up to 10 minutes (`BOT_WAITING_ROOM_TIMEOUT=600`)
   and in empty rooms up to 5 minutes (`BOT_EMPTY_ROOM_TIMEOUT=300`).
   Phase 3's live trainer would have paid for all of that dwell on
   AssemblyAI; on Deepgram it costs nothing because no speech is billed.

### Request shape (pinned, asserted in tests)

```
POST https://api.deepgram.com/v1/listen
Authorization: Token $DEEPGRAM_API_KEY
```

Query parameters:

| Param | Value | Why |
|---|---|---|
| `model` | `nova-3` | settled model; omitting it silently downgrades to `base` |
| `diarize_model` | `latest` | **the** diarization parameter — per https://developers.deepgram.com/docs/diarization/ , the legacy `diarize=true` is deprecated and requests that set *both* are rejected by the API, so exactly one is pinned. `latest` enables diarization and selects the newest GA batch diarizer in one parameter. |
| `utterances` | `true` | provider-native utterances (`transcript`/`start`/`end`/`speaker`) map 1:1 onto the segment schema |
| `punctuate` | `true` | readable product text (the transcript is the product) |
| `smart_format` | `true` | numbers, dates, currencies formatted |

The audio arrives as PR 1 pinned it — **16 kHz mono s16le WAV** — and is
never transcoded; Deepgram accepts it directly.

### Transports (hybrid, decided by the storage adapter)

The storage layer (`integrations/object_storage`) builds an
`AudioSource` via `transcribable_source(call_id)`; the transcription
adapter just consumes it:

- **S3 adapter** → `AudioSource(url=<presigned GET>, size_bytes=<head_object>)`.
  Deepgram fetches the object from the bucket. The URL is presigned,
  time-limited (`S3_PRESIGN_TTL_SECONDS`, default 3600) — the
  presigned-only serving rule is intact; audio is never served through a
  public link. Note the retry window: transient retries can span ~11
  minutes (3 × 660 s read timeout worst case), so keep the presign TTL
  comfortably above it (the 3600 s default is).
- **Local dev fallback** → `AudioSource(local_path=<stored copy>)`. A
  remote provider cannot fetch a `file://` location, so the adapter
  POSTs the stored copy itself as a raw binary body
  (`Content-Type: audio/wav`, streamed in chunks). Dev/staging only —
  the path never leaves the host except as request bytes to the
  provider, and it is never logged.

### Provider behavior pins

- **Deepgram does not store transcripts — the synchronous response is
  the only copy** (their docs' wording). A misparsed 200 is therefore a
  lost transcript and is treated as a permanent error, never as a
  silent empty result. Same for a response whose utterances all fail
  field validation (provider drift): that must fail the call, not
  masquerade as a muted meeting.
- The batch endpoint is synchronous: the response IS the final result
  including diarization. The adapter never moves to a callback/202 flow
  (that would re-introduce a "transcript finished" ≠ "diarization
  finished" race).
- Speaker labels come back as provider ints (0, 1, …) and are stored
  as-is per the webhook contract's rendering: `S0`, `S1`. Mapping to
  real names is the AI layer's job, out of scope.
- An empty segments list is legitimate (a call where nobody spoke); the
  call still lands `done` and the webhook carries `transcript: []`.

### Error mapping

| Status | Class | Runner behavior |
|---|---|---|
| 429, 500–599, timeout, network | transient | 3 attempts, backoff between attempts (1 s, 4 s) |
| 400, 401 | permanent | immediate `failed` |
| **402** | permanent | immediate `failed` — insufficient credits, operator action required (no retry helps) |
| other 4xx | permanent | immediate `failed` |
| 3xx (e.g. a redirect on the pinned POST endpoint) | permanent | immediate `failed` — the endpoint is pinned; redirects are contract drift |

Failure reason format: `transcription_failed:<detail>` (status codes and
counts only — never transcript text). `transcription_failed:source_
unavailable` covers the rare case of the stored object vanishing after
a successful upload.

### Size guard

`AUDIO_MAX_BYTES` (default 2 GB, matching Deepgram's own pre-recorded
file-size limit) refuses transcription above the limit before any
request: `transcription_failed:audio too large: N bytes > AUDIO_MAX_BYTES (M)`.

## Dev/staging stub provider

`TRANSCRIPTION_PROVIDER=stub` (or unset outside production) selects a
stub that performs **no I/O and returns an honest empty transcript** —
every call lands `done` with `transcript: []`. It exists so the full
runner flow runs before any credentials land. It is never valid in
production: the runner fail-fasts at startup. The unset-outside-
production fallback warns loudly, like the local storage fallback.

## Realtime (phase 3) — PENDING CONFIRMATION

Deepgram realtime (streaming) is reserved for the phase 3 live trainer
and stays on the **same account/key**; PR 7 builds only the batch path
(`supports_realtime = True` on the adapter, pointing at
https://developers.deepgram.com/docs/live-streaming-audio/).

**Confirmation owed:** a dashboard/docs link or screenshot proving
realtime is enabled on the same API key used for batch. Place the link
or screenshot reference here when the `DEEPGRAM_API_KEY` lands.

## Transcript storage

`Call.transcript` is JSONB on Postgres (plain JSON on SQLite tests):
a list of segments round-tripped through the Pydantic model —
Postgres-specific JSONB operators are never used in service code.
Migration: `20260917_b81e0f4c7d25_transcript_jsonb` (converts the
PR 5-era JSON column in place; validated with a real transcript row).

Sample transcript (one webhook payload field, pretty-printed):

```json
[
  {"speaker": "S0", "start": 0.1,  "end": 1.4, "text": "Testing one two three, can you hear me?"},
  {"speaker": "S1", "start": 1.8,  "end": 2.9, "text": "Loud and clear."},
  {"speaker": "S0", "start": 3.2,  "end": 5.1, "text": "Great — I'll walk through the agenda."}
]
```

`start`/`end` are seconds from the start of the recording.

## Whisper

**Whisper was considered and rejected** — it streams poorly, and the
phase 3 live trainer needs realtime. It appears in no dependency file
by standing rule.

## Logging

Transcript text, audio bytes, recording paths, stored-object keys, and
presigned URLs never appear in logs — only lengths and counts
(`segments=N`, byte sizes). See the standing rules in `AGENTS.md`.
