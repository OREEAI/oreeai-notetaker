import enum


class MeetingPlatform(enum.StrEnum):
    google_meet = "google_meet"
    zoom = "zoom"
    in_person = "in_person"
    other = "other"


class MeetingStatus(enum.StrEnum):
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"
