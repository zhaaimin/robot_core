from contextvars import ContextVar


CONTROL_OPERATION: ContextVar[bool] = ContextVar("robot_control_operation", default=False)
