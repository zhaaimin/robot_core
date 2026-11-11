import subprocess
import sys
from importlib.util import find_spec

from robot_agent_runtime.errors import AgentRuntimeError


def test_hotel_workflow_error_is_owned_by_the_hotel_domain() -> None:
    assert find_spec('hotel_agent.errors') is not None
    from hotel_agent.errors import HotelWorkflowError

    assert HotelWorkflowError.__module__ == 'hotel_agent.errors'
    assert issubclass(HotelWorkflowError, AgentRuntimeError)
    error = HotelWorkflowError('Guest consent is required', details={'phase': 'reception'})
    assert error.to_dict() == {
        'code': 'HOTEL_WORKFLOW_REJECTED',
        'message': 'Guest consent is required',
        'details': {'phase': 'reception'},
    }


def test_generic_errors_do_not_expose_hotel_workflow_error() -> None:
    import robot_agent_core.errors as errors

    assert not hasattr(errors, 'HotelWorkflowError')


def test_hotel_contract_imports_do_not_load_business_implementations() -> None:
    for module in ('hotel_agent.errors', 'hotel_agent.models'):
        probe = subprocess.run([
            sys.executable, '-c',
            'import importlib, sys; importlib.import_module(sys.argv[1]); '
            'loaded = [name for name in ("hotel_agent.service", '
            '"hotel_agent.task_coordination", "hotel_agent.repository", '
            '"robot_agent_runtime.sdk") if name in sys.modules]; '
            'assert not loaded, loaded',
            module,
        ], text=True, capture_output=True, timeout=10)
        assert probe.returncode == 0, f'{module}: {probe.stderr}'
