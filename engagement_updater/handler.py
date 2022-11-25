# SPDX-FileCopyrightText: 2022 Magenta ApS
# SPDX-License-Identifier: MPL-2.0
# pylint: disable=too-few-public-methods,missing-class-docstring
"""The business logic of the AMQP trigger."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import auto
from enum import Enum
from uuid import UUID

import structlog
from gql import gql
from more_itertools import first_true
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from raclients.graph.client import PersistentGraphQLClient
from raclients.modelclient.mo import ModelClient
from ramodels.mo import Validity
from ramodels.mo.details import Association
from ramodels.mo.details import Engagement
from ramqp.mo.models import MORoutingKey
from ramqp.mo.models import PayloadType
from ramqp.mo.models import RequestType

from .config import Settings


logger = structlog.get_logger()


@dataclass
class ResultType:
    """Indicates the result of calling `handle_engagement_update`."""

    class Action(Enum):
        """Indicates the action taken in `handle_engagement_update`."""

        BAIL_VALIDATION_ERROR = auto()
        BAIL_TERMINATE_NOT_SUPPORTED = auto()
        BAIL_NO_RELATED_ORG_UNITS = auto()
        BAIL_FOUND_REVERSE_ASSOCIATION = auto()
        SKIP_ALREADY_PROCESSED = auto()
        SUCCESS_PROCESSED_ENGAGEMENT = auto()

    action: Action
    dry_run: bool = False
    association: Association | None = None
    engagement: Engagement | None = None


class _Employee(BaseModel):
    uuid: UUID


class _Association(BaseModel):
    employee: list[_Employee]


class _OrgUnit(BaseModel):
    uuid: UUID
    associations: list[_Association] = Field(min_items=0)


class _OrgUnitList(BaseModel):
    org_units: list[_OrgUnit]


class _OrgUnitWithRelatedUnits(BaseModel):
    uuid: UUID
    related_units: list[_OrgUnitList] = Field(min_items=1, max_items=1)
    associations: list[_Association] | None = Field(None, min_items=0)


class _Engagement(BaseModel):
    org_units: list[_OrgUnitWithRelatedUnits] = Field(
        alias="org_unit", min_items=1, max_items=1
    )
    job_function_uuid: UUID
    engagement_type_uuid: UUID
    primary_uuid: UUID
    user_key: str
    validity: Validity


class _EngagementList(BaseModel):
    objects: list[_Engagement] = Field(min_items=1, max_items=1)


class _Result(BaseModel):
    engagements: list[_EngagementList] = Field(min_items=1, max_items=1)


class _ClassUUID(BaseModel):
    uuid: UUID


class _AssociationTypeUUID(BaseModel):
    classes: list[_ClassUUID] = Field(min_items=1, max_items=1)


class _EmployeeFlat(BaseModel):
    employee_uuid: UUID


class _EngagementFlat(BaseModel):
    uuid: UUID
    objects: list[_EmployeeFlat]


class _EngagementResult(BaseModel):
    engagements: list[_EngagementFlat]


async def handle_engagement_update(  # pylint: disable=too-many-locals
    gql_client: PersistentGraphQLClient,
    model_client: ModelClient,
    settings: Settings,
    mo_routing_key: MORoutingKey,
    payload: PayloadType,
) -> ResultType:
    """Perform the central business logic of the program."""
    global logger  # pylint: disable=global-statement,invalid-name

    if mo_routing_key.request_type == RequestType.TERMINATE:
        logger.info("Don't yet know how to handle engagement terminations, sorry")
        return ResultType(action=ResultType.Action.BAIL_TERMINATE_NOT_SUPPORTED)

    employee_uuid = payload.uuid
    engagement_uuid = payload.object_uuid

    # Always include engagement UUID in logged messages.
    # This mutates the state of the module-level `logger` variable.
    logger = logger.bind(engagement_uuid=engagement_uuid)

    # Examine existing engagement and see if it:
    # * resides in an organisation unit which is linked to another related unit, and
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

    result: dict = await gql_client.execute(query, {"uuids": [str(engagement_uuid)]})
    try:
        parsed_result: _Result = _Result.parse_obj(result)
    except ValidationError:
        logger.exception(event="validation error")
        return ResultType(action=ResultType.Action.BAIL_VALIDATION_ERROR)

    # Once we have parsed the GQL response, we can assume that these objects can be
    # found in the response (we may still encounter empty lists, however.)
    current_engagement: _Engagement = parsed_result.engagements[0].objects[0]
    current_org_unit: _OrgUnitWithRelatedUnits = current_engagement.org_units[0]
    related_units: list[_OrgUnit] = current_org_unit.related_units[0].org_units

    # Find the "other" org unit, e.g. the related unit B, if we are currently looking at
    # unit A, or vice versa.
    other_unit: _OrgUnit | None = find_related_unit(related_units, current_org_unit)
    if other_unit is None:
        return ResultType(action=ResultType.Action.BAIL_NO_RELATED_ORG_UNITS)

    # Check if we have already processed this engagement previously, and bail early if
    # we have.
    # This can happen in the following way, which we want to prevent:
    #   1. MO publishes an "engagement created" or "engagement edited" event.
    #   2. The AMQP trigger (this code) receives the event.
    #   3. The AMQP trigger edits the MO engagement.
    #   4. MO publishes an "engagement edited" event for the same engagement.
    #   5. The AMQP trigger (this code) receives the event.
    #   6. The AMQP trigger edits the MO engagement.
    # We want to avoid editing the engagement in step 6, as that would edit the
    # engagement back to its original state, nullifying the work of this AMQP trigger.
    reverse_associations: list[_Association] = _get_association_list(other_unit)
    reverse_association: _Association | None = find_current_association(
        employee_uuid, reverse_associations
    )
    if reverse_association:
        logger.info("Found association in other unit, doing nothing")
        return ResultType(action=ResultType.Action.BAIL_FOUND_REVERSE_ASSOCIATION)

    # Check if the current unit already has an association for this employee,
    # indicating that we have already moved the engagement to the "other" unit.
    current_associations: list[_Association] = _get_association_list(current_org_unit)
    current_association: _Association | None = find_current_association(
        employee_uuid, current_associations
    )
    if current_association is None:
        # Look up class UUID for the given association type user key
        association_type_uuid: UUID = await _get_association_type_uuid(
            settings.association_type,
            gql_client,
        )
        # Prepare payload to create association in "current" org unit
        new_association: Association = get_association_obj(
            employee_uuid, current_org_unit, association_type_uuid
        )
        # Prepare payload to edit engagement, so it belongs to the "other" org unit
        edited_engagement = get_engagement_obj(
            employee_uuid,
            engagement_uuid,
            current_engagement,
            other_unit,
        )

        # Perform dry run, or actual API requests, depending on setting
        if settings.dry_run:
            _dry_process_engagement(
                association=new_association,
                engagement=edited_engagement,
            )
        else:
            await _process_engagement(
                association=new_association,
                engagement=edited_engagement,
                model_client=model_client,
            )

        return ResultType(
            action=ResultType.Action.SUCCESS_PROCESSED_ENGAGEMENT,
            engagement=edited_engagement,
            association=new_association,
            dry_run=settings.dry_run,
        )

    # A current association was found, indicating that we already processed this
    # engagement.
    logger.info(
        "Already processed this engagement, doing nothing",
        current_association=current_association,
    )
    return ResultType(action=ResultType.Action.SKIP_ALREADY_PROCESSED)


async def _process_engagement(
    association: Association,
    engagement: Engagement,
    model_client: ModelClient,
) -> None:
    """Edit the engagement and create the association. In case `dry_run` is True, only
    build the MO API payloads, but do not POST them to the API.
    """
    # Create association in "current" org unit
    association_response = await model_client.upload_object(association)
    logger.info("Created association", response=association_response)
    # Edit engagement, so it belongs to the "other" org unit
    engagement_response = await model_client.upload_object(engagement, edit=True)
    logger.info("Updated engagement", response=engagement_response)


def _dry_process_engagement(
    association: Association,
    engagement: Engagement,
) -> None:
    logger.info("Would create association", association=association)
    logger.info("Would update engagement", engagement=engagement)


def find_current_association(
    employee_uuid: UUID,
    associations: list[_Association],
) -> _Association | None:
    """Find the first association (if any) whose employee UUID matches the given
    `employee_uuid`.
    """
    association: _Association | None = first_true(
        associations,
        pred=lambda assoc: assoc.employee[0].uuid == employee_uuid,
    )
    return association


def find_related_unit(
    related_units: list[_OrgUnit],
    current_org_unit: _OrgUnitWithRelatedUnits,
) -> _OrgUnit | None:
    """Find the "other" org unit in `related_units`, e.g. the org unit which is *not*
    `current_org_unit`. E.g. if units A and B are related, and we pass unit A as the
    `current_org_unit`, this returns unit B.
    """
    other_unit: _OrgUnit | None = first_true(
        related_units,
        pred=lambda org_unit: org_unit.uuid != current_org_unit.uuid,
    )
    if other_unit is not None:
        logger.debug(
            "Found related unit",
            this_unit=current_org_unit.uuid,
            other_unit=other_unit.uuid,
        )
    return other_unit


def get_association_obj(
    employee_uuid: UUID,
    current_org_unit: _OrgUnitWithRelatedUnits,
    association_type_uuid: UUID,
) -> Association:
    """Build a new `Association` object based on `employee_uuid` and `current_org_unit`
    which is used to indicate the original organisation unit of the engagement after it
    has been processed.
    """
    return Association.from_simplified_fields(
        person_uuid=employee_uuid,
        org_unit_uuid=current_org_unit.uuid,
        association_type_uuid=association_type_uuid,
        # TODO: should the from date be identical to the from date of the engagement?
        from_date=datetime.now().strftime("%Y-%m-%d"),
    )


def get_engagement_obj(
    employee_uuid: UUID,
    engagement_uuid: UUID,
    engagement: _Engagement,
    other_unit: _OrgUnit,
) -> Engagement:
    """Build an edited `Engagement` object which updates the found engagement, so it is
    related to `other_unit` rather than its original org unit.
    """
    return Engagement.from_simplified_fields(
        uuid=engagement_uuid,
        person_uuid=employee_uuid,
        org_unit_uuid=other_unit.uuid,
        # Copy values from current engagement
        job_function_uuid=engagement.job_function_uuid,
        engagement_type_uuid=engagement.engagement_type_uuid,
        primary_uuid=engagement.primary_uuid,
        user_key=engagement.user_key,
        from_date=engagement.validity.from_date.strftime("%Y-%m-%d"),
    )


def _get_association_list(
    org_unit: _OrgUnit | _OrgUnitWithRelatedUnits,
) -> list[_Association]:
    """Return either a list of associations, or an empty list, in case
    `org_unit.associations` is None.
    """
    associations: list[_Association] = org_unit.associations or []
    return associations


async def _get_association_type_uuid(
    association_type: str,
    gql_client: PersistentGraphQLClient,
) -> UUID:
    """Return the class UUID of the association type specified in settings.

    Args:
        association_type: `user_key` of the association type to use.
        gql_client: GraphQL client instance.

    Returns:
        UUID
    """
    query = gql(
        """
        query AssociationTypeUUID($user_keys: [String!]) {
            classes(user_keys: $user_keys) {
                uuid
            }
        }
        """
    )
    result: dict = await gql_client.execute(query, {"user_keys": [association_type]})
    try:
        parsed_result: _AssociationTypeUUID = _AssociationTypeUUID.parse_obj(result)
    except ValidationError as exc:
        raise ValueError(
            f"could not find class UUID for class with user_key={association_type!r}"
        ) from exc
    return parsed_result.classes[0].uuid


async def get_bulk_update_payloads(
    gql_client: PersistentGraphQLClient
) -> iter[PayloadType]:
    # Retrieve all engagement UUIDs, as well as the employee UUID for each engagement
    query = gql(
        """
        query EngagementUUID {
            engagements {
                uuid
                objects {
                    employee_uuid
                }
            }
        }
        """
    )
    result: dict = await gql_client.execute(query)
    parsed_result: _EngagementResult = _EngagementResult.parse_obj(result)
    # Yield a payload for each engagement found
    for engagement in parsed_result.engagements:
        for employee in engagement.objects:
            yield PayloadType(
                uuid=employee.employee_uuid,
                object_uuid=engagement.uuid,
                time=datetime.now(),
            )
