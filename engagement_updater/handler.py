# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
"The business logic of the AMQP trigger."
from dataclasses import dataclass
from datetime import datetime
from enum import auto
from enum import Enum
from typing import Any
from uuid import UUID

import structlog
from gql import gql
from more_itertools import first_true
from more_itertools import one
from raclients.graph.client import PersistentGraphQLClient
from raclients.modelclient.mo import ModelClient
from ramodels.mo.details import Association
from ramodels.mo.details import Engagement
from ramqp.mo.models import MORoutingKey
from ramqp.mo.models import PayloadType
from ramqp.mo.models import RequestType

from .config import get_settings


logger = structlog.get_logger()


@dataclass
class ResultType:
    """Indicates the result of calling `handle_engagement_update`."""

    class Action(Enum):
        """Indicates the action taken in `handle_engagement_update`."""

        BAIL_TERMINATE_NOT_SUPPORTED = auto()
        BAIL_NO_ENGAGEMENTS = auto()
        BAIL_NO_ORG_UNIT_FOR_ENGAGEMENT = auto()
        BAIL_NO_RELATED_ORG_UNITS = auto()
        BAIL_FOUND_REVERSE_ASSOCIATION = auto()
        SKIP_ALREADY_PROCESSED = auto()
        SUCCESS_PROCESSED_ENGAGEMENT = auto()

    action: Action
    dry_run: bool = False
    association: Association = None  # type: ignore
    engagement: Engagement = None  # type: ignore


async def handle_engagement_update(  # pylint: disable=too-many-return-statements
    gql_client: PersistentGraphQLClient,
    model_client: ModelClient,
    mo_routing_key: MORoutingKey,
    payload: PayloadType,
) -> ResultType:
    """Perform the central business logic of the program."""

    if mo_routing_key.request_type == RequestType.TERMINATE:
        logger.info("Don't yet know how to handle engagement terminations, sorry")
        return ResultType(action=ResultType.Action.BAIL_TERMINATE_NOT_SUPPORTED)

    employee_uuid = payload.uuid
    engagement_uuid = payload.object_uuid

    # Examine existing engagement and see if it:
    # * resides in a organisation unit which is linked to another related unit, and
    # * has already been moved to its proper organisation unit.
    # (This query also collects data necessary to create an eventual "engagement edit".)
    query = gql(
        """
        query RelatedOrgUnitQuery($uuids: [UUID!]) {
            engagements(uuids: $uuids) {
                objects {
                    org_unit {
                        uuid
                        related_units {
                            org_units {
                                uuid
                                associations {
                                    employee {
                                        uuid
                                    }
                                }
                            }
                        }
                        associations {
                            employee {
                                uuid
                            }
                        }
                    }
                    job_function_uuid
                    engagement_type_uuid
                    primary_uuid
                    user_key
                    validity {
                        from
                        to
                    }
                }
            }
        }
        """
    )

    result = await gql_client.execute(query, {"uuids": [str(engagement_uuid)]})

    # Perform sanity checks on the data returned - some fields are not always present
    # and we cannot proceed in those cases.

    try:
        current_engagement = one(one(result["engagements"])["objects"])
    except ValueError:
        logger.info(
            "Could not find engagement",
            engagement_uuid=engagement_uuid,
        )
        return ResultType(action=ResultType.Action.BAIL_NO_ENGAGEMENTS)

    try:
        current_org_unit = one(current_engagement["org_unit"])
    except ValueError:
        logger.info(
            "Could not find org unit of engagement",
            engagement_uuid=engagement_uuid,
        )
        return ResultType(action=ResultType.Action.BAIL_NO_ORG_UNIT_FOR_ENGAGEMENT)

    try:
        related_units = one(current_org_unit["related_units"])["org_units"]
    except (ValueError, KeyError):
        logger.info(
            "Could not find related org units",
            engagement_uuid=engagement_uuid,
        )
        return ResultType(action=ResultType.Action.BAIL_NO_RELATED_ORG_UNITS)

    # Find the "other" org unit, e.g. the related unit B if we are currently looking at
    # unit A, or vice versa.
    other_unit = find_related_unit(related_units, current_org_unit)
    if other_unit is None:
        return ResultType(action=ResultType.Action.BAIL_NO_RELATED_ORG_UNITS)

    # See if we are in the "second event", and `related_units` already have associations
    reverse_association = find_current_association(employee_uuid, other_unit)
    if reverse_association:
        logger.info(
            "Found association in other unit, doing nothing",
            engagement_uuid=engagement_uuid,
        )
        return ResultType(action=ResultType.Action.BAIL_FOUND_REVERSE_ASSOCIATION)

    # Check if the current unit already has an association for this employee,
    # indicating that we have already moved the engagement to the "other" unit.
    current_association = find_current_association(employee_uuid, current_org_unit)
    if current_association is None:  # pylint: disable=no-else-return
        # Perform the actual changes against the MO API (or log what would happen, in
        # case of a dry-run.)
        return await process_engagement(
            model_client,
            employee_uuid,
            engagement_uuid,
            current_engagement,
            current_org_unit,
            other_unit,
        )
    else:
        logger.info(
            "Already processed this engagement, doing nothing",
            current_association=current_association,
            engagement_uuid=engagement_uuid,
        )
        return ResultType(action=ResultType.Action.SKIP_ALREADY_PROCESSED)


async def process_engagement(  # pylint: disable=too-many-arguments
    model_client: ModelClient,
    employee_uuid: UUID,
    engagement_uuid: UUID,
    current_engagement: dict,
    current_org_unit: dict,
    other_unit: dict | None,
) -> ResultType:
    """Edit the engagement and create the association. In case `dry_run` is True, only
    build the MO API payloads, but do not POST them to the API.
    """
    dry_run = _get_dry_run()

    # Create association in "current" org unit
    association = get_association_obj(employee_uuid, current_org_unit)
    if dry_run:
        logger.info(
            "Would create association",
            association=association,
            engagement_uuid=engagement_uuid,
        )
    else:
        association_response = await model_client.upload_object(association)
        logger.info(
            "Created association",
            response=association_response,
            engagement_uuid=engagement_uuid,
        )

    # Edit engagement, so it belongs to the "other" org unit
    engagement = get_engagement_obj(
        employee_uuid,
        engagement_uuid,
        current_engagement,
        other_unit,  # type: ignore
    )
    if dry_run:
        logger.info(
            "Would update engagement",
            engagement_uuid=engagement_uuid,
            engagement=engagement,
        )
    else:
        engagement_response = await model_client.upload_object(engagement, edit=True)
        logger.info(
            "Updated engagement",
            response=engagement_response,
            engagement_uuid=engagement_uuid,
        )

    return ResultType(
        action=ResultType.Action.SUCCESS_PROCESSED_ENGAGEMENT,
        engagement=engagement,
        association=association,
        dry_run=dry_run,
    )


def find_current_association(
    employee_uuid: UUID,
    current_org_unit: dict,
) -> Any:
    """Find the first association (if any) in `current_org_unit` whose employee UUID
    matches the given `employee_uuid`.
    """
    return first_true(
        current_org_unit["associations"],
        pred=lambda assoc: UUID(one(assoc["employee"])["uuid"]) == employee_uuid,
    )


def find_related_unit(
    related_units: list[dict],
    current_org_unit: dict,
) -> Any:
    """Find the "other" org unit in `related_units`, e.g. the org unit which is *not*
    `current_org_unit`. E.g. if units A and B are related, and we pass unit A as the
    `current_org_unit`, this returns unit B.
    """
    other_unit = first_true(
        related_units,
        pred=lambda org_unit: org_unit["uuid"] != current_org_unit["uuid"],
    )
    if other_unit is not None:
        logger.debug(
            "Found related unit",
            this_unit=current_org_unit["uuid"],
            other_unit=other_unit["uuid"],
        )
    return other_unit


def get_association_obj(employee_uuid: UUID, current_org_unit: dict) -> Association:
    """Build a new `Association` object based on `employee_uuid` and `current_org_unit`
    which is used to indicate the original organisation unit of the engagement after it
    has been processed.
    """
    return Association.from_simplified_fields(
        person_uuid=employee_uuid,
        org_unit_uuid=UUID(current_org_unit["uuid"]),
        association_type_uuid=_get_association_type_uuid(),
        # TODO: should the from date be identical to the from date of the engagement?
        from_date=datetime.now().strftime("%Y-%m-%d"),
    )


def get_engagement_obj(
    employee_uuid: UUID, engagement_uuid: UUID, engagement: dict, other_unit: dict
) -> Engagement:
    """Build an edited `Engagement` object which updates the found engagement so it is
    related to `other_unit` rather than its original org unit.
    """
    return Engagement.from_simplified_fields(
        uuid=engagement_uuid,
        person_uuid=employee_uuid,
        org_unit_uuid=UUID(other_unit["uuid"]),
        # Copy values from current engagement
        job_function_uuid=UUID(engagement["job_function_uuid"]),
        engagement_type_uuid=UUID(engagement["engagement_type_uuid"]),
        primary_uuid=(
            UUID(engagement["primary_uuid"]) if engagement["primary_uuid"] else None
        ),
        user_key=engagement["user_key"],
        from_date=engagement["validity"]["from"],
    )


def _get_dry_run() -> bool:
    # This is a separate function to allow it to be mocked in tests.
    return get_settings().dry_run


def _get_association_type_uuid() -> UUID:
    # This is a separate function to allow it to be mocked in tests.
    return get_settings().association_type
