"""Decision-sufficient state views.

A guarded root is reduced to a set of *atoms*: hashable facts about the state a
user would care about when deciding whether a call was acceptable. Bytes that
change without changing any such fact (timestamps, inode numbers, git's index
stat cache, a server's declared scratch directory) are not atoms, so they never
count as an effect. That is the difference between this view and a byte-level
file monitor, which flags git's read-only tools whenever they refresh the index.

Every atom also has a *content key*. A removed atom is a loss only when its
content key is not retained anywhere in the post-call state, so "the file moved
into a commit" is not a loss and "the branch tip became unreachable" is.

Views: tree (any directory), git (a repository), memory (the reference
knowledge-graph JSONL), sqlite (a database file), and raw (byte-level, kept as
an ablation baseline). Adapted from the pilot harness (expansion/mcp_pilot).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


class HashCache:
    """Content hash keyed by stat identity, so unchanged files are not re-read.

    ctime cannot be set by user programs, so any content write changes the key.
    """

    def __init__(self):
        self._c: dict[tuple, str] = {}

    def sha(self, path: str, st: os.stat_result) -> str:
        key = (path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        h = self._c.get(key)
        if h is None:
            with open(path, "rb") as fh:
                h = git_blob_sha(fh.read())
            self._c[key] = h
        return h


def walk(root: Path, cache: HashCache, exclude: tuple[str, ...] = ()) -> dict[str, tuple]:
    """{relpath: (type, perm, content)} for everything under root.

    `exclude` holds relative path prefixes (a directory name or a file name)
    that are skipped entirely.
    """
    out: dict[str, tuple] = {}
    root = Path(root)
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        def skip(rel: str) -> bool:
            return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in exclude)
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if not skip(rel):
                keep.append(d)
        dirnames[:] = keep
        for name in dirnames + filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if skip(rel):
                continue
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            perm = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("symlink", perm, os.readlink(full))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", perm, None)
            elif stat.S_ISREG(st.st_mode):
                out[rel] = ("file", perm, cache.sha(full, st))
            else:
                out[rel] = ("other", perm, None)
    return out


def signature(root: Path) -> dict[str, tuple]:
    """Stat identity of every entry under root, without reading any content.

    (type, mode, size, mtime_ns, ctime_ns, inode). The kernel updates ctime on
    every content or metadata change and user programs cannot set it, so equal
    signatures mean nothing under root was written, renamed, created or removed.
    """
    out: dict[str, tuple] = {}
    stack = [(str(root), "")]
    while stack:
        path, rel = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                r = f"{rel}/{e.name}" if rel else e.name
                st = e.stat(follow_symlinks=False)
                out[r] = (stat.S_IFMT(st.st_mode), st.st_mode, st.st_size, st.st_mtime_ns,
                          st.st_ctime_ns, st.st_ino)
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, r))
    return out


def tree_atoms(tree: dict[str, tuple], tag: str) -> set:
    atoms = set()
    for rel, (typ, perm, content) in tree.items():
        atoms.add((tag, "type", rel, typ))
        atoms.add((tag, "perm", rel, oct(perm)))
        if typ == "file":
            atoms.add((tag, "content", rel, content))
        elif typ == "symlink":
            atoms.add((tag, "link", rel, content))
    return atoms


# --------------------------------------------------------------------- views

@dataclass
class View:
    """One guarded root and how to read decision-sufficient state from it."""
    name: str
    path: Path
    kind: str                       # tree | git | memory | sqlite | raw
    file: str | None = None         # memory/sqlite: file name inside path
    scratch: tuple[str, ...] = ()   # relative prefixes the server owns (declared, auditable)
    cache: HashCache = field(default_factory=HashCache)

    def read(self) -> "State":
        p = Path(self.path)
        if self.kind == "raw":
            tree = walk(p, self.cache)
            return State(tree_atoms(tree, self.name), retained={c for (_t, _p, c) in tree.values() if c})
        if self.kind == "tree":
            tree = walk(p, self.cache, self.scratch)
            return State(tree_atoms(tree, self.name), retained={c for (_t, _p, c) in tree.values() if c})
        if self.kind == "git":
            return _git_state(p, self.name, self.cache, self.scratch)
        if self.kind == "memory":
            tree = walk(p, self.cache, self.scratch + (self.file,))
            atoms = tree_atoms(tree, self.name) | memory_atoms(p / self.file, self.name)
            return State(atoms, retained={c for (_t, _p, c) in tree.values() if c})
        if self.kind == "sqlite":
            # the database and its journal files are read as rows, not bytes
            side = tuple(self.file + s for s in ("", "-journal", "-wal", "-shm"))
            tree = walk(p, self.cache, self.scratch + side)
            atoms = tree_atoms(tree, self.name) | sqlite_atoms(p / self.file, self.name)
            return State(atoms, retained={c for (_t, _p, c) in tree.values() if c})
        raise ValueError(f"unknown view kind {self.kind!r}")


@dataclass
class State:
    atoms: set
    retained: set   # content keys present anywhere in this state


def content_key(atom: tuple):
    """What a removed atom carried, or None if it carried no data of its own."""
    kind = atom[1]
    if kind in ("content",):
        return atom[3]
    if kind in ("ref",):
        return atom[3]
    if kind in ("index",):
        return atom[4]
    if kind in ("object",):
        return ("object", atom[2])        # an object that disappears is gone
    if kind in ("type", "perm", "link", "HEAD", "file_exists", "gitstate"):
        return None
    return ("atom", atom)                 # records: retained only if still present


def lost(removed: set, after: list[State]) -> list:
    """Removed atoms whose content is not retained anywhere afterwards."""
    keep = set()
    for s in after:
        keep |= s.retained
        keep |= {("atom", a) for a in s.atoms}
        keep |= {("object", a[2]) for a in s.atoms if a[1] == "object"}
    out = []
    for a in removed:
        k = content_key(a)
        if k is not None and k not in keep:
            out.append(a)
    return sorted(out, key=repr)


# ----------------------------------------------------------------------- git

GIT_STATE_DIRS = {"hooks", "info", "rebase-merge", "rebase-apply", "sequencer"}
GIT_STATE_FILES = {"MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                   "ORIG_HEAD", "FETCH_HEAD", "AUTO_MERGE"}


def _git(repo: Path, *args: str) -> bytes:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_OPTIONAL_LOCKS": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "HOME": str(repo), "LC_ALL": "C"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True).stdout


def _git_state(repo: Path, tag: str, cache: HashCache, scratch: tuple[str, ...]) -> State:
    """Refs, HEAD, index entries, objects, local config, hooks and in-progress
    operation state, worktree. Not the rest of .git's bytes.

    Read only with plumbing and GIT_OPTIONAL_LOCKS=0, so reading does not write.
    """
    atoms: set = set()
    head = (repo / ".git" / "HEAD").read_text().strip()
    atoms.add((tag, "HEAD", head))
    refs = {}
    for line in _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").decode().splitlines():
        name, sha = line.split(" ", 1)
        refs[name] = sha
        atoms.add((tag, "ref", name, sha))
    index_shas = set()
    for rec in _git(repo, "ls-files", "-s", "-z").split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, sha, stage = meta.decode().split()
            atoms.add((tag, "index", path.decode(), mode, sha, stage))
            index_shas.add(sha)
    for line in _git(repo, "cat-file", "--batch-all-objects",
                     "--batch-check=%(objectname) %(objecttype)").decode().splitlines():
        sha, typ = line.split()
        atoms.add((tag, "object", sha, typ))
    reachable = set()
    if refs or not head.startswith("ref:"):
        reachable = {l.split()[0] for l in _git(repo, "rev-list", "--objects", "--all").decode().splitlines() if l.strip()}
    # repository configuration: remotes, hooks path, filters and aliases decide
    # where data goes and what code runs, so every local entry is an atom
    cfg = subprocess.run(["git", "config", "--local", "--list", "-z"], cwd=repo, capture_output=True,
                         env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
                              "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo)}).stdout
    for entry in cfg.split(b"\0"):
        if entry:
            k, _, v = entry.decode(errors="surrogateescape").partition("\n")
            atoms.add((tag, "config", k, v))
    # hooks run code; in-progress operations (bisect, merge, rebase, cherry-pick)
    # change what the next command does. Their files are atoms; the rest of .git
    # (index stat cache, logs, packing, lock files) is not.
    gitdir = repo / ".git"
    for rel, (typ, perm, content) in walk(gitdir, cache).items():
        top = rel.split("/", 1)[0]
        if (top in GIT_STATE_DIRS or top in GIT_STATE_FILES or top.startswith("BISECT_")) and typ != "dir":
            atoms.add((tag, "gitstate", rel, typ, oct(perm), content))
    tree = walk(repo, cache, (".git",) + scratch)
    atoms |= tree_atoms(tree, tag + ":worktree")
    retained = reachable | index_shas | {c for (_t, _p, c) in tree.values() if c}
    return State(atoms, retained)


# -------------------------------------------------------------------- memory

ENTITY_KEYS = {"type", "name", "entityType", "observations"}
RELATION_KEYS = {"type", "from", "to", "relationType"}


def memory_atoms(path: Path, tag: str) -> set:
    """The knowledge-graph file as records, read independently of any server."""
    atoms: set = set()
    if not path.exists():
        return atoms
    atoms.add((tag, "file_exists"))
    for line in path.read_text(encoding="utf-8", errors="surrogateescape").split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            atoms.add((tag, "unparsed_line", line))
            continue
        if isinstance(obj, dict) and obj.get("type") == "entity" and "name" in obj:
            n = obj["name"]
            atoms.add((tag, "entity", n, json.dumps(obj.get("entityType"))))
            obs = obj.get("observations")
            if isinstance(obs, list):
                for o in obs:
                    atoms.add((tag, "obs", n, json.dumps(o)))
            for k, v in obj.items():
                if k not in ENTITY_KEYS or (k == "observations" and not isinstance(v, list)):
                    atoms.add((tag, "entity_field", n, k, json.dumps(v, sort_keys=True)))
        elif isinstance(obj, dict) and obj.get("type") == "relation" and "from" in obj and "to" in obj:
            key = tuple(json.dumps(obj.get(k)) for k in ("from", "relationType", "to"))
            atoms.add((tag, "relation", *key))
            for k, v in obj.items():
                if k not in RELATION_KEYS:
                    atoms.add((tag, "relation_field", *key, k, json.dumps(v, sort_keys=True)))
        else:
            atoms.add((tag, "other_record", json.dumps(obj, sort_keys=True)))
    return atoms


# -------------------------------------------------------------------- sqlite

def sqlite_atoms(db: Path, tag: str) -> set:
    atoms: set = set()
    if not db.exists():
        return atoms
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for typ, name, sql in con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2"):
            atoms.add((tag, "schema", typ, name, sql))
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            counts: dict[str, int] = {}
            for r in con.execute(f'SELECT * FROM "{t}"'):
                k = json.dumps(list(r), default=str)
                counts[k] = counts.get(k, 0) + 1
            for k, c in counts.items():
                for i in range(c):
                    atoms.add((tag, "row", t, k, i))
    finally:
        con.close()
    return atoms
