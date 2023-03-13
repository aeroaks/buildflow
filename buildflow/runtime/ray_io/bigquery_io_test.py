"""Tests for redis.py"""

from dataclasses import dataclass
import inspect
import unittest
from unittest import mock

from google.api_core import exceptions
from google.cloud import bigquery
from google.cloud import bigquery_storage_v1

import buildflow
from buildflow.runtime.ray_io import bigquery_io


@dataclass
class FakeTable:
    project: str
    dataset_id: str
    table_id: str
    num_rows: int


# NOTE: Async actors don't support local mode / mocks so this really only tests
# the initial setup of the source by calling the source_args() source method.
class BigQueryTest(unittest.TestCase):

    @mock.patch('google.cloud.bigquery.Client')
    @mock.patch('google.cloud.bigquery_storage_v1.BigQueryReadClient')
    def test_validate_setup_query_no_temp_dataset(
        self,
        bq_storage_mock: mock.MagicMock,
        bq_mock: mock.MagicMock,
    ):

        bq_storage_client = bq_storage_mock.return_value
        bq_client_mock = bq_mock.return_value

        query = 'SELECT * FROM TABLE'

        buildflow.BigQuerySource(query=query, billing_project='tmp').actor([])

        bq_client_mock.create_dataset.assert_called_once()
        bq_client_mock.update_dataset.assert_called_once()
        bq_client_mock.query.assert_called_once_with(query,
                                                     job_config=mock.ANY)
        bq_client_mock.get_table.assert_called_once()

        bq_storage_client.create_read_session.assert_called_once()

    @mock.patch('google.cloud.bigquery.Client')
    @mock.patch('google.cloud.bigquery_storage_v1.BigQueryReadClient')
    def test_validate_setup_query_with_temp_dataset(
        self,
        bq_storage_mock: mock.MagicMock,
        bq_mock: mock.MagicMock,
    ):

        bq_storage_client = bq_storage_mock.return_value

        query = 'SELECT * FROM TABLE'

        source = buildflow.BigQuerySource(query=query,
                                          temp_dataset='p.ds',
                                          billing_project='tmp')
        source.actor([])

        bq_client_mock = bq_mock.return_value
        bq_client_mock.create_dataset.assert_not_called()
        bq_client_mock.update_dataset.assert_not_called()
        bq_client_mock.query.assert_called_once_with(query,
                                                     job_config=mock.ANY)
        bq_client_mock.get_table.assert_called_once()

        bq_storage_client.create_read_session.assert_called_once()

    @mock.patch('google.cloud.bigquery.Client')
    @mock.patch('google.cloud.bigquery_storage_v1.BigQueryReadClient')
    def test_validate_setup_table(
        self,
        bq_storage_mock: mock.MagicMock,
        bq_mock: mock.MagicMock,
    ):

        bq_storage_client = bq_storage_mock.return_value

        bq_client_mock = bq_mock.return_value
        bq_client_mock.get_table.return_value = FakeTable('p', 'd', 't', 10)

        source = buildflow.BigQuerySource(table_id='p.d.t',
                                          billing_project='tmp')
        source.actor([])

        bq_client_mock.create_dataset.assert_not_called()
        bq_client_mock.update_dataset.assert_not_called()
        bq_client_mock.query.assert_not_called()
        bq_client_mock.get_table.assert_called_once_with('p.d.t')

        bq_storage_client.create_read_session.assert_called_once_with(
            parent='projects/p',
            read_session=bigquery_storage_v1.types.ReadSession(
                table='projects/p/datasets/d/tables/t',
                data_format=bigquery_storage_v1.types.DataFormat.ARROW),
            max_stream_count=1000)

    @mock.patch('google.cloud.bigquery.Client')
    def test_bigquery_source_setup_table_id(self, bq_mock: mock.MagicMock):
        bq = bigquery_io.BigQuerySource(table_id='p.ds.t')

        bq.setup()

        bq_mock.return_value.get_table.assert_called_once_with(table='p.ds.t')

    @mock.patch('google.cloud.bigquery.Client')
    def test_bigquery_source_setup_query(self, bq_mock: mock.MagicMock):
        bq = bigquery_io.BigQuerySource(query='query')

        bq.setup()

        bq_mock.return_value.query.assert_called_once()

    @mock.patch('google.cloud.bigquery.Client')
    def test_bigquery_sink_setup_create_table(self, bq_mock: mock.MagicMock):
        bq_client_mock = bq_mock.return_value
        bq_client_mock.get_table.side_effect = exceptions.NotFound('unused')

        bq = bigquery_io.BigQuerySink(table_id='p.ds.t')

        @dataclass
        class Output:
            field: int

        def process() -> Output:
            pass

        bq.setup(process_arg_spec=inspect.getfullargspec(process))

        bq_client_mock.create_dataset.assert_called_once_with('p.ds',
                                                              exists_ok=True)

        bq_client_mock.create_table.assert_called_once()

        table_call: bigquery.Table = bq_client_mock.create_table.call_args[0][
            0]
        self.assertEqual(table_call.project, 'p')
        self.assertEqual(table_call.dataset_id, 'ds')
        self.assertEqual(table_call.table_id, 't')
        self.assertEqual(table_call.schema, [
            bigquery.SchemaField(
                name='field', field_type='INTEGER', mode='REQUIRED')
        ])

    @mock.patch('google.cloud.bigquery.Client')
    def test_bigquery_sink_setup_schema_mismatch(self,
                                                 bq_mock: mock.MagicMock):
        bq_client_mock = bq_mock.return_value
        bq_client_mock.get_table.return_value = bigquery.Table(
            'p.ds.j',
            schema=[
                bigquery.SchemaField(name='field',
                                     field_type='FLOAT',
                                     mode='REQUIRED')
            ])

        bq = bigquery_io.BigQuerySink(table_id='p.ds.t')

        @dataclass
        class Output:
            field: int

        def process() -> Output:
            pass

        with self.assertRaises(ValueError):
            bq.setup(process_arg_spec=inspect.getfullargspec(process))

    @mock.patch('google.cloud.bigquery.Client')
    def test_bigquery_sink_setup_schema_match(self, bq_mock: mock.MagicMock):
        bq_client_mock = bq_mock.return_value
        bq_client_mock.get_table.return_value = bigquery.Table(
            'p.ds.j',
            schema=[
                bigquery.SchemaField(name='field',
                                     field_type='INTEGER',
                                     mode='REQUIRED')
            ])

        bq = bigquery_io.BigQuerySink(table_id='p.ds.t')

        @dataclass
        class Output:
            field: int

        def process() -> Output:
            pass

        bq.setup(process_arg_spec=inspect.getfullargspec(process))


if __name__ == '__main__':
    unittest.main()
