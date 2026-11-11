"""Keep the hotel command-line tests runnable without credentials or a model.

The hotel application requires a model name and ``OPENAI_API_KEY`` like any
other agent, so these fixtures supply inert values for argument validation and
substitute the scripted guest rules from ``scripted_models.py`` at the single
``create_model`` seam. The real graph, workflow, simulator and storage paths
still run underneath.
"""

import pytest

from scripted_models import scripted_model


@pytest.fixture(autouse=True)
def offline_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('ROBOT_AGENT_MODEL', 'test-model')
    monkeypatch.setattr(
        'hotel_agent.app.create_model',
        lambda config: scripted_model(),
    )
