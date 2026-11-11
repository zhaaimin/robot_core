import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


class RecordingRuntime:
    def __init__(self, runtime: RobotRuntime) -> None:
        self.runtime = runtime
        self.requests: list[TaskRequest] = []

    async def submit_task(self, request: TaskRequest) -> Any:
        self.requests.append(request)
        return await self.runtime.submit_task(request)

    async def get_task_status(self, task_id: str) -> Any:
        return await self.runtime.get_task_status(task_id)

    async def cancel_task(self, task_id: str) -> Any:
        return await self.runtime.cancel_task(task_id)

    def subscribe_task_events(self, task_id: str, after_sequence: int = 0) -> Any:
        return self.runtime.subscribe_task_events(task_id, after_sequence=after_sequence)


@asynccontextmanager
async def running(*, delay_s: float = 0, fail: str | None = None, backend: str = 'simulator', wait_timeout_s: float = 1) -> AsyncIterator[Any]:
    from guide_agent.catalog import DestinationCatalog
    from guide_agent.service import GuideWorkflow

    simulator = Simulator(delay_s=delay_s, fail_skills={fail} if fail else set())
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    wrapper = RecordingRuntime(runtime)
    with tempfile.TemporaryDirectory() as directory:
        workflow = GuideWorkflow(str(Path(directory) / 'guide.db'), wrapper,
                                 DestinationCatalog.from_registry(SkillRegistry.bundled('guide_agent')),
                                 backend=backend, wait_timeout_s=wait_timeout_s)
        await workflow.start()
        try:
            yield workflow, simulator, wrapper, directory
        finally:
            await workflow.close()
            await runtime.close()


async def propose(workflow: Any, session: str = 'one', operation: str = 'propose') -> dict[str, Any]:
    return await workflow.request_escort(session, 'exhibition_hall', operation_id=operation)


async def test_turn_binding_persists_across_restart_and_preserves_canceled_epoch() -> None:
    from guide_agent.errors import GuideError
    from guide_agent.service import GuideWorkflow

    async with running() as (workflow, _, runtime, directory):
        assert await workflow.bind_turn('one', operation_id='turn', expected_epoch=0) == 0
        await workflow.close()
        recovered = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await recovered.start()
        try:
            assert await recovered.saved_turn_epoch('one') == 0
            assert (await recovered.cancel('one', operation_id='stop'))['epoch'] == 1
            assert await recovered.saved_turn_epoch('one') == 0
            assert await recovered.bind_turn('one', operation_id='turn', expected_epoch=0) == 0
            with pytest.raises(GuideError):
                await recovered.request_escort('one', 'exhibition_hall', operation_id='late',
                                               expected_epoch=await recovered.saved_turn_epoch('one'))
            assert not runtime.requests
            assert await recovered.bind_turn('one', operation_id='next-turn', expected_epoch=1) == 1
            assert await recovered.saved_turn_epoch('one') == 1
        finally:
            await recovered.close()


async def test_turn_binding_requires_saved_authority_and_rejects_conflicting_epoch() -> None:
    from guide_agent.errors import GuideError

    async with running() as (workflow, _, _, _):
        with pytest.raises(GuideError):
            await workflow.saved_turn_epoch('one')
        with pytest.raises(GuideError):
            await workflow.bind_turn('one', operation_id='stale', expected_epoch=1)
        with pytest.raises(GuideError):
            await workflow.saved_turn_epoch('one')
        await workflow.bind_turn('one', operation_id='turn', expected_epoch=0)
        with pytest.raises(GuideError):
            await workflow.bind_turn('one', operation_id='turn', expected_epoch=1)
        with pytest.raises(GuideError):
            await workflow.bind_turn('one', operation_id='turn', expected_epoch=True)
        await propose(workflow, operation='other-kind')
        with pytest.raises(GuideError):
            await workflow.bind_turn('one', operation_id='other-kind')


async def test_confirmation_cannot_select_a_replacement_proposal() -> None:
    from guide_agent.errors import GuideError

    async with running() as (workflow, _, runtime, _):
        first = await propose(workflow)
        replacement = await workflow.request_escort('one', 'service_desk', operation_id='replacement')
        assert first['tour_id'] != replacement['tour_id']
        with pytest.raises(GuideError):
            await workflow.confirm_escort('one', operation_id='stale-confirm', expected_tour_id=first['tour_id'])
        with pytest.raises(GuideError):
            await workflow.confirm_escort('one', operation_id='missing-confirm')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('one', operation_id='empty-confirm', expected_tour_id='')
        assert (await workflow.status('one'))['tour_id'] == replacement['tour_id']
        assert not runtime.requests


async def test_confirmation_replay_preserves_the_expected_proposal_identity() -> None:
    from guide_agent.errors import GuideError

    async with running() as (workflow, _, runtime, _):
        proposal = await propose(workflow)
        await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=proposal['tour_id'])
        await workflow.wait_for_settled('one')
        await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=proposal['tour_id'])
        with pytest.raises(GuideError):
            await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id='another-tour')
        assert len(runtime.requests) == 2


async def test_unknown_speech_during_navigation_blocks_arrival_and_keeps_robot_lease() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, _, runtime, _):
        proposal = await propose(workflow)
        started = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=proposal['tour_id'])
        original_status = runtime.get_task_status

        async def lost(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            raise ConnectionError('speech response lost')

        async def arrived(task_id: str) -> Any:
            task = await original_status(task_id)
            return task.model_copy(update={'state': TaskState.SUCCEEDED})

        runtime.submit_task = lost
        assert (await workflow.speak('one', '地点资料', operation_id='speech'))['requires_attention']
        runtime.get_task_status = arrived
        status = await workflow.status('one')
        assert status['navigation_task_id'] == started['navigation_task_id']
        assert status['phase'] == 'reconciliation_required'
        assert not status['introduction_intent']
        assert len(runtime.requests) == 2
        other = await propose(workflow, 'two', 'propose-two')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 2


async def test_arrival_rechecks_unknown_intents_saved_by_another_workflow() -> None:
    from guide_agent.service import GuideWorkflow

    async with running(delay_s=30) as (workflow, _, runtime, directory):
        proposal = await propose(workflow)
        await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=proposal['tour_id'])
        second = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await second.start()
        observing = asyncio.Event()
        speech_saved = asyncio.Event()
        original_status = runtime.get_task_status

        async def arrived(task_id: str) -> Any:
            task = await original_status(task_id)
            observing.set()
            await speech_saved.wait()
            return task.model_copy(update={'state': TaskState.SUCCEEDED})

        async def lost(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            raise ConnectionError('speech response lost')

        runtime.get_task_status = arrived
        observation = asyncio.create_task(workflow.status('one'))
        try:
            async with asyncio.timeout(1):
                await observing.wait()
            runtime.submit_task = lost
            assert (await second.speak('one', '地点资料', operation_id='speech'))['requires_attention']
            speech_saved.set()
            async with asyncio.timeout(1):
                status = await observation
            assert status['phase'] == 'reconciliation_required'
            assert not status['introduction_intent']
            assert len(runtime.requests) == 2
        finally:
            speech_saved.set()
            await observation
            await second.close()


async def test_proposal_requires_confirmation_then_automatically_introduces_once() -> None:
    async with running() as (workflow, simulator, runtime, _):
        assert (await workflow.status('one'))['epoch'] == 0
        proposed = await propose(workflow)
        assert proposed['phase'] == 'awaiting_confirmation'
        assert not runtime.requests
        started = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        assert started['navigation_task_id']
        settled = await workflow.wait_for_settled('one')
        assert settled['phase'] == 'completed', settled
        assert settled['introduction_task_id']
        assert settled['robot_status'] == 'available'
        assert simulator.skills['navigation.navigate_to'].calls == 1
        assert simulator.skills['audio.speak'].calls == 1
        assert runtime.requests[1].steps[0].params['message'] == workflow._catalog.get('exhibition_hall').introduction
        await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        await propose(workflow)
        assert len(runtime.requests) == 2


async def test_mcp_missing_target_rejects_without_submitting() -> None:
    from guide_agent.errors import GuideError

    async with running(backend='mcp') as (workflow, _, runtime, _):
        await propose(workflow)
        with pytest.raises(GuideError):
            await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        assert not runtime.requests
        assert (await workflow.status('one'))['phase'] == 'awaiting_confirmation'


async def test_failed_navigation_never_introduces() -> None:
    async with running(fail='navigation.navigate_to') as (workflow, simulator, _, _):
        await propose(workflow)
        await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        assert (await workflow.wait_for_settled('one'))['phase'] == 'failed'
        assert simulator.skills['audio.speak'].calls == 0


async def test_cancel_invalidates_model_epoch_and_releases_known_stop_lease() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, simulator, runtime, _):
        await propose(workflow)
        first = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        with pytest.raises(GuideError):
            await propose(workflow, 'two', 'propose-two')
            await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=(await workflow.status('two'))['tour_id'])
        canceled = await workflow.cancel('one', operation_id='stop')
        assert canceled['phase'] == 'canceled'
        assert canceled['epoch'] == 1
        assert (await runtime.get_task_status(first['navigation_task_id'])).state == TaskState.CANCELED
        with pytest.raises(GuideError):
            await workflow.request_escort('one', 'service_desk', operation_id='late', expected_epoch=0)
        with pytest.raises(GuideError):
            await workflow.speak('one', '迟到回答', operation_id='late-speech', expected_epoch=0)
        assert simulator.skills['audio.speak'].calls == 0
        await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=(await workflow.status('two'))['tour_id'])
        assert (await workflow.status('two'))['phase'] == 'navigating'


async def test_repeated_wake_does_not_reset_tour_or_repeat_greeting() -> None:
    async with running() as (workflow, simulator, _, _):
        await workflow.wake('one', operation_id='wake')
        await workflow.wake('one', operation_id='wake-two')
        await propose(workflow)
        before = await workflow.status('one')
        after = await workflow.wake('one', operation_id='wake-three')
        assert after['tour_id'] == before['tour_id']
        assert after['phase'] == 'awaiting_confirmation'
        assert simulator.skills['audio.speak'].calls == 1


async def test_event_deduplication_persists_and_rejects_conflicting_reuse() -> None:
    from guide_agent.errors import GuideError

    async with running() as (workflow, _, _, _):
        assert await workflow.claim_event('one', 'event', {'kind': 'wakeup'})
        assert not await workflow.claim_event('one', 'event', {'kind': 'wakeup'})
        with pytest.raises(GuideError):
            await workflow.claim_event('one', 'event', {'kind': 'confirm'})


async def test_unknown_submit_is_persistent_and_never_replayed() -> None:
    from guide_agent.service import GuideWorkflow

    async with running() as (workflow, _, runtime, directory):
        async def lost(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            await runtime.runtime.submit_task(request)
            raise ConnectionError('response lost')
        runtime.submit_task = lost
        await propose(workflow)
        unknown = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        assert unknown['phase'] == 'reconciliation_required'
        assert unknown['robot_status'] == 'quarantined'
        await workflow.close()
        recovered = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await recovered.start()
        try:
            assert (await recovered.status('one'))['phase'] == 'reconciliation_required'
            await recovered.confirm_escort('one', operation_id='confirm', expected_tour_id=(await recovered.status('one'))['tour_id'])
            assert len(runtime.requests) == 1
        finally:
            await recovered.close()


async def test_speech_publishes_task_before_wait_and_can_be_canceled() -> None:
    async with running(delay_s=30, wait_timeout_s=.1) as (workflow, _, runtime, _):
        submitted = asyncio.Event()
        original = runtime.submit_task
        async def signal(request: TaskRequest) -> Any:
            task = await original(request)
            submitted.set()
            return task
        runtime.submit_task = signal
        speech = asyncio.create_task(workflow.speak('one', '请看这里', operation_id='speak'))
        async with asyncio.timeout(1):
            await submitted.wait()
        stopped = await workflow.cancel('one', operation_id='stop')
        observation = await speech
        assert stopped['phase'] == 'canceled'
        assert (await runtime.get_task_status(observation['task_id'])).state == TaskState.CANCELED


async def test_unknown_stop_holds_lease_and_never_introduces() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, simulator, runtime, _):
        await propose(workflow)
        first = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        async def unknown_cancel(task_id: str) -> Any:
            task = await runtime.runtime.get_task_status(task_id)
            return task.model_copy(update={'state': TaskState.RECONCILIATION_REQUIRED})
        runtime.cancel_task = unknown_cancel
        stopped = await workflow.cancel('one', operation_id='stop')
        assert stopped['phase'] == 'reconciliation_required'
        assert stopped['navigation_task_id'] == first['navigation_task_id']
        assert stopped['robot_status'] == 'quarantined'
        await propose(workflow, 'two', 'propose-two')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=(await workflow.status('two'))['tour_id'])
        assert simulator.skills['audio.speak'].calls == 0


async def test_unknown_speech_submission_cannot_be_cleared_by_cancel() -> None:
    async with running() as (workflow, _, runtime, _):
        async def lost(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            raise ConnectionError('speech response lost')
        runtime.submit_task = lost
        result = await workflow.speak('one', '已知资料', operation_id='speak')
        assert result['requires_attention']
        assert (await workflow.cancel('one', operation_id='stop'))['phase'] == 'reconciliation_required'


async def test_unknown_standalone_speech_blocks_navigation_across_restart() -> None:
    from guide_agent.errors import GuideError
    from guide_agent.service import GuideWorkflow

    async with running() as (workflow, _, runtime, directory):
        async def lost(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            raise ConnectionError('speech response lost')

        runtime.submit_task = lost
        assert (await workflow.speak('one', '地点资料', operation_id='speech'))['requires_attention']
        other = await propose(workflow, 'two', 'propose-two')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 1
        await workflow.close()
        recovered = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await recovered.start()
        try:
            assert (await recovered.status('one'))['robot_status'] == 'quarantined'
            another = await propose(recovered, 'three', 'propose-three')
            with pytest.raises(GuideError):
                await recovered.confirm_escort('three', operation_id='confirm-three', expected_tour_id=another['tour_id'])
            assert len(runtime.requests) == 1
        finally:
            await recovered.close()


async def test_releasing_tour_lease_preserves_another_sessions_quarantine() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, _, runtime, _):
        first = await propose(workflow)
        await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=first['tour_id'])

        async def lost(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            raise ConnectionError('speech response lost')

        runtime.submit_task = lost
        assert (await workflow.speak('two', '地点资料', operation_id='speech'))['requires_attention']
        assert workflow._repository._db().execute('SELECT owner FROM guide_robot WHERE id=1').fetchone()[0] == 'one'
        assert (await workflow.cancel('one', operation_id='stop'))['phase'] == 'canceled'
        assert (await workflow.status('two'))['robot_status'] == 'quarantined'
        other = await propose(workflow, 'three', 'propose-three')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('three', operation_id='confirm-three', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 2


async def test_initial_unknown_speech_task_quarantines_until_known_stop() -> None:
    from guide_agent.errors import GuideError
    from guide_agent.service import GuideWorkflow

    async with running(delay_s=30) as (workflow, _, runtime, directory):
        original_submit = runtime.submit_task
        original_status = runtime.get_task_status

        async def unknown_submit(request: TaskRequest) -> Any:
            task = await original_submit(request)
            return task.model_copy(update={'state': TaskState.UNKNOWN})

        async def unknown_status(task_id: str) -> Any:
            task = await original_status(task_id)
            return task.model_copy(update={'state': TaskState.UNKNOWN})

        runtime.submit_task = unknown_submit
        runtime.get_task_status = unknown_status
        speech = await workflow.speak('one', '地点资料', operation_id='speech')
        assert speech['requires_attention']
        assert speech['task_id']
        assert (await workflow.status('one'))['phase'] == 'reconciliation_required'
        other = await propose(workflow, 'two', 'propose-two')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 1
        runtime.submit_task = original_submit
        runtime.get_task_status = original_status
        assert (await workflow.cancel('one', operation_id='stop'))['phase'] == 'canceled'
        await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 2
        await workflow.close()
        recovered = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await recovered.start()
        try:
            assert (await recovered.status('one'))['phase'] == 'canceled'
        finally:
            await recovered.close()


async def test_stopping_quarantined_speech_preserves_another_sessions_navigation_lease() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, _, runtime, _):
        proposal = await propose(workflow)
        started = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=proposal['tour_id'])
        original_submit = runtime.submit_task
        original_status = runtime.get_task_status
        speech_task_id: str | None = None

        async def unknown_submit(request: TaskRequest) -> Any:
            nonlocal speech_task_id
            task = await original_submit(request)
            speech_task_id = task.id
            return task.model_copy(update={'state': TaskState.UNKNOWN})

        async def unknown_speech_status(task_id: str) -> Any:
            task = await original_status(task_id)
            return task.model_copy(update={'state': TaskState.UNKNOWN}) if task_id == speech_task_id else task

        runtime.submit_task = unknown_submit
        runtime.get_task_status = unknown_speech_status
        assert (await workflow.speak('two', '地点资料', operation_id='speech'))['requires_attention']
        assert (await workflow.status('two'))['phase'] == 'reconciliation_required'
        runtime.submit_task = original_submit
        runtime.get_task_status = original_status
        assert (await workflow.cancel('two', operation_id='stop-speech'))['phase'] == 'canceled'
        assert workflow._repository._db().execute('SELECT owner FROM guide_robot WHERE id=1').fetchone()[0] == 'one'
        assert (await runtime.get_task_status(started['navigation_task_id'])).state not in {
            TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED}
        other = await propose(workflow, 'three', 'propose-three')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('three', operation_id='confirm-three', expected_tour_id=other['tour_id'])
        await workflow.cancel('one', operation_id='stop-navigation')
        await workflow.confirm_escort('three', operation_id='confirm-three', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 3


async def test_observed_unresolved_speech_blocks_arrival_until_known_stop() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, _, runtime, _):
        proposal = await propose(workflow)
        started = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=proposal['tour_id'])
        original_status = runtime.get_task_status

        async def unresolved_status(task_id: str) -> Any:
            task = await original_status(task_id)
            state = TaskState.SUCCEEDED if task_id == started['navigation_task_id'] else TaskState.RECONCILIATION_REQUIRED
            return task.model_copy(update={'state': state})

        runtime.get_task_status = unresolved_status
        speech = await workflow.speak('one', '地点资料', operation_id='speech')
        assert speech['requires_attention']
        assert speech['task_id']
        status = await workflow.status('one')
        assert status['phase'] == 'reconciliation_required'
        assert not status['introduction_intent']
        assert len(runtime.requests) == 2
        other = await propose(workflow, 'two', 'propose-two')
        with pytest.raises(GuideError):
            await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=other['tour_id'])
        runtime.get_task_status = original_status
        assert (await workflow.cancel('one', operation_id='stop'))['phase'] == 'canceled'
        await workflow.confirm_escort('two', operation_id='confirm-two', expected_tour_id=other['tour_id'])
        assert len(runtime.requests) == 3


async def test_recovery_observes_known_task_without_replaying_navigation() -> None:
    from guide_agent.service import GuideWorkflow

    async with running(delay_s=.02) as (workflow, simulator, runtime, directory):
        await propose(workflow)
        first = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        recovered = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await recovered.start()
        try:
            settled = await recovered.wait_for_settled('one')
            assert settled['phase'] == 'completed', (settled.get('error'), len(runtime.requests))
            assert settled['navigation_task_id'] == first['navigation_task_id']
            assert simulator.skills['navigation.navigate_to'].calls == 1
            assert simulator.skills['audio.speak'].calls == 1
            assert len(runtime.requests) == 2
        finally:
            await recovered.close()


async def test_resource_version_change_blocks_confirm_but_allows_query_and_cancel() -> None:
    from guide_agent.catalog import DestinationCatalog
    from guide_agent.errors import GuideError
    from guide_agent.service import GuideWorkflow

    async with running() as (workflow, _, runtime, directory):
        await propose(workflow)
        await workflow.close()
        changed = DestinationCatalog(tuple(workflow._catalog._destinations.values()), 'different-content')
        recovered = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, changed, backend='simulator')
        await recovered.start()
        try:
            # close revoked the proposal; restore a genuine persisted proposal for
            # the version boundary rather than asking the changed catalog anew.
            session = recovered._repository.get('one')
            session.phase = 'awaiting_confirmation'
            recovered._repository.save(session)
            assert (await recovered.status('one'))['fingerprint'] != changed.fingerprint
            with pytest.raises(GuideError):
                await recovered.confirm_escort('one', operation_id='confirm', expected_tour_id=(await recovered.status('one'))['tour_id'])
            assert not runtime.requests
            assert (await recovered.cancel('one', operation_id='stop'))['phase'] == 'canceled'
        finally:
            await recovered.close()


async def test_close_stops_active_tasks_without_closing_owned_runtime_and_is_shared() -> None:
    from guide_agent.errors import GuideError

    async with running(delay_s=30) as (workflow, _, runtime, _):
        await propose(workflow)
        first = await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
        await asyncio.gather(workflow.close(), workflow.close())
        assert (await runtime.get_task_status(first['navigation_task_id'])).state == TaskState.CANCELED
        with pytest.raises(GuideError):
            await workflow.wake('one', operation_id='after-close')


async def test_cancel_proposal_clears_target_and_does_not_repeat_on_same_operation() -> None:
    async with running() as (workflow, _, runtime, _):
        await propose(workflow)
        first = await workflow.cancel('one', operation_id='stop')
        second = await workflow.cancel('one', operation_id='stop')
        assert first['epoch'] == second['epoch'] == 1
        assert second['destination_id'] is None
        assert second['phase'] == 'canceled'
        assert not runtime.requests


async def test_single_robot_lease_is_atomic_across_workflow_instances() -> None:
    from guide_agent.errors import GuideError
    from guide_agent.service import GuideWorkflow

    async with running(delay_s=30) as (workflow, _, runtime, directory):
        second = GuideWorkflow(str(Path(directory) / 'guide.db'), runtime, workflow._catalog, backend='simulator')
        await second.start()
        try:
            await propose(workflow)
            await workflow.confirm_escort('one', operation_id='confirm', expected_tour_id=(await workflow.status('one'))['tour_id'])
            await propose(second, 'two', 'propose-two')
            with pytest.raises(GuideError):
                await second.confirm_escort('two', operation_id='confirm-two', expected_tour_id=(await second.status('two'))['tour_id'])
            assert (await second.status('two'))['phase'] == 'awaiting_confirmation'
            assert len(runtime.requests) == 1
        finally:
            await second.close()

