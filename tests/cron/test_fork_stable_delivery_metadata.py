"""FN metadata survives stable's restart-safe delivery queue without leaking paths."""
from cron import delivery_queue, scheduler_delivery


def test_external_worker_queues_only_validated_output_reference(monkeypatch):
    captured = []
    monkeypatch.setenv('_HERMES_CRON_EXTERNAL_WORKER', 'fixture-execution')
    monkeypatch.setattr(scheduler_delivery, '_resolve_delivery_targets', lambda *a, **k: [{'platform': 'telegram', 'chat_id': 'fixture'}])
    monkeypatch.setattr(delivery_queue, 'enqueue_and_wait', lambda *args, **kw: captured.append(args) or None)
    job = {'id': 'fixture', 'execution_id': 'fixture-execution'}
    scheduler_delivery._deliver_result(job, 'failed', output_ref='2026-08-30_10-20-31.md', cron_process_failed=True, for_failure=True)
    queued = captured[0][1]
    assert queued['_cron_output_ref'] == '2026-08-30_10-20-31.md'
    assert queued['_cron_process_failed'] is True
    assert '_cron_output_ref' not in job

    captured.clear()
    scheduler_delivery._deliver_result(job, 'failed', output_ref='/private/2026-08-30_10-20-31.md', for_failure=True)
    assert '_cron_output_ref' not in captured[0][1]
