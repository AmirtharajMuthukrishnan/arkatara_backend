import os
from collections.abc import Callable


class EnvironmentConfigurationError(RuntimeError):
    pass


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise EnvironmentConfigurationError(f"Required environment variable {name} is not set.")
    return value or ""


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise EnvironmentConfigurationError(f"{name} must be a boolean value.")


def env_list(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


def env_cast[T](name: str, cast: Callable[[str], T], default: T) -> T:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError) as exc:
        raise EnvironmentConfigurationError(f"{name} has an invalid value.") from exc
