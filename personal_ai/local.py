"""Local providers. Paths are relative to the existing allowed notes folder."""
import hashlib
import os
import platform
import selectors
import tempfile
import time
from dataclasses import dataclass
import json
import stat
import subprocess
from pathlib import Path
from typing import Protocol

from .files import Workspace, FileDenied, MAX_BYTES, MAX_ENTRIES

LOW = {'list_directory', 'read_file', 'list_applications', 'get_system_info', 'command', 'filter_files'}
MEDIUM = {'create_file', 'create_directory', 'rename', 'copy', 'move', 'open_file',
          'open_application', 'reveal_in_finder'}
HIGH = {'delete', 'overwrite', 'terminate_process', 'shell', 'external_upload'}
FIELDS = {
    'list_directory': {'path'}, 'read_file': {'path'}, 'create_file': {'path', 'content'},
    'create_directory': {'path'}, 'rename': {'source', 'destination'},
    'copy': {'source', 'destination'}, 'move': {'source', 'destination'},
    'open_file': {'path'}, 'open_application': {'application'}, 'reveal_in_finder': {'path'},
    'get_system_info': set(), 'list_applications': set(), 'command': {'command', 'path'},
    'filter_files': {'suffix'},
}
VERSION_EXECUTABLES = {'python --version': ('/usr/bin/python3',),
                       'node --version': ('/opt/homebrew/bin/node', '/usr/local/bin/node')}
COMMANDS = {'pwd', 'git status', 'git diff', 'git log', 'git branch', 'python --version', 'node --version'}


@dataclass(frozen=True)
class LocalPermission:
    action_digest: str
    risk_level: str
    state: str = 'granted'


def action_digest(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def grant(action):
    return LocalPermission(action_digest(action), classify(action['name']))

APPLICATIONS = {'TextEdit': '/System/Applications/TextEdit.app',
                'Calculator': '/System/Applications/Calculator.app'}


def classify(name):
    if name in LOW:
        return 'LOW'
    if name in MEDIUM:
        return 'MEDIUM'
    return 'HIGH'


def validate(action):
    if not isinstance(action, dict) or set(action) != {'name', 'arguments'}:
        raise ValueError('invalid_action')
    name, args = action['name'], action['arguments']
    if not isinstance(name, str) or name not in FIELDS or not isinstance(args, dict):
        raise ValueError('unsupported_action')
    if set(args) != FIELDS[name] or any(not isinstance(v, str) for v in args.values()):
        raise ValueError('invalid_arguments')
    for key, value in args.items():
        if len(value.encode('utf-8')) > (MAX_BYTES if key == 'content' else 1024):
            raise ValueError('argument_too_large')
        if key != 'content' and (not value or '\x00' in value):
            raise ValueError('invalid_arguments')
    if name == 'command' and args['command'] not in COMMANDS:
        raise ValueError('command_denied')
    if name == 'open_application' and args['application'] not in APPLICATIONS:
        raise ValueError('application_denied')


class LocalWorkspace(Workspace):
    @staticmethod
    def parts(path):
        if (not isinstance(path, str) or not path or '\x00' in path or '\\' in path
                or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/'))):
            raise FileDenied('path_denied')
        return path.split('/')

    def directory(self, path):
        if path == '.':
            return os.dup(self.fd)
        parent, name = self.parent(path)
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)

    def listing(self, path):
        fd = self.directory(path)
        try:
            with os.scandir(fd) as entries:
                result = []
                for entry in entries:
                    if len(result) >= MAX_ENTRIES:
                        raise FileDenied('too_many_entries')
                    info = entry.stat(follow_symlinks=False)
                    result.append({'name': entry.name, 'type': 'file' if stat.S_ISREG(info.st_mode)
                                   else 'directory' if stat.S_ISDIR(info.st_mode) else 'blocked'})
                return sorted(result, key=lambda x: x['name'])
        finally:
            os.close(fd)

    def mkdir(self, path):
        parent, name = self.parent(path)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        finally:
            os.close(parent)
        return {'created': True}

    def transfer(self, source, destination, move=False):
        # Bounded UTF-8 regular files only. Exclusive creation never replaces a target.
        content = self.read(source)
        if not move:
            self.create(destination, content)
            return {'sha256': digest(content)}
        src, sn = self.parent(source)
        dst = None
        try:
            dst, dn = self.parent(destination)
            before = os.stat(sn, dir_fd=src, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise FileDenied('not_a_private_regular_file')
            os.link(sn, dn, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
            after = os.stat(dn, dir_fd=dst, follow_symlinks=False)
            current = os.stat(sn, dir_fd=src, follow_symlinks=False)
            if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)):
                raise FileDenied('resource_changed_partial')
            os.unlink(sn, dir_fd=src)
            return {'sha256': digest(content)}
        finally:
            os.close(src)
            if dst is not None:
                os.close(dst)


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


class LocalProvider(Protocol):
    root: str
    identity: tuple
    repositories: tuple
    provider_id: str

    def execute(self, action, permission): ...
    def verify(self, action, result): ...


class FileLocalProvider:
    """Shared POSIX file implementation; native side effects supplied by adapters."""

    def __init__(self, root, identity, repositories=()):
        self.root, self.identity = str(root), identity
        self.repositories = tuple(repositories)

    def execute(self, action, permission):
        validate(action)
        name, args = action['name'], action['arguments']
        if (not isinstance(permission, LocalPermission) or permission.state != 'granted'
                or permission.action_digest != action_digest(action)
                or permission.risk_level != classify(name)):
            raise FileDenied('permission_denied')
        # Risk is enforced here too; unsupported HIGH actions never reach an OS API.
        if classify(name) == 'HIGH':
            raise FileDenied('high_action_disabled')
        ws = LocalWorkspace(self.root, self.identity)
        try:
            if name == 'list_directory': return ws.listing(args['path'])
            if name == 'read_file': return ws.read(args['path'])
            if name == 'create_file':
                ws.create(args['path'], args['content'])
                return {'sha256': digest(args['content'])}
            if name == 'create_directory': return ws.mkdir(args['path'])
            if name in ('copy', 'move', 'rename'):
                return ws.transfer(args['source'], args['destination'], name != 'copy')
            if name in ('open_file', 'reveal_in_finder'):
                # Only bounded text files can be opened; no scripts, apps or directories.
                if Path(args['path']).suffix.lower() not in ('.md', '.txt'):
                    raise FileDenied('only_text_open_allowed')
                ws.read(args['path'])
                return self.open_resource(ws, name, args['path'])
            if name == 'open_application': return self.open_app(args['application'])
            if name == 'list_applications': return list(APPLICATIONS)
            if name == 'get_system_info': return self.system_info()
            if name == 'command':
                fd = ws.directory(args['path'])
                os.close(fd)
                if args['command'].startswith('git ') and args['path'] not in self.repositories:
                    raise FileDenied('repository_denied')
                return self.command(ws, args['command'], args['path'])
            raise FileDenied('action_denied')
        finally:
            ws.close()

    def open_resource(self, ws, name, path): raise NotImplementedError
    def open_app(self, name): raise NotImplementedError
    def system_info(self): raise NotImplementedError
    def command(self, ws, command, path): raise NotImplementedError

    def verify(self, action, result):
        name, args = action['name'], action['arguments']
        ws = LocalWorkspace(self.root, self.identity)
        try:
            if name == 'create_directory':
                fd = ws.directory(args['path'])
                os.close(fd)
                return True
            if name in ('create_file', 'copy', 'move', 'rename'):
                path = args['path'] if name == 'create_file' else args['destination']
                matched = digest(ws.read(path)) == result['sha256']
                if name in ('move', 'rename'):
                    parent, leaf = ws.parent(args['source'])
                    try:
                        try: os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError: return matched
                        return False
                    finally: os.close(parent)
                return matched
            if name in ('open_file', 'open_application', 'reveal_in_finder', 'command'):
                return False  # Dispatch/mock output is not evidence of OS state.
            if name == 'read_file': return digest(ws.read(args['path'])) == digest(result)
            if name == 'list_directory': return ws.listing(args['path']) == result
            if name == 'get_system_info': return self.system_info() == result
            if name == 'list_applications': return self.execute(action, grant(action)) == result
            return False
        finally:
            ws.close()


class MockLocalProvider(FileLocalProvider):
    """Real confined file operations; offline simulated apps and commands."""
    provider_id = 'mock-local'

    def open_resource(self, ws, name, path): return {'simulated': True}
    def open_app(self, name): return {'simulated': True}
    def system_info(self): return {'system': 'MockOS', 'architecture': 'mock'}
    def command(self, ws, command, path): return {'simulated': True, 'output': command + ': mock'}


class MacOSLocalProvider(FileLocalProvider):
    provider_id = 'macos-local'

    def __init__(self, *args, **kwargs):
        if platform.system() != 'Darwin': raise ValueError('macos_required')
        super().__init__(*args, **kwargs)

    @staticmethod
    def run(argv, cwd=None):
        return run_command(argv, cwd)

    def open_resource(self, ws, name, path):
        # Validate every component again while holding the allowed root descriptor.
        # LaunchServices accepts pathnames, so a hostile concurrent local renamer
        # is outside this dispatch guarantee (see README).
        ws.read(path)
        target = str(Path(ws.root) / path)
        if Path(target).resolve(strict=True) != Path(target):
            raise FileDenied('resource_changed')
        self.run(['/usr/bin/open'] + (['-R'] if name == 'reveal_in_finder' else []) + [target])
        return {'dispatched': True}

    def open_app(self, name):
        target = APPLICATIONS[name]
        if Path(target).resolve() != Path(target): raise FileDenied('application_changed')
        self.run(['/usr/bin/open', '-a', target])
        return {'dispatched': True}

    def system_info(self):
        return {'system': platform.system(), 'release': platform.release(), 'architecture': platform.machine()}

    def command(self, ws, command, path):
        if command == 'pwd': return {'output': str(Path(self.root) / path)}
        if command in ('python --version', 'node --version'):
            executable = next((path for path in VERSION_EXECUTABLES[command] if Path(path).is_file()), None)
            if executable is None: raise FileDenied('command_unavailable')
            return self.run([executable, '--version'], cwd='/')
        fd = ws.directory(path)
        try:
            # Run Git on a private, bounded snapshot. Never load source .git/config,
            # hooks, alternates, gitdir indirections, or executable working-tree code.
            with tempfile.TemporaryDirectory(prefix='personal-ai-git-',
                                             dir=getattr(self, 'scratch_root', None)) as temp:
                snapshot_repository(fd, Path(temp))
                result = self.run(git_argv(command), cwd=temp)
                result['snapshot'] = True
                return result
        finally:
            os.close(fd)


def git_argv(command):
    options = {
        'git status': ['status', '--short', '--untracked-files=normal'],
        'git diff': ['diff', '--no-ext-diff', '--no-textconv', '--'],
        'git log': ['log', '-30', '--format=%h %s', '--no-decorate'],
        'git branch': ['branch', '--list', '--no-color'],
    }
    if command not in options: raise FileDenied('command_denied')
    return ['/usr/bin/git', '--no-pager', '--no-optional-locks',
            '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.quotePath=true', '-c', 'color.ui=false',
            '-c', 'protocol.allow=never', '-c', 'diff.renames=false'] + options[command]


def snapshot_repository(root_fd, target):
    """Copy at most 1,000 regular files / 16 MiB; reject all link indirections.

    Configured repo must have an actual .git directory, not a worktree gitdir file.
    Config and hooks are omitted; alternates and submodules are rejected.
    """
    git_fd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    os.close(git_fd)
    count, total = 0, 0

    def walk(fd, dest, parts=()):
        nonlocal count, total
        if len(parts) > 16: raise FileDenied('repository_too_deep')
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES: raise FileDenied('repository_too_large')
                rel = parts + (entry.name,)
                control = tuple(part.casefold() for part in rel)
                if parts and entry.name.casefold() == '.git':
                    raise FileDenied('repository_indirection_denied')
                if control in (('.git', 'config'), ('.git', 'config.worktree'), ('.git', 'hooks')):
                    continue
                if entry.name.casefold() in ('.gitmodules', 'alternates', 'http-alternates', 'commondir', 'gitdir') or control[:2] in (
                        ('.git', 'modules'), ('.git', 'worktrees')):
                    raise FileDenied('repository_indirection_denied')
                info = entry.stat(follow_symlinks=False)
                output = dest / entry.name
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        output.mkdir(mode=0o700)
                        walk(child, output, rel)
                    finally: os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        actual = os.fstat(child)
                        if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                            raise FileDenied('repository_resource_changed')
                        if actual.st_size > 4 * 1024 * 1024: raise FileDenied('repository_file_too_large')
                        with os.fdopen(child, 'rb', closefd=False) as stream:
                            raw = stream.read(4 * 1024 * 1024 + 1)
                        total += len(raw)
                        if total > 16 * 1024 * 1024 or len(raw) > 4 * 1024 * 1024:
                            raise FileDenied('repository_too_large')
                        output.write_bytes(raw)
                        output.chmod(0o700 if actual.st_mode & 0o111 else 0o600)
                    finally: os.close(child)
                else:
                    raise FileDenied('repository_link_or_special_file_denied')
    walk(root_fd, target)
    (target / '.git' / 'config').write_text('[core]\nrepositoryformatversion = 0\nbare = false\n', encoding='utf-8')


def run_command(argv, cwd=None):
    """Bound stdout AND stderr without a shell or inherited Git/Node/Python env."""
    allowed = [git_argv(name) for name in ('git status', 'git diff', 'git log', 'git branch')]
    allowed += [[path, '--version'] for paths in VERSION_EXECUTABLES.values() for path in paths]
    allowed += [['/usr/bin/open', '-a', path] for path in APPLICATIONS.values()]
    file_open = (isinstance(argv, list) and len(argv) in (2, 3)
                 and argv[0] == '/usr/bin/open' and (len(argv) == 2 or argv[1] == '-R')
                 and isinstance(argv[-1], str) and Path(argv[-1]).is_absolute()
                 and Path(argv[-1]).suffix.lower() in ('.txt', '.md'))
    if argv not in allowed and not file_open:
        raise FileDenied('command_denied')
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/var/empty', 'LC_ALL': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat', 'GIT_ATTR_NOSYSTEM': '1'}
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = bytearray()
    size = 0
    deadline = time.monotonic() + 5
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline: raise FileDenied('command_timeout')
                for key, _ in selector.select(0.05):
                    raw = os.read(key.fileobj.fileno(), 8192)
                    if not raw:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(raw)
                    if size > MAX_BYTES: raise FileDenied('output_too_large')
                    if key.fileobj is process.stdout: output.extend(raw)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code: raise FileDenied('process_failed')
        return {'output': output.decode('utf-8', errors='replace')}
    finally:
        if process.poll() is None: process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
