import enum


class CallPlatform(enum.StrEnum):
    google_meet = "google_meet"


class CallStatus(enum.StrEnum):
    queued = "queued"
    joining = "joining"
    recording = "recording"
    processing = "processing"
    done = "done"
    failed = "failed"


ACTIVE_STATUSES: tuple[CallStatus, ...] = (
    CallStatus.queued,
    CallStatus.joining,
    CallStatus.recording,
    CallStatus.processing,
)

TERMINAL_STATUSES: tuple[CallStatus, ...] = (CallStatus.done, CallStatus.failed)
