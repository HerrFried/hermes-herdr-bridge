"""Hermes observer hooks. Human events fail closed without a proven turn."""
import collections
import hashlib
import os
import threading
import time

from . import introspect, peer, relay
from .state import Session, outcome_of

MAX_SESSIONS = 256
MAX_ENDED = 500000  # inserts before the ended-ID filter saturates (then: unknown everywhere)
ENDED_BITS = 1 << 23  # 1 MiB; false-positive rate stays below ~0.2% up to MAX_ENDED
ENDED_HASHES = 4
REFRESH_SECONDS = 2.0
PUBLISH_BUDGET = 1.0

# Every hook updates the authoritative tracker, stamps a new version and marks
# the session dirty inside ONE critical section with no I/O, so state changes
# are atomic and ordered by lock acquisition. A single daemon thread publishes
# complete snapshots (registry scan, ps, sendto) outside the lock, and
# republishes every known session each REFRESH_SECONDS so viewers can expire
# (lease) a snapshot that stops being confirmed.
_cond = threading.Condition(threading.Lock())
_sessions = collections.OrderedDict()  # (home, session) -> Session, LRU order
_turn_owner = {}  # turn -> set of (home, session) holding an active run of it, any profile
# Bloom filter of every ended (or ended-before-start) turn ID: no false negatives, so a
# late duplicate start is always uncertain; a false positive only yields 'unknown'.
_ended_bits = bytearray(ENDED_BITS // 8)
_ended_count = 0
_versions = {}  # (home, session) -> version stamped at its latest change
_dirty = set()
_degraded = False  # latched: a busy session could not be tracked
_worker = None
_pid = None
_busy = False
_source = None


def _valid(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(32 <= ord(c) < 127 for c in value)


def _source_identity():
    global _source
    if _source is None:
        start = relay.process_identity(os.getpid())
        _source = '{}:{}'.format(os.getpid(), start) if start else ''
    return _source or None


def _snapshots(keys):
    """Called with _cond held: complete, versioned snapshots grouped by home."""
    out = {}
    for key in keys:
        session = _sessions.get(key)
        state = 'unknown' if _degraded else (session.display() if session else None)
        if state is None or key not in _versions:
            continue
        out.setdefault(key[0], {})[key[1]] = {'type': 'state', 'session': key[1], 'state': state,
                                              'seq': _versions[key]}
    return out


def _run_worker():
    global _busy
    next_refresh = time.monotonic()
    while True:
        with _cond:
            while not _dirty and time.monotonic() < next_refresh:
                _busy = False
                _cond.notify_all()
                _cond.wait(max(0, next_refresh - time.monotonic()))
            _busy = True
            if time.monotonic() >= next_refresh:
                keys = list(_sessions)
                next_refresh = time.monotonic() + REFRESH_SECONDS
            else:
                keys = list(_dirty)
            _dirty.difference_update(keys)
            batches = _snapshots(keys)
        source = _source_identity()
        for home, events in batches.items():
            try:
                if source:
                    for event in events.values():
                        event['src'] = source
                relay.publish_many(home, events, budget=PUBLISH_BUDGET)
            except Exception:
                pass  # one failed publish must not stop later ones
        try:
            if _identity_enabled():
                _publish_identity(source)
        except Exception:
            pass


IDENTITY_FLAG = 'HERMES_HERDR_PEER_IDENTITY'


def _identity_enabled():
    """Experimental, off by default; only the gateway process that serves the API."""
    home = None
    if IDENTITY_FLAG not in os.environ:
        try:
            home = _home()
        except Exception:
            return False
    return relay.peer_identity_enabled(home) and introspect.runner() is not None and peer.self_test()


def _combined(members):
    """One display for a compression lineage: [(busy, display, version)] -> state or None."""
    busy = [display for active, display, _ in members if active]
    if 'unknown' in busy:
        return 'unknown'
    if 'blocked' in busy:
        return 'blocked'
    if busy:
        return 'working'
    settled = [(version, display) for _, display, version in members if display is not None]
    return max(settled)[1] if settled else None


def _publish_identity(source):
    """Send each identity registration the state of the lineage its own process
    tree's gateway connection is subscribed to; 'unknown' whenever unproven."""
    stable = None
    with _cond:
        degraded = _degraded
        tracked = {key: (s.busy(), s.display(), _versions.get(key, 0)) for key, s in _sessions.items()}
    for _ in range(3):
        first = introspect.subscriptions()
        if first is None:
            break
        with _cond:
            degraded = _degraded
            tracked = {key: (s.busy(), s.display(), _versions.get(key, 0)) for key, s in _sessions.items()}
        if introspect.subscriptions() == first:
            stable = first  # no subscription moved while the state was copied
            break
    seq = time.monotonic_ns()
    active = introspect.runner()
    profiles = {introspect._profile(a.profile_id) for a in (introspect.authorities(active) or ())} if active else set()
    groups, unresolved = {}, set()
    if stable is not None:
        for (home, session_id), info in tracked.items():
            profile = introspect._profile(home)
            owner = introspect.logical_owner(profile, session_id)
            if owner is None:
                unresolved.add(profile)
            else:
                groups.setdefault((profile, owner), []).append(info)
    # Speak only for profiles this instance has hooks for: another (or a stale) plugin
    # instance in the same process must not override their owner with 'unknown'.
    profiles &= {introspect._profile(home) for home, _ in tracked}
    for profile in profiles:
        for rec in relay.identity_records(profile) or ():
            event = {'type': 'state', 'mode': 'peer', 'state': 'unknown', 'session': '', 'peers': [], 'seq': seq}
            if source:
                event['src'] = source
            try:
                tree = None
                if stable is not None and not degraded and relay.process_identity(rec['pid']) == rec['start']:
                    tree = peer.tree_endpoints(rec['pid'], rec['start_us'])
                if tree is not None:
                    matched = [endpoint for endpoint in tree if endpoint in stable]
                    keys = set().union(*(stable[e] for e in matched)) if matched else set()
                    lineages = {(p, introspect.logical_owner(p, sid)) for p, sid in keys}
                    if len(lineages) == 1:
                        (lineage,) = lineages
                        state = _combined(groups.get(lineage, []))
                        if (lineage[1] is not None and lineage[0] == profile
                                and profile not in unresolved and state is not None):
                            event.update(state=state, session=lineage[1], peers=[list(e) for e in matched])
                relay.send_to(profile, rec, event)
            except Exception:
                continue  # one registration must not stop the others


def _ensure_worker():
    """Called with _cond held. Reset after a fork (never inherit a parent's turns)."""
    global _worker, _pid, _source, _degraded, _ended_bits, _ended_count
    if _pid != os.getpid():
        _sessions.clear(); _turn_owner.clear(); _versions.clear(); _dirty.clear()
        _ended_bits, _ended_count = bytearray(ENDED_BITS // 8), 0
        _worker, _pid, _source, _degraded = None, os.getpid(), None, False
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_worker, name='hermes-herdr-publisher', daemon=True)
        _worker.start()


def _session(key, create):
    """Called with _cond held. Bounded LRU that never forgets a busy session."""
    session = _sessions.get(key)
    if session is not None:
        _sessions.move_to_end(key)
        return session
    if not create:
        return None
    if len(_sessions) >= MAX_SESSIONS:
        idle = next((k for k, v in _sessions.items() if not v.busy()), None)
        if idle is None:
            _degrade()  # an untracked turn could hide anywhere: unknown everywhere
            return None
        del _sessions[idle]
        _versions.pop(idle, None)
    session = _sessions[key] = Session()
    return session


def _changed(key):
    _versions[key] = time.monotonic_ns()
    _dirty.add(key)
    _cond.notify_all()


def _positions(turn):
    digest = hashlib.blake2b(turn.encode(), digest_size=4 * ENDED_HASHES).digest()
    return [int.from_bytes(digest[i:i + 4], 'big') % ENDED_BITS for i in range(0, 4 * ENDED_HASHES, 4)]


def _was_ended(turn):
    return all(_ended_bits[p >> 3] & (1 << (p & 7)) for p in _positions(turn))


def _degrade():
    """Called with _cond held. Latched until the gateway process is replaced."""
    global _degraded
    _degraded = True
    for key in _sessions:
        _changed(key)  # new version: 'unknown' must never share a version with older content


def _remember_ended(turn):
    global _ended_count
    for p in _positions(turn):
        _ended_bits[p >> 3] |= 1 << (p & 7)
    _ended_count += 1
    if _ended_count > MAX_ENDED:
        _degrade()  # the filter's error rate is no longer bounded


def _apply(home, kind, session_id, turn=None, request=None, outcome=None):
    """The whole state transition, under one lock, with no I/O."""
    with _cond:
        _ensure_worker()
        if _degraded:
            return
        if kind == 'start':
            key = (home, session_id)
            session = _session(key, True)
            if session is None:
                return
            holders = _turn_owner.setdefault(turn, set())
            # Turn IDs are not enforced unique: if this ID is active anywhere else
            # (any session, any profile home) or ever ended, every run of it is
            # ambiguous, and no later end can prove which run finished.
            ambiguous = _was_ended(turn) or bool(holders)
            if session.start(turn, ambiguous):
                _changed(key)
            if turn in session.turns:
                holders.add(key)
            if not holders:
                del _turn_owner[turn]
            if ambiguous:
                for other in holders:
                    held = _sessions.get(other)
                    if held is not None and held.taint(turn):
                        _changed(other)
            return
        if kind == 'open':
            key = (home, session_id)
            session = _session(key, True)
            if session is not None and session.open():
                _changed(key)
            return
        if kind == 'interrupt':
            key = (home, session_id)
            session = _session(key, False)
            if session is not None and session.interrupt():
                _changed(key)
            return
        # A uniquely started turn belongs to the session where it started, even after
        # compression moved it to a child session (the end then names the child).
        # Tainted (colliding) IDs never route: they stay unknown in every holder.
        holders = _turn_owner.get(turn, ())
        key = next(iter(holders)) if len(holders) == 1 else None
        session = _sessions.get(key) if key else None
        if session is None or key[0] != home or session.turns[turn].tainted:
            if kind == 'end':
                _remember_ended(turn)  # a racing or later start of this ID becomes tainted
            return
        if kind == 'request':
            changed = session.request(turn, request)
        elif kind == 'resolved':
            changed = session.resolved(turn, request)
        else:
            changed = session.end(turn, outcome)
            _remember_ended(turn)
            if turn not in session.turns:
                del _turn_owner[turn]
        if changed:
            _changed(key)


def _flush(timeout=2.0):
    """Wait until every change was handed to the transport (tests, shutdown)."""
    deadline = time.monotonic() + timeout
    with _cond:
        _ensure_worker()
        while _dirty or _busy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _cond.wait(remaining)
    return True


def _home():
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def _event(kind, session, turn, outcome=None, request=None):
    try:
        if not _valid(session):
            return
        if kind == 'end' and turn in (None, '') and outcome is not None and outcome[2] is True:
            # Session-level interrupted end without a turn (tui_gateway finalize,
            # TUI shutdown): no active turn is provably over or still running.
            _apply(_home(), 'interrupt', session)
            return
        if not _valid(turn):
            return
        if kind in ('request', 'resolved') and not _valid(request):
            return
        home = _home()  # before the lock: may consult context, never held across I/O
        if kind == 'pre_llm_call':
            _apply(home, 'start', session, turn)
        elif kind == 'end':
            if outcome is None:
                return
            _apply(home, 'end', session, turn, outcome=outcome_of(*outcome))
        else:
            _apply(home, kind, session, turn, request=request)
    except Exception:
        return  # observer must never affect Hermes


def on_session_open(**kwargs):
    # A resumed session might already have a running turn. Never guess idle.
    if kwargs.get('platform') != 'cli' or kwargs.get('reason') != 'create':
        return
    session = kwargs.get('session_id')
    try:
        if _valid(session):
            # The CLI may write its breadcrumb after this hook; the periodic
            # republication delivers the snapshot once it does.
            _apply(_home(), 'open', session)
    except Exception:
        pass


def pre_llm_call(**kwargs):
    _event('pre_llm_call', kwargs.get('session_id'), kwargs.get('turn_id'))


def on_session_end(**kwargs):
    _event('end', kwargs.get('session_id'), kwargs.get('turn_id'),
           (kwargs.get('completed'), kwargs.get('failed'), kwargs.get('interrupted')))


def _human_event(event_type, **kwargs):
    # The public human-input payload lacks turn_id. This compatibility adapter
    # reads Hermes's per-context correlation IDs, not process-wide state. If
    # either context variable changes across an upgrade, fail closed.
    try:
        from tools import approval_context
        session = kwargs.get('session_id')
        context_session = approval_context._approval_session_id.get()
        turn = approval_context._approval_turn_id.get()
        if not _valid(session) or session != context_session or not _valid(turn):
            return
        _event(event_type, session, turn, request=kwargs.get('request_id'))
    except Exception:
        return


def on_human_input_request(**kwargs):
    _human_event('request', **kwargs)


def on_human_input_resolved(**kwargs):
    _human_event('resolved', **kwargs)


def correlate_tool_execution(tool_name, args, next_call, session_id=None, turn_id=None,
                             tool_call_id=None, **kwargs):
    """Bind correlation for inline clarify, forwarding its call exactly once.

    Hermes currently omits approval observability context for inline clarify.
    This scoped middleware fills that gap only when the execution contract
    supplies valid IDs. It leaves all other tools and all return values alone.
    """
    if (tool_name != 'clarify' or not isinstance(session_id, str) or not isinstance(turn_id, str)
            or not _valid(session_id) or not _valid(turn_id)):
        return next_call(args)
    try:
        from tools.approval_context import set_current_observability_context, reset_current_observability_context
        tokens = set_current_observability_context(session_id=session_id, turn_id=turn_id,
                                                   tool_call_id=tool_call_id or '')
    except Exception:
        return next_call(args)
    try:
        return next_call(args)
    finally:
        try:
            reset_current_observability_context(tokens)
        except Exception:
            pass


def _verify_contract():
    try:
        from hermes_cli.plugins import VALID_HOOKS, VALID_MIDDLEWARE
        hooks = {'on_session_open', 'pre_llm_call', 'on_session_end',
                 'on_human_input_request', 'on_human_input_resolved'}
        if 'tool_execution' not in VALID_MIDDLEWARE:
            raise RuntimeError('Hermes tool_execution middleware is unsupported; bridge disabled')
        if not hooks.issubset(VALID_HOOKS):
            raise RuntimeError('Hermes lifecycle hooks are incompatible; bridge disabled')
    except (ImportError, AttributeError, TypeError) as exc:
        raise RuntimeError('Cannot verify Hermes plugin contract; bridge disabled') from exc
    try:
        import inspect
        from tools import approval_context
        for name in ('_approval_session_id', '_approval_turn_id'):
            variable = getattr(approval_context, name)
            if not all(callable(getattr(variable, method, None)) for method in ('get', 'set', 'reset')):
                raise TypeError('invalid approval context variable')
        inspect.signature(approval_context.set_current_observability_context).bind(
            session_id='session', turn_id='turn', tool_call_id='tool')
        inspect.signature(approval_context.reset_current_observability_context).bind(object())
        # Exercise the private binding in a fresh Context, so even an incompatible
        # reset cannot leak values into the loading thread's existing context.
        import contextvars
        def probe():
            session_var = approval_context._approval_session_id
            turn_var = approval_context._approval_turn_id
            session_var.set('bridge-previous-session')
            turn_var.set('bridge-previous-turn')
            tokens = approval_context.set_current_observability_context(
                session_id='bridge-probe-session', turn_id='bridge-probe-turn', tool_call_id='bridge-probe-tool')
            try:
                if session_var.get() != 'bridge-probe-session' or turn_var.get() != 'bridge-probe-turn':
                    raise ValueError('approval context did not bind the requested identifiers')
            finally:
                approval_context.reset_current_observability_context(tokens)
            if session_var.get() != 'bridge-previous-session' or turn_var.get() != 'bridge-previous-turn':
                raise ValueError('approval context did not restore previous identifiers')
        contextvars.Context().run(probe)
    except Exception as exc:
        raise RuntimeError('Hermes approval context contract is incompatible; bridge disabled') from exc


def register(ctx):
    register_middleware = getattr(ctx, 'register_middleware', None)
    if not callable(register_middleware):
        raise RuntimeError('Hermes tool_execution middleware is unavailable; bridge disabled')
    _verify_contract()
    ctx.register_hook('on_session_open', on_session_open)
    ctx.register_hook('pre_llm_call', pre_llm_call)
    ctx.register_hook('on_session_end', on_session_end)
    ctx.register_hook('on_human_input_request', on_human_input_request)
    ctx.register_hook('on_human_input_resolved', on_human_input_resolved)
    register_middleware('tool_execution', correlate_tool_execution)
