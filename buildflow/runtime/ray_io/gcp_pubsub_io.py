"""IO connectors for Pub/Sub and Ray."""

import dataclasses
import datetime
from google.cloud.monitoring_v3 import query
import inspect
import logging
import json
from typing import Any, Callable, Dict, Iterable, Optional, Union, Type

import ray

from buildflow import utils
from buildflow.api import io
from buildflow.api.depends import Publisher
from buildflow.runtime.ray_io import base
from buildflow.runtime.ray_io import gcp_pubsub_utils
from buildflow.runtime.ray_io.gcp import clients

_BACKLOG_QUERY_TEMPLATE = """\
fetch pubsub_subscription
| metric 'pubsub.googleapis.com/subscription/num_unacked_messages_by_region'
| filter
    resource.project_id == '{project}'
    && (resource.subscription_id == '{sub_id}')
| group_by 1m,
    [value_num_unacked_messages_by_region_mean:
       mean(value.num_unacked_messages_by_region)]
| every 1m
| group_by [],
    [value_num_unacked_messages_by_region_mean_aggregate:
       aggregate(value_num_unacked_messages_by_region_mean)]
"""


@dataclasses.dataclass(frozen=True)
class _PubSubSourcePlan:
    topic: str
    subscription: str


@dataclasses.dataclass(frozen=True)
class _PubSubSinkPlan:
    topic: str


class PubSubPublisher(Publisher):
    def __init__(self, topic: str, project: str):
        self.client = clients.get_publisher_client(project)
        self.topic = topic

    def publish(self, element: Union[Dict[str, Any], Any]):
        if dataclasses.is_dataclass(element):
            element = utils.dataclass_to_json(element)
        elif not isinstance(element, dict):
            raise ValueError("only dataclasses and dicts may be published")
        json_element = json.dumps(element)
        return self.client.publish(self.topic, json_element.encode("utf-8"))


@dataclasses.dataclass(frozen=True)
class PubsubMessage:
    data: Dict[str, Any]
    attributes: Dict[str, Any]


@dataclasses.dataclass
class GCPPubSubSource(io.StreamingSource):
    """Source for connecting to a Pub/Sub subscription."""

    subscription: str
    # The topic to connect to for the subscription. If this is provided and
    # subscription does not exist we will create it.
    topic: str = ""
    # Whether or not to include the pubsub attributes. If this is true you will
    # get a buildflow.PubsubMessage class as your input.
    include_attributes: bool = False
    # The project to bill for Pub/Sub usage. If not set we use the project that
    # the subscription exists in.
    billing_project: str = ""

    def __post_init__(self):
        if not self.billing_project:
            split_sub = self.subscription.split("/")
            self.billing_project = split_sub[1]

    def plan(self, process_arg_spec: inspect.FullArgSpec) -> Dict[str, Any]:
        plan_dict = dataclasses.asdict(
            _PubSubSourcePlan(topic=self.topic, subscription=self.subscription)
        )
        if not plan_dict["topic"]:
            del plan_dict["topic"]
        return plan_dict

    def setup(self):
        gcp_pubsub_utils.maybe_create_subscription(
            pubsub_subscription=self.subscription,
            pubsub_topic=self.topic,
            billing_project=self.billing_project,
        )

    def actor(self, ray_sinks, proc_input_type: Optional[Type]):
        return PubSubSourceActor.remote(ray_sinks, proc_input_type, self)

    def backlog(self) -> Optional[int]:
        split_sub = self.subscription.split("/")
        project = split_sub[1]
        sub_id = split_sub[3]
        client = clients.get_metrics_client(project)
        backlog_query = query.Query(
            client=client,
            project=project,
            end_time=datetime.datetime.now(),
            metric_type=(
                "pubsub.googleapis.com/subscription" "/num_unacked_messages_by_region"
            ),
            minutes=5,
        )
        backlog_query = backlog_query.select_resources(subscription_id=sub_id)
        last_timeseries = None
        try:
            for backlog_data in backlog_query.iter():
                last_timeseries = backlog_data
            if last_timeseries is None:
                return None
        except Exception:
            logging.error(
                "Failed to get backlog for subscription %s please ensure your user "
                "has: roles/monitoring.viewer to read the backlog, "
                "no autoscaling will happen.",
                self.subscription,
            )
            return None
        points = list(last_timeseries.points)
        points.sort(key=lambda p: p.interval.end_time, reverse=True)
        return points[0].value.int64_value

    @classmethod
    def recommended_num_threads(cls):
        # The actor becomes mainly network bound after roughly 4 threads, and
        # additional threads start to hurt cpu utilization.
        # This number is based on a single actor instance.
        return 4

    def publisher(self):
        return PubSubPublisher(self.topic, self.billing_project)


@dataclasses.dataclass
class GCPPubSubSink(io.Sink):
    """Source for writing to a Pub/Sub topic."""

    topic: str
    # The project to bill for Pub/Sub usage. If not set we use the project that
    # the topic exists in.
    billing_project: str = ""

    def __post_init__(self):
        if not self.billing_project:
            split_topic = self.topic.split("/")
            self.billing_project = split_topic[1]

    def setup(self, process_arg_spec: inspect.FullArgSpec):
        gcp_pubsub_utils.maybe_create_topic(
            pubsub_topic=self.topic, billing_project=self.billing_project
        )

    def actor(self, remote_fn: Callable, is_streaming: bool):
        return PubSubSinkActor.remote(remote_fn, self)

    def plan(self, process_arg_spec: inspect.FullArgSpec) -> Dict[str, Any]:
        return dataclasses.asdict(_PubSubSinkPlan(topic=self.topic))


@ray.remote(num_cpus=GCPPubSubSource.num_cpus())
class PubSubSourceActor(base.StreamingRaySource):
    def __init__(
        self,
        ray_sinks: Dict[str, base.RaySink],
        processor_input_type: Optional[Type],
        pubsub_ref: GCPPubSubSource,
    ) -> None:
        super().__init__(ray_sinks, processor_input_type)
        self.subscription = pubsub_ref.subscription
        self.include_attributes = pubsub_ref.include_attributes
        self.billing_project = pubsub_ref.billing_project
        self.batch_size = 1000
        self.running = True

    async def run(self):
        pubsub_client = clients.get_async_subscriber_client(self.billing_project)
        while self.running:
            ack_ids = []
            payloads = []
            try:
                response = await pubsub_client.pull(
                    subscription=self.subscription,
                    max_messages=self.batch_size,
                    return_immediately=True,
                )
            except Exception as e:
                logging.error("pubsub pull failed with: %s", e)
                continue
            for received_message in response.received_messages:
                json_loaded = {}
                if received_message.message.data:
                    decoded_data = received_message.message.data.decode()
                    json_loaded = json.loads(decoded_data)
                if self.include_attributes:
                    att_dict = {}
                    attributes = received_message.message.attributes
                    for key, value in attributes.items():
                        att_dict[key] = value
                    payload = PubsubMessage(json_loaded, att_dict)
                else:
                    payload = json_loaded
                payloads.append(payload)
                ack_ids.append(received_message.ack_id)

            # payloads will be empty if the pull times out (usually because
            # there's no data to pull).
            if payloads:
                try:
                    await self._send_batch_to_sinks_and_await(payloads)
                    await pubsub_client.acknowledge(
                        ack_ids=ack_ids, subscription=self.subscription
                    )
                except Exception:
                    logging.exception("Failed to process message, will not be acked.")
                    # This nacks the messages. See:
                    # https://github.com/googleapis/python-pubsub/pull/123/files
                    ack_deadline_seconds = 0
                    await pubsub_client.modify_ack_deadline(
                        subscription=self.subscription,
                        ack_ids=ack_ids,
                        ack_deadline_seconds=ack_deadline_seconds,
                    )
            # For pub/sub we determine the utilization based on the number of
            # messages received versus how many we received.
            self.update_metrics(len(payloads))

    def shutdown(self):
        self.running = False
        print("Shutting down Pub/Sub subscription")
        return True


@ray.remote(num_cpus=GCPPubSubSink.num_cpus())
class PubSubSinkActor(base.RaySink):
    def __init__(
        self,
        remote_fn: Callable,
        pubsub_ref: GCPPubSubSink,
    ) -> None:
        super().__init__(remote_fn)
        self.pubslisher_client = clients.get_publisher_client(
            pubsub_ref.billing_project
        )
        self.topic = pubsub_ref.topic

    async def _write(
        self,
        elements: Union[ray.data.Dataset, Iterable[Dict[str, Any]]],
    ):
        # TODO: need to support writing to Pub/Sub in batch mode.
        def publish_element(item):
            future = self.pubslisher_client.publish(
                self.topic, json.dumps(item).encode("UTF-8")
            )
            future.result()

        for elem in elements:
            publish_element(elem)
        return
