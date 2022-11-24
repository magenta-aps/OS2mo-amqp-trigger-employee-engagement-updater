# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
"Test `engagement_updater.handler`"
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ramodels.mo.details import Association
from ramodels.mo.details import Engagement
from ramqp.mo.models import MORoutingKey
from ramqp.mo.models import PayloadType

from engagement_updater.config import Settings
from engagement_updater.handler import handle_engagement_update
from engagement_updater.handler import ResultType
from tests import ASSOCIATION_TYPE_USER_KEY

_employee_uuid = uuid.uuid4()


def _mock_settings(dry_run: bool = False) -> Settings:
    return Settings(
        dry_run=dry_run,
        association_type=ASSOCIATION_TYPE_USER_KEY,
        client_secret="",
    )


async def _invoke(
    gql_response: dict | None = None,
    routing_key: str = "employee.engagement.create",
    settings: Settings = _mock_settings(dry_run=True),
) -> ResultType:
    """Invoke `handle_engagement_update` using mocked GraphQL and model clients.

    Args:
          `gql_response`: The simulated response from the GraphQL query.
          `routing_key`: The AMQP message routing key.

    Returns:
          The result of calling `handle_engagement_update` (an instance of `ResultType`)
    """
    if gql_response is None:
        gql_response = {}

    def _get_gql_response(*args: Any) -> dict | None:
        # Mock response expected by `_get_association_type_uuid`
        if len(args) == 2 and args[1] == {"user_key": ASSOCIATION_TYPE_USER_KEY}:
            return {"classes": [{"uuid": str(uuid.uuid4())}]}
        # Mock response expected by `handle_engagement_update`
        return gql_response

    # Mock `PersistentGraphQLClient`
    gql_client = AsyncMock()
    gql_client.execute.side_effect = _get_gql_response

    # Mock `ModelClient`
    model_client = AsyncMock()

    # Mock payload
    engagement_uuid = uuid.uuid4()
    payload = PayloadType(
        uuid=_employee_uuid, object_uuid=engagement_uuid, time=datetime.now()
    )

    # Call the function under test
    result: ResultType = await handle_engagement_update(
        gql_client,
        model_client,
        MORoutingKey.from_routing_key(routing_key),
        payload,
        settings,
    )

    # Assert no MO upload API call is made during dry runs
    if settings.dry_run:
        model_client.upload.assert_not_awaited()

    return result


def _non_nullable_engagement_fields() -> dict[str, str | dict]:
    return {
        "job_function_uuid": str(uuid.uuid4()),
        "engagement_type_uuid": str(uuid.uuid4()),
        "primary_uuid": str(uuid.uuid4()),
        "user_key": "user_key",
        "validity": {
            "from": "2022-12-31",
            "to": None,
        },
    }


async def test_handle_engagement_update_bails_on_terminate_request() -> None:
    """Test that we bail correctly on attempts to terminate an engagement."""
    result = await _invoke(routing_key="employee.engagement.terminate")
    assert result.action == ResultType.Action.BAIL_TERMINATE_NOT_SUPPORTED


async def test_handle_engagement_update_bails_on_no_engagements() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any
    engagements."""
    result = await _invoke(gql_response={"engagements": [{"objects": []}]})
    assert result.action == ResultType.Action.BAIL_VALIDATION_ERROR


async def test_handle_engagement_update_bails_on_no_org_unit_for_engagement() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any
    organisation units for the found engagement."""
    engagements: dict = {"org_unit": []}
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_VALIDATION_ERROR


async def test_handle_engagement_update_bails_on_no_related_org_units() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any related
    organisation units.
    """
    org_unit: dict = {"related_units": []}
    engagements: dict = {"org_unit": [org_unit]}
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_VALIDATION_ERROR


async def test_handle_engagement_update_bails_on_reverse_association() -> None:
    """Test that we bail correctly if GraphQL query result contains a matching
    association in the "reverse" organisation unit.
    """
    associated_employee: list[dict] = [{"uuid": str(_employee_uuid)}]
    reverse_related_org_units: list[dict] = [
        {
            "uuid": str(uuid.uuid4()),
            "associations": [{"employee": associated_employee}],
        }
    ]
    org_unit: dict = {
        "uuid": str(uuid.uuid4()),
        "related_units": [{"org_units": reverse_related_org_units}],
    }
    engagements: dict = {
        "org_unit": [org_unit],
        **_non_nullable_engagement_fields(),
    }
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_FOUND_REVERSE_ASSOCIATION


async def test_handle_engagement_update_skips_already_processed_engagement() -> None:
    """Test that we bail correctly if GraphQL query result contains a matching
    association in the "current" organisation unit.
    """
    related_org_units: list[dict] = [{"uuid": str(uuid.uuid4()), "associations": []}]
    associated_employees: list[dict] = [{"uuid": str(_employee_uuid)}]
    org_unit: dict = {
        "uuid": str(uuid.uuid4()),
        "related_units": [{"org_units": related_org_units}],
        "associations": [{"employee": associated_employees}],
    }
    engagements: dict = {
        "org_unit": [org_unit],
        **_non_nullable_engagement_fields(),
    }
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.SKIP_ALREADY_PROCESSED


@pytest.mark.parametrize("dry_run", [True, False])
async def test_handle_engagement_update_processes_engagement(dry_run: bool) -> None:
    """Test that we process an engagement correctly if GraphQL query result indicates an
    engagement we have not yet processed (e.g. no association was created in either the
    "current" or the "other" organisation unit, *and* the current organisation unit has
    a related "other" organisation unit.
    """
    related_org_units: list[dict] = [{"uuid": str(uuid.uuid4()), "associations": []}]
    associated_employees: list[dict] = [{"uuid": str(uuid.uuid4())}]
    org_unit: dict = {
        "uuid": str(uuid.uuid4()),
        "related_units": [{"org_units": related_org_units}],
        "associations": [{"employee": associated_employees}],
    }
    engagements: dict = {
        "org_unit": [org_unit],
        **_non_nullable_engagement_fields(),
    }
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    settings = _mock_settings(dry_run=dry_run)
    result = await _invoke(gql_response=gql_response, settings=settings)
    assert result.action == ResultType.Action.SUCCESS_PROCESSED_ENGAGEMENT
    assert result.dry_run == dry_run
    assert isinstance(result.engagement, Engagement)
    assert isinstance(result.association, Association)
