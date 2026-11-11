import asyncio
import io
from contextlib import aclosing

import pytest


async def test_text_input_provides_final_utterances_and_trusted_controls() -> None:
    from guide_agent.interaction import TextInteractionSource

    source = TextInteractionSource(io.StringIO('/wake\n展厅在哪里\n确认带路\n停止\n/status\n/wait\n/exit\nignored\n'),
                                   session_id='visitor')
    async with aclosing(source.events()) as events:
        received = [event async for event in events]
    assert [event.kind for event in received] == ['wakeup', 'utterance', 'confirm', 'cancel', 'status', 'wait']
    assert received[1].text == '展厅在哪里'
    assert all(event.session_id == 'visitor' and event.final for event in received)
    assert len({event.event_id for event in received}) == len(received)


async def test_json_input_keeps_stable_ids_and_partial_result_boundary() -> None:
    from guide_agent.interaction import JsonInteractionSource

    source = JsonInteractionSource(io.StringIO(
        '{"event_id":"wake-1","kind":"wakeup","session_id":"visitor"}\n'
        '{"event_id":"speech-1","kind":"utterance","session_id":"visitor","text":"展","final":false}\n'
        '{"event_id":"speech-1","kind":"utterance","session_id":"visitor","text":"展厅在哪","final":true}\n',
    ))
    async with aclosing(source.events()) as events:
        received = [event async for event in events]
    assert [event.event_id for event in received] == ['wake-1', 'speech-1', 'speech-1']
    assert [event.final for event in received] == [True, False, True]
    assert received[-1].text == '展厅在哪'


async def test_json_input_rejects_ambiguous_or_unbounded_events() -> None:
    from guide_agent.interaction import JsonInteractionSource
    from robot_agent_core.errors import AgentConfigurationError

    for line in (
        '{}', '{"event_id":"a","kind":"unknown","session_id":"v"}',
        '{"event_id":"a","kind":"utterance","session_id":"v","text":"hello","final":"false"}',
        '{"event_id":"a","kind":"utterance","session_id":"v","text":""}',
        '{"event_id":"a","kind":"cancel","session_id":"v","destination":"invented"}',
        '{"event_id":"a","event_id":"b","kind":"cancel","session_id":"v"}',
        '{"event_id":"a","kind":"cancel","session_id":"v","proposal_id":"x"}',
        'x' * (64 * 1024 + 1),
    ):
        source = JsonInteractionSource(io.StringIO(line + '\n'))
        async with aclosing(source.events()) as events:
            with pytest.raises(AgentConfigurationError):
                await anext(events)


async def test_input_port_contract_is_reusable() -> None:
    from guide_agent.interaction import TextInteractionSource
    from guide_agent.ports import InteractionSource

    async def check(source: InteractionSource) -> None:
        async with aclosing(source.events()) as events:
            async with asyncio.timeout(1):
                received = await anext(events)
            assert received.session_id and received.event_id
            assert received.kind == 'wakeup'

    await check(TextInteractionSource(io.StringIO('/wake\n'), session_id='visitor'))


async def test_json_confirmation_can_explicitly_bind_a_presented_proposal() -> None:
    from guide_agent.interaction import JsonInteractionSource

    source = JsonInteractionSource(io.StringIO(
        '{"event_id":"a","kind":"confirm","session_id":"v","proposal_id":"tour-1"}\n',
    ))
    async with aclosing(source.events()) as events:
        event = await anext(events)
    assert event.proposal_id == 'tour-1'
