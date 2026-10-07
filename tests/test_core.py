"""Core checks: exact rollback, views that ignore noise but see loss, contracts."""

import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

from dssguard import ADDITIVE, LOSSLESS, NONE, ANY, Guard, View, contract_for
from dssguard.checkpoint import Checkpoint
from dssguard.state import HashCache, walk

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="dssg-test-")).resolve()


def git(repo, *a):
    env = {"PATH": os.environ["PATH"], "HOME": str(repo), **GIT_ENV}
    return subprocess.run(["git", *a], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def test_rollback_is_byte_exact():
    root, store = tmp(), tmp()
    (root / "d" / "e").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "d" / "b.txt").write_text("b")
    (root / "ro.txt").write_text("ro"); os.chmod(root / "ro.txt", 0o444)
    os.symlink("a.txt", root / "ln")
    before = walk(root, HashCache())
    ck = Checkpoint(root, store)
    ino = root.stat().st_ino
    # every kind of change
    (root / "a.txt").write_text("changed")
    (root / "new.txt").write_text("new")
    (root / "d" / "b.txt").unlink()
    (root / "d" / "e").rmdir(); (root / "d" / "e").write_text("now a file")
    os.chmod(root / "ro.txt", 0o644); (root / "ro.txt").write_text("rw")
    (root / "ln").unlink(); os.symlink("new.txt", root / "ln")
    (root / "x" / "y").mkdir(parents=True); (root / "x" / "y" / "z").write_text("z")
    assert ck.restore()
    assert walk(root, HashCache()) == before
    assert root.stat().st_ino == ino, "root must be restored in place"


def test_rollback_keeps_open_handles_valid():
    root, store = tmp(), tmp()
    (root / "db").write_bytes(b"original")
    fh = open(root / "db", "rb")
    ck = Checkpoint(root, store)
    (root / "db").write_bytes(b"modified!!")
    assert ck.restore()
    fh.seek(0)
    assert fh.read() == b"original", "a handle opened before the call must see the restored bytes"


def test_git_view_ignores_stat_cache_but_sees_lost_tip():
    repo = tmp()
    git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    (repo / "a.txt").write_text("1\n"); git(repo, "add", "-A"); git(repo, "commit", "-qm", "one")
    git(repo, "checkout", "-qb", "feature")
    (repo / "f.txt").write_text("f\n"); git(repo, "add", "-A"); git(repo, "commit", "-qm", "feat")
    git(repo, "checkout", "-q", "main")
    git(repo, "pack-refs", "--all")
    time.sleep(1.1); git(repo, "update-index", "-q", "--really-refresh")
    v, raw = View("r", repo, "git"), View("r", repo, "raw")
    s0, r0 = v.read(), raw.read()
    # stale stat cache, then a read-only command that refreshes it (the git server's git_status does this)
    p = repo / "a.txt"; st = p.stat(); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    s0, r0 = v.read(), raw.read()
    git(repo, "status")
    s1, r1 = v.read(), raw.read()
    assert s1.atoms == s0.atoms, "decision-sufficient view must ignore the index refresh"
    assert r1.atoms != r0.atoms, "the byte view sees it (this is the ablation's false positive)"
    # now lose the feature tip: force-reset feature to main
    git(repo, "branch", "-f", "feature", "main")
    from dssguard.state import lost
    s2 = v.read()
    gone = lost(s1.atoms - s2.atoms, [s2])
    assert any(a[1] == "ref" and a[2] == "refs/heads/feature" for a in gone)


def test_git_view_sees_config_and_hooks():
    repo = tmp()
    git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    (repo / "a.txt").write_text("1\n"); git(repo, "add", "-A"); git(repo, "commit", "-qm", "one")
    v = View("r", repo, "git")
    s0 = v.read()
    git(repo, "remote", "add", "mirror", "/nonexistent/mirror.git")
    s1 = v.read()
    assert any(a[1] == "config" and a[2] == "remote.mirror.url" for a in s1.atoms - s0.atoms)
    (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    s2 = v.read()
    assert any(a[1] == "gitstate" and a[2] == "hooks/pre-commit" for a in s2.atoms - s1.atoms)
    git(repo, "config", "remote.mirror.url", "/elsewhere.git")
    from dssguard.state import lost
    s3 = v.read()
    assert any(a[2] == "remote.mirror.url" for a in lost(s2.atoms - s3.atoms, [s3])), "overwriting a URL loses it"


def test_contracts():
    assert contract_for(NS(readOnlyHint=True, destructiveHint=None)) == NONE
    assert contract_for(NS(readOnlyHint=False, destructiveHint=False)) == LOSSLESS
    assert contract_for(NS(readOnlyHint=None, destructiveHint=False), ADDITIVE) == ADDITIVE
    assert contract_for(NS(readOnlyHint=None, destructiveHint=None)) == ANY
    assert contract_for(None) == ANY


def test_guard_blocks_and_restores_readonly_write():
    root = tmp()
    (root / "keep.txt").write_text("keep")
    g = Guard([View("w", root, "tree")])
    async def writes():
        (root / "out.md").write_text("written by a read-only tool")
        return "ok"
    async def reads():
        return (root / "keep.txt").read_text()
    ro = NS(readOnlyHint=True, destructiveHint=False)
    _, d1 = asyncio.run(g.call("reads", ro, {}, reads))
    _, d2 = asyncio.run(g.call("writes", ro, {}, writes))
    assert d1.verdict == "committed"
    assert d2.verdict == "blocked" and d2.restored
    assert not (root / "out.md").exists()


def test_fast_path_sees_disguised_write_and_ignores_outside_changes():
    root = tmp()
    (root / "f.txt").write_text("aaaa")
    g = Guard([View("w", root, "tree")])
    ro = NS(readOnlyHint=True, destructiveHint=False)
    async def noop():
        return None
    async def disguised():
        p = root / "f.txt"
        st = p.stat()
        p.write_text("bbbb")                                  # same size
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))       # mtime put back
    _, d0 = asyncio.run(g.call("noop", ro, {}, noop))
    assert d0.verdict == "committed" and d0.ms["roots_changed"] == 0
    (root / "f.txt").write_text("cccc")                       # someone else, between calls
    _, d1 = asyncio.run(g.call("noop", ro, {}, noop))
    assert d1.verdict == "committed", "a change between calls is not the next call's effect"
    _, d2 = asyncio.run(g.call("disguised", ro, {}, disguised))
    assert d2.verdict == "blocked" and d2.restored
    assert (root / "f.txt").read_text() == "cccc"
    _, d3 = asyncio.run(g.call("noop", ro, {}, noop))
    assert d3.verdict == "committed"


def test_lossless_allows_additions_blocks_loss():
    root = tmp()
    (root / "m.jsonl").write_text('{"type":"entity","name":"A","entityType":"p","observations":["x"],"extra":1}\n')
    g = Guard([View("m", root, "memory", "m.jsonl")])
    nd = NS(readOnlyHint=False, destructiveHint=False)
    async def add():
        with open(root / "m.jsonl", "a") as fh:
            fh.write('{"type":"entity","name":"B","entityType":"p","observations":[]}\n')
    async def rewrite_dropping_extra():
        (root / "m.jsonl").write_text('{"type":"entity","name":"A","entityType":"p","observations":["x"]}\n'
                                      '{"type":"entity","name":"B","entityType":"p","observations":[]}\n'
                                      '{"type":"entity","name":"C","entityType":"p","observations":[]}\n')
    _, d1 = asyncio.run(g.call("add", nd, {}, add))
    _, d2 = asyncio.run(g.call("rewrite", nd, {}, rewrite_dropping_extra))
    assert d1.verdict == "committed"
    assert d2.verdict == "blocked" and d2.restored and len(d2.lost) == 1
    assert '"extra":1' in (root / "m.jsonl").read_text()


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except Exception as e:
                fails += 1; print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
