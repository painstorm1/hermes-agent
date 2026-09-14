"""Exact scheduled identities, independent of mutable jobs.json dispatch stamps."""
from datetime import datetime, timezone


class OccurrenceLookupError(RuntimeError):
    """Completion is unknown; neither execution nor consuming the slot is safe."""


def scheduled_instant(value):
    """Canonicalize aware instants; legacy/ambiguous values carry no exact identity."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            return None
        return instant.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def completed_occurrence(job, instant):
    """Return completion evidence, or raise when the ledger cannot establish it."""
    from cron.executions import _transaction, execution_row_fingerprint

    instant = scheduled_instant(instant)
    if instant is None:
        return False
    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM executions WHERE job_id=? AND scheduled_instant=? "
                "AND status='completed'", (str(job['id']), instant)
            ).fetchall()
            for row in rows:
                annotation = conn.execute(
                    "SELECT 1 FROM manual_occurrence_annotations "
                    "WHERE execution_id=? AND job_id=? AND scheduled_instant=? AND row_fingerprint=? "
                    "AND length(trim(manual_request_receipt_ref)) > 0 "
                    "AND length(trim(reason)) > 0 AND length(trim(approval_ref)) > 0",
                    (row['id'], row['job_id'], instant, execution_row_fingerprint(row)),
                ).fetchone()
                if annotation is None:
                    return True
            return False
    except Exception as exc:
        raise OccurrenceLookupError(
            f"Cannot check completed occurrence for job {job['id']}: {exc}") from exc
