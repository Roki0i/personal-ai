"""POSIX descriptor-relative file operations; symlinks are never followed."""
import os
import stat
from pathlib import Path, PurePosixPath


MAX_BYTES = 64 * 1024
MAX_ENTRIES = 1000
MAX_RESULTS = 30


class FileDenied(Exception):
    pass


class Workspace:
    def __init__(self, root, identity=None):
        # root is canonicalized once by the trusted application configuration.
        self.root = str(root)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self.fd = os.open("/", flags)
        try:
            for part in Path(root).parts[1:]:
                child = os.open(part, flags, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = child
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
            if identity is not None and self.identity != identity:
                raise FileDenied("workspace_changed")
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    @staticmethod
    def parts(path):
        if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
            raise FileDenied("invalid_path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or any(part in ("", ".", "..") for part in path.split("/")):
            raise FileDenied("path_denied")
        if parsed.suffix.lower() not in (".md", ".txt"):
            raise FileDenied("only_text_notes_allowed")
        return path.split("/")

    def parent(self, path):
        parts = self.parts(path)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def read(self, path):
        parent, name = self.parent(path)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise FileDenied("not_a_private_regular_file")
                if info.st_size > MAX_BYTES:
                    raise FileDenied("file_too_large")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise FileDenied("file_too_large")
                return raw.decode("utf-8")
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def create(self, path, content):
        raw = content.encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise FileDenied("file_too_large")
        parent, name = self.parent(path)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            return {"path": path, "bytes": len(raw)}
        finally:
            os.close(parent)

    def search(self, query):
        results = []
        visited = 0
        skipped = 0
        truncated = False

        def walk(fd, prefix, depth):
            nonlocal visited, skipped, truncated
            if depth > 16:
                truncated = True
                return
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES or len(results) >= MAX_RESULTS:
                        truncated = True
                        return
                    relative = prefix + entry.name
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                            dir_fd=fd)
                            try:
                                walk(child, relative + "/", depth + 1)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(info.st_mode) and Path(entry.name).suffix.lower() in (".md", ".txt"):
                            content = self.read(relative)
                            if query.casefold() in relative.casefold() or query.casefold() in content.casefold():
                                results.append(relative)
                        else:
                            skipped += 1
                    except (OSError, FileDenied, UnicodeError):
                        skipped += 1
                    if visited > MAX_ENTRIES or len(results) >= MAX_RESULTS:
                        truncated = True
                        return

        walk(self.fd, "", 0)
        return {"paths": results, "skipped": skipped, "truncated": truncated}


def execute_file(root, identity, name, arguments):
    workspace = None
    try:
        workspace = Workspace(root, identity)
        if name == "search_notes":
            return True, workspace.search(arguments["query"]), None
        if name == "read_note":
            return True, workspace.read(arguments["path"]), None
        if name == "create_note":
            return True, workspace.create(arguments["path"], arguments["content"]), None
        return False, None, "tool_denied"
    except FileDenied as exc:
        return False, None, str(exc)
    except FileExistsError:
        return False, None, "already_exists"
    except FileNotFoundError:
        return False, None, "not_found"
    except UnicodeError:
        return False, None, "invalid_utf8"
    except OSError:
        return False, None, "filesystem_denied_or_failed"
    finally:
        if workspace is not None:
            workspace.close()
