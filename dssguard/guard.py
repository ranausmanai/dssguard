"""DSS-Guard: a tool call is a proposal; commit only what its declaration permits.

The declaration a tool publishes is turned into a contract over the guarded
state:

  readOnlyHint: true                      -> NONE      no atom added or removed
  destructiveHint: false (not read-only)  -> LOSSLESS  nothing that existed is lost
                                             (or ADDITIVE: no atom removed at all,
                                              the spec's literal wording)
  anything else, including no annotations -> ANY       not checked; the spec already
                                              tells clients to confirm these

A checked call runs against a copy-on-write checkpoint of every guarded root.
Afterwards the guard reads the decision-sufficient state again and tests the
contract. If it holds, the call is committed (the checkpoint is dropped). If it
does not, every root is rolled back to the checkpoint and the call returns a
blocked result naming what would have changed. Every decision is logged.

What the guard cannot see, it does not claim: effects outside the guarded
roots (remote services, other directories) and a server's in-process memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .checkpoint import Checkpoint, default_store
from .state import State, View, lost, signature

NONE, LOSSLESS, ADDITIVE, ANY = "none", "lossless", "additive", "any"


def contract_for(annotations: Any, nondestructive: str = LOSSLESS) -> str:
    a = annotations
    ro = getattr(a, "readOnlyHint", None) if a is not None else None
    de = getattr(a, "destructiveHint", None) if a is not None else None
    if ro is True:
        return NONE
    if de is False:
        return nondestructive
    return ANY


@dataclass
class Decision:
    tool: str
    contract: str
    verdict: str                 # committed | blocked | passthrough | error
    added: int = 0
    removed: int = 0
    lost: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    restored: bool | None = None
    ms: dict = field(default_factory=dict)

    def record(self, args: dict) -> dict:
        d = self.__dict__.copy()
        d["args_sha256"] = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
        d["lost"] = [list(map(str, a)) for a in self.lost[:20]]
        return d


def _short(atom: tuple) -> str:
    parts = [str(x) for x in atom]
    s = " ".join(parts)
    return s if len(s) <= 120 else s[:117] + "..."


class Guard:
    def __init__(self, views: list[View], *, nondestructive: str = LOSSLESS,
                 store: Path | None = None, log: Path | None = None,
                 enforce: bool = True):
        self.views = views
        self.nondestructive = nondestructive
        self.store = Path(store) if store else default_store()
        self.log = Path(log) if log else None
        self.enforce = enforce          # False: observe and log, never roll back
        self.lock = asyncio.Lock()      # one call at a time: effects must be attributable
        self.decisions: list[Decision] = []
        # view of each root at the stat signature it was read at; reused while
        # the signature is unchanged, so an idle root is never re-read
        self._cache: list[tuple[dict, State] | None] = [None] * len(views)

    def _state(self, i: int, sig: dict) -> State:
        c = self._cache[i]
        if c is not None and c[0] == sig:
            return c[1]
        st = self.views[i].read()
        self._cache[i] = (sig, st)
        return st

    async def call(self, tool: str, annotations: Any, args: dict,
                   invoke: Callable[[], Awaitable[Any]]) -> tuple[Any, Decision]:
        contract = contract_for(annotations, self.nondestructive)
        async with self.lock:
            if contract == ANY:
                t0 = time.perf_counter()
                result = await invoke()
                d = Decision(tool, contract, "passthrough",
                             ms={"call": (time.perf_counter() - t0) * 1e3})
                self._emit(d, args)
                return result, d
            return await self._checked(tool, contract, args, invoke)

    async def _checked(self, tool, contract, args, invoke):
        ms = {}
        t = time.perf_counter()
        # the checkpoint (one clonefile per root, in the kernel) and the stat walk
        # only read the roots, so they run concurrently
        ck_task = asyncio.create_task(asyncio.to_thread(
            lambda: [Checkpoint(v.path, self.store) for v in self.views]))
        sig0 = [signature(v.path) for v in self.views]
        before = [self._state(i, s) for i, s in enumerate(sig0)]
        ms["read_before"] = (time.perf_counter() - t) * 1e3
        cks = await ck_task
        ms["checkpoint"] = (time.perf_counter() - t) * 1e3   # read_before and checkpoint overlap
        t = time.perf_counter()
        try:
            result = await invoke()
        except BaseException:
            ok = all(c.restore() for c in cks) if self.enforce else None
            for c in cks:
                c.discard()
            self._cache = [None] * len(self.views)
            self._emit(Decision(tool, contract, "error", restored=ok, ms=ms), args)
            raise
        ms["call"] = (time.perf_counter() - t) * 1e3
        t = time.perf_counter()
        sig1 = [signature(v.path) for v in self.views]
        # a root whose signature did not move cannot have changed: reuse its view
        after = [before[i] if sig1[i] == sig0[i] else self._state(i, sig1[i])
                 for i in range(len(self.views))]
        ms["read_after"] = (time.perf_counter() - t) * 1e3
        ms["roots_changed"] = sum(a != b for a, b in zip(sig0, sig1))

        added, removed = set(), set()
        for b, a in zip(before, after):
            added |= a.atoms - b.atoms
            removed |= b.atoms - a.atoms
        gone = lost(removed, after)
        if contract == NONE:
            violated = bool(added or removed)
        elif contract == ADDITIVE:
            violated = bool(removed)
        else:  # LOSSLESS
            violated = bool(gone)

        d = Decision(tool, contract, "blocked" if violated else "committed",
                     added=len(added), removed=len(removed), lost=gone,
                     examples=[_short(x) for x in sorted(gone or removed or added, key=repr)[:5]],
                     ms=ms)
        t = time.perf_counter()
        if violated and self.enforce:
            d.restored = all(c.restore() for c in cks)
            ms["restore"] = (time.perf_counter() - t) * 1e3
            # bytes are back, so the decision-sufficient state is `before` again
            self._cache = [(signature(v.path), b) for v, b in zip(self.views, before)]
        for c in cks:
            c.discard()
        self._emit(d, args)
        return result, d

    def _emit(self, d: Decision, args: dict):
        self.decisions.append(d)
        if self.log:
            with open(self.log, "a") as fh:
                fh.write(json.dumps(d.record(args), default=str) + "\n")


def explain(d: Decision) -> str:
    """The text a blocked call returns to the agent and the user."""
    what = {NONE: "declares readOnlyHint=true (no changes)",
            LOSSLESS: "declares destructiveHint=false (nothing existing is lost)",
            ADDITIVE: "declares destructiveHint=false (additive changes only)"}[d.contract]
    lines = [f"DSS-Guard blocked '{d.tool}': the tool {what}, but this call "
             f"added {d.added} and removed {d.removed} state facts"
             + (f", losing {len(d.lost)}" if d.lost else "") + ".",
             "All changes were rolled back." if d.restored else
             "WARNING: rollback could not be verified.",
             "Observed changes (first few):"]
    lines += [f"  - {e}" for e in d.examples]
    lines.append("If this effect is intended, the tool's annotations should declare it, "
                 "and the call should go through confirmation.")
    return "\n".join(lines)
