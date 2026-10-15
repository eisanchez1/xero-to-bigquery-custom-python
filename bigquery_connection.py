from google.cloud import bigquery

from config import GCP_PROJECT_ID, BQ_DATASET


CONTACT_SCHEMA = [
    bigquery.SchemaField("contact_id", "STRING"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("email_address", "STRING"),
    bigquery.SchemaField("is_supplier", "BOOL"),
    bigquery.SchemaField("is_customer", "BOOL"),
    bigquery.SchemaField("updated_date_utc", "TIMESTAMP"),
    bigquery.SchemaField("source_json", "STRING"),
]

INVOICE_SCHEMA = [
    bigquery.SchemaField("invoice_id", "STRING"),
    bigquery.SchemaField("invoice_number", "STRING"),
    bigquery.SchemaField("type", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("contact_id", "STRING"),
    bigquery.SchemaField("invoice_date", "STRING"),
    bigquery.SchemaField("due_date", "STRING"),
    bigquery.SchemaField("total", "FLOAT64"),
    bigquery.SchemaField("updated_date_utc", "TIMESTAMP"),
    bigquery.SchemaField("source_json", "STRING"),
]


class BigQueryConnection:
    def __init__(self):
        self.client = bigquery.Client(project=GCP_PROJECT_ID)
        self.dataset_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

    def table_id(self, table_name):
        return f"{self.dataset_id}.{table_name}"

    def exists(self, table_name):
        try:
            self.client.get_table(self.table_id(table_name))
            return True
        except Exception as exc:
            # Only treat a BigQuery "not found" error as absence.
            from google.api_core.exceptions import NotFound

            if isinstance(exc, NotFound):
                return False
            raise

    def load_rows(self, table_name, rows, schema, write_disposition):
        """Load JSON-compatible dictionaries using a BigQuery load job."""
        if not rows:
            print(f"{table_name}: no rows to load.")
            return

        job_config = bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=write_disposition,
        )

        job = self.client.load_table_from_json(
            rows,
            self.table_id(table_name),
            job_config=job_config,
        )
        job.result()

        table = self.client.get_table(self.table_id(table_name))
        print(f"{table_name}: BigQuery table now has {table.num_rows:,} rows.")

    def merge_stage(self, target_name, stage_name, key_column, schema):
        """Upsert stage records by their stable Xero ID."""
        target = self.table_id(target_name)
        stage = self.table_id(stage_name)

        columns = [field.name for field in schema]
        update_columns = [name for name in columns if name != key_column]

        update_clause = ",\n".join(
            f"T.`{name}` = S.`{name}`" for name in update_columns
        )
        insert_columns = ", ".join(f"`{name}`" for name in columns)
        insert_values = ", ".join(f"S.`{name}`" for name in columns)

        sql = f"""
        MERGE `{target}` AS T
        USING `{stage}` AS S
        ON T.`{key_column}` = S.`{key_column}`
        WHEN MATCHED THEN
          UPDATE SET {update_clause}
        WHEN NOT MATCHED THEN
          INSERT ({insert_columns})
          VALUES ({insert_values})
        """

        self.client.query(sql).result()
        self.client.delete_table(stage, not_found_ok=True)