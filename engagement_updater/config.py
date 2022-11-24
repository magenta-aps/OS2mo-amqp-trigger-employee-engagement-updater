# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
# pylint: disable=too-few-public-methods,missing-class-docstring
"""Settings handling."""
import logging
from enum import Enum
from functools import cache
from typing import Any

import structlog
from pydantic import AnyHttpUrl
from pydantic import BaseSettings
from pydantic import Field
from pydantic import parse_obj_as
from pydantic import SecretStr
from ramqp.config import ConnectionSettings


class LogLevel(Enum):
    """Log levels."""

    NOTSET = logging.NOTSET
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


logger = structlog.get_logger()


class EmployeeEngagementUpdaterConnectionSettings(ConnectionSettings):
    """Connection settings specific to `engagement_updater`"""

    queue_prefix = "os2mo-amqp-trigger-employee-engagement-updater"

    # TODO: Ensure we don't crash MO when running somewhat concurrently
    prefetch_count = 1


class Settings(BaseSettings):
    """Settings for `engagement_updater`.

    Note that AMQP related settings are taken directly by RAMQP:
    * https://git.magenta.dk/rammearkitektur/ramqp/-/blob/master/ramqp/config.py
    """

    amqp: EmployeeEngagementUpdaterConnectionSettings = Field(
        default_factory=EmployeeEngagementUpdaterConnectionSettings
    )

    commit_tag: str = Field("HEAD", description="Git commit tag.")
    commit_sha: str = Field("HEAD", description="Git commit SHA.")

    mo_url: AnyHttpUrl = Field(
        parse_obj_as(AnyHttpUrl, "http://mo-service:5000"),
        description="Base URL for OS2mo.",
    )
    client_id: str = Field(
        "engagement_updater", description="Client ID for OIDC client."
    )
    client_secret: SecretStr = Field(..., description="Client Secret for OIDC client.")
    auth_server: AnyHttpUrl = Field(
        parse_obj_as(AnyHttpUrl, "http://keycloak-service:8080/auth"),
        description="Base URL for OIDC server (Keycloak).",
    )
    auth_realm: str = Field("mo", description="Realm to authenticate against")

    dry_run: bool = Field(
        False, description="Run in dry-run mode, only printing what would have changed."
    )

    log_level: LogLevel = LogLevel.DEBUG

    expose_metrics: bool = Field(True, description="Whether to expose metrics.")

    graphql_timeout: int = 120

    association_type: str = Field(
        description=(
            "User key of the association type to use for new associations created by "
            "this program."
        )
    )

    class Config:
        env_nested_delimiter = "__"  # allows setting e.g. AMQP__QUEUE_PREFIX=foo


@cache
def get_settings(*args: Any, **kwargs: Any) -> Settings:
    """Fetch settings object.

    Args:
        args: overrides
        kwargs: overrides

    Return:
        Cached settings object.
    """
    settings = Settings(*args, **kwargs)
    logger.debug("Settings fetched", settings=settings, args=args, kwargs=kwargs)
    return settings
