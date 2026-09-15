"""Windows adapter: local drive paths, pinned ancestors, no reparse points.

No shell, file associations, PATH executable lookup or user-supplied argv.
"""
import ctypes
import ntpath
import os
from pathlib import Path
import platform
import stat
import subprocess
import tempfile
import threading
import queue
import time
from contextlib import contextmanager

from .files import FileDenied, MAX_BYTES, MAX_ENTRIES, MAX_RESULTS
from .local import FileLocalProvider, digest, git_argv, skip_repository_entry


def path_parts(path, allow_root=False):
    if allow_root and path == '.': return []
    if not isinstance(path, str) or not path or ntpath.splitdrive(path)[0] or path.startswith(('/', '\\')):
        raise FileDenied('path_denied')
    parts = path.replace('\\', '/').split('/')
    for part in parts:
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.split('.')[0].rstrip(' ').upper() in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
                or part.split('.')[0].rstrip(' ').upper() in {prefix + n for prefix in ('COM', 'LPT') for n in '123456789¹²³'}):
            raise FileDenied('path_denied')
    return parts


def reject_link(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise FileDenied('path_denied')


def native_open(path, *, write=False, directory=False, delete=False):
    """Return CRT fd owning a CreateFileW handle; deny delete sharing.

    Directory handles also deny write sharing, preventing reparse mutation.
    OPEN_REPARSE_POINT opens the link itself, which fstat then rejects.
    """
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    access = (0x40000000 if write else (0x80 if directory else 0x80000000)) | (0x10000 if delete else 0)
    handle = api.CreateFileW(str(path), access,
                             1, None, 1 if write else 3, 0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in (80, 183): raise FileExistsError(str(path))
        if code in (2, 3): raise FileNotFoundError(str(path))
        raise ctypes.WinError(code)
    try:
        # Query attributes on the actual handle, not a pathname or CRT metadata.
        class FileInfo(ctypes.Structure):
            _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                        ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                        ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                        ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                        ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
        api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInfo)]
        info = FileInfo()
        if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400: raise FileDenied('path_denied')
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_WRONLY if write else os.O_RDONLY))
    except BaseException:
        api.CloseHandle(handle)
        raise
    try:
        reject_link(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


class WindowsWorkspace:
    def __init__(self, root, identity=None):
        self.root = str(Path(root).absolute())
        self._pins = {}
        try:
            if os.name == 'nt':
                drive, tail = ntpath.splitdrive(self.root)
                if len(drive) != 2 or drive[1] != ':': raise FileDenied('path_denied')
            current = Path(self.root).anchor
            self._pin(Path(current))
            for part in Path(self.root).parts[1:]:
                current = Path(current) / part
                self._pin(current)
            info = os.stat(self.root, follow_symlinks=False)
            self.identity = (info.st_dev, info.st_ino)
            if identity is not None and identity != self.identity: raise FileDenied('workspace_changed')
        except BaseException:
            self.close()
            raise

    def _pin(self, path):
        key = str(path)
        if key in self._pins: return
        if os.name == 'nt':
            fd = native_open(path, directory=True)
            try:
                if not stat.S_ISDIR(os.fstat(fd).st_mode): raise FileDenied('path_denied')
            except BaseException:
                os.close(fd)
                raise
            self._pins[key] = fd
        else:  # Portable policy tests; production Windows always uses native handles.
            info = os.lstat(path)
            reject_link(info)
            if not stat.S_ISDIR(info.st_mode): raise FileDenied('path_denied')

    def close(self):
        for fd in self._pins.values(): os.close(fd)
        self._pins.clear()

    parts = staticmethod(path_parts)

    def target(self, path, directory=False):
        parts = self.parts(path, allow_root=directory)
        target = Path(self.root)
        for part in parts[:-1] if not directory else parts:
            target /= part
            self._pin(target)
        return target if directory or not parts else target / parts[-1]

    def check_directory(self, path):
        self.target(path, directory=True)

    def exists(self, path):
        try: os.lstat(self.target(path))
        except FileNotFoundError: return False
        return True

    @contextmanager
    def reader(self, path, delete=False):
        target = self.target(path)
        fd = native_open(target, delete=delete) if os.name == 'nt' else os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            reject_link(info)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise FileDenied('not_a_private_regular_file')
            with os.fdopen(fd, 'rb', closefd=False) as stream: yield stream, info
        finally: os.close(fd)

    def read_bytes(self, path, limit=MAX_BYTES):
        with self.reader(path) as (stream, info):
            if info.st_size > limit: raise FileDenied('file_too_large')
            raw = stream.read(limit + 1)
            if len(raw) > limit: raise FileDenied('file_too_large')
            return raw

    def read(self, path):
        return self.read_bytes(path).decode('utf-8')

    def create(self, path, content):
        raw = content.encode('utf-8')
        if len(raw) > MAX_BYTES: raise FileDenied('file_too_large')
        target = self.target(path)
        fd = native_open(target, write=True) if os.name == 'nt' else os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return {'path': path, 'bytes': len(raw)}

    def mkdir(self, path):
        self.target(path).mkdir()
        return {'created': True}

    def listing(self, path):
        target = self.target(path, directory=True)
        result = []
        with os.scandir(target) as entries:
            for entry in entries:
                if len(result) >= MAX_ENTRIES: raise FileDenied('too_many_entries')
                info = entry.stat(follow_symlinks=False)
                blocked = stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                kind = 'blocked' if blocked else 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'blocked'
                result.append({'name': entry.name, 'type': kind})
        return sorted(result, key=lambda item: item['name'])

    def transfer(self, source, destination, move=False):
        if move and os.name == 'nt':
            destination_path = self.target(destination)
            if self.exists(destination): raise FileExistsError(str(destination_path))
            with self.reader(source, delete=True) as (stream, info):
                if info.st_size > MAX_BYTES: raise FileDenied('file_too_large')
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES: raise FileDenied('file_too_large')
                content = raw.decode('utf-8')
                rename_handle(stream.fileno(), destination_path)
        else:
            content = self.read(source)
            src, dst = self.target(source), self.target(destination)
            if move:  # Portable tests only; Windows uses atomic handle rename above.
                os.link(src, dst, follow_symlinks=False)
                os.unlink(src)
            else:
                self.create(destination, content)
        return {'sha256': digest(content)}

    def search(self, query):
        paths, skipped, visited, truncated = [], 0, 0, False
        def walk(path, depth):
            nonlocal skipped, visited, truncated
            if depth > 16:
                truncated = True
                return
            for item in self.listing(path):
                visited += 1
                if visited > MAX_ENTRIES or len(paths) >= MAX_RESULTS:
                    truncated = True
                    return
                relative = item['name'] if path == '.' else path + '/' + item['name']
                try:
                    if item['type'] == 'directory': walk(relative, depth + 1)
                    elif item['type'] == 'file' and Path(relative).suffix.lower() in ('.md', '.txt'):
                        content = self.read(relative)
                        if query.casefold() in relative.casefold() or query.casefold() in content.casefold(): paths.append(relative)
                    else: skipped += 1
                except (OSError, FileDenied, UnicodeError): skipped += 1
        walk('.', 0)
        return {'paths': paths, 'skipped': skipped, 'truncated': truncated}


class WindowsNotesWorkspace(WindowsWorkspace):
    @staticmethod
    def parts(path, allow_root=False):
        parts = path_parts(path, allow_root)
        if not allow_root and Path(parts[-1]).suffix.lower() not in ('.md', '.txt'):
            raise FileDenied('only_text_notes_allowed')
        return parts


def system_directory():
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    buffer = ctypes.create_unicode_buffer(32768)
    length = api.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer): raise FileDenied('command_unavailable')
    return Path(buffer.value)


class WindowsLocalProvider(FileLocalProvider):
    provider_id = 'windows-local'
    applications = {'Notepad': 'notepad.exe', 'Calculator': 'calc.exe'}

    def __init__(self, *args, **kwargs):
        if platform.system() != 'Windows': raise ValueError('windows_required')
        super().__init__(*args, **kwargs)

    def workspace(self): return WindowsWorkspace(self.root, self.identity)

    def repository_key(self, path):
        return ntpath.normcase('\\'.join(path_parts(path, allow_root=True)))

    def system_info(self):
        return {'system': platform.system(), 'release': platform.release(), 'architecture': platform.machine()}

    def open_resource(self, ws, name, path):
        with ws.reader(path):
            target = str(ws.target(path))
            if name == 'open_file':
                self.run([str(system_directory() / 'notepad.exe'), target], dispatch=True)
            else:
                self.run([str(system_directory().parent / 'explorer.exe'), '/select,', target], dispatch=True)
        return {'dispatched': True}

    def open_app(self, name):
        if name not in self.applications: raise FileDenied('application_denied')
        self.run([str(system_directory() / self.applications[name])], dispatch=True)
        return {'dispatched': True}

    @staticmethod
    def run(argv, cwd=None, dispatch=False): return run_windows_command(argv, cwd, dispatch)

    def command(self, ws, command, path):
        argv = windows_git_argv(command)
        with tempfile.TemporaryDirectory(prefix='personal-ai-git-', dir=getattr(self, 'scratch_root', None)) as temp:
            snapshot_windows_repository(ws, path, Path(temp))
            result = self.run(argv, cwd=temp)
            result['snapshot'] = True
            return result


def windows_git_argv(command):
    argv = git_argv(command)  # Same exact four-command allowlist and safety options.
    # Fixed machine installation only; no PATH, user-profile or repository lookup.
    argv[0] = r'C:\Program Files\Git\cmd\git.exe'
    return [
        'core.hooksPath=NUL' if item == 'core.hooksPath=/dev/null' else item for item in argv]


def snapshot_windows_repository(ws, path, target):
    ws.check_directory(path)
    prefix = '' if path == '.' else '/'.join(path_parts(path)) + '/'
    ws.check_directory(prefix + '.git')
    count, total = 0, 0
    def walk(parts=()):
        nonlocal count, total
        if len(parts) > 16: raise FileDenied('repository_too_deep')
        relative = (prefix + '/'.join(parts)).rstrip('/')
        for item in ws.listing(relative or '.'):
            count += 1
            if count > MAX_ENTRIES: raise FileDenied('repository_too_large')
            rel = parts + (item['name'],)
            if skip_repository_entry(rel): continue
            source = prefix + '/'.join(rel)
            output = target.joinpath(*rel)
            if item['type'] == 'directory':
                ws.check_directory(source)
                output.mkdir()
                walk(rel)
            elif item['type'] == 'file':
                raw = ws.read_bytes(source, 4 * 1024 * 1024)
                total += len(raw)
                if total > 16 * 1024 * 1024: raise FileDenied('repository_too_large')
                output.write_bytes(raw)
            else: raise FileDenied('repository_link_or_special_file_denied')
    walk()
    (target / '.git' / 'config').write_text('[core]\nrepositoryformatversion = 0\nbare = false\n', encoding='utf-8')


def run_windows_command(argv, cwd=None, dispatch=False):
    system = system_directory()
    allowed = [windows_git_argv('git ' + name) for name in ('status', 'diff', 'log', 'branch')]
    apps = [[str(system / name)] for name in WindowsLocalProvider.applications.values()]
    opening = (isinstance(argv, list) and len(argv) in (2, 3)
               and ((len(argv) == 2 and argv[0] == str(system / 'notepad.exe'))
                    or (len(argv) == 3 and argv[:2] == [str(system.parent / 'explorer.exe'), '/select,']))
               and Path(argv[-1]).is_absolute() and Path(argv[-1]).suffix.lower() in ('.md', '.txt'))
    if not ((dispatch and (argv in apps or opening)) or (not dispatch and argv in allowed)):
        raise FileDenied('command_denied')
    env = {'SystemRoot': str(system.parent), 'WINDIR': str(system.parent), 'PATH': str(system),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': 'NUL', 'GIT_ATTR_NOSYSTEM': '1',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': '', 'LANG': 'C.UTF-8'}
    if dispatch:
        # Direct executable dispatch; application state remains unverified.
        subprocess.Popen(argv, shell=False, cwd=str(system), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return {'dispatched': True}
    return bounded_windows_process(argv, cwd, env)


def bounded_windows_process(argv, cwd, env):
    """Windows pipes cannot be polled by selectors; bound both reader queues."""
    process = subprocess.Popen(argv, shell=False, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    chunks = queue.Queue(maxsize=16)
    stop = threading.Event()
    def read(stream, stdout):
        try:
            while not stop.is_set():
                raw = stream.read(4096)
                while not stop.is_set():
                    try:
                        chunks.put((stdout, raw), timeout=.05)
                        break
                    except queue.Full: pass
                if not raw: break
        finally: stream.close()
    readers = [threading.Thread(target=read, args=(stream, is_stdout), daemon=True)
               for stream, is_stdout in ((process.stdout, True), (process.stderr, False))]
    for reader in readers: reader.start()
    output, size, ended = bytearray(), 0, 0
    deadline = time.monotonic() + 5
    try:
        while ended < 2:
            from .runtime import check_pending
            check_pending()
            if time.monotonic() >= deadline: raise FileDenied('command_timeout')
            try: stdout, raw = chunks.get(timeout=.05)
            except queue.Empty: continue
            if not raw:
                ended += 1
                continue
            size += len(raw)
            if size > MAX_BYTES: raise FileDenied('output_too_large')
            if stdout: output.extend(raw)
        if process.wait(timeout=max(.01, deadline - time.monotonic())): raise FileDenied('process_failed')
        return {'output': output.decode('utf-8', errors='replace')}
    finally:
        stop.set()
        if process.poll() is None: process.kill()
        process.wait()
        for reader in readers: reader.join(.5)


def check_configured_path(path):
    """Reject existing junctions before trusted config canonicalization hides them."""
    drive, _ = ntpath.splitdrive(str(path))
    if os.name == 'nt' and (len(drive) != 2 or drive[1] != ':'):
        raise FileDenied('path_denied')
    for candidate in reversed((Path(path), *Path(path).parents)):
        try: reject_link(os.lstat(candidate))
        except FileNotFoundError: break


def rename_handle(fd, destination):
    import msvcrt
    from ctypes import wintypes
    name = str(destination)
    # WCHAR count must include UTF-16 surrogate pairs (e.g. emoji paths).
    units = len(name.encode('utf-16-le')) // 2
    class RenameInfo(ctypes.Structure):
        _fields_ = [('replace', wintypes.BOOLEAN), ('root', wintypes.HANDLE),
                    ('length', wintypes.DWORD), ('name', wintypes.WCHAR * (units + 1))]
    info = RenameInfo()
    info.replace = False
    info.length = units * 2
    info.name = name
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    if not api.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 3, ctypes.byref(info), ctypes.sizeof(info)):
        code = ctypes.get_last_error()
        if code in (80, 183): raise FileExistsError(name)
        raise ctypes.WinError(code)
