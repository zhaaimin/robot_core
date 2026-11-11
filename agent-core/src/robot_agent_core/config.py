from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robot_agent_core.configuration import StorageProfile


@dataclass(frozen=True)
class AppConfig:
    """Keep connection ownership and execution policy outside model arguments.

    Storage paths and runtime identity are resolved by the composition boundary
    before construction, from the storage profile this snapshot carries. Reading
    this snapshot therefore never consults an application registry, and a later
    change to a profile cannot rewrite a configuration that was already built.
    """

    backend: str
    model: str | None
    api_key: str | None = field(repr=False)
    base_url: str | None
    api_id: str | None
    address: str | None
    api_token: str = field(repr=False)
    data_dir: Path
    thread_id: str
    prompt: str | None
    resume: bool
    max_turns: int
    wait_timeout_s: float
    task_timeout_s: float
    model_timeout_s: float
    allowed_skills: frozenset[str]
    task_database: Path | None
    checkpoint_database: Path
    business_database: Path | None
    runtime_id: str
    profile: StorageProfile
    skills_dir: Path | None = None
    enabled_skills: tuple[str, ...] | None = None
    runtime_socket: Path | None = None
