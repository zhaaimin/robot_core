"""Keep the command-line tests runnable without credentials or a language model.

The application itself has no offline mode any more: a model name and
``OPENAI_API_KEY`` are required for every run. These fixtures supply inert
values for argument validation and substitute the scripted decision rules from
``scripted_models.py`` at the single ``create_model`` seam, so tests still
exercise the real graph, SDK, skills and storage paths. The hotel agent has its
own conftest and its own substitute in a separate project.
"""

import pytest

from scripted_models import scripted_model


@pytest.fixture(autouse=True)
def offline_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('ROBOT_AGENT_MODEL', 'test-model')
    monkeypatch.setattr(
        'robot_agent.app.create_model',
        lambda config: scripted_model(),
    )
