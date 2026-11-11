"""The storage and backend policy this application owns.

The generic robot agent ships exactly one profile. It is a module constant
rather than something a command line argument selects, so an assembled
configuration can never be pointed at another agent's databases, and adding an
agent means adding a project instead of extending a shared registry.
"""

from robot_agent_core.configuration import StorageProfile

ROBOT_PROFILE = StorageProfile(
    name='robot', allowed_backends=frozenset({'simulator', 'cc'}),
    simulator_database='simulator-tasks.sqlite3', runtime_identity_tag=None,
    checkpoint_database='checkpoints.sqlite3', business_database=None,
)
