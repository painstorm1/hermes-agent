"""Real isolated manual callers must not spend recurring reservations or budgets."""
import copy
import json
from datetime import datetime, timedelta

import pytest

from tests.cron.test_scheduled_occurrence import occurrence_store


@pytest.fixture
def recurring(occurrence_store, monkeypatch):
    jobs, executions, scheduler, job, ran = occurrence_store
    clock = [datetime.fromisoformat('2026-02-03T09:00:00+09:00')]
    monkeypatch.setattr(jobs, '_hermes_now', lambda: clock[0])
    monkeypatch.setattr(scheduler, '_hermes_now', lambda: clock[0])
    jobs.update_job(job['id'], {'next_run_at': '2026-02-03T09:30:00+09:00',
                                'repeat': {'times': 2, 'completed': 1}})
    return jobs, executions, scheduler, jobs.get_job(job['id']), ran, clock


def reservation(job):
    return copy.deepcopy({k: job.get(k) for k in ('next_run_at', 'enabled', 'state', 'repeat')})


@pytest.mark.parametrize('mode', ['sync', 'background', 'pool-fallback', 'registry-fallback'])
@pytest.mark.parametrize('schedule', ['30 9 * * *', 'every 1h'])
@pytest.mark.parametrize('success', [True, False])
def test_manual_callers_preserve_admission_and_completion(recurring, monkeypatch, mode, schedule, success):
    from tools import cronjob_tools as tools, async_delegation
    from gateway import session_context
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.update_job(job['id'], {'schedule': schedule})
    before = reservation(jobs.get_job(job['id']))
    monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: True)
    monkeypatch.setattr(tools, '_background_session_key', lambda sid: 'fixture-session')
    monkeypatch.setattr(tools, '_reap_stale_executions', lambda name: None)
    queued = []
    def dispatch(**kw):
        queued.append(kw['runner'])
        return {'status': 'dispatched', 'delegation_id': 'fixture'} if mode == 'background' else {'error': 'full'}
    monkeypatch.setattr(async_delegation, 'dispatch_async_delegation', dispatch)
    if mode == 'registry-fallback':
        monkeypatch.setattr(async_delegation, '_current_origin_session_id', lambda: (_ for _ in ()).throw(RuntimeError('registry unavailable')))
    def run(claimed, **kw):
        assert reservation(jobs.get_job(job['id'])) == before
        assert claimed['_scheduled_instant'] is None
        ran.append(claimed)
        clock[0] += timedelta(hours=3)
        return success, 'fixture', 'fixture', None if success else 'fixture failure'
    monkeypatch.setattr(scheduler, 'run_job', run)
    for _ in range(2):
        if mode == 'sync':
            result = tools._execute_job_now(job)
            assert result['success'] is success
        else:
            result = tools._try_dispatch_background_run(job, session_id='fixture')
            assert result['claimed']
            if mode == 'background':
                assert reservation(jobs.get_job(job['id'])) == before
                queued.pop()()
            else:
                assert result['success'] is success
        assert reservation(jobs.get_job(job['id'])) == before
        row = executions.latest_execution(job['id'])
        assert row['scheduled_instant'] is None
        assert row['status'] == ('completed' if success else 'failed')
    assert len(ran) == 2


@pytest.mark.parametrize('success', [True, False])
@pytest.mark.parametrize('schedule', ['30 9 * * *', 'every 1h'])
def test_tick_while_manual_owner_held_then_single_catchup(recurring, monkeypatch, schedule, success):
    from tools import cronjob_tools as tools
    from cron.occurrences import scheduled_instant
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.update_job(job['id'], {'schedule': schedule})
    before = reservation(jobs.get_job(job['id']))
    def run(claimed, **kw):
        ran.append(claimed)
        if claimed['_scheduled_instant'] is None:
            clock[0] += timedelta(hours=3)
            assert jobs.heartbeat_fire_claim(job['id'], expected_owner=claimed['fire_claim']['by'])
            assert scheduler.tick(verbose=False, sync=True) == 0
            assert reservation(jobs.get_job(job['id'])) == before
            return success, 'fixture', 'fixture', None if success else 'fixture failure'
        return True, 'fixture', 'fixture', None
    monkeypatch.setattr(scheduler, 'run_job', run)
    assert tools._execute_job_now(job)['success'] is success
    assert reservation(jobs.get_job(job['id'])) == before
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert len(ran) == 2 and ran[1]['_scheduled_instant'] == scheduled_instant(before['next_run_at'])
    assert scheduler.tick(verbose=False, sync=True) == 0
    after = jobs.get_job(job['id'])
    assert after['repeat'] == {'times': 2, 'completed': 2}
    assert not after['enabled']


@pytest.mark.parametrize('mode', ['trigger', 'finite', 'console', 'api', 'dashboard'])
def test_queued_and_provider_callers_preserve_reservation(recurring, monkeypatch, mode):
    from tools import cronjob_tools as tools
    from gateway import session_context
    jobs, executions, scheduler, job, ran, clock = recurring
    before = reservation(job)
    if mode == 'finite':
        monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: False)
        result = json.loads(tools.cronjob(action='run', job_id=job['id'], session_id='fixture'))
        assert result['job']['execution_mode'] == 'scheduler'
    elif mode == 'console':
        from hermes_cli.console_engine import _cron_run
        assert 'Triggered' in _cron_run(None, [job['id']])
    elif mode == 'api':
        import asyncio
        from unittest.mock import AsyncMock
        from aiohttp.test_utils import make_mocked_request
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'fixture-key'}))
        request = make_mocked_request('POST', '/', headers={'Authorization': 'Bearer fixture-key'}, match_info={'job_id': job['id']})
        request.json = AsyncMock(return_value={'prompt': 'fixture request'})
        assert asyncio.run(adapter._handle_run_job(request)).status == 200
    elif mode == 'dashboard':
        from cron.scheduler_provider import InProcessCronScheduler
        from hermes_cli.web_server_cron import _fire_cron_job_for_profile
        from hermes_cli import web_server_cron
        home = jobs._current_cron_store().cron_dir.parent
        monkeypatch.setattr(web_server_cron, '_cron_profile_home', lambda profile: (profile, home))
        monkeypatch.setattr('cron.scheduler_provider.resolve_cron_scheduler', lambda: InProcessCronScheduler())
        assert _fire_cron_job_for_profile('fixture', job['id'])
    else:
        jobs.trigger_job(job['id'], extra_prompt='fixture request')
    assert reservation(jobs.get_job(job['id'])) == before
    if mode != 'dashboard':
        assert scheduler.tick(verbose=False, sync=True) == 1
    assert reservation(jobs.get_job(job['id'])) == before
    assert len(ran) == 1 and ran[0]['_scheduled_instant'] is None
    assert executions.latest_execution(job['id'])['scheduled_instant'] is None


@pytest.mark.parametrize('fault', ['before', 'after-save'])
def test_unknown_claim_persistence_does_not_complete_or_retry(recurring, monkeypatch, fault):
    from tools import cronjob_tools as tools
    jobs, executions, scheduler, job, ran, clock = recurring
    before = reservation(job)
    original = jobs.save_jobs
    def fail(*args, **kw):
        if fault == 'after-save':
            original(*args, **kw)
        raise OSError('fixture ambiguous persistence')
    monkeypatch.setattr(jobs, 'save_jobs', fail)
    result = tools._execute_job_now(job)
    assert not result['claimed'] and not result['success']
    assert result['admission'] == 'unknown'
    assert result['retry_safe'] is False
    assert not ran
    assert reservation(jobs.get_job(job['id'])) == before


def test_new_request_and_edit_survive_old_manual_completion(recurring):
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.trigger_job(job['id'], extra_prompt='old')
    due = jobs.get_due_jobs()[0]
    claim = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True,
                                    expected_manual_run_at=due['manual_run_at'])
    clock[0] += timedelta(seconds=1)
    newer = jobs.trigger_job(job['id'], extra_prompt='new')
    jobs.update_job(job['id'], {'schedule': 'every 2h'})
    jobs.pause_job(job['id'])
    before = jobs.get_job(job['id'])
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner=claim['fire_claim']['by'])
    after = jobs.get_job(job['id'])
    assert reservation(after) == reservation(before)
    assert after['manual_run_at'] == newer['manual_run_at'] and after['manual_run_prompt'] == 'new'


def test_direct_manual_cannot_steal_queued_context(recurring):
    from tools import cronjob_tools as tools
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.trigger_job(job['id'], extra_prompt='queued')
    before = jobs.get_job(job['id'])
    assert not tools._execute_job_now(job)['claimed']
    assert jobs.get_job(job['id']) == before
    assert not ran


@pytest.mark.parametrize('phase', ['handoff', 'cancel', 'shutdown', 'outer', 'delivery'])
def test_terminal_callers_preserve_manual_budget(recurring, monkeypatch, phase):
    import threading
    from tools import cronjob_tools as tools
    from cron.scheduler_provider import InProcessCronScheduler
    jobs, executions, scheduler, job, ran, clock = recurring
    before = reservation(job)
    def fail(*args, **kw):
        raise RuntimeError('fixture failure')
    if phase == 'handoff':
        monkeypatch.setattr(scheduler, '_launch_external_cron_worker', fail)
        assert not tools._execute_job_now(job)['success']
    elif phase == 'outer':
        monkeypatch.setattr(scheduler, 'run_one_job', fail)
        assert not tools._execute_job_now(job)['success']
    elif phase == 'cancel':
        claim = InProcessCronScheduler().claim_fire(job['id'], occurrence=None)
        event = threading.Event()
        event.set()
        def cancelled(claimed, **kwargs):
            assert kwargs['cancel_event'].is_set()
            return False, '', '', 'fixture cancelled before business driver'
        monkeypatch.setattr(scheduler, 'run_job', cancelled)
        scheduler.run_one_job(json.loads(json.dumps(claim)), cancel_event=event)
        assert executions.latest_execution(job['id'])['status'] == 'failed'
        assert not ran
    elif phase == 'shutdown':
        def run(claimed, **kw):
            assert job['id'] in scheduler.mark_running_jobs_interrupted('fixture shutdown')
            return True, 'fixture', 'fixture', None
        monkeypatch.setattr(scheduler, 'run_job', run)
        assert not tools._execute_job_now(job)['success']
    else:
        monkeypatch.setattr(scheduler, '_deliver_result', lambda *a, **kw: 'fixture delivery failed')
        assert not tools._execute_job_now(job)['success']
        assert jobs.get_job(job['id'])['last_status'] == 'delivery_failed'
    assert reservation(jobs.get_job(job['id'])) == before


def test_stale_owner_cannot_complete_replacement(recurring):
    jobs, executions, scheduler, job, ran, clock = recurring
    old = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
    clock[0] += timedelta(seconds=jobs.FIRE_CLAIM_TTL_SECONDS + 1)
    new = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
    before = jobs.get_job(job['id'])
    assert not jobs.mark_job_run(job['id'], True, expected_fire_owner=old['fire_claim']['by'])
    assert not jobs.heartbeat_fire_claim(job['id'], expected_owner=old['fire_claim']['by'])
    assert jobs.get_job(job['id']) == before
    assert jobs.mark_job_run(job['id'], False, expected_fire_owner=new['fire_claim']['by'])
    assert reservation(jobs.get_job(job['id'])) == reservation(job)


def test_manual_completion_preserves_edit_to_once(recurring):
    jobs, executions, scheduler, job, ran, clock = recurring
    claim = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
    jobs.update_job(job['id'], {'schedule': '2026-02-04T09:00:00+09:00'})
    before = reservation(jobs.get_job(job['id']))
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner=claim['fire_claim']['by'])
    assert reservation(jobs.get_job(job['id'])) == before


def test_replacement_requests_have_distinct_stamps_at_same_clock(recurring):
    jobs, executions, scheduler, job, ran, clock = recurring
    first = jobs.trigger_job(job['id'])
    claim = jobs.claim_job_for_fire(job['id'], return_job=True)
    second = jobs.trigger_job(job['id'])
    third = jobs.trigger_job(job['id'], extra_prompt='newest')
    assert len({first['manual_run_at'], second['manual_run_at'], third['manual_run_at']}) == 3
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner=claim['fire_claim']['by'])
    assert jobs.get_job(job['id'])['manual_run_prompt'] == 'newest'


def test_stale_release_is_diagnostic_not_completion(recurring):
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.update_job(job['id'], {'repeat': {'times': None, 'completed': 4}})
    claim = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
    before = jobs.get_job(job['id'])
    scheduler._record_stale_release({}, job['id'], 1000, 900, None, 'age')
    after = jobs.get_job(job['id'])
    assert reservation(after) == reservation(before)
    assert after['fire_claim'] == claim['fire_claim']
    assert after['last_run_at'] == before['last_run_at']
    assert after['last_fire_error']['detail'].startswith('Stale in-flight claim')


def test_manual_owner_arriving_after_due_scan_blocks_advance(recurring):
    jobs, executions, scheduler, job, ran, clock = recurring
    clock[0] += timedelta(minutes=30)
    due = jobs.get_due_jobs()[0]
    claim = jobs.claim_job_for_fire(job['id'], occurrence=None, return_job=True)
    before = reservation(jobs.get_job(job['id']))
    assert jobs.advance_next_runs([job['id']], expected_manual_runs={job['id']: None},
                                  dispatch_snapshots={job['id']: due}) == 0
    assert reservation(jobs.get_job(job['id'])) == before
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner=claim['fire_claim']['by'])
    assert scheduler.tick(verbose=False, sync=True) == 1


def test_claim_failure_does_not_clear_other_owner_or_request(recurring, monkeypatch):
    from tools import cronjob_tools as tools
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.claim_job_for_fire(job['id'], occurrence=None)
    jobs.trigger_job(job['id'], extra_prompt='pending')
    before = jobs.get_job(job['id'])
    def fail(*args, **kw):
        raise OSError('fixture admission failure')
    monkeypatch.setattr(tools, 'claim_job_for_fire', fail)
    assert not tools._execute_job_now(job)['claimed']
    assert jobs.get_job(job['id']) == before


@pytest.mark.parametrize('manual', [False, True])
def test_once_still_consumes_budget(recurring, manual):
    from tools import cronjob_tools as tools
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.update_job(job['id'], {'schedule': '2026-02-03T09:01:00+09:00',
                               'repeat': {'times': 1, 'completed': 0}})
    if manual:
        assert tools._execute_job_now(job)['success']
    else:
        clock[0] += timedelta(minutes=1)
        assert scheduler.tick(verbose=False, sync=True) == 1
    after = jobs.get_job(job['id'])
    assert after['repeat']['completed'] == 1 and not after['enabled']
    assert not jobs.claim_job_for_fire(job['id'], occurrence=None, force=True)


def test_manual_worker_handoff_preserves_reservation_in_fresh_process(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from tests.cron import test_scheduled_occurrence as existing
    from hermes_time import now
    home = tmp_path / 'worker'
    store = home / 'cron' / 'jobs.json'
    store.parent.mkdir(parents=True)
    script = home / 'scripts' / 'fixture.py'
    script.parent.mkdir()
    effect = home / 'effects.txt'
    script.write_text(f"from pathlib import Path\np=Path({str(effect)!r})\n"
                      "with p.open('a') as f: f.write('fixture\\n')\nprint('fixture')\n")
    slot = (now() + timedelta(hours=1)).isoformat()
    job = {'id': 'manual-worker', 'name': 'fixture', 'prompt': '',
           'schedule': {'kind': 'interval', 'minutes': 60},
           'next_run_at': slot, 'repeat': {'times': 2, 'completed': 1},
           'enabled': True, 'state': 'scheduled', 'script': str(script),
           'no_agent': True, 'deliver': 'local'}
    store.write_text(json.dumps({'jobs': [job]}))
    env = dict(os.environ, HERMES_HOME=str(home))
    code = existing._FIRE.replace("provider.claim_fire(job['id'])",
                                  "provider.claim_fire(job['id'], occurrence=None)")
    for _ in range(2):
        result = subprocess.run([sys.executable, '-B', '-c', code, 'worker'], env=env,
                                capture_output=True, encoding='utf-8', timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        after = json.loads(store.read_text())['jobs'][0]
        assert reservation(after) == reservation(job)
        assert after['fire_claim'] is None
    assert effect.read_text().splitlines() == ['fixture', 'fixture']


@pytest.mark.parametrize('background', [False, True])
def test_public_tool_preserves_unknown_admission(recurring, monkeypatch, background):
    from tools import cronjob_tools as tools
    from gateway import session_context
    jobs, executions, scheduler, job, ran, clock = recurring
    before = jobs.get_job(job['id'])
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise OSError('fixture unknown admission')
    monkeypatch.setattr(tools, 'claim_job_for_fire', fail)
    monkeypatch.setattr(tools, '_background_session_key', lambda sid: 'fixture' if background else '')
    monkeypatch.setattr(tools, '_forward_relay_fronted_run', lambda *a, **kw: None)
    monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: True)
    result = json.loads(tools.cronjob(action='run', job_id=job['id']))
    assert result['success'] is False and result['admission'] == 'unknown'
    assert result['retry_safe'] is False and result['job']['executed'] is False
    assert len(calls) == 1 and not ran
    assert jobs.get_job(job['id']) == before


@pytest.mark.parametrize('mode', ['trigger', 'force'])
def test_manual_run_of_paused_job_does_not_replay_stale_slot(recurring, mode):
    """A slot skipped while paused is not a pending reservation: resuming via a manual run
    re-anchors it like ``resume_job`` instead of replaying it right after the manual run."""
    from cron.scheduler_provider import InProcessCronScheduler
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.update_job(job['id'], {'next_run_at': '2026-02-02T09:30:00+09:00'})
    assert jobs.pause_job(job['id'])
    if mode == 'trigger':
        assert jobs.trigger_job(job['id'])
        assert scheduler.tick(verbose=False, sync=True) == 1
    else:
        assert InProcessCronScheduler().fire_due(job['id'], force=True, occurrence=None)
    assert len(ran) == 1 and ran[0]['_scheduled_instant'] is None
    after = jobs.get_job(job['id'])
    assert after['enabled'] and after['state'] == 'scheduled'
    assert after['next_run_at'] == '2026-02-03T09:30:00+09:00'
    assert after['repeat'] == {'times': 2, 'completed': 1}
    assert scheduler.tick(verbose=False, sync=True) == 0
    assert len(ran) == 1


def test_repeated_manual_runs_today_leave_tomorrow_slot_intact(recurring, monkeypatch):
    """Operator scenario: run tomorrow-09:30's job several times today (queued and direct, one
    failing); tomorrow 09:30 still fires exactly once as the regular occurrence."""
    from tools import cronjob_tools as tools
    from cron.occurrences import scheduled_instant
    jobs, executions, scheduler, job, ran, clock = recurring
    jobs.update_job(job['id'], {'next_run_at': '2026-02-04T09:30:00+09:00',
                                'repeat': {'times': None, 'completed': 5}})
    before = reservation(jobs.get_job(job['id']))
    outcomes = iter([True, False, True])
    def run(claimed, **kw):
        ran.append(claimed)
        ok = next(outcomes, True)
        return ok, 'fixture', 'fixture', None if ok else 'fixture failure'
    monkeypatch.setattr(scheduler, 'run_job', run)
    assert jobs.trigger_job(job['id'])
    assert scheduler.tick(verbose=False, sync=True) == 1
    clock[0] += timedelta(hours=1)
    assert tools._execute_job_now(job)['success'] is False
    clock[0] += timedelta(hours=1)
    assert jobs.trigger_job(job['id'])
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert reservation(jobs.get_job(job['id'])) == before
    assert [r['_scheduled_instant'] for r in ran] == [None, None, None]
    assert jobs.get_job(job['id']).get('manual_run_at') is None
    clock[0] = datetime.fromisoformat('2026-02-04T09:30:30+09:00')
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert ran[3]['_scheduled_instant'] == scheduled_instant('2026-02-04T09:30:00+09:00')
    assert scheduler.tick(verbose=False, sync=True) == 0
    after = jobs.get_job(job['id'])
    assert after['next_run_at'] == '2026-02-05T09:30:00+09:00'
    assert after['repeat'] == {'times': None, 'completed': 6}
