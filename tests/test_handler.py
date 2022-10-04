# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
"Test `engagement_updater.handler`"
import uuid
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from ramodels.mo.details import Association
from ramodels.mo.details import Engagement
from ramqp.mo.models import MORoutingKey
from ramqp.mo.models import PayloadType

from engagement_updater.handler import handle_engagement_update
from engagement_updater.handler import ResultType


_employee_uuid = uuid.uuid4()


async def _invoke(
    gql_response: dict = None,
    routing_key: str = "employee.engagement.create",
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

    # Mock `PersistentGraphQLClient`
    gql_client = AsyncMock()
    gql_client.execute.return_value = gql_response

    # Mock `ModelClient`
    model_client = AsyncMock()

    # Mock payload
    engagement_uuid = uuid.uuid4()
    payload = PayloadType(
        uuid=_employee_uuid, object_uuid=engagement_uuid, time=datetime.now()
    )

    # Call function under test
    return await handle_engagement_update(
        gql_client,
        model_client,
        MORoutingKey.from_routing_key(routing_key),
        payload,
    )


async def test_handle_engagement_update_bails_on_terminate_request() -> None:
    """Test that we bail correctly on attempts to terminate an engagement."""
    result = await _invoke(routing_key="employee.engagement.terminate")
    assert result.action == ResultType.Action.BAIL_TERMINATE_NOT_SUPPORTED


async def test_handle_engagement_update_bails_on_no_engagements() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any
    engagements."""
    result = await _invoke(gql_response={"engagements": [{"objects": []}]})
    assert result.action == ResultType.Action.BAIL_NO_ENGAGEMENTS


async def test_handle_engagement_update_bails_on_no_org_unit_for_engagement() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any
    organisation units for the found engagement."""
    gql_response: dict = {
        "engagements": [
            {
                "objects": [
                    {
                        "org_unit": [],
                    }
                ],
            }
        ],
    }
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_NO_ORG_UNIT_FOR_ENGAGEMENT


async def test_handle_engagement_update_bails_on_no_related_org_units() -> None:
    """Test that we bail correctly if GraphQL query result does not contain any related
    organisation units.
    """
    gql_response: dict = {
        "engagements": [
            {
                "objects": [
                    {
                        "org_unit": [{"related_units": []}],
                    }
                ],
            }
        ],
    }
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_NO_RELATED_ORG_UNITS


async def test_handle_engagement_update_bails_on_reverse_association() -> None:
    """Test that we bail correctly if GraphQL query result contains a matching
    association in the "reverse" organisation unit.
    """
    gql_response: dict = {
        "engagements": [
            {
                "objects": [
                    {
                        "org_unit": [
                            {
                                "uuid": str(uuid.uuid4()),
                                "related_units": [
                                    {
                                        "org_units": [
                                            {
                                                "uuid": str(uuid.uuid4()),
                                                "associations": [
                                                    {
                                                        "employee": [
                                                            {
                                                                "uuid": str(
                                                                    _employee_uuid
                                                                ),
                                                            }
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.BAIL_FOUND_REVERSE_ASSOCIATION


async def test_handle_engagement_update_skips_already_processed_engagement() -> None:
    """Test that we bail correctly if GraphQL query result contains a matching
    association in the "current" organisation unit.
    """
    gql_response: dict = {
        "engagements": [
            {
                "objects": [
                    {
                        "org_unit": [
                            {
                                "uuid": str(uuid.uuid4()),
                                "related_units": [
                                    {
                                        "org_units": [
                                            {
                                                "uuid": str(uuid.uuid4()),
                                                "associations": [],
                                            }
                                        ],
                                    }
                                ],
                                "associations": [
                                    {
                                        "employee": [
                                            {
                                                "uuid": str(_employee_uuid),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    result = await _invoke(gql_response=gql_response)
    assert result.action == ResultType.Action.SKIP_ALREADY_PROCESSED


@pytest.mark.parametrize("dry_run", [True, False])
async def test_handle_engagement_update_processes_engagement(dry_run: bool) -> None:
    """Test that we process an engagement correctly if GraphQL query result indicates an
    engagement we have not yet processed (e.g. no association was created in either the
    "current" or the "other" organisation unit, *and* the current organisation unit has
    a related "other" organisation unit.
    """
    gql_response: dict = {
        "engagements": [
            {
                "objects": [
                    {
                        "org_unit": [
                            {
                                "uuid": str(uuid.uuid4()),
                                "related_units": [
                                    {
                                        "org_units": [
                                            {
                                                "uuid": str(uuid.uuid4()),
                                                "associations": [],
                                            }
                                        ],
                                    }
                                ],
                                "associations": [
                                    {
                                        "employee": [
                                            {
                                                "uuid": str(uuid.uuid4()),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                        "job_function_uuid": str(uuid.uuid4()),
                        "engagement_type_uuid": str(uuid.uuid4()),
                        "primary_uuid": str(uuid.uuid4()),
                        "user_key": "user_key",
                        "validity": {
                            "from": "2022-12-31",
                            "to": None,
                        },
                    }
                ],
            }
        ],
    }
    with patch("engagement_updater.handler._get_dry_run", return_value=dry_run):
        with patch(
            "engagement_updater.handler._get_association_type_uuid",
            return_value=uuid.uuid4(),
        ):
            result = await _invoke(gql_response=gql_response)
            assert result.action == ResultType.Action.SUCCESS_PROCESSED_ENGAGEMENT
            assert result.dry_run == dry_run
            assert isinstance(result.engagement, Engagement)
            assert isinstance(result.association, Association)
