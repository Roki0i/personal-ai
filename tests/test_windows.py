import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from personal_ai.app import Assistant
from personal_ai.files import FileDenied, MAX_BYTES
from personal_ai.local import LocalPermission, MacOSLocalProvider, MockLocalProvider, grant, classify
from personal_ai.runtime import local_provider_class, run_bounded, OperationError
from personal_ai.tasks import action, natural_request
from personal_ai.windows import (WindowsLocalProvider, WindowsWorkspace, WindowsNotesWorkspace,
                                 path_parts, reject_link, snapshot_windows_repository,
                                 windows_git_argv, run_windows_command, bounded_windows_process,
                                 check_configured_path)


class WindowsPathTests(unittest.TestCase):
    def test_drive_absolute_denied(self):
        for path in ('C:/secret.txt', 'c:\\secret.txt', 'D:/秘密.txt'):
            with self.subTest(path=path), self.assertRaises(FileDenied): path_parts(path)

    def test_drive_relative_denied(self):
        for path in ('C:secret.txt', 'c:'):
            with self.subTest(path=path), self.assertRaises(FileDenied): path_parts(path)

    def test_unc_and_device_denied(self):
        for path in ('//server/share/a', '\\\\server\\share\\a', '\\\\?\\C:\\a', '\\\\.\\pipe\\a', '/a', '\\a'):
            with self.subTest(path=path), self.assertRaises(FileDenied): path_parts(path)

    def test_mixed_slashes_and_unicode(self):
        self.assertEqual(path_parts('日本語 フォルダ\\sub/資料.txt'), ['日本語 フォルダ', 'sub', '資料.txt'])

    def test_traversal_denied(self):
        for path in ('../a', 'a\\..\\b', 'a/..\\b', './a', 'a//b', 'a/', 'a\\', ''):
            with self.subTest(path=path), self.assertRaises(FileDenied): path_parts(path)

    def test_ads_control_and_wildcards_denied(self):
        for path in ('a.txt:stream', 'a\x00.txt', 'a\n.txt', '*.txt', 'a?.txt', 'a|b.txt', 'a"b'):
            with self.subTest(path=path), self.assertRaises(FileDenied): path_parts(path)

    def test_reserved_devices_and_aliases_denied(self):
        for path in ('CON', 'CON .txt', 'nul.txt', 'Com1.txt', 'LPT9', 'COM¹', 'AUX.md', 'a. ', 'a.', 'a '):
            with self.subTest(path=path), self.assertRaises(FileDenied): path_parts(path)

    def test_root_only_for_directory(self):
        self.assertEqual(path_parts('.', True), [])
        with self.assertRaises(FileDenied): path_parts('.')

    def test_reparse_attribute_denied(self):
        for mode in (stat.S_IFDIR, stat.S_IFREG):
            with self.assertRaises(FileDenied): reject_link(SimpleNamespace(st_mode=mode, st_file_attributes=0x400))

    def test_symlink_attribute_denied(self):
        with self.assertRaises(FileDenied): reject_link(SimpleNamespace(st_mode=stat.S_IFLNK))

    def test_runtime_auto_selection(self):
        for system, expected in (('Windows', WindowsLocalProvider), ('Darwin', MacOSLocalProvider), ('Linux', MockLocalProvider)):
            with patch('platform.system', return_value=system): self.assertIs(local_provider_class(), expected)

    def test_runtime_explicit_selection_and_invalid(self):
        self.assertIs(local_provider_class('mock'), MockLocalProvider)
        self.assertIs(local_provider_class('windows'), WindowsLocalProvider)
        with self.assertRaises(ValueError): local_provider_class('powershell')

    def test_windows_platform_guard(self):
        with patch('platform.system', return_value='Linux'), self.assertRaises(ValueError): WindowsLocalProvider('.', None)

    def test_explorer_natural_request_and_risk(self):
        self.assertEqual(natural_request('資料 名.txtをExplorerで表示して'), [action('reveal_in_explorer', path='資料 名.txt')])
        self.assertEqual(classify('reveal_in_explorer'), 'MEDIUM')
        self.assertEqual(natural_request('Notepadを開いて'), [action('open_application', application='Notepad')])


class WindowsProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.notes = self.root / 'notes'
        self.notes.mkdir()
        ws = WindowsWorkspace(self.notes)
        self.identity = ws.identity
        ws.close()
        with patch('platform.system', return_value='Windows'):
            self.provider = WindowsLocalProvider(self.notes, self.identity, ('Repo',))

    def tearDown(self): self.temp.cleanup()

    def execute(self, name, **args):
        step = action(name, **args)
        return self.provider.execute(step, grant(step))

    def test_create_read_list_japanese_spaces_mixed_slash(self):
        self.execute('create_directory', path='日本語 資料')
        self.execute('create_directory', path='日本語 資料/sub')
        step = action('create_file', path='日本語 資料\\sub/メモ.txt', content='こんにちは\n')
        result = self.provider.execute(step, grant(step))
        self.assertTrue(self.provider.verify(step, result))
        self.assertEqual(self.execute('read_file', path=step['arguments']['path']), 'こんにちは\n')
        self.assertEqual(self.execute('list_directory', path='日本語 資料\\sub'), [{'name': 'メモ.txt', 'type': 'file'}])

    def test_copy_move_rename_verified(self):
        self.execute('create_file', path='元.txt', content='content')
        for name, src, dst in (('copy', '元.txt', 'copy.txt'), ('move', 'copy.txt', '移動.txt'), ('rename', '移動.txt', 'new name.txt')):
            step = action(name, source=src, destination=dst)
            result = self.provider.execute(step, grant(step))
            self.assertTrue(self.provider.verify(step, result))
        self.assertTrue((self.notes / '元.txt').exists())

    def test_no_overwrite_all_mutations(self):
        self.execute('create_file', path='source.txt', content='source')
        self.execute('create_file', path='target.txt', content='target')
        with self.assertRaises(FileExistsError): self.execute('create_file', path='target.txt', content='bad')
        for name in ('copy', 'move', 'rename'):
            with self.subTest(name=name), self.assertRaises(FileExistsError):
                self.execute(name, source='source.txt', destination='target.txt')
        self.assertEqual((self.notes / 'target.txt').read_text(), 'target')
        self.assertEqual((self.notes / 'source.txt').read_text(), 'source')

    def test_wrong_permission_denied(self):
        step = action('create_file', path='a.txt', content='x')
        for permission in (None, grant(action('create_file', path='b.txt', content='x')), LocalPermission(grant(step).action_digest, 'LOW')):
            with self.subTest(permission=permission), self.assertRaises(FileDenied): self.provider.execute(step, permission)
        self.assertFalse((self.notes / 'a.txt').exists())

    def test_disallowed_actions_and_shells(self):
        for command in ('cmd.exe', 'powershell.exe', 'pwsh', 'git status & calc', 'git checkout', 'git status --help', 'python --version', 'pwd'):
            with self.subTest(command=command), self.assertRaises((ValueError, FileDenied)):
                self.execute('command', command=command, path='.')
        for name in ('shell', 'delete', 'registry', 'scheduled_task'):
            with self.subTest(name=name), self.assertRaises(ValueError): self.execute(name)

    def test_hardlink_denied(self):
        (self.notes / 'a.txt').write_text('secret')
        os.link(self.notes / 'a.txt', self.notes / 'b.txt')
        with self.assertRaises(FileDenied): self.execute('read_file', path='a.txt')

    def test_oversized_and_invalid_utf8(self):
        (self.notes / 'large.txt').write_bytes(b'x' * (MAX_BYTES + 1))
        with self.assertRaises(FileDenied): self.execute('read_file', path='large.txt')
        (self.notes / 'bad.txt').write_bytes(b'\xff')
        with self.assertRaises(UnicodeError): self.execute('read_file', path='bad.txt')

    def test_symlink_escape_read_create_and_listing(self):
        outside = self.root / 'outside'
        outside.mkdir()
        try: (self.notes / 'link').symlink_to(outside, target_is_directory=True)
        except OSError: self.skipTest('symlink privilege unavailable')
        with self.assertRaises((FileDenied, OSError)): self.execute('create_file', path='link/a.txt', content='bad')
        self.assertEqual(self.execute('list_directory', path='.')[0]['type'], 'blocked')
        self.assertFalse((outside / 'a.txt').exists())

    def test_configured_root_link_denied(self):
        link = self.root / 'linked'
        try: link.symlink_to(self.notes, target_is_directory=True)
        except OSError: self.skipTest('symlink privilege unavailable')
        with self.assertRaises(FileDenied): check_configured_path(link / 'child')
        with self.assertRaises(FileDenied): WindowsWorkspace(link)

    def test_workspace_identity_replacement(self):
        self.notes.rename(self.root / 'old')
        self.notes.mkdir()
        with self.assertRaises(FileDenied): self.execute('get_system_info')

    def test_application_allowlist(self):
        self.assertEqual(self.execute('list_applications'), ['Notepad', 'Calculator'])
        for name in ('cmd.exe', 'PowerShell', 'TextEdit', 'C:\\evil.exe'):
            with self.subTest(name=name), self.assertRaises((ValueError, FileDenied)):
                self.execute('open_application', application=name)

    def test_fixed_application_and_file_dispatch(self):
        self.execute('create_file', path='日本語 space.txt', content='safe')
        system = self.root / 'Windows' / 'System32'
        with patch('personal_ai.windows.system_directory', return_value=system), patch.object(self.provider, 'run') as run:
            self.execute('open_file', path='日本語 space.txt')
            self.assertEqual(run.call_args.args[0], [str(system / 'notepad.exe'), str(self.notes / '日本語 space.txt')])
            self.execute('reveal_in_explorer', path='日本語 space.txt')
            self.assertEqual(run.call_args.args[0], [str(system.parent / 'explorer.exe'), '/select,', str(self.notes / '日本語 space.txt')])
            self.execute('open_application', application='Calculator')
            self.assertEqual(run.call_args.args[0], [str(system / 'calc.exe')])
            self.assertTrue(run.call_args.kwargs['dispatch'])

    def test_open_scripts_denied_and_dispatch_unverified(self):
        for path in ('a.cmd', 'a.ps1', 'a.exe', 'a.lnk', 'a.url'):
            with self.subTest(path=path), self.assertRaises(FileDenied): self.execute('open_file', path=path)
        step = action('reveal_in_explorer', path='a.txt')
        self.assertFalse(self.provider.verify(step, {'dispatched': True}))

    def test_repository_case_and_slash_keys(self):
        self.assertEqual(self.provider.repository_key('REPO\\Sub'), self.provider.repository_key('repo/sub'))
        with self.assertRaises(FileDenied): self.provider.repository_key('repo/../outside')
        self.assertNotEqual(self.provider.repository_key('straße'), self.provider.repository_key('strasse'))

    def test_unconfigured_repository_denied(self):
        (self.notes / 'other').mkdir()
        with self.assertRaises(FileDenied): self.execute('command', command='git status', path='other')

    def test_notes_adapter_shared_search_and_suffix_policy(self):
        ws = WindowsNotesWorkspace(self.notes, self.identity)
        try:
            ws.create('日本語.txt', '検索語')
            self.assertEqual(ws.search('検索')['paths'], ['日本語.txt'])
            with self.assertRaises(FileDenied): ws.create('a.py', 'code')
        finally: ws.close()

    def make_repo(self):
        repo = self.notes / 'Repo'
        (repo / '.git' / 'hooks').mkdir(parents=True)
        (repo / '.git' / 'config').write_text('[include]\npath=evil\n')
        (repo / '.git' / 'hooks' / 'evil').write_text('bad')
        (repo / 'file.txt').write_text('content')
        return repo

    def test_git_snapshot_strips_config_hooks(self):
        self.make_repo()
        target = self.root / 'snapshot'
        target.mkdir()
        ws = self.provider.workspace()
        try: snapshot_windows_repository(ws, 'Repo', target)
        finally: ws.close()
        self.assertNotIn('evil', (target / '.git' / 'config').read_text())
        self.assertFalse((target / '.git' / 'hooks').exists())
        self.assertEqual((target / 'file.txt').read_text(), 'content')

    def test_git_indirections_rejected(self):
        repo = self.make_repo()
        for filename in ('alternates', 'commondir', '.gitmodules', 'gitdir'):
            target = self.root / ('snapshot-' + filename)
            target.mkdir()
            (repo / filename).write_text('outside')
            ws = self.provider.workspace()
            try:
                with self.subTest(filename=filename), self.assertRaises(FileDenied): snapshot_windows_repository(ws, 'Repo', target)
            finally:
                ws.close()
                (repo / filename).unlink()

    def test_git_execution_uses_private_snapshot(self):
        self.make_repo()
        def run(argv, cwd):
            self.assertEqual(argv, windows_git_argv('git status'))
            self.assertNotEqual(Path(cwd), self.notes / 'Repo')
            self.assertTrue((Path(cwd) / 'file.txt').is_file())
            return {'output': 'ok'}
        with patch.object(self.provider, 'run', side_effect=run):
            self.assertTrue(self.execute('command', command='git status', path='Repo')['snapshot'])

    def test_task_permission_and_audit_shared(self):
        app = Assistant(self.root / 'data', self.notes, local_provider=self.provider)
        try:
            task = app.tasks.submit('create', [action('create_file', path='秘密.txt', content='PRIVATE_CONTENT')])
            self.assertFalse((self.notes / '秘密.txt').exists())
            result = app.tasks.approve(task['task_id'])
            self.assertEqual(result['status'], 'completed')
            self.assertTrue((self.notes / '秘密.txt').exists())
            self.assertNotIn('PRIVATE_CONTENT', '\n'.join(app.store.db.iterdump()))
        finally: app.close()


class WindowsCommandTests(unittest.TestCase):
    def test_git_only_four_fixed_commands(self):
        for name in ('status', 'diff', 'log', 'branch'):
            argv = windows_git_argv('git ' + name)
            self.assertEqual(argv[0], r'C:\Program Files\Git\cmd\git.exe')
            self.assertIn('--no-optional-locks', argv)
            self.assertIn('core.hooksPath=NUL', argv)
        with self.assertRaises(FileDenied): windows_git_argv('git config')

    def test_runner_denies_arbitrary_argv(self):
        with patch('personal_ai.windows.system_directory', return_value=Path('/Windows/System32')):
            for argv in (['cmd.exe', '/c', 'dir'], ['powershell.exe'], ['git', 'status'], ['C:/evil.exe']):
                with self.subTest(argv=argv), self.assertRaises(FileDenied): run_windows_command(argv)

    def test_runner_shell_false_and_clean_environment(self):
        with patch('personal_ai.windows.system_directory', return_value=Path('/Windows/System32')), patch('personal_ai.windows.subprocess.Popen') as popen:
            run_windows_command(['/Windows/System32/notepad.exe'], dispatch=True)
            self.assertIs(popen.call_args.kwargs['shell'], False)
            self.assertNotIn('GIT_CONFIG_COUNT', popen.call_args.kwargs['env'])

    def test_pipe_output_bounded(self):
        with self.assertRaisesRegex(FileDenied, 'output_too_large'):
            bounded_windows_process([sys.executable, '-c', 'import sys; sys.stderr.write("x"*100000)'], None, dict(os.environ))

    def test_pipe_success_and_nonzero(self):
        result = bounded_windows_process([sys.executable, '-c', 'print("ok")'], None, dict(os.environ))
        self.assertEqual(result['output'].strip(), 'ok')
        with self.assertRaisesRegex(FileDenied, 'process_failed'):
            bounded_windows_process([sys.executable, '-c', 'raise SystemExit(1)'], None, dict(os.environ))


@unittest.skipUnless(os.name == 'nt', 'requires native Windows filesystem and Job Objects')
class WindowsNativeTests(unittest.TestCase):
    setUp = WindowsProviderTests.setUp
    tearDown = WindowsProviderTests.tearDown
    execute = WindowsProviderTests.execute

    def test_native_case_insensitive_no_overwrite(self):
        self.execute('create_file', path='Case.txt', content='x')
        self.assertEqual(self.execute('read_file', path='CASE.TXT'), 'x')
        with self.assertRaises(FileExistsError): self.execute('create_file', path='case.txt', content='bad')

    def test_native_pinned_root_cannot_be_renamed(self):
        ws = self.provider.workspace()
        try:
            with self.assertRaises(OSError): self.notes.rename(self.root / 'moved')
        finally: ws.close()

    def test_native_job_timeout(self):
        with self.assertRaises(OperationError): run_bounded(time.sleep, (20,), .5, process_group=True)

    def test_native_junction_escape_denied(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('secret')
        junction = self.notes / 'junction'
        junction.mkdir()
        create_test_junction(junction, outside)
        try:
            for name, args in (
                    ('read_file', {'path': 'junction/secret.txt'}),
                    ('create_file', {'path': 'junction/new.txt', 'content': 'bad'}),
                    ('create_directory', {'path': 'junction/new'}),
                    ('copy', {'source': 'junction/secret.txt', 'destination': 'copy.txt'})):
                with self.subTest(name=name), self.assertRaises(FileDenied): self.execute(name, **args)
            self.assertFalse((outside / 'new.txt').exists())
            with self.assertRaises(FileDenied): check_configured_path(junction)
        finally: os.rmdir(junction)

    def test_native_shared_notes_and_auto_provider(self):
        from personal_ai.files import workspace_for
        ws = workspace_for(self.notes, self.identity)
        try:
            self.assertIsInstance(ws, WindowsNotesWorkspace)
            ws.create('日本語 空白.txt', 'note')
            self.assertEqual(ws.search('note')['paths'], ['日本語 空白.txt'])
        finally: ws.close()
        app = Assistant(self.root / 'data', self.notes)
        try: self.assertIsInstance(app.tasks.provider, WindowsLocalProvider)
        finally: app.close()

    def test_native_job_kills_descendants(self):
        marker = str(self.root / 'survived.txt')
        with self.assertRaises(OperationError):
            run_bounded(spawn_test_child, (marker,), 1, process_group=True)
        time.sleep(2)
        self.assertFalse(Path(marker).exists())


def spawn_test_child(marker):
    import subprocess
    subprocess.Popen([sys.executable, '-c',
                      'import time, pathlib; time.sleep(2); pathlib.Path(' + repr(marker) + ').write_text("bad")'], shell=False)
    time.sleep(20)


def create_test_junction(link, target):
    """Create a mount-point reparse fixture without cmd, PowerShell or elevation."""
    import ctypes
    from ctypes import wintypes
    import struct
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateFileW(str(link), 0x40000000, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    try:
        substitute = ('\\??\\' + str(target)).encode('utf-16-le')
        display = str(target).encode('utf-16-le')
        names = substitute + b'\x00\x00' + display + b'\x00\x00'
        payload = struct.pack('<HHHH', 0, len(substitute), len(substitute) + 2, len(display)) + names
        raw = struct.pack('<IHH', 0xA0000003, len(payload), 0) + payload
        buffer = ctypes.create_string_buffer(raw)
        returned = wintypes.DWORD()
        if not api.DeviceIoControl(handle, 0x900A4, buffer, len(raw), None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally: api.CloseHandle(handle)


if __name__ == '__main__': unittest.main()
