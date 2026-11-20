"""Truth-table tests for PushLink transport grace and epoch transitions."""

import asyncio

import pytest

from custom_components.subzero.api import ApiError
from custom_components.subzero.coordinator import PushLink


def make_link(
    *, grace_seconds: float = 0.05
) -> tuple[PushLink, list[ApiError], list[asyncio.Task]]:
    expired: list[ApiError] = []
    tasks: list[asyncio.Task] = []

    def schedule_grace(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    link = PushLink(
        schedule_grace=schedule_grace,
        on_expire=expired.append,
        grace_seconds=grace_seconds,
    )
    return link, expired, tasks


@pytest.mark.parametrize(
    ("lost_flag", "can_grace", "expected_in_grace", "expected_expired"),
    [
        (True, True, True, False),
        (True, False, False, True),
        (False, True, True, False),
        (False, False, False, True),
    ],
)
async def test_first_loss_transitions(lost_flag, can_grace, expected_in_grace, expected_expired):
    link, expired, tasks = make_link()
    assert link.healthy
    assert not link.in_grace
    assert not link.expired
    assert link.epoch == 0

    err = ApiError("Stream disconnected")
    advanced = link.lost(err, lost=lost_flag, can_grace=can_grace)
    try:
        assert advanced is True
        assert link.epoch == 1
        assert link.error is err
        assert not link.healthy
        assert link.in_grace is expected_in_grace
        assert link.expired is expected_expired
        assert len(tasks) == (1 if expected_in_grace else 0)
        assert expired == []
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("lost_flag", "expected_epoch", "expected_advanced"),
    [
        (False, 1, False),
        (True, 2, True),
    ],
)
async def test_repeated_loss_during_grace_does_not_restart_timer(
    lost_flag, expected_epoch, expected_advanced
):
    link, expired, tasks = make_link()
    link.lost(ApiError("First"), lost=True, can_grace=True)
    first_task = tasks[0]
    second_err = ApiError("Second")
    try:
        advanced = link.lost(second_err, lost=lost_flag, can_grace=True)
        assert advanced is expected_advanced
        assert link.epoch == expected_epoch
        assert link.error is second_err
        assert link.in_grace
        assert not link.expired
        assert len(tasks) == 1
        assert not first_task.cancelled()
        assert expired == []
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("lost_flag", "expected_epoch", "expected_advanced"),
    [
        (False, 1, False),
        (True, 2, True),
    ],
)
async def test_repeated_loss_after_expiry_stays_expired(
    lost_flag, expected_epoch, expected_advanced
):
    link, expired, tasks = make_link(grace_seconds=0)
    link.lost(ApiError("First"), lost=True, can_grace=True)
    await tasks[0]
    assert link.expired
    assert len(expired) == 1

    second_err = ApiError("Second")
    advanced = link.lost(second_err, lost=lost_flag, can_grace=True)
    assert advanced is expected_advanced
    assert link.epoch == expected_epoch
    assert link.error is second_err
    assert not link.in_grace
    assert link.expired
    assert len(tasks) == 1


async def test_grace_expiry_fires_callback_with_latest_error():
    link, expired, tasks = make_link(grace_seconds=0)
    link.lost(ApiError("First"), lost=True, can_grace=True)
    latest = ApiError("Latest")
    link.lost(latest, lost=False, can_grace=True)
    await tasks[0]
    assert not link.in_grace
    assert link.expired
    assert expired == [latest]


async def test_cancel_grace_marks_expired_without_firing_callback():
    link, expired, tasks = make_link(grace_seconds=10)
    link.lost(ApiError("First"), lost=True, can_grace=True)
    link.cancel_grace()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert tasks[0].cancelled()
    assert not link.in_grace
    assert link.expired
    assert expired == []


@pytest.mark.parametrize("push_connected", [False, True])
async def test_confirm_while_healthy_is_noop(push_connected):
    link, expired, tasks = make_link()
    link.confirm(push_connected=push_connected)
    assert link.healthy
    assert not link.in_grace
    assert not link.expired
    assert link.epoch == 0
    assert tasks == []
    assert expired == []


@pytest.mark.parametrize(
    ("initial", "push_connected", "expected_healthy", "expected_in_grace", "expected_tasks"),
    [
        ("in_grace", True, True, False, 1),
        ("in_grace", False, False, True, 2),
        ("expired", True, True, False, 1),
        ("expired", False, False, True, 2),
    ],
)
async def test_confirm_transitions(
    initial, push_connected, expected_healthy, expected_in_grace, expected_tasks
):
    link, expired, tasks = make_link(grace_seconds=0 if initial == "expired" else 10)
    link.lost(ApiError("Disconnected"), lost=True, can_grace=True)
    if initial == "expired":
        await tasks[0]
        assert link.expired
    try:
        link.confirm(push_connected=push_connected)
        assert link.healthy is expected_healthy
        assert link.in_grace is expected_in_grace
        assert not link.expired
        assert link.epoch == 1
        assert len(tasks) == expected_tasks
        if initial == "in_grace":
            await asyncio.gather(tasks[0], return_exceptions=True)
            assert tasks[0].cancelled()
            assert expired == []
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)
