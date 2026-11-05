from dataclasses import dataclass
from typing import Generic, Literal, TypeVar, cast, final

from robot_agent_runtime.errors import InvalidRequest


T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


@final
@dataclass(frozen=True, eq=False)
class ThreadBinding(Generic[T_co]):
    """Choose a hosted object's stable loop without changing its port contract.

    Bindings apply within one runtime. Sharing a loop permits cooperative
    concurrency but also shares the impact of synchronous blocking. Construction
    of the target and any adapter-owned loop handoffs remain the caller's concern.
    Plain objects retain dedicated ownership for backwards compatibility.
    """

    target: T_co
    mode: Literal["shared", "group", "dedicated"] = "dedicated"
    group_name: str | None = None

    def __post_init__(self) -> None:
        if self.target is None:
            raise InvalidRequest("Thread binding target cannot be None")
        if type(self.mode) is not str or self.mode not in ("shared", "group", "dedicated"):
            raise InvalidRequest("Thread binding mode must be shared, group or dedicated")
        if self.mode == "group":
            if type(self.group_name) is not str or not self.group_name.strip():
                raise InvalidRequest("A thread group requires a nonempty plain string name")
        elif self.group_name is not None:
            raise InvalidRequest("Only group thread bindings accept a group name")
        if type(self.target) is ThreadBinding:
            raise InvalidRequest("Thread bindings cannot be nested")

    @staticmethod
    def shared(target: T) -> "ThreadBinding[T]":
        return ThreadBinding(target, mode="shared")

    @staticmethod
    def group(target: T, name: str) -> "ThreadBinding[T]":
        return ThreadBinding(target, mode="group", group_name=name)

    @staticmethod
    def dedicated(target: T) -> "ThreadBinding[T]":
        return ThreadBinding(target, mode="dedicated")


def unwrap_thread_binding(value: T | ThreadBinding[T]) -> T:
    if type(value) is ThreadBinding:
        return value.target
    return cast(T, value)
