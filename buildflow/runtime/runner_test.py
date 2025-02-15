import unittest

from buildflow.runtime import runner
from buildflow.runtime import Processor
from buildflow.runtime.ray_io import bigquery_io
from buildflow.runtime.ray_io import gcp_pubsub_io


class StreamProcessor1(Processor):
    def source(self):
        return gcp_pubsub_io.GCPPubSubSource(
            subscription="projects/project/subscriptions/sub"
        )

    def process(self, payload):
        return payload


class StreamProcessor2(Processor):
    def source(self):
        return gcp_pubsub_io.GCPPubSubSource(
            subscription="projects/project/subscriptions/sub"
        )

    def process(self, payload):
        return payload


class BatchProcessor1(Processor):
    def source(self):
        return bigquery_io.BigQuerySource(table_id="a.b.c")

    def process(self, payload):
        return payload


class BatchProcessor2(Processor):
    def source(self):
        return bigquery_io.BigQuerySource(table_id="a.b.c")

    def process(self, payload):
        return payload


_ERROR_TEXT = "Flows can only contain batch or streaming processors."


class RunnerTest(unittest.TestCase):
    def test_add_multiple_streaming(self):
        runtime = runner.Runtime()
        runtime.register_processor(StreamProcessor1())
        runtime.register_processor(StreamProcessor2())

    def test_add_multiple_batch(self):
        runtime = runner.Runtime()
        runtime.register_processor(BatchProcessor1())
        runtime.register_processor(BatchProcessor2())

    def test_add_batch_then_streaming(self):
        runtime = runner.Runtime()
        runtime.register_processor(BatchProcessor1())
        with self.assertRaisesRegex(ValueError, _ERROR_TEXT):
            runtime.register_processor(StreamProcessor2())

    def test_add_streaming_then_batch(self):
        runtime = runner.Runtime()
        runtime.register_processor(StreamProcessor1())
        with self.assertRaisesRegex(ValueError, _ERROR_TEXT):
            runtime.register_processor(BatchProcessor2())


if __name__ == "__main__":
    unittest.main()
