"""Explicit user tasks, with immutable proposals and per-plan permission.

Payloads live only in this session; SQLite holds redacted lifecycle metadata.
Neither the LLM nor tool output can call this router.
"""
import copy
import json
import re
import tempfile
import time
from pathlib import Path
import uuid
from dataclasses import asdict, dataclass, field

from .local import (FIELDS, HIGH, COMMANDS, APPLICATIONS,
                    classify, validate, grant)
from .files import FileDenied
from .runtime import OperationError, check_pending, run_bounded
from .storage import now


@dataclass
class Task:
    task_id: str
    requested_at: str
    intent: str
    proposed_actions: list
    risk_level: str
    permission_state: str = 'pending'
    started_at: str = None
    finished_at: str = None
    status: str = 'proposed'
    result: list = field(default_factory=list)
    error: str = None
    audit_reference: list = field(default_factory=list)
    allowed_root: str = ''


def action(name, **arguments):
    return {'name': name, 'arguments': arguments}


def execute_step(provider, step, permission, scratch):
    if getattr(provider, "provider_id", None) in ("macos-local", "windows-local"):
        provider.scratch_root = scratch
    try:
        return {'ok': True, 'data': provider.execute(step, permission), 'error': None}
    except FileExistsError:
        return {'ok': False, 'data': None, 'error': 'already_exists_no_overwrite'}
    except FileNotFoundError:
        return {'ok': False, 'data': None, 'error': 'not_found'}
    except FileDenied as exc:
        safe_codes = {'path_denied', 'permission_denied', 'repository_denied', 'command_denied',
                      'only_text_open_allowed', 'file_too_large', 'not_a_private_regular_file',
                      'workspace_changed', 'resource_changed_partial', 'too_many_entries',
                      'command_unavailable', 'repository_indirection_denied',
                      'repository_link_or_special_file_denied', 'repository_too_large',
                      'repository_file_too_large', 'repository_too_deep', 'resource_changed',
                      'process_failed', 'command_timeout', 'output_too_large'}
        return {'ok': False, 'data': None, 'error': str(exc) if str(exc) in safe_codes else 'local_operation_denied'}
    except Exception:
        return {'ok': False, 'data': None, 'error': 'execution_failed_effects_unknown'}


def verify_step(provider, step, result):
    return provider.verify(step, result)


class TaskManager:
    def __init__(self, store, provider, timeout=10, turn_timeout=30):
        self.store, self.provider, self.timeout = store, provider, timeout
        self.turn_timeout = turn_timeout
        self._tasks = {}
        self._contexts = {}
        store.db.execute('''CREATE TABLE IF NOT EXISTS local_tasks (
            task_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)''')
        store.db.commit()

    def _save(self, task):
        # No intent, path, command, arguments, output, hash or provider errors on disk.
        metadata = {key: value for key, value in asdict(task).items()
                    if key not in ('intent', 'proposed_actions', 'result', 'allowed_root')}
        metadata['action_count'] = len(task.proposed_actions)
        metadata['intent'] = '[not retained]'
        metadata['proposed_actions'] = [{'name': step['name'], 'arguments': '[not retained]'}
                                        for step in task.proposed_actions]
        metadata['result'] = [{'step': item['step'], 'verified': item['verified']}
                              for item in task.result]
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO local_tasks VALUES (?, ?)',
                                  (task.task_id, json.dumps(metadata)))

    def _audit(self, task, event, step=None, **safe):
        metadata = {'task_id': task.task_id, 'risk_level': task.risk_level, **safe}
        if step is not None:
            metadata['step'] = step
            metadata['tool'] = task.proposed_actions[step]['name']
        ref = self.store.start_operation('task_' + event, metadata)
        task.audit_reference.append(ref)
        self.store.finish_operation(ref, 'success')
        self._save(task)

    def _summary(self, metadata):
        item = json.loads(metadata)
        item['session_available'] = item['task_id'] in self._tasks
        if not item['session_available'] and item['status'] in ('proposed', 'running'):
            item['status'] = 'interrupted' if item['started_at'] else 'expired'
        return item

    def list(self):
        rows = self.store.db.execute('SELECT metadata FROM local_tasks ORDER BY rowid DESC LIMIT 100')
        return [self._summary(row[0]) for row in rows]

    def show(self, task_id):
        if task_id in self._tasks: return copy.deepcopy(asdict(self._tasks[task_id]))
        row = self.store.db.execute('SELECT metadata FROM local_tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None: raise ValueError('task_not_found')
        return self._summary(row[0])

    def close(self):
        self._tasks.clear()
        self._contexts.clear()

    def propose(self, intent, actions):
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 16000:
            raise ValueError('invalid_intent')
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise ValueError('task_requires_1_to_8_actions')
        actions = copy.deepcopy(actions)
        try:
            for index, step in enumerate(actions):
                validate(step)
                if step['name'] == 'filter_files' and (index == 0 or actions[index-1]['name'] != 'list_directory'):
                    raise ValueError('filter_requires_directory_listing')
        except ValueError:
            ref = self.store.start_operation('task_proposal_denied', {'risk_level': 'HIGH'})
            self.store.finish_operation(ref, 'denied', 'invalid_or_disabled_action')
            raise
        risk = max((classify(s['name']) for s in actions), key=('LOW', 'MEDIUM', 'HIGH').index)
        task = Task(uuid.uuid4().hex[:12], now(), intent, actions, risk, allowed_root=self.provider.root)
        self._tasks[task.task_id] = task
        self._contexts[task.task_id] = self._context()
        self._audit(task, 'proposed')
        self._audit(task, 'permission_requested')
        return self.show(task.task_id)

    def deny(self, task_id):
        task = self._pending(task_id)
        task.permission_state, task.status, task.finished_at = 'denied', 'denied', now()
        self._audit(task, 'permission_denied')
        return self.show(task_id)

    def cancel(self, task_id):
        task = self._pending(task_id)
        task.permission_state, task.status, task.finished_at = 'denied', 'cancelled', now()
        self._audit(task, 'permission_denied')
        return self.show(task_id)

    def _pending(self, task_id):
        task = self._tasks.get(task_id)
        if task is None or task.status != 'proposed': raise ValueError('task_not_pending_in_session')
        return task

    def _context(self):
        return (id(self.provider), self.provider.root, self.provider.identity,
                tuple(self.provider.repositories))

    def approve(self, task_id):
        task = self._pending(task_id)
        if self._contexts[task_id] != self._context():
            raise ValueError('task_configuration_changed')
        deadline = time.monotonic() + self.turn_timeout
        task.permission_state = 'granted'
        self._audit(task, 'permission_granted')
        task.status, task.started_at = 'running', now()
        self._save(task)
        try:
            for index, step in enumerate(task.proposed_actions):
                check_pending()
                if time.monotonic() >= deadline:
                    raise OperationError('timeout')
                validate(step)
                self._audit(task, 'tool_started', index)
                try:
                    if step['name'] == 'filter_files':
                        previous = task.result[-1]['data']
                        data = [entry for entry in previous if entry['type'] == 'file'
                                and entry['name'].endswith(step['arguments']['suffix'])]
                        verified = True
                    else:
                        # Parent owns temporary snapshots and cleans them even if a
                        # worker is killed by cancellation or timeout.
                        with tempfile.TemporaryDirectory(prefix='personal-ai-task-') as scratch:
                            try:
                                outcome = run_bounded(execute_step, (self.provider, step, grant(step), scratch),
                                                   min(self.timeout, deadline - time.monotonic()), process_group=not provider_is_windows_dispatch(self.provider, step))
                            except OperationError as exc:
                                # Dispatch may have changed files before the worker
                                # lost its response or reached its time limit.
                                if str(exc) in ('timeout', 'worker_failed', 'worker_unavailable'):
                                    raise OperationError(str(exc) + '_effects_unknown') from None
                                raise
                        if not outcome['ok']:
                            raise OperationError(outcome['error'])
                        data = outcome['data']
                        self._audit(task, 'tool_completed', index)
                        # Track dispatched effects before verification, including when verification fails.
                        task.result.append({'step': index, 'data': data, 'verified': False})
                        try:
                            verified = run_bounded(verify_step, (self.provider, step, data),
                                                   min(self.timeout, deadline - time.monotonic()), process_group=not provider_is_windows_dispatch(self.provider, step))
                        except BaseException:
                            self._audit(task, 'verification', index, verified=False)
                            raise
                    if step['name'] == 'filter_files':
                        self._audit(task, 'tool_completed', index)
                        task.result.append({'step': index, 'data': data, 'verified': verified})
                    else:
                        task.result[-1]['verified'] = verified is True
                    self._audit(task, 'verification', index, verified=verified is True)
                    verifiable = step['name'] not in ('open_file', 'open_application', 'reveal_in_finder', 'reveal_in_explorer', 'command')
                    if verifiable and verified is not True:
                        raise OperationError('verification_failed')
                except (Exception, KeyboardInterrupt):
                    self._audit(task, 'tool_failed', index)
                    raise
            task.status = 'completed' if all(r['verified'] for r in task.result) else 'unverified'
        except (Exception, KeyboardInterrupt) as exc:
            # A timed-out worker may already have created something: never claim atomic failure.
            task.status = 'partial' if task.result else 'failed'
            task.error = ('cancelled_effects_unknown' if isinstance(exc, KeyboardInterrupt)
                          else str(exc) if isinstance(exc, OperationError)
                          else 'execution_failed_effects_unknown')
            self._audit(task, 'rollback', outcome='not_attempted_manual_review_required')
        finally:
            task.finished_at = now()
            self._save(task)
        return self.show(task_id)

    def submit(self, intent, actions):
        task = self.propose(intent, actions)
        # The explicit read request grants exactly this LOW proposal, never future tasks.
        if task['risk_level'] == 'LOW': return self.approve(task['task_id'])
        return task


def render(task):
    if task['status'] == 'proposed':
        lines = ['予定: Task ' + task['task_id'] + ' / ' + task['risk_level'],
                 '許可フォルダ: ' + json.dumps(task['allowed_root'], ensure_ascii=False)]
        for index, step in enumerate(task['proposed_actions'], 1):
            lines.append('{}. {} {}'.format(index, step['name'], json.dumps(step['arguments'], ensure_ascii=False)))
        lines.append('実行しますか? /task approve {} （拒否: /task deny {}）'.format(task['task_id'], task['task_id']))
        return '\n'.join(lines)
    return json.dumps(task, ensure_ascii=False, indent=2)


def natural_request(message, root=None):
    """Anchored grammar over the current explicit user input, not model output."""
    if message in ('このフォルダのPythonファイル一覧を出して', 'Pythonファイル一覧を出して'):
        return [action('list_directory', path='.'), action('filter_files', suffix='.py')]
    match = re.fullmatch(r'(.+)のPythonファイル一覧を出して', message)
    if match:
        return [action('list_directory', path=match[1]), action('filter_files', suffix='.py')]
    match = re.fullmatch(r'(?:Documentsに)?([^\s/]+)フォルダとREADMEを作って', message)
    if match:
        if message.startswith('Documentsに') and Path(root or '').resolve() != (Path.home() / 'Documents').resolve():
            raise ValueError('Documentsを対象にするには --notes-dir ~/Documents を明示設定してください')
        folder = match[1]
        return [action('create_directory', path=folder),
                action('create_file', path=folder + '/README.md', content='# ' + folder + '\n')]
    if message == 'システム情報を表示して': return [action('get_system_info')]
    if message == 'アプリ一覧を表示して': return [action('list_applications')]
    match = re.fullmatch(r'(.+)を読んで', message)
    if match: return [action('read_file', path=match[1])]
    match = re.fullmatch(r'(.+)を(?:Explorer|エクスプローラー)で表示して', message)
    if match: return [action('reveal_in_explorer', path=match[1])]
    match = re.fullmatch(r'(.+)をFinderで表示して', message)
    if match: return [action('reveal_in_finder', path=match[1])]
    match = re.fullmatch(r'(.+)を開いて', message)
    if match:
        return [action('open_application', application=match[1])] if match[1] in set(APPLICATIONS) | {'Notepad'} else [action('open_file', path=match[1])]
    match = re.fullmatch(r'(.+)で(git (?:status|diff|log|branch))を表示して', message)
    if match: return [action('command', path=match[1], command=match[2])]
    return None


def route(manager, line):
    if line == '/tools':
        from .tools import SCHEMAS
        return json.dumps({'legacy': [schema['name'] for schema in SCHEMAS], 'local': {name: classify(name) for name in FIELDS},
                           'commands': sorted(COMMANDS), 'disabled_high': sorted(HIGH)}, ensure_ascii=False, indent=2)
    if line == '/permissions':
        return ('LOW: 明示依頼の範囲で実行。MEDIUM: 表示したTaskごとに /task approve ID が必要。'
                '\nHIGH（上書き・削除・shell等）: 今回は無効。包括承認・永続承認なし。'
                '\n許可フォルダ: ' + manager.provider.root + '\nVoiceも同じ規則。Task本文と結果はセッション内のみ。')
    if line.startswith('/task'):
        if line == '/task list': return json.dumps(manager.list(), ensure_ascii=False, indent=2)
        if line.startswith('/task propose '):
            value = json.loads(line[len('/task propose '):])
            return render(manager.submit('explicit structured local task', value))
        parts = line.split()
        if len(parts) == 3 and parts[1] in ('show', 'approve', 'deny', 'cancel'):
            return render(getattr(manager, parts[1])(parts[2]))
        raise ValueError('形式: /task list|show ID|approve ID|deny ID|cancel ID|propose JSON配列')
    if line.startswith(('/', '覚えて ', '忘れて ')):
        return None
    actions = natural_request(line, manager.provider.root)
    if actions is not None: return render(manager.submit(line, actions))
    return None


def provider_is_windows_dispatch(provider, step):
    # GUI dispatch is intentionally persistent; bounded command trees use a Job.
    return (getattr(provider, 'provider_id', None) == 'windows-local'
            and step['name'] in ('open_file', 'open_application', 'reveal_in_finder', 'reveal_in_explorer'))
