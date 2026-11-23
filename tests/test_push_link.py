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
    assert link.established
    assert not link.in_grace
    assert not link.expired
    assert link.epoch == 0

    err = ApiError("Stream disconnected")
    advanced = link.fail(err, lost=lost_flag, can_grace=can_grace)
    try:
        assert advanced is True
        assert link.epoch == 1
        assert link.error is err
        assert not link.established
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
    link.fail(ApiError("First"), lost=True, can_grace=True)
    first_task = tasks[0]
    second_err = ApiError("Second")
    try:
        advanced = link.fail(second_err, lost=lost_flag, can_grace=True)
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
    link.fail(ApiError("First"), lost=True, can_grace=True)
    await tasks[0]
    assert link.expired
    assert len(expired) == 1

    second_err = ApiError("Second")
    advanced = link.fail(second_err, lost=lost_flag, can_grace=True)
    assert advanced is expected_advanced
    assert link.epoch == expected_epoch
    assert link.error is second_err
    assert not link.in_grace
    assert link.expired
    assert len(tasks) == 1


async def test_grace_expiry_fires_callback_with_latest_error():
    link, expired, tasks = make_link(grace_seconds=0)
    link.fail(ApiError("First"), lost=True, can_grace=True)
    latest = ApiError("Latest")
    link.fail(latest, lost=False, can_grace=True)
    await tasks[0]
    assert not link.in_grace
    assert link.expired
    assert expired == [latest]


async def test_cancel_grace_marks_expired_without_firing_callback():
    link, expired, tasks = make_link(grace_seconds=10)
    link.fail(ApiError("First"), lost=True, can_grace=True)
    link.cancel_grace()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert tasks[0].cancelled()
    assert not link.in_grace
    assert link.expired
    assert expired == []


@pytest.mark.parametrize("push_connected", [False, True])
async def test_confirm_before_the_channel_opens_leaves_the_link_in_grace(push_connected):
    link, expired, tasks = make_link(grace_seconds=10)
    try:
        # A read that setup makes before the appliance's channel has opened.
        link.confirm(push_connected=push_connected)
        assert not link.established
        assert link.in_grace
        assert link.epoch == 0
        # The channel opening makes that read stale, and a later read confirms.
        assert link.channel_opened()
        assert link.epoch == 1
        link.confirm(push_connected=True)
        assert link.established
        assert not link.in_grace
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert expired == []


async def test_confirm_on_an_established_link_changes_nothing():
    link, expired, tasks = make_link()
    link.channel_opened()
    link.confirm(push_connected=True)
    assert link.established
    assert not link.in_grace
    assert not link.expired
    assert link.epoch == 1
    assert tasks == []
    assert expired == []


@pytest.mark.parametrize(
    ("initial", "push_connected", "expected_established", "expected_in_grace", "expected_tasks"),
    [
        ("in_grace", True, True, False, 1),
        ("in_grace", False, False, True, 2),
        ("expired", True, True, False, 1),
        ("expired", False, False, True, 2),
    ],
)
async def test_confirm_transitions(
    initial, push_connected, expected_established, expected_in_grace, expected_tasks
):
    link, expired, tasks = make_link(grace_seconds=0 if initial == "expired" else 10)
    link.fail(ApiError("Disconnected"), lost=True, can_grace=True)
    if initial == "expired":
        await tasks[0]
        assert link.expired
    try:
        if push_connected:
            # The stream is back and the appliance's channel has opened on it.
            link.channel_opened()
        link.confirm(push_connected=push_connected)
        assert link.established is expected_established
        assert link.in_grace is expected_in_grace
        assert not link.expired
        assert link.epoch == (2 if push_connected else 1)
        assert len(tasks) == expected_tasks
        if initial == "in_grace":
            await asyncio.gather(tasks[0], return_exceptions=True)
            assert tasks[0].cancelled()
            assert expired == []
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_refused_channel_needs_a_read_after_it_reopens():
    link, expired, tasks = make_link(grace_seconds=10)
    error = ApiError("Channel refused")
    link.channel_failed(error)
    assert not link.established
    assert link.channel_refused
    assert not link.channel_open
    assert link.expired
    assert link.epoch == 0
    try:
        # A read while the channel is down keeps state for a grace window only.
        link.confirm(push_connected=True)
        assert not link.established
        assert link.in_grace
        link.channel_opened()
        # A read from before the channel opened is no longer current.
        assert link.epoch == 1
        assert not link.established
        link.confirm(push_connected=True)
        assert link.established
        assert not link.in_grace
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert expired == []


async def test_stream_failure_replaces_a_refused_channel():
    link, _, tasks = make_link()
    link.channel_failed(ApiError("Channel refused"))
    advanced = link.fail(ApiError("Stream disconnected"), lost=True, can_grace=True)
    assert advanced
    assert not link.channel_refused
    assert not link.channel_open
    assert link.epoch == 1
    assert tasks == []


async def test_read_on_a_returned_stream_needs_the_channel_open_to_confirm():
    link, _, tasks = make_link(grace_seconds=10)
    link.fail(ApiError("Stream disconnected"), lost=True, can_grace=True)
    try:
        link.confirm(push_connected=True)
        assert not link.established
        assert link.in_grace
        link.channel_opened()
        link.channel_opened()
        assert link.epoch == 2
        link.confirm(push_connected=True)
        assert link.established
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_first_channel_opening_outdates_a_read_from_before_it():
    link, _, tasks = make_link()
    assert link.channel_opened()
    assert link.established
    assert link.epoch == 1
    assert not link.channel_opened()
    assert link.epoch == 1
    assert tasks == []


async def test_unconfirm_requires_a_read_without_changing_the_connection():
    link, _, tasks = make_link(grace_seconds=10)
    link.channel_opened()
    first, later = ApiError("Read failed"), ApiError("Read failed again")
    link.unconfirm(first)
    link.unconfirm(later)
    assert link.error is first
    assert link.epoch == 1
    assert link.expired
    try:
        # Fresh state is kept available while a read has yet to confirm it.
        link.extend_grace()
        assert link.in_grace
        link.confirm(push_connected=True)
        assert link.established
    finally:
        link.cancel_grace()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_extend_grace_is_a_noop_while_live_updates_are_established():
    link, _, tasks = make_link()
    link.extend_grace()
    assert link.established
    assert tasks == []
