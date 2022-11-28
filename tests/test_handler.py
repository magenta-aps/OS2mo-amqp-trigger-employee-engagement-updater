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
from engagement_updater.handler import _get_association_type_uuid
from engagement_updater.handler import get_bulk_update_payloads
from engagement_updater.handler import get_single_update_payload
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
        if len(args) == 2 and args[1] == {"user_keys": [ASSOCIATION_TYPE_USER_KEY]}:
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
        settings,
        MORoutingKey.from_routing_key(routing_key),
        payload,
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


async def test_handle_engagement_update_bails_on_missing_related_units() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any related
    organisation units.
    """
    org_unit: dict = {"related_units": []}
    engagements: dict = {"org_unit": [org_unit]}
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_VALIDATION_ERROR


async def test_handle_engagement_update_bails_on_empty_related_org_units() -> None:
    """Test that we bail correctly if GraphQL query result contains an empty list
    of related org units.
    """
    org_unit: dict = {
        "uuid": str(uuid.uuid4()),
        "related_units": [],
    }
    engagements: dict = {
        "org_unit": [org_unit],
        **_non_nullable_engagement_fields(),
    }
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_NO_RELATED_ORG_UNITS


async def test_handle_engagement_update_bails_on_incomplete_related_org_units() -> None:
    """Test that we bail correctly if GraphQL query result does not contain a "full"
    list of related org units, e.g. a list containing two org units, one being "this"
    org unit, and the other being the "other" org unit.
    """
    org_unit_uuid = uuid.uuid4()
    related_units: list[dict] = [
        {
            "uuid": str(org_unit_uuid),
            "associations": [],
        }
    ]
    org_unit: dict = {
        "uuid": str(org_unit_uuid),
        "related_units": [{"org_units": related_units}],
    }
    engagements: dict = {
        "org_unit": [org_unit],
        **_non_nullable_engagement_fields(),
    }
    gql_response: dict = {"engagements": [{"objects": [engagements]}]}
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_NO_RELATED_ORG_UNITS


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


async def test_get_association_type_uuid_finds_uuid() -> None:
    """Test that `get_association_type_uuid` finds the association type UUID in the GQL
    response.
    """
    expected_association_type_uuid: uuid.UUID = uuid.uuid4()
    gql_client = AsyncMock()
    gql_client.execute.return_value = {
        "classes": [{"uuid": str(expected_association_type_uuid)}]
    }
    actual_association_type_uuid: uuid.UUID = await _get_association_type_uuid(
        ASSOCIATION_TYPE_USER_KEY,
        gql_client,
    )
    assert actual_association_type_uuid == expected_association_type_uuid


async def test_get_association_type_uuid_raises_valueerror() -> None:
    """Test that `get_association_type_uuid` raises `ValueError` if it cannot find the
    association type UUID in the GQL response.
    """
    gql_client = AsyncMock()
    gql_client.execute.return_value = {}  # empty response
    with pytest.raises(ValueError):
        await _get_association_type_uuid(ASSOCIATION_TYPE_USER_KEY, gql_client)


def _get_mock_gql_client_for_engagement_query(
    employee_uuid: uuid.UUID,
    engagement_uuid: uuid.UUID,
) -> AsyncMock:
    employee_uuids: list[dict] = [{"employee_uuid": str(employee_uuid)}]
    engagements: list[dict] = [
        {
            "uuid": str(engagement_uuid),
            "objects": employee_uuids,
        }
    ]
    gql_client = AsyncMock()
    gql_client.execute.return_value = {"engagements": engagements}
    return gql_client


async def test_get_bulk_update_payloads() -> None:
    """Test that `get_bulk_update_payloads` returns the expected payload.
    In this test, we mock a single engagement in the GQL response, and assert that the
    payload contains the expected employee and engagement UUIDs.
    """
    expected_employee_uuid: uuid.UUID = uuid.uuid4()
    expected_engagement_uuid: uuid.UUID = uuid.uuid4()
    gql_client: AsyncMock = _get_mock_gql_client_for_engagement_query(
        expected_employee_uuid, expected_engagement_uuid
    )
    async for payload in get_bulk_update_payloads(gql_client):
        assert payload.uuid == expected_employee_uuid
        assert payload.object_uuid == expected_engagement_uuid


async def test_get_single_update_payload() -> None:
    """Test that `get_single_update_payload` returns the expected payload.
    In this test, we mock a single engagement matching the given engagment UUID, and
    assert that the payload contains the expected employee UUID.
    """
    expected_employee_uuid: uuid.UUID = uuid.uuid4()
    engagement_uuid: uuid.UUID = uuid.uuid4()
    gql_client: AsyncMock = _get_mock_gql_client_for_engagement_query(
        expected_employee_uuid, engagement_uuid
    )
    async for payload in get_single_update_payload(gql_client, engagement_uuid):
        assert payload.uuid == expected_employee_uuid
        assert payload.object_uuid == engagement_uuid
