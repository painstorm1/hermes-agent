"""Completed scheduled attempts survive a jobs.json rollback, not just a process restart."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_FIRE = """
import json
import os
from pathlib import Path
import subprocess
import sys
from cron import jobs, scheduler
from cron.scheduler_provider import InProcessCronScheduler
if sys.argv[1] == 'builtin':
    scheduler.tick(verbose=False, sync=True)
else:
    provider = InProcessCronScheduler()
    for job in jobs.load_jobs():
        if sys.argv[1] != 'worker':
            provider.fire_due(job['id'], **({'occurrence': None} if sys.argv[1] == 'manual' else {}))
            continue
        from cron.executions import mark_execution_handoff_pending
        claim = provider.claim_fire(job['id'])
        if claim is None:
            continue
        mark_execution_handoff_pending(claim['execution_id'])
        home = Path(os.environ['HERMES_HOME'])
        payload = home / (claim['execution_id'] + '.json')
        ack = home / (claim['execution_id'] + '.ready')
        payload.write_text(json.dumps({'job': claim, 'profile_home': str(home),
                                       'multiplex_active': False}))
        subprocess.run([sys.executable, '-m', 'cron.scheduler',
                        '--external-worker-file', str(payload), '--ack-file', str(ack)],
                       stdin=subprocess.DEVNULL, check=True, timeout=60)
        assert json.loads(ack.read_text())['pid'] != os.getpid()
"""


def _fire(home, mode):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('HERMES_', '_HERMES_'))
           and not k.endswith(('_API_KEY', '_TOKEN'))}
    env['HERMES_HOME'] = str(home)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, '-c', _FIRE, mode], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('mode', ['builtin', 'provider', 'worker'])
def test_completed_occurrence_survives_restart_and_prestamp_rollback(tmp_path, mode):
    from datetime import timedelta
    from hermes_time import now

    home = tmp_path / mode
    cron = home / 'cron'
    cron.mkdir(parents=True)
    effect = home / 'effects.txt'
    (home / 'scripts').mkdir()
    script = home / 'scripts' / 'effect.py'
    script.write_text(f"from pathlib import Path\np = Path({str(effect)!r})\n"
                      "with p.open('a') as f: f.write('effect\\n')\nprint('done')\n")
    slot = (now() - timedelta(minutes=30)).isoformat()
    job = {'id': 'occurrence', 'name': 'occurrence', 'prompt': '',
           'schedule': {'kind': 'interval', 'minutes': 240},
           'next_run_at': slot, 'enabled': True, 'state': 'scheduled',
           'script': str(script), 'no_agent': True, 'deliver': 'local',
           'repeat': {'times': None, 'completed': 0}}
    snapshot = json.dumps({'jobs': [job]})
    store = cron / 'jobs.json'
    store.write_text(snapshot)
    _fire(home, mode)
    assert effect.read_text().splitlines() == ['effect']
    store.write_text(snapshot)  # no last_dispatch stamp, no post-completion fields
    _fire(home, mode)  # a fresh interpreter, same authoritative ledger
    assert effect.read_text().splitlines() == ['effect'], 'completed occurrence executed twice'

    # A different missed slot must remain runnable despite the completed row.
    job['next_run_at'] = (now() - timedelta(minutes=10)).isoformat()
    store.write_text(json.dumps({'jobs': [job]}))
    _fire(home, mode)
    assert len(effect.read_text().splitlines()) == 2
    # Manual force is not a completion of the pending scheduled slot.
    job['next_run_at'] = (now() - timedelta(minutes=5)).isoformat()
    pending = json.dumps({'jobs': [job]})
    store.write_text(pending)
    _fire(home, 'manual')
    store.write_text(pending)
    _fire(home, mode)
    assert len(effect.read_text().splitlines()) == 4


def test_ledger_migration_and_completion_identity(tmp_path, monkeypatch):
    import sqlite3
    from cron import executions, jobs
    from cron.occurrences import completed_occurrence
    from cron.scheduler_provider import InProcessCronScheduler

    db = tmp_path / 'executions.db'
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', db)
    # Pre-migration schema, not initialized by the implementation under test.
    with sqlite3.connect(db) as conn:
        conn.execute('''CREATE TABLE executions (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
            process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
            status TEXT NOT NULL, claimed_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, error TEXT)''')
        conn.execute("INSERT INTO executions VALUES "
                     "('legacy','job','builtin','old',-1,NULL,'completed',"
                     "'2026-01-01T00:00:00Z',NULL,NULL,NULL)")
    slot = '2026-01-01T00:00:00+00:00'
    job = {'id': 'job'}
    assert not completed_occurrence(job, slot)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT id, scheduled_instant FROM executions').fetchall() == [('legacy', None)]
    for status in ('failed', 'unknown', 'running', 'claimed'):
        row = executions.create_execution('job', source='control', scheduled_instant=slot)
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE executions SET status=? WHERE id=?', (status, row['id']))
        assert not completed_occurrence(job, slot)
    row = executions.create_execution('job', source='builtin', scheduled_instant=slot)
    executions.finish_execution(row['id'], success=True)
    executions.create_execution('job', source='manual')  # latest row is not the completed one
    assert completed_occurrence(job, '2026-01-01T08:00:00+0800')
    assert not completed_occurrence(job, '2026-01-01T00:00:01Z')
    assert not completed_occurrence({'id': 'other'}, slot)

    # Provider capture is before next_run_at advances; adoption cannot lose identity.
    with jobs.use_cron_store(tmp_path / 'cron'):
        stored = jobs.create_job(prompt='test', schedule='every 4h')
        rows = jobs.load_jobs()
        rows[0]['next_run_at'] = slot
        jobs.save_jobs(rows)
        claim = InProcessCronScheduler().claim_fire(stored['id'])
        assert claim is not None
        assert claim['_scheduled_instant'] == slot
        assert claim['next_run_at'] != slot
        assert '_scheduled_instant' not in jobs.load_jobs()[0]
        executions.mark_execution_handoff_pending(claim['execution_id'])
        adopted = executions.adopt_claimed_execution(claim['execution_id'])
        assert adopted is not None
        assert adopted['scheduled_instant'] == slot

        # A runnable legacy wall-clock value cannot establish an exact UTC identity.
        from datetime import timedelta
        from hermes_time import now
        naive = jobs.create_job(prompt='legacy', schedule='every 4h')
        rows = jobs.load_jobs()
        for item in rows:
            if item['id'] == naive['id']:
                item['next_run_at'] = (now() - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
        jobs.save_jobs(rows)
        assert not any(item['id'] == naive['id'] for item in jobs.get_due_jobs())
        assert jobs.claim_job_for_fire(naive['id']) is False


@pytest.fixture
def occurrence_store(tmp_path, monkeypatch):
    from datetime import datetime
    from cron import executions, jobs, scheduler
    from tools import cronjob_tools

    clock = datetime.fromisoformat('2026-02-03T10:18:00+09:00')
    monkeypatch.setattr(jobs, '_hermes_now', lambda: clock)
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'executions.db')
    for name in ('_maybe_run_worktree_maintenance', '_sweep_mcp_orphans', '_maybe_reap_dead_owners'):
        monkeypatch.setattr(scheduler, name, lambda: None)
    monkeypatch.setattr(scheduler, '_should_yield_tick_to_fresh_gateway', lambda: None)
    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lambda job: False)
    monkeypatch.setattr(cronjob_tools, '_notify_provider_jobs_changed_safe', lambda: None)
    ran = []
    monkeypatch.setattr(scheduler, 'run_job', lambda job, **kw: ran.append(dict(job)) or (True, 'fixture', 'fixture', None))
    monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: None)
    with jobs.use_cron_store(tmp_path):
        job = jobs.create_job(prompt='offline fixture', schedule='30 9 * * *', deliver='local')
        yield jobs, executions, scheduler, job, ran
    scheduler._shutdown_parallel_pool()


@pytest.mark.parametrize('mode', ['sync', 'background', 'pool-fallback'])
def test_direct_manual_callers_do_not_complete_next_regular_slot(occurrence_store, monkeypatch, mode):
    from tools import cronjob_tools as tools
    from tools import async_delegation
    from gateway import session_context
    jobs, executions, scheduler, job, ran = occurrence_store
    before = jobs.get_job(job['id'])['next_run_at']
    monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: True)
    monkeypatch.setattr(tools, '_background_session_key', lambda sid: 'offline-session')
    queued = []

    def dispatch(**kwargs):
        queued.append(kwargs['runner'])
        return {'status': 'dispatched', 'delegation_id': 'fixture'} if mode == 'background' else {'error': 'full'}

    monkeypatch.setattr(async_delegation, 'dispatch_async_delegation', dispatch)
    if mode == 'sync':
        assert tools._execute_job_now(job)['success']
    else:
        result = tools._try_dispatch_background_run(job, session_id='fixture')
        assert result['claimed']
        if mode == 'background':
            assert not ran
            queued[0]()
        else:
            assert result['success']
    assert len(ran) == 1
    assert ran[0]['fire_claim']['by']
    assert ran[0]['_scheduled_instant'] is None
    row = executions.latest_execution(job['id'])
    assert row['status'] == 'completed' and row['scheduled_instant'] is None
    assert jobs.get_job(job['id'])['next_run_at'] == before
    from cron.occurrences import completed_occurrence
    assert not completed_occurrence(job, before)


@pytest.mark.parametrize('mode', ['trigger', 'finite', 'console', 'relay', 'scheduled'])
def test_tick_claim_uses_identity_captured_before_advance(occurrence_store, monkeypatch, mode):
    from tools import cronjob_tools as tools
    from gateway import session_context
    from cron.occurrences import scheduled_instant
    jobs, executions, scheduler, job, ran = occurrence_store
    # Reproduce a future completion that must not be borrowed after tick's pre-advance.
    future = executions.create_execution(job['id'], source='direct', scheduled_instant=job['next_run_at'])
    executions.finish_execution(future['id'], success=True)
    if mode == 'finite':
        monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: False)
        result = json.loads(tools.cronjob(action='run', job_id=job['id'], session_id='fixture-' + mode))
        assert result['job']['execution_mode'] == 'scheduler'
    elif mode == 'console':
        from hermes_cli.console_engine import _cron_run
        assert 'Triggered' in _cron_run(None, [job['id']])
    elif mode == 'relay':
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from aiohttp.test_utils import make_mocked_request
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'fixture-key'}))
        monkeypatch.setattr(tools, '_relay_fronted_delivery_platforms', lambda j: {'discord'})
        monkeypatch.setattr('agent.secret_scope.get_secret', lambda *a: 'fixture-key')

        def post(url, *, headers, json, timeout):
            assert url.endswith(f'/api/jobs/{job["id"]}/run')
            request = make_mocked_request('POST', url, headers=headers, match_info={'job_id': job['id']})
            request.json = AsyncMock(return_value=json)
            response = asyncio.run(adapter._handle_run_job(request))
            return SimpleNamespace(status_code=response.status)

        monkeypatch.setattr('httpx.post', post)
        result = json.loads(tools._forward_relay_fronted_run(job))
        assert result['forwarded_to_gateway']
    elif mode == 'trigger':
        jobs.trigger_job(job['id'])
    else:
        rows = jobs.load_jobs()
        rows[0]['next_run_at'] = '2026-02-03T09:30:00+09:00'
        jobs.save_jobs(rows)
    snapshot = jobs.get_job(job['id'])
    expected = scheduled_instant(snapshot['next_run_at']) if mode == 'scheduled' else None
    original_claim = scheduler.claim_job_for_fire
    claims = []

    def claim(jid, **kwargs):
        claims.append(kwargs)
        assert jobs.get_job(jid)['next_run_at'] != snapshot['next_run_at']
        return original_claim(jid, **kwargs)

    monkeypatch.setattr(scheduler, 'claim_job_for_fire', claim)
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert len(ran) == 1 and ran[0]['_scheduled_instant'] == expected
    assert claims[0]['occurrence'] == expected
    row = executions.latest_execution(job['id'])
    assert row['status'] == 'completed' and row['scheduled_instant'] == expected


@pytest.mark.parametrize('replacement', [None, '2026-02-03T10:18:01+09:00'])
def test_cancelled_or_retriggered_manual_snapshot_loses_claim(occurrence_store, replacement):
    jobs, executions, scheduler, job, ran = occurrence_store
    jobs.trigger_job(job['id'])
    due = jobs.get_due_jobs()[0]
    jobs.advance_next_runs([job['id']])
    rows = jobs.load_jobs()
    rows[0]['manual_run_at'] = replacement
    jobs.save_jobs(rows)
    before = jobs.get_job(job['id'])
    row = executions.create_execution(job['id'], source='builtin')
    scheduler._process_due_job(dict(due, execution_id=row['id']), None, None, False)
    assert not ran
    assert jobs.get_job(job['id']) == before
    assert executions.get_execution(row['id'])['status'] == 'failed'


@pytest.mark.parametrize('value', ['not-a-time', '2026-02-04T09:30:00', '', 5, {}])
def test_explicit_invalid_occurrence_never_claims_or_resumes(occurrence_store, value):
    jobs, executions, scheduler, job, ran = occurrence_store
    jobs.pause_job(job['id'])
    before = jobs.get_job(job['id'])
    with pytest.raises(ValueError, match='aware'):
        jobs.claim_job_for_fire(job['id'], occurrence=value, force=True)
    assert jobs.get_job(job['id']) == before


def test_manual_identity_does_not_grant_force_or_override_live_claim(occurrence_store):
    jobs, executions, scheduler, job, ran = occurrence_store
    jobs.pause_job(job['id'])
    assert jobs.claim_job_for_fire(job['id'], occurrence=None) is False
    claim = jobs.claim_job_for_fire(job['id'], occurrence=None, force=True, return_job=True)
    assert claim['_scheduled_instant'] is None and jobs.is_job_runnable(claim)
    assert jobs.claim_job_for_fire(job['id'], occurrence=None, force=True) is False
    assert jobs.claim_job_for_fire(job['id'], occurrence=job['next_run_at']) is False


def test_force_without_occurrence_does_not_mean_manual(occurrence_store):
    jobs, executions, scheduler, job, ran = occurrence_store
    attempt = executions.create_execution(job['id'], source='direct', scheduled_instant=job['next_run_at'])
    executions.finish_execution(attempt['id'], success=True)
    assert jobs.claim_job_for_fire(job['id'], force=True) is False
    claim = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
    assert claim['_scheduled_instant'] is None


def test_concurrent_manual_and_scheduled_fire_share_one_claim(occurrence_store):
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    jobs, executions, scheduler, job, ran = occurrence_store
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(contextvars.copy_context().run, jobs.claim_job_for_fire,
                                job['id'], occurrence=instant)
                    for instant in (None, job['next_run_at'])]
        assert sorted(attempt.result() for attempt in attempts) == [False, True]


def test_verified_annotation_is_exact_append_only_and_preserves_original(occurrence_store, monkeypatch):
    import sqlite3
    from cron.occurrences import completed_occurrence, scheduled_instant
    jobs, executions, scheduler, job, ran = occurrence_store
    slot = scheduled_instant(job['next_run_at'])
    attempt = executions.create_execution(job['id'], source='direct', scheduled_instant=slot)
    original = executions.finish_execution(attempt['id'], success=True)
    original_bytes = json.dumps(original, sort_keys=True).encode()
    assert completed_occurrence(job, slot)
    evidence = dict(execution_id=original['id'], job_id=job['id'], scheduled_instant=slot,
                    row_fingerprint=executions.execution_row_fingerprint(original),
                    manual_request_receipt_ref='fixture-session/request-and-response/sha256:reviewed',
                    reason='Reviewed explicit run request was bound to a future scheduled slot',
                    approval_ref='fixture-operator-approval')
    for field, bad in [('execution_id', 'absent'), ('job_id', 'other'),
                       ('scheduled_instant', '2026-02-05T00:30:00Z'), ('row_fingerprint', 'wrong'),
                       ('manual_request_receipt_ref', ''), ('approval_ref', ''), ('reason', '')]:
        with pytest.raises(ValueError):
            executions.record_manual_occurrence_annotation(**{**evidence, field: bad})
        assert completed_occurrence(job, slot)
    annotation = executions.record_manual_occurrence_annotation(**evidence)
    assert executions.record_manual_occurrence_annotation(**evidence) == annotation
    with pytest.raises(ValueError, match='Conflicting'):
        executions.record_manual_occurrence_annotation(**{**evidence, 'reason': 'different'})
    assert not completed_occurrence(job, job['next_run_at'])  # KST == UTC
    repaired = jobs.claim_job_for_fire(job['id'], occurrence=job['next_run_at'], return_job=True)
    assert repaired['_scheduled_instant'] == slot
    jobs.mark_job_run(job['id'], True, expected_fire_owner=repaired['fire_claim']['by'])
    assert json.dumps(executions.get_execution(original['id']), sort_keys=True).encode() == original_bytes
    with executions._transaction() as conn:
        for sql in ['DELETE FROM manual_occurrence_annotations', 'UPDATE manual_occurrence_annotations SET reason="other"']:
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                conn.execute(sql)
        # A rollback or later source change invalidates the annotation, not the completion guard.
        conn.execute('UPDATE executions SET error=? WHERE id=?', ('changed fixture', original['id']))
    assert completed_occurrence(job, slot)
    with executions._transaction() as conn:
        conn.execute('UPDATE executions SET error=NULL WHERE id=?', (original['id'],))
    # Normal scheduled source=direct must still block the very same slot.
    normal = executions.create_execution(job['id'], source='direct', scheduled_instant=slot)
    executions.finish_execution(normal['id'], success=True)
    assert completed_occurrence(job, slot)
    assert jobs.claim_job_for_fire(job['id'], occurrence=job['next_run_at']) is False
    # Retention cannot delete the reviewed source row out from under its audit evidence.
    monkeypatch.setattr(executions, 'MAX_TERMINAL_EXECUTIONS', 0)
    with executions._transaction() as conn:
        executions._prune_unlocked(conn)
    assert json.dumps(executions.get_execution(original['id']), sort_keys=True).encode() == original_bytes
    assert not completed_occurrence(job, slot)


@pytest.mark.parametrize('status', ['claimed', 'running', 'failed', 'unknown'])
def test_annotation_rejects_noncompleted_original(occurrence_store, status):
    jobs, executions, scheduler, job, ran = occurrence_store
    row = executions.create_execution(job['id'], source='direct', scheduled_instant=job['next_run_at'])
    with executions._transaction() as conn:
        conn.execute('UPDATE executions SET status=? WHERE id=?', (status, row['id']))
    row = executions.get_execution(row['id'])
    with pytest.raises(ValueError, match='completed execution'):
        executions.record_manual_occurrence_annotation(
            execution_id=row['id'], job_id=row['job_id'], scheduled_instant=row['scheduled_instant'],
            row_fingerprint=executions.execution_row_fingerprint(row), manual_request_receipt_ref='fixture-request',
            reason='fixture', approval_ref='fixture-approval')


@pytest.mark.parametrize('original', ['manual', 'scheduled'])
def test_retrigger_before_tick_advance_keeps_new_request_due(occurrence_store, monkeypatch, original):
    from datetime import timedelta
    jobs, executions, scheduler, job, ran = occurrence_store
    if original == 'manual':
        jobs.trigger_job(job['id'])
    else:
        jobs.update_job(job['id'], {'next_run_at': '2026-02-03T09:30:00+09:00'})
    old_due = jobs.get_due_jobs()[0]
    later = jobs._hermes_now() + timedelta(seconds=1)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: later)
    replacement = jobs.trigger_job(job['id'], extra_prompt='new manual request')
    monkeypatch.setattr(scheduler, 'get_due_jobs', lambda: [old_due])
    scheduler.tick(verbose=False, sync=True)
    assert not ran
    persisted = jobs.get_job(job['id'])
    assert persisted['next_run_at'] == persisted['manual_run_at'] == replacement['manual_run_at']
    assert persisted['manual_run_prompt'] == 'new manual request'
    monkeypatch.setattr(scheduler, 'get_due_jobs', jobs.get_due_jobs)
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert len(ran) == 1 and ran[0]['_scheduled_instant'] is None
    assert ran[0]['manual_run_prompt'] == 'new manual request'
    assert executions.latest_execution(job['id'])['scheduled_instant'] is None
    assert scheduler.tick(verbose=False, sync=True) == 0


@pytest.mark.parametrize('mode', ['due-scan', 'claim-inferred', 'claim-explicit'])
def test_corrupt_annotation_lookup_cannot_authorize_duplicate(occurrence_store, monkeypatch, caplog, mode):
    jobs, executions, scheduler, job, ran = occurrence_store
    slot = '2026-02-03T09:30:00+09:00'
    jobs.update_job(job['id'], {'next_run_at': slot})
    before = jobs.get_job(job['id'])
    assert executions.list_executions(job_id=job['id']) == []

    def unavailable():
        raise OSError('fixture ledger unavailable')

    with monkeypatch.context() as fault:
        fault.setattr(executions, '_connect', unavailable)
        if mode == 'due-scan':
            assert scheduler.tick(verbose=False, sync=True) == 0
            assert 'fixture ledger unavailable' in caplog.text
        else:
            with pytest.raises(RuntimeError, match='completed occurrence'):
                jobs.claim_job_for_fire(job['id'], **({'occurrence': slot} if mode == 'claim-explicit' else {}))
    assert not ran
    assert jobs.get_job(job['id']) == before
    assert executions.list_executions(job_id=job['id']) == []
    due = jobs.get_due_jobs()
    assert len(due) == 1 and due[0]['next_run_at'] == slot
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert len(ran) == 1


@pytest.mark.parametrize('phase', ['create', 'claim', 'claim-persistent'])
@pytest.mark.parametrize('slot', ['2026-02-03T09:30:00+09:00', '2026-02-02T09:30:00+09:00'])
def test_lookup_outage_after_advance_preserves_unstarted_slot(occurrence_store, monkeypatch, caplog, phase, slot):
    from unittest.mock import patch
    from cron.occurrences import scheduled_instant
    jobs, executions, scheduler, job, ran = occurrence_store
    jobs.update_job(job['id'], {'next_run_at': slot})
    real_advance, real_claim = scheduler.advance_next_runs, scheduler.claim_job_for_fire

    def unavailable():
        raise OSError('fixture post-advance ledger outage')

    with monkeypatch.context() as fault:
        def advance(*args, **kwargs):
            result = real_advance(*args, **kwargs)
            assert jobs.get_job(job['id'])['next_run_at'] != slot
            if phase == 'create':
                fault.setattr(executions, '_connect', unavailable)
            return result

        def claim(*args, **kwargs):
            assert jobs.get_job(job['id'])['next_run_at'] != slot
            if phase == 'claim-persistent':
                fault.setattr(executions, '_connect', unavailable)
                return real_claim(*args, **kwargs)
            with patch.object(executions, '_connect', unavailable):
                return real_claim(*args, **kwargs)

        fault.setattr(scheduler, 'advance_next_runs', advance)
        fault.setattr(scheduler, 'claim_job_for_fire', claim)
        scheduler.tick(verbose=False, sync=True)
    after = jobs.get_job(job['id'])
    assert not ran and not after.get('fire_claim')
    assert after['next_run_at'] == slot
    assert 'fixture post-advance ledger outage' in caplog.text
    rows = executions.list_executions(job_id=job['id'])
    assert len(rows) == (0 if phase == 'create' else 1)
    if rows:
        assert rows[0]['status'] == ('claimed' if phase == 'claim-persistent' else 'failed')
        assert rows[0]['scheduled_instant'] == scheduled_instant(slot)
        if phase == 'claim':
            assert 'completed occurrence' in rows[0]['error']
        else:
            assert 'Could not record blocked occurrence lookup' in caplog.text
    due = jobs.get_due_jobs()
    assert len(due) == 1 and due[0]['_scheduled_instant'] == scheduled_instant(slot)
    # Use this exact due snapshot: an overdue scan itself may have pre-advanced the store.
    with monkeypatch.context() as recovered:
        recovered.setattr(scheduler, 'get_due_jobs', lambda: due)
        assert scheduler.tick(verbose=False, sync=True) == 1
    assert len(ran) == 1 and ran[0]['_scheduled_instant'] == scheduled_instant(slot)
    assert executions.latest_execution(job['id'])['status'] == 'completed'
    assert scheduler.tick(verbose=False, sync=True) == 0


@pytest.mark.parametrize('replacement', ['manual', 'pause', 'schedule', 'claim', 'completion', 'terminal', 'no-receipt'])
def test_failed_dispatch_does_not_restore_over_newer_record(occurrence_store, monkeypatch, replacement):
    jobs, executions, scheduler, job, ran = occurrence_store
    slot = '2026-02-02T09:30:00+09:00'
    jobs.update_job(job['id'], {'next_run_at': slot})
    due = jobs.get_due_jobs()
    real_advance = scheduler.advance_next_runs
    if replacement == 'no-receipt':
        due[0].pop('_dispatch_record')
    expected = []

    def unavailable():
        raise OSError('fixture ledger outage after concurrent change')

    with monkeypatch.context() as fault:
        def advance(*args, **kwargs):
            result = real_advance(*args, **kwargs)
            if replacement == 'manual':
                jobs.trigger_job(job['id'], extra_prompt='preserve replacement')
            elif replacement == 'pause':
                jobs.pause_job(job['id'])
            elif replacement == 'schedule':
                jobs.update_job(job['id'], {'schedule': {'kind': 'interval', 'minutes': 60}})
            elif replacement in ('claim', 'completion'):
                claim = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
                assert claim
                if replacement == 'completion':
                    jobs.mark_job_run(job['id'], True, expected_fire_owner=claim['fire_claim']['by'])
            elif replacement == 'terminal':
                jobs.update_job(job['id'], {'state': 'completed', 'enabled': False})
            expected.append(jobs.get_job(job['id']))
            fault.setattr(executions, '_connect', unavailable)
            return result

        fault.setattr(scheduler, 'get_due_jobs', lambda: due)
        fault.setattr(scheduler, 'advance_next_runs', advance)
        scheduler.tick(verbose=False, sync=True)
    assert not ran
    assert jobs.get_job(job['id']) == expected[0]
    assert executions.list_executions(job_id=job['id']) == []


@pytest.mark.parametrize('paused', [False, True])
@pytest.mark.parametrize('provider_kind', [
    'builtin', 'explicit', 'legacy-fire', 'legacy-claim', 'legacy-fire-kwargs', 'legacy-claim-kwargs',
])
def test_dashboard_trigger_route_passes_explicit_manual_or_fails_closed(
    occurrence_store, monkeypatch, paused, provider_kind,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_cli import web_server_cron
    from hermes_cli.web_routers import cron as routes
    jobs, executions, scheduler, job, ran = occurrence_store
    if paused:
        jobs.pause_job(job['id'])
    before = jobs.get_job(job['id'])
    home = jobs._current_cron_store().cron_dir.parent
    monkeypatch.setattr(web_server_cron, '_cron_profile_home', lambda profile: (profile, home))
    monkeypatch.setattr(routes, '_job_profile', lambda jid, profile: 'fixture')
    provider = InProcessCronScheduler()
    if provider_kind == 'legacy-fire':
        class LegacyFire(InProcessCronScheduler):
            def fire_due(self, job_id, *, force=False, adapters=None, loop=None):
                pytest.fail('legacy scheduled-only provider must not run a manual request')
        provider = LegacyFire()
    elif provider_kind == 'legacy-claim':
        class LegacyClaim(InProcessCronScheduler):
            def claim_fire(self, job_id, *, force=False):
                pytest.fail('inherited fire_due must not hide a legacy claim override')
        provider = LegacyClaim()
    elif provider_kind == 'legacy-fire-kwargs':
        class LegacyFireKwargs(InProcessCronScheduler):
            def fire_due(self, job_id, *, force=False, adapters=None, loop=None, **kwargs):
                return super().fire_due(job_id, force=force, adapters=adapters, loop=loop)
        provider = LegacyFireKwargs()
    elif provider_kind == 'legacy-claim-kwargs':
        class LegacyClaimKwargs(InProcessCronScheduler):
            def claim_fire(self, job_id, *, force=False, **kwargs):
                return super().claim_fire(job_id, force=force)
        provider = LegacyClaimKwargs()
    elif provider_kind == 'explicit':
        class ExplicitManual(InProcessCronScheduler):
            def fire_due(self, job_id, *, occurrence, **kwargs):
                return super().fire_due(job_id, occurrence=occurrence, **kwargs)
        provider = ExplicitManual()
    monkeypatch.setattr('cron.scheduler_provider.resolve_cron_scheduler', lambda: provider)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.post(f'/api/cron/jobs/{job["id"]}/trigger?profile=fixture')
    if provider_kind.startswith('legacy'):
        assert response.status_code == 409
        assert 'manual occurrence' in response.json()['detail']
        assert jobs.get_job(job['id']) == before
        assert not ran
        assert executions.list_executions(job_id=job['id']) == []
    else:
        assert response.status_code == 200, response.text
        assert len(ran) == 1
        row = executions.latest_execution(job['id'])
        assert row['status'] == 'completed' and row['scheduled_instant'] is None
        assert jobs.is_job_runnable(jobs.get_job(job['id']))
        assert jobs.get_job(job['id'])['next_run_at'] == before['next_run_at']
