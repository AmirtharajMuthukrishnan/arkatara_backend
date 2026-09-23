import json
import os
import subprocess
import sys

import pytest

from config.env import EnvironmentConfigurationError, env_bool, env_cast


def load_settings(module, overrides=None):
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "POSTGRES_", "APP_ENV"))
    }
    process_env.update(overrides or {})
    process_env["DJANGO_SETTINGS_MODULE"] = f"config.settings.{module}"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from django.conf import settings as s; "
            "print(json.dumps({k: getattr(s, k) for k in "
            "['ENVIRONMENT', 'DEBUG', 'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE', "
            "'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS', 'ALLOWED_HOSTS']}))",
        ],
        env=process_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.fixture
def deployment_env():
    # Fictional test environment; never a deployable credential or policy fixture.
    return {
        "DJANGO_SECRET_KEY": "test-only-strong-secret-with-varied-characters-123456789!",
        "DJANGO_ALLOWED_HOSTS": "api.example.test",
        "POSTGRES_DB": "scenario",
        "POSTGRES_USER": "scenario",
        "POSTGRES_PASSWORD": "test-only",
        "POSTGRES_HOST": "db.example.test",
        "POSTGRES_PORT": "5432",
    }


@pytest.mark.parametrize("module", ["staging", "production"])
def test_deployments_do_not_load_local_dotenv_or_default_secret(module):
    result = load_settings(module)
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY is not set" in result.stderr


@pytest.mark.parametrize("module", ["staging", "production"])
@pytest.mark.parametrize(
    "missing",
    ["POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT"],
)
def test_deployments_require_explicit_database_settings(module, missing, deployment_env):
    deployment_env.pop(missing)
    result = load_settings(module, deployment_env)
    assert result.returncode != 0
    assert f"{missing} is not set" in result.stderr


@pytest.mark.parametrize("module", ["staging", "production"])
def test_deployments_enforce_security_and_do_not_inherit_local_origins(module, deployment_env):
    deployment_env["DJANGO_DEBUG"] = "true"
    result = load_settings(module, deployment_env)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values == {
        "ENVIRONMENT": module,
        "DEBUG": False,
        "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": True,
        "CORS_ALLOWED_ORIGINS": [],
        "CSRF_TRUSTED_ORIGINS": [],
        "ALLOWED_HOSTS": ["api.example.test"],
    }


@pytest.mark.parametrize("hosts", ["", "*"])
def test_deployment_rejects_unrestricted_hosts(hosts, deployment_env):
    deployment_env["DJANGO_ALLOWED_HOSTS"] = hosts
    result = load_settings("production", deployment_env)
    assert result.returncode != 0
    assert "explicit deployment hosts" in result.stderr


@pytest.mark.parametrize("secret", ["unsafe-development-only", "x" * 60, "django-insecure-" * 5])
def test_deployment_rejects_weak_secret(secret, deployment_env):
    deployment_env["DJANGO_SECRET_KEY"] = secret
    result = load_settings("production", deployment_env)
    assert result.returncode != 0
    assert "strong deployment secret" in result.stderr


def test_invalid_environment_values_fail_clearly(monkeypatch):
    monkeypatch.setenv("EXAMPLE_BOOLEAN", "perhaps")
    monkeypatch.setenv("EXAMPLE_INTEGER", "NaN")
    with pytest.raises(EnvironmentConfigurationError, match="EXAMPLE_BOOLEAN"):
        env_bool("EXAMPLE_BOOLEAN")
    with pytest.raises(EnvironmentConfigurationError, match="EXAMPLE_INTEGER"):
        env_cast("EXAMPLE_INTEGER", int, 1)
