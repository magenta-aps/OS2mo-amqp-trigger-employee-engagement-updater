<!--
SPDX-FileCopyrightText: 2022 Magenta ApS <https://magenta.dk>
SPDX-License-Identifier: MPL-2.0
-->

# Employee Engagement Updater

This repository contains an OS2mo AMQP Trigger that updates employee engagements based on "organisation unit relations."

The intended usage is:
* Organisation units can be related to each other in MO, using the concept of `related_units`.
* This allows specifying a relationship between different organisation hierarchies (e.g. administrative and payroll hierarchies.)
* Engagements created in the payroll hierarchy can then be moved automatically to the corresponding location in the administrative hierarchy.

Program functionality:
* If an engagement is created in the payroll hierarchy, this trigger can detect it, and update the engagement so it belongs to the corresponding organisation unit in the administrative hierarchy instead.
* To mark the origin of the original engagement, an association is created in the original organisation unit.  

## Usage

* Adjust the `AMQP__URL` variable to point to the currently running message broker (RabbitMQ instance) of your OS2mo installation, either:
  * directly in `docker-compose.yml` or
  * by creating a `docker-compose.override.yaml` file.
* Configure the required `ASSOCIATION_TYPE_UUID` variable to contain the UUID of the desired association type. 
This used when the program creates associations marking the origin of the original engagement.

Now start the container using `docker-compose`:
```
docker-compose up -d
```

You should see the following:
```
2022-10-04 08:08.36 [info     ] Starting metrics server
2022-10-04 08:08.36 [info     ] Setting up clients
2022-10-04 08:08.36 [info     ] Setting up AMQP system
2022-10-04 08:08.36 [info     ] Register called                function=on_amqp_message routing_key=employee.engagement.*
2022-10-04 08:08.36 [info     ] Starting AMQP system
2022-10-04 08:08.36 [info     ] Starting AMQP system
2022-10-04 08:08.36 [info     ] Establishing AMQP connection   host=msg_broker path=/ port=5672 scheme=amqp user=guest
2022-10-04 08:08.36 [info     ] Creating AMQP channel
2022-10-04 08:08.36 [info     ] Attaching AMQP exchange to channel exchange=os2mo
2022-10-04 08:08.36 [info     ] Declaring unique message queue function=on_amqp_message queue_name=os2mo-amqp-trigger-employee-engagement-updater_on_amqp_message
2022-10-04 08:08.36 [info     ] Starting message listener      function=on_amqp_message
2022-10-04 08:08.36 [info     ] Binding routing keys           function=on_amqp_message
2022-10-04 08:08.36 [info     ] Binding routing-key            function=on_amqp_message routing_key=employee.engagement.*
```
Creating an employee engagement in a relevant organisation unit will then log the following:
```
2022-10-04 08:10.48 [debug    ] Received message               function=on_amqp_message message_id=4471787f0fa54055982ffeed1700833c routing_key=employee.engagement.create
2022-10-04 08:10.48 [debug    ] Message received               object_type=engagement payload=PayloadType(uuid=UUID('fceb334b-7fff-40c6-bc30-a10b665af3d2'), object_uuid=UUID('93aae37e-ad69-4fa8-ad9b-69c2a0fd2d76'), time=datetime.datetime(2022, 10, 4, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(seconds=7200)))) request_type=create service_type=employee
2022-10-04 08:10.49 [debug    ] Found related unit             other_unit=6453b4d3-ced6-4f0c-9720-0353081c94be this_unit=bf6d084e-f211-4322-95aa-40f30f0e6d3b
2022-10-04 08:10.49 [info     ] Created association            engagement_uuid=UUID('93aae37e-ad69-4fa8-ad9b-69c2a0fd2d76') response=dcf8abaf-4cee-40ca-aad1-dad9de3a209f
2022-10-04 08:10.49 [info     ] Updated engagement             engagement_uuid=UUID('93aae37e-ad69-4fa8-ad9b-69c2a0fd2d76') response=93aae37e-ad69-4fa8-ad9b-69c2a0fd2d76
```
And also log:
```
2022-10-04 08:10.49 [debug    ] Received message               function=on_amqp_message message_id=9d96374f78d14972bf370ddf0b1f12ad routing_key=employee.engagement.edit
2022-10-04 08:10.49 [debug    ] Message received               object_type=engagement payload=PayloadType(uuid=UUID('fceb334b-7fff-40c6-bc30-a10b665af3d2'), object_uuid=UUID('93aae37e-ad69-4fa8-ad9b-69c2a0fd2d76'), time=datetime.datetime(2022, 10, 4, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(seconds=7200)))) request_type=edit service_type=employee
2022-10-04 08:10.50 [debug    ] Found related unit             other_unit=bf6d084e-f211-4322-95aa-40f30f0e6d3b this_unit=6453b4d3-ced6-4f0c-9720-0353081c94be
2022-10-04 08:10.50 [info     ] Found association in other unit, doing nothing engagement_uuid=UUID('93aae37e-ad69-4fa8-ad9b-69c2a0fd2d76')
```
As the first request to edit the engagement triggers a subsequent `employee.engagement.edit` message from MO, relating to the "other" organisation unit in a pair of related organisation units.
The trigger logic detects this scenario and logs `Found association in other unit, doing nothing`.
Otherwise an infinite loop of cascading edits would be triggered. 

## Development

### Prerequisites

- [Poetry](https://github.com/python-poetry/poetry)

### Getting Started

1. Clone the repository:
```
git clone git@git.magenta.dk:rammearkitektur/os2mo-triggers/os2mo-amqp-trigger-employee-engagement-updater.git
```

2. Install all dependencies:
```
poetry install
```

3. Set up pre-commit:
```
poetry run pre-commit install
```

### Running the tests

You use `poetry` and `pytest` to run the tests:

`poetry run pytest`

You can also run specific files

`poetry run pytest tests/<test_folder>/<test_file.py>`

and even use filtering with `-k`

`poetry run pytest -k "Manager"`

You can use the flags `-vx` where `v` prints the test & `x` makes the test stop if any tests fails (Verbose, X-fail)

#### Running the integration tests

To run the integration tests, an AMQP instance must be available.

If an instance is already available, it can be used by configuring the `AMQP__URL` environment variable. 
Alternatively a RabbitMQ can be started in docker, using:
```
docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:3-management
```

## Versioning

This project uses [Semantic Versioning](https://semver.org/) with the following strategy:
- MAJOR: Incompatible changes to existing data models
- MINOR: Backwards compatible updates to existing data models OR new models added
- PATCH: Backwards compatible bug fixes

## Authors

Magenta ApS <https://magenta.dk>

## License

This project uses: [MPL-2.0](MPL-2.0.txt)

This project uses [REUSE](https://reuse.software) for licensing.
All licenses can be found in the [LICENSES folder](LICENSES/) of the project.
