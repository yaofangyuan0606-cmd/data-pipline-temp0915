"""File-system abstraction so a dataset can live on a local disk or on a remote machine reached over SSH.

    LocalFS   plain directories (the default)
    SFTPFS    sftp://user@host[:port]/absolute/path  -> paramiko for metadata and single reads, rsync over
              OpenSSH for bulk read-ahead; raw file bytes are cached on local disk mirroring the remote tree

Readers only ever call: listdir, is_dir, is_file, exists, stat_size, read_bytes, read_text, prefetch, local_path.
Nothing here writes to the data source.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int | None = None


class FS:
    scheme = "file"

    def join(self, *parts: str) -> str:
        raise NotImplementedError

    def listdir(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def is_dir(self, path: str) -> bool:
        raise NotImplementedError

    def is_file(self, path: str) -> bool:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return self.is_dir(path) or self.is_file(path)

    def stat_size(self, path: str) -> int | None:
        raise NotImplementedError

    def read_bytes(self, path: str) -> bytes:
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8")

    def url(self, path: str) -> str:
        """Canonical string stored in the database (a plain path for LocalFS, an sftp:// URL otherwise)."""
        return path

    def prefetch(self, paths: list[str]) -> None:  # read-ahead hint, optional
        pass

    def local_path(self, path: str) -> Path | None:
        """A real file on this machine for `path` (the file itself for LocalFS, a cached copy for remote FS)."""
        return None

    def basename(self, path: str) -> str:
        return posixpath.basename(path.rstrip("/"))

    def parent(self, path: str) -> str:
        return posixpath.dirname(path.rstrip("/"))

    def suffix(self, path: str) -> str:
        return posixpath.splitext(path)[1]


# ----------------------------------------------------------------------------- local


class LocalFS(FS):
    def join(self, *parts: str) -> str:
        return str(Path(*parts))

    def listdir(self, path: str) -> list[Entry]:
        out = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    out.append(Entry(e.name, e.is_dir(), e.stat().st_size if e.is_file() else None))
                except OSError:
                    continue
        return sorted(out, key=lambda e: e.name)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def stat_size(self, path: str) -> int | None:
        try:
            return os.stat(path).st_size
        except OSError:
            return None

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def local_path(self, path: str) -> Path | None:
        return Path(path)

    def basename(self, path: str) -> str:
        return Path(path).name

    def parent(self, path: str) -> str:
        return str(Path(path).parent)

    def suffix(self, path: str) -> str:
        return Path(path).suffix


# ----------------------------------------------------------------------------- sftp


class _SSHHost:
    """One SSH transport per (user, host, port); SFTP channels are handed out per thread."""

    def __init__(self, user: str, host: str, port: int, timeout: float, key_file: str | None):
        import paramiko

        self.user, self.host, self.port = user, host, port
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(username=user, port=port, timeout=timeout, banner_timeout=timeout, auth_timeout=timeout, allow_agent=True, look_for_keys=True, compress=False)
        if key_file:
            kw["key_filename"] = os.path.expanduser(key_file)
        self.client.connect(host, **kw)
        self.transport = self.client.get_transport()
        self.transport.set_keepalive(30)
        self._local = threading.local()
        self._lock = threading.Lock()

    def sftp(self):
        s = getattr(self._local, "sftp", None)
        if s is None:
            with self._lock:
                s = self.transport.open_sftp_client()
            self._local.sftp = s
        return s


_HOSTS: dict[tuple, _SSHHost] = {}
_HOSTS_LOCK = threading.Lock()


def ssh_host(user: str, host: str, port: int = 22, timeout: float = 10.0, key_file: str | None = None) -> _SSHHost:
    key = (user, host, port)
    with _HOSTS_LOCK:
        h = _HOSTS.get(key)
        if h is None or not h.transport.is_active():
            h = _SSHHost(user, host, port, timeout, key_file)
            _HOSTS[key] = h
        return h


class SFTPFS(FS):
    scheme = "sftp"

    def __init__(self, user: str, host: str, port: int = 22, cache_dir: str | Path | None = None, timeout: float = 10.0, key_file: str | None = None, workers: int = 4, batch: int = 8):
        self.user, self.host, self.port, self.timeout, self.key_file = user, host, port, timeout, key_file
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.batch = batch
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sftp-prefetch")
        self._inflight: dict[str, threading.Event] = {}  # path -> event set when its prefetch batch has landed
        self._inflight_lock = threading.Lock()
        self._attr_cache: dict[str, list[Entry]] = {}
        self.rsync = shutil.which("rsync")  # bulk transfers go through OpenSSH (much faster than paramiko)

    # -- plumbing
    def _sftp(self):
        return ssh_host(self.user, self.host, self.port, self.timeout, self.key_file).sftp()

    def url(self, path: str) -> str:
        port = f":{self.port}" if self.port != 22 else ""
        return f"sftp://{self.user}@{self.host}{port}{path}"

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def _cache_file(self, path: str) -> Path | None:
        """Cache mirrors the remote tree: <cache_dir>/<host>/<remote path>, so rsync can fill it directly."""
        if not self.cache_dir:
            return None
        return self.cache_dir / self.host / path.lstrip("/")

    # -- metadata
    def listdir(self, path: str) -> list[Entry]:
        if path in self._attr_cache:
            return self._attr_cache[path]
        import stat as st

        out = []
        for a in self._sftp().listdir_attr(path):
            mode, size = a.st_mode, a.st_size
            if mode is not None and st.S_ISLNK(mode):  # follow symlinks (e.g. datasets -> /mnt/... network share)
                target = self._stat(self.join(path, a.filename))
                if target is None:
                    continue
                mode, size = target.st_mode, target.st_size
            is_dir = st.S_ISDIR(mode) if mode is not None else False
            out.append(Entry(a.filename, is_dir, None if is_dir else size))
        out.sort(key=lambda e: e.name)
        self._attr_cache[path] = out
        return out

    def _stat(self, path: str):
        try:
            return self._sftp().stat(path)
        except OSError:
            return None

    def is_dir(self, path: str) -> bool:
        import stat as st

        a = self._stat(path)
        return bool(a and a.st_mode is not None and st.S_ISDIR(a.st_mode))

    def is_file(self, path: str) -> bool:
        import stat as st

        a = self._stat(path)
        return bool(a and a.st_mode is not None and st.S_ISREG(a.st_mode))

    def stat_size(self, path: str) -> int | None:
        a = self._stat(path)
        return None if a is None else a.st_size

    # -- data
    def read_bytes(self, path: str) -> bytes:
        cf = self._cache_file(path)
        if cf is not None and cf.is_file():
            return cf.read_bytes()
        ev = self._inflight.get(path)
        if ev is not None:  # a bulk transfer is bringing this file: wait for it instead of fetching twice
            ev.wait(timeout=600)
            if cf is not None and cf.is_file():
                return cf.read_bytes()
        with self._sftp().open(path, "rb") as f:
            f.prefetch()
            data = f.read()
        if cf is not None:
            cf.parent.mkdir(parents=True, exist_ok=True)
            tmp = cf.with_suffix(cf.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, cf)
        return data

    def local_path(self, path: str) -> Path | None:
        cf = self._cache_file(path)
        if cf is None:
            return None
        if not cf.is_file():
            self.read_bytes(path)
        return cf

    def prefetch(self, paths: list[str]) -> None:
        """Warm the disk cache in the background. With rsync on PATH the files go in batches through one
        OpenSSH transfer each (pipelined: readers wait only for the batch that holds their file); otherwise
        a small pool of paramiko channels fetches them one by one."""
        if not self.cache_dir:
            return
        todo = []
        with self._inflight_lock:
            for p in paths:
                cf = self._cache_file(p)
                if cf is None or cf.is_file() or p in self._inflight:
                    continue
                todo.append(p)
        if not todo:
            return
        if self.rsync and len(todo) >= 2:
            batches = [todo[i : i + self.batch] for i in range(0, len(todo), self.batch)]
            events = []
            with self._inflight_lock:
                for b in batches:
                    ev = threading.Event()
                    for p in b:
                        self._inflight[p] = ev
                    events.append(ev)
            self._pool.submit(self._rsync_batches, batches, events)
            return
        for p in todo:
            ev = threading.Event()
            with self._inflight_lock:
                self._inflight[p] = ev

            def _job(p=p, ev=ev):
                try:
                    self.read_bytes(p)
                except Exception as e:  # pragma: no cover - network
                    log.warning("prefetch %s failed: %s", p, e)
                finally:
                    with self._inflight_lock:
                        self._inflight.pop(p, None)
                    ev.set()

            self._pool.submit(_job)

    def _rsync_batches(self, batches: list[list[str]], events: list[threading.Event]) -> None:
        import subprocess
        import tempfile

        dest = self.cache_dir / self.host
        dest.mkdir(parents=True, exist_ok=True)
        ssh = f"ssh -o BatchMode=yes -o ConnectTimeout={int(self.timeout)} -p {self.port}" + (f" -i {os.path.expanduser(self.key_file)}" if self.key_file else "")
        for b, ev in zip(batches, events):
            lst = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".lst", delete=False) as fl:
                    fl.write("\n".join(p.lstrip("/") for p in b) + "\n")
                    lst = fl.name
                cmd = [self.rsync, "-a", "--files-from=" + lst, "-e", ssh, f"{self.user}@{self.host}:/", str(dest) + "/"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                if r.returncode != 0:
                    log.warning("rsync prefetch failed (%s): %s", r.returncode, r.stderr.strip()[:300])
            except Exception as e:  # pragma: no cover - network
                log.warning("rsync prefetch error: %s", e)
            finally:
                if lst:
                    try:
                        os.unlink(lst)
                    except OSError:
                        pass
                with self._inflight_lock:
                    for p in b:
                        self._inflight.pop(p, None)
                ev.set()


# ----------------------------------------------------------------------------- roots & globbing


def parse_root(root: str | Path, cache_dir: str | Path | None = None, timeout: float = 10.0, key_file: str | None = None) -> tuple[FS, str]:
    """'/local/dir' -> (LocalFS, '/local/dir');  'sftp://u@h:22/p' -> (SFTPFS, '/p')."""
    s = str(root)
    if s.startswith("sftp://") or s.startswith("ssh://"):
        u = urlparse(s)
        if not u.username or not u.hostname or not u.path:
            raise ValueError(f"remote root needs user, host and path: {s}")
        return SFTPFS(u.username, u.hostname, u.port or 22, cache_dir=cache_dir, timeout=timeout, key_file=key_file), u.path.rstrip("/") or "/"
    return LocalFS(), str(Path(s))


def fs_glob(fs: FS, base: str, pattern: str) -> list[str]:
    """Expand a relative glob like 'project_terminal/*/datasets/datasets/*' below base (directories only).
    A root that already points at the first pattern level (…/project_terminal) is accepted too."""
    parts = [p for p in pattern.split("/") if p]
    tail = [c for c in base.replace("\\", "/").split("/") if c]
    # the root may already point inside the pattern (e.g. .../project_terminal/<project>): match the longest
    # suffix of the root against a prefix of the pattern and glob only the remainder
    for k in range(min(len(parts), len(tail)), 0, -1):
        if all(fnmatch.fnmatch(t, p) for t, p in zip(tail[-k:], parts[:k])):
            parts = parts[k:]
            break
    cur = [base]
    for part in parts:
        nxt = []
        for d in cur:
            try:
                entries = fs.listdir(d)
            except OSError:
                continue
            for e in entries:
                if e.is_dir and not e.name.startswith(".") and fnmatch.fnmatch(e.name, part):
                    nxt.append(fs.join(d, e.name))
        cur = nxt
        if not cur:
            break
    return sorted(cur)


def is_remote(root: str | Path) -> bool:
    s = str(root)
    return s.startswith("sftp://") or s.startswith("ssh://")
