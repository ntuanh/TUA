from __future__ import annotations

import itertools

import pytest

from tua.errors import InvalidTransitionError
from tua.models import ALLOWED_TRANSITIONS, FORCE_MERGE_STATES, TaskStatus, check_transition

S = TaskStatus

EXPECTED = {
    (S.CREATED, S.PREPARING),
    (S.PREPARING, S.RUNNING),
    (S.PREPARING, S.FAILED),
    (S.PREPARING, S.CANCELLED),
    (S.RUNNING, S.COMPLETED),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.TIMEOUT),
    (S.RUNNING, S.CANCELLED),
    (S.COMPLETED, S.MERGED),
    (S.COMPLETED, S.DISCARDED),
    (S.FAILED, S.DISCARDED),
    (S.FAILED, S.MERGED),
    (S.TIMEOUT, S.DISCARDED),
    (S.TIMEOUT, S.MERGED),
    (S.CANCELLED, S.DISCARDED),
    (S.CANCELLED, S.MERGED),
}


def test_transition_table_matches_documented_lifecycle() -> None:
    actual = {(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
    assert actual == EXPECTED


@pytest.mark.parametrize(("current", "new"), list(itertools.product(S, S)))
def test_every_pair(current: TaskStatus, new: TaskStatus) -> None:
    if (current, new) not in EXPECTED:
        with pytest.raises(InvalidTransitionError):
            check_transition(current, new, force=True)
    elif new is S.MERGED and current in FORCE_MERGE_STATES:
        with pytest.raises(InvalidTransitionError, match="--force"):
            check_transition(current, new)
        check_transition(current, new, force=True)
    else:
        check_transition(current, new)


def test_terminal_states_have_no_exits() -> None:
    assert not ALLOWED_TRANSITIONS[S.MERGED]
    assert not ALLOWED_TRANSITIONS[S.DISCARDED]
    assert S.COMPLETED not in {b for (a, b) in EXPECTED if a in (S.FAILED, S.TIMEOUT, S.CANCELLED)}
