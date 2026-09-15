"""Audio retention enforcement (PR 6) — the policy owner.

Deletes stored audio past the configured retention windows and nulls
``audio_url``; the row's transcript is never touched.

Windows (env-configurable):

- ``AUDIO_RETENTION_DAYS`` (default 0) for ``done`` calls. 0 means the
  object is eligible the moment the call lands in ``done`` — the
  runner's immediate sweep (fired right after the webhook, so the
  payload still delivered the URI snapshot) makes the audio disappear
  at once, and this sweep is the enforcement path for it.
- ``FAILED_AUDIO_RETENTION_DAYS`` (default 7) for ``failed`` calls —
  relevant from PR 7 on, when an upload can precede the failure
  (transcription permanently failing after a successful upload).

Enforcement rules:

- ``recording`` (and every other non-eligible status) is never touched.
- Object deletion is idempotent (missing object = no-op success), so a
  missing object never crashes the sweep.
- Any per-row error is logged loudly and skipped — the row keeps its
  ``audio_url`` and is retried next tick. The sweep itself never
  raises; the runner loop and the immediate post-done call must never
  crash because of retention.
- Rows locked by another transaction (``FOR UPDATE SKIP LOCKED``, the
  same primitive as the runner's claim) are skipped and retried next
  tick.

The object key is recomputed inside the adapter from the call id (the
canonical ``audio_key`` layout) — the stored ``audio_url`` is never
parsed.

Wired process-local for phase 1: the bot-runner main loop calls this
every ``RETENTION_INTERVAL_S`` (and once immediately after each
``done``); the call site stays in ``workers/`` for the later queue swap
(Celery beat / ARQ cron). Run it by hand with::

    python -c "import asyncio; from oreeai_notetaker.workers.retention \\
        import enforce_retention; print(asyncio.run(enforce_retention()))"
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oreeai_notetaker.core.config import settings
from oreeai_notetaker.db.session import session_factory
from oreeai_notetaker.enums import CallStatus
from oreeai_notetaker.integrations.object_storage.base import ConfigurationError
from oreeai_notetaker.models.call import Call
from oreeai_notetaker.services.storage import (
    ObjectStorageService,
    build_object_storage_service,
)

logger = logging.getLogger("oreeai.retention")

RETENTION_INTERVAL_S = 60.0


async def enforce_retention(storage: ObjectStorageService | None = None) -> int:
    """Delete expired audio objects and null their ``audio_url``.

    Returns the number of rows reclaimed; never raises. ``storage``
    defaults to the process-cached service (tests inject a fake).
    """
    if storage is None:
        try:
            storage = build_object_storage_service()
        except ConfigurationError as exc:
            logger.error("retention skipped: storage misconfigured: %s", exc)
            return 0

    now = datetime.now(UTC)
    reclaimed = 0
    async with session_factory() as session:
        done_rows = await _expired_rows(
            session, CallStatus.done, settings.audio_retention_days, now
        )
        failed_rows = await _expired_rows(
            session, CallStatus.failed, settings.failed_audio_retention_days, now
        )
        # Capture plain identity up front: any later rollback expires every
        # loaded instance, so ORM attribute access must not happen after
        # the first failure — the loop below uses plain values only.
        expired = [(row.id, row.status.value) for row in [*done_rows, *failed_rows]]

    for call_id, status_value in expired:
        async with session_factory() as session:
            try:
                # Re-claim the row (same locking primitive as the runner's
                # claim): locked rows are skipped and retried next tick.
                claimed = await session.execute(
                    select(Call.id).where(Call.id == call_id).with_for_update(skip_locked=True)
                )
                if claimed.scalar_one_or_none() is None:
                    logger.info("retention skipped locked call %s; retrying next tick", call_id)
                    continue
                await storage.delete_for_call(call_id)
                # Bulk UPDATE by primary key: immune to instance expiry and
                # scoped to exactly this row, even if the provider is slow.
                await session.execute(update(Call).where(Call.id == call_id).values(audio_url=None))
                await session.commit()
                reclaimed += 1
                logger.info(
                    "retention reclaimed audio for call %s (status=%s)",
                    call_id,
                    status_value,
                )
            except Exception:
                await session.rollback()
                logger.exception("retention could not reclaim call %s; retrying next tick", call_id)
    return reclaimed


async def _expired_rows(
    session: AsyncSession, status: CallStatus, retention_days: int, now: datetime
) -> list[Call]:
    """Terminal calls past their retention cutoff with audio to reclaim.

    Locks are skipped, not awaited: a row mid-update elsewhere (or held
    by another sweep) stays out of this pass and is retried next tick.
    """
    cutoff = now - timedelta(days=retention_days)
    stmt = (
        select(Call)
        .where(
            Call.status == status,
            Call.audio_url.is_not(None),
            Call.updated_at < cutoff,
        )
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
