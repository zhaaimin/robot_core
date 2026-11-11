from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def test_hotel_simulator_starts_at_the_declared_meeting_point_without_navigation() -> None:
    from hotel_agent.simulator import HotelSimulator

    simulator = HotelSimulator(delay_s=0)
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        final = await runtime.submit_and_wait(TaskRequest(
            task_type='hotel-bootstrap-state', steps=[SkillCall(skill='robot.get_state')],
        ))
        state = final.result['steps'][0]['output']['state']
        assert state['position'] == 'hotel-lobby'
        assert state['navigation_count'] == 0
    finally:
        await runtime.close()


async def test_hotel_simulator_offers_audio_speak_on_the_shared_state_without_moving_the_robot() -> None:
    """Announcement is the hotel scene's only speaker path, so it must share device state.

    Speaking uses the same position the navigation skill owns; a speech that
    moved the robot would silently corrupt the escort destination bookkeeping.
    """
    from hotel_agent.simulator import HotelSimulator

    simulator = HotelSimulator(delay_s=0)
    assert 'audio.speak' in simulator.skills
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        spoken = await runtime.submit_and_wait(TaskRequest(
            task_type='hotel-bootstrap-speak',
            steps=[SkillCall(skill='audio.speak', params={'message': '早餐七点到十点'})],
        ))
        assert spoken.state == TaskState.SUCCEEDED
        assert spoken.result['steps'][0]['output'] == {'message': '早餐七点到十点', 'spoken': True}
        final = await runtime.submit_and_wait(TaskRequest(
            task_type='hotel-bootstrap-state', steps=[SkillCall(skill='robot.get_state')],
        ))
        state = final.result['steps'][0]['output']['state']
        assert state['last_message'] == '早餐七点到十点'
        assert state['speech_count'] == 1
        assert state['position'] == 'hotel-lobby'
        assert state['navigation_count'] == 0
    finally:
        await runtime.close()
