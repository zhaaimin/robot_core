"""The storage and backend policy this application owns.

The hotel agent ships exactly one profile, and its name is part of every
persisted identity this agent has ever produced: ``hotel`` enters the simulator
runtime hash as a tag and the MCP runtime hash as the agent name, so both keep
hotel histories separate from the generic robot agent sharing one data
directory. Changing either database name or this tag orphans existing stays.
"""

from robot_agent_core.configuration import StorageProfile

HOTEL_PROFILE = StorageProfile(
    name='hotel', allowed_backends=frozenset({'simulator', 'mcp'}),
    simulator_database='hotel-simulator-tasks.sqlite3', runtime_identity_tag='hotel',
    checkpoint_database='hotel-selectable-skills-checkpoints.sqlite3', business_database='hotel.sqlite3',
    mcp_checkpoint_database='hotel-mcp-{endpoint}-checkpoints.sqlite3',
    mcp_business_database='hotel-mcp-{endpoint}.sqlite3',
)
