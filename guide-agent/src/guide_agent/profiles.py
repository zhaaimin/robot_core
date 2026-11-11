"""Guide histories are owned by this application and isolated from other agents."""

from robot_agent_core.configuration import StorageProfile


GUIDE_PROFILE = StorageProfile(
    name='guide', allowed_backends=frozenset({'simulator', 'mcp'}),
    simulator_database='guide-simulator-tasks.sqlite3', runtime_identity_tag='guide',
    checkpoint_database='guide-checkpoints.sqlite3', business_database='guide.sqlite3',
    mcp_checkpoint_database='guide-mcp-{endpoint}-checkpoints.sqlite3',
    mcp_business_database='guide-mcp-{endpoint}.sqlite3',
)
