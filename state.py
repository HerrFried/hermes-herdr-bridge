"""Authoritative per-session lifecycle tracker, owned by the gateway observer.

Hooks run in the gateway process, so the tracker sees every hook invocation
exactly once and in lock order; nothing here depends on datagram delivery.
Viewers receive only complete, versioned snapshots (see observer.py), so a lost,
duplicated or reordered datagram can never be replayed into turn state.

Displayed state, in precedence order:
  'unknown'  the session is poisoned (more active turns than can be tracked)
  'blocked'  a certain turn has an open human-input request
  'unknown'  some active turn is tainted (its ID was seen started more than once,
             in any session or profile, or had already ended) -- sticky: no end
             can prove which run finished, so it is never removed -- or
             uncertain (interrupted by a session-level end; its own end of a
             uniquely started ID removes it)
  'working'  some turn is active
  outcome    'done' / 'error' / 'idle' / 'unknown' of the latest ended turn
  None       nothing known (nothing is published)

Hermes allows overlapping turns on one session (agent_runtime_helpers.
note_turn_start only logs them), so one turn never ends or evicts another.
"""
from collections import OrderedDict

MAX_ACTIVE = 64


class Turn:
    __slots__ = ('requests', 'uncertain', 'tainted')

    def __init__(self, tainted=False):
        self.requests, self.uncertain, self.tainted = set(), False, tainted

    def certain(self):
        return not (self.uncertain or self.tainted)


class Session:
    def __init__(self):
        self.turns = OrderedDict()
        self.outcome = None
        self.poisoned = False  # latched: an untracked turn may be active forever

    def busy(self):
        """Must not be evicted: forgetting it could hide an active turn."""
        return bool(self.turns) or self.poisoned

    def display(self):
        if self.poisoned:
            return 'unknown'
        if any(t.requests and t.certain() for t in self.turns.values()):
            return 'blocked'
        if any(not t.certain() for t in self.turns.values()):
            return 'unknown'
        if self.turns:
            return 'working'
        return self.outcome

    def open(self):
        if self.turns or self.outcome is not None or self.poisoned:
            return False
        self.outcome = 'idle'
        return True

    def start(self, turn, ambiguous):
        """ambiguous: this ID is or was also seen elsewhere (caller's global view)."""
        if self.poisoned:
            return False
        current = self.turns.get(turn)
        if current is not None:
            # Same ID started twice here: no end can prove which run finished.
            changed = not current.tainted
            current.tainted = True
            return changed
        if len(self.turns) >= MAX_ACTIVE:
            self.poisoned = True
            return True
        self.turns[turn] = Turn(tainted=ambiguous)
        return True

    def taint(self, turn):
        t = self.turns.get(turn)
        if t is None or t.tainted:
            return False
        t.tainted = True
        return True

    def request(self, turn, request):
        t = self.turns.get(turn)
        if t is None or t.tainted or request in t.requests:
            return False
        t.requests.add(request)
        return True

    def resolved(self, turn, request):
        t = self.turns.get(turn)
        if t is None or t.tainted or request not in t.requests:
            return False
        t.requests.remove(request)
        return True

    def end(self, turn, outcome):
        """The finalizer ran for this uniquely started turn: it is over."""
        t = self.turns.get(turn)
        if t is None or t.tainted:
            return False  # tainted is sticky: a duplicate end must not complete anything
        del self.turns[turn]
        self.outcome = outcome
        return True

    def interrupt(self):
        """Session-level interrupted end without a turn ID: no turn is provably over."""
        for t in self.turns.values():
            t.uncertain = True
        return bool(self.turns)


def outcome_of(completed, failed, interrupted):
    if not all(type(x) is bool for x in (completed, failed, interrupted)):
        return 'unknown'
    if interrupted:
        return 'idle'
    if failed:
        return 'error'
    return 'done' if completed else 'unknown'
