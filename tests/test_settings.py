# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
"""Test our settings handling."""
import pytest
from pydantic import SecretStr
from pydantic import ValidationError

from engagement_updater.config import get_settings
from tests import ASSOCIATION_TYPE_USER_KEY


def test_missing_client_secret() -> None:
    """Test that we must add client_secret to construct settings."""

    get_settings.cache_clear()
    with pytest.raises(ValidationError) as excinfo:
        get_settings(association_type=ASSOCIATION_TYPE_USER_KEY)
    assert "client_secret\n  field required" in str(excinfo.value)


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that we can construct settings."""
    get_settings.cache_clear()

    settings = get_settings(
        association_type=ASSOCIATION_TYPE_USER_KEY,
        client_secret="hunter2",
    )
    assert isinstance(settings.client_secret, SecretStr)
    assert settings.client_secret.get_secret_value() == "hunter2"

    monkeypatch.setenv("CLIENT_SECRET", "AzureDiamond")
    settings = get_settings(association_type=ASSOCIATION_TYPE_USER_KEY)
    assert isinstance(settings.client_secret, SecretStr)
    assert settings.client_secret.get_secret_value() == "AzureDiamond"


def test_graphql_timeout_default() -> None:
    """Test that default GraphQL client timeout is set correctly"""
    get_settings.cache_clear()
    settings = get_settings(
        association_type=ASSOCIATION_TYPE_USER_KEY,
        client_secret="not important",
    )
    assert 120 == settings.graphql_timeout


def test_graphql_timeout_non_default() -> None:
    """Test that GraphQL client timeout is set to overridden value"""
    get_settings.cache_clear()
    settings = get_settings(
        association_type=ASSOCIATION_TYPE_USER_KEY,
        client_secret="not important",
        graphql_timeout=10,
    )
    assert 10 == settings.graphql_timeout
