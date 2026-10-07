"""Copy-on-write checkpoints of guarded roots, and exact rollback.

On APFS a directory is checkpointed with one clonefile(2) call: blocks are
shared until written, so the cost is the directory metadata, not the data.
Elsewhere the checkpoint falls back to a full copy (correct, slower).

Rollback restores the root from the checkpoint *in place*: the root directory
keeps its inode, so a server whose working directory is the root keeps working,
and a file that exists on both sides is rewritten through its existing inode, so
a server holding it open (a database connection) sees the restored bytes.
Rollback compares raw bytes, not the decision-sufficient view: whatever the
blocked call changed, cache files included, is put back exactly.
"""

from __future__ import annotations

import ctypes
import errno
import os
import atexit
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from .state import HashCache, walk

_libc = None
try:
    _libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    _libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
except (OSError, AttributeError):
    _libc = None


def _clone(src: Path, dst: Path) -> str:
    if _libc is not None:
        if _libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
            return "clonefile"
        e = ctypes.get_errno()
        if e not in (errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP):
            raise OSError(e, os.strerror(e), str(src))
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return "copy"


class Checkpoint:
    def __init__(self, root: Path, store: Path):
        self.root = Path(root)
        self.path = Path(store) / f"ck-{uuid.uuid4().hex}"
        self.method = _clone(self.root, self.path)

    def restore(self) -> bool:
        """Make root byte-identical to the checkpoint; return whether it is."""
        _sync(self.path, self.root)
        a, b = HashCache(), HashCache()
        return walk(self.root, a) == walk(self.path, b)

    def discard(self, background: bool = True):
        if background:
            _Trash.put(self.path)
        else:
            shutil.rmtree(self.path, ignore_errors=True)


class _Trash:
    """Deferred deletion of checkpoints, off the critical path and off the GIL.

    One long-lived, low-priority `xargs rm -rf` process receives discarded
    checkpoint paths on a pipe. Deleting in a Python thread instead contends
    with the next guarded call, and spawning `rm` per call costs a fork.
    """
    proc: subprocess.Popen | None = None

    @classmethod
    def put(cls, path: Path):
        if cls.proc is None or cls.proc.poll() is not None:
            cls.proc = subprocess.Popen(["nice", "-n", "10", "xargs", "-0", "-n", "1", "rm", "-rf"],
                                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        cls.proc.stdin.write(os.fsencode(path) + b"\0")
        cls.proc.stdin.flush()

    @classmethod
    def close(cls):
        if cls.proc is not None and cls.proc.poll() is None:
            cls.proc.stdin.close()
            cls.proc.wait()


atexit.register(_Trash.close)


def _sync(src: Path, dst: Path):
    """Restore dst from src in place: remove extras, then rewrite differences."""
    want = walk(src, HashCache())
    have = walk(dst, HashCache())
    # make every directory on the way writable while we work
    os.chmod(dst, os.stat(dst).st_mode | stat.S_IWUSR | stat.S_IXUSR)
    for rel in sorted(have, key=lambda r: r.count("/"), reverse=True):
        if rel in want and want[rel][0] == have[rel][0] == "dir":
            continue
        if rel in want and want[rel][0] == have[rel][0] == "file":
            continue  # rewritten in place below: open handles keep seeing the file
        if rel not in want or want[rel] != have[rel]:
            p = dst / rel
            os.chmod(p.parent, os.stat(p.parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
            if have[rel][0] == "dir":
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists() or p.is_symlink():
                p.unlink()
    for rel in sorted(want, key=lambda r: r.count("/")):
        typ, perm, content = want[rel]
        p, s = dst / rel, src / rel
        if typ == "dir":
            if not p.is_dir():
                p.mkdir()
            continue
        if typ == "file" and rel in have and have[rel][0] == "file":
            if have[rel][2] != content:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
                with open(s, "rb") as fi, open(p, "r+b") as fo:
                    fo.truncate(0)
                    shutil.copyfileobj(fi, fo)
            continue
        if p.exists() or p.is_symlink():
            continue
        if typ == "symlink":
            os.symlink(content, p)
        else:
            _clone(s, p)
    # permissions last, deepest first, so directories stay writable until the end
    for rel in sorted(want, key=lambda r: r.count("/"), reverse=True):
        typ, perm, _ = want[rel]
        if typ != "symlink":
            os.chmod(dst / rel, perm)
    os.chmod(dst, stat.S_IMODE(os.stat(src).st_mode))


def default_store() -> Path:
    p = Path(tempfile.gettempdir()) / "dssguard-checkpoints"
    p.mkdir(exist_ok=True)
    return p
