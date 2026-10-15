import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import time

from google.cloud import bigquery

from config import WATERMARK_FILE
from bigquery_connection import (
    BigQueryConnection,
    CONTACT_SCHEMA,
    INVOICE_SCHEMA,
)
from xero_connection import XeroConnection


PAGE_SIZE = 100

ENTITY_CONFIG = {
    "Contacts": {
        "response_key": "Contacts",
        "id_field": "ContactID",
        "table_key": "contact_id",
        "schema": CONTACT_SCHEMA,
    },
    "Invoices": {
        "response_key": "Invoices",
        "id_field": "InvoiceID",
        "table_key": "invoice_id",
        "schema": INVOICE_SCHEMA,
    },
}


def parse_xero_datetime(value):
    """Convert Xero's ISO or /Date(milliseconds)/ format to UTC ISO text."""
    if not value:
        return None

    if isinstance(value, str):
        match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d+)?\)/", value)
        if match:
            dt = datetime.fromtimestamp(
                int(match.group(1)) / 1000,
                tz=timezone.utc,
            )
            return dt.isoformat()

        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return None

    return None


def normalize_xero_date(value):
    """Normalize a Xero date to YYYY-MM-DD when possible."""
    if not value:
        return None

    parsed = parse_xero_datetime(value)
    if parsed:
        return parsed[:10]

    return str(value)[:10]


def safe_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def transform_contact(contact):
    return {
        "contact_id": contact.get("ContactID"),
        "name": contact.get("Name"),
        "email_address": contact.get("EmailAddress"),
        "is_supplier": contact.get("IsSupplier"),
        "is_customer": contact.get("IsCustomer"),
        "updated_date_utc": parse_xero_datetime(
            contact.get("UpdatedDateUTC")
        ),
        "source_json": json.dumps(
            contact,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def transform_invoice(invoice):
    contact = invoice.get("Contact") or {}

    return {
        "invoice_id": invoice.get("InvoiceID"),
        "invoice_number": invoice.get("InvoiceNumber"),
        "type": invoice.get("Type"),
        "status": invoice.get("Status"),
        "contact_id": contact.get("ContactID"),
        "invoice_date": normalize_xero_date(invoice.get("Date")),
        "due_date": normalize_xero_date(invoice.get("DueDate")),
        "total": safe_float(invoice.get("Total")),
        "updated_date_utc": parse_xero_datetime(
            invoice.get("UpdatedDateUTC")
        ),
        "source_json": json.dumps(
            invoice,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def load_watermarks() -> dict[str, str]:
    if not WATERMARK_FILE.exists():
        return {}

    with WATERMARK_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        return {}

    return data

def save_watermarks(watermarks):
    WATERMARK_FILE.write_text(
        json.dumps(watermarks, indent=2),
        encoding="utf-8",
    )


def fetch_entity(xero, entity_name, modified_since=None):
    config = ENTITY_CONFIG[entity_name]
    all_records = []
    page = 1

    while True:
        response = xero.get_accounting_page(
            entity_name,
            params={"page": page},
            modified_since=modified_since,
        )

        records = response.get(config["response_key"], [])
        if not records:
            break

        all_records.extend(records)
        print(
            f"{entity_name}: fetched page {page} "
            f"({len(records)} records)"
        )

        if len(records) < PAGE_SIZE:
            break

        page += 1

    return all_records


def sync_entity(
    xero,
    bq,
    entity_name,
    full_refresh=False,
    watermarks=None,
):
    if watermarks is None:
        watermarks = {}
        
    config = ENTITY_CONFIG[entity_name]
    table_name = entity_name
    schema = config["schema"]
    key_column = config["table_key"]

    # Use the same cutoff for the next incremental run.
    cutoff = datetime.now(timezone.utc)

    old_watermark = watermarks.get(entity_name)
    modified_since = None

    if not full_refresh and old_watermark and bq.exists(table_name):
        modified_since = datetime.fromisoformat(old_watermark)
        print(f"{entity_name}: changes since {old_watermark}")
    else:
        full_refresh = True
        print(f"{entity_name}: performing a full refresh")

    records = fetch_entity(
        xero,
        entity_name,
        modified_since=modified_since,
    )

    if entity_name == "Contacts":
        rows = [transform_contact(record) for record in records]
    else:
        rows = [transform_invoice(record) for record in records]

    # A full refresh replaces the destination. Incremental runs merge
    # records by their stable Xero IDs to prevent duplicate rows.
    if full_refresh:
        bq.load_rows(
            table_name,
            rows,
            schema,
            bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
    elif rows:
        stage_name = f"{table_name}_stage_{uuid4().hex[:10]}"

        try:
            bq.load_rows(
                stage_name,
                rows,
                schema,
                bigquery.WriteDisposition.WRITE_TRUNCATE,
            )
            bq.merge_stage(
                table_name,
                stage_name,
                key_column,
                schema,
            )
        except Exception:
            bq.client.delete_table(
                bq.table_id(stage_name),
                not_found_ok=True,
            )
            raise
    else:
        print(f"{entity_name}: no changed records returned by Xero.")

    # Advance only after extraction and loading/merging succeeded.
    watermarks[entity_name] = cutoff.isoformat()
    save_watermarks(watermarks)

    print(
        f"{entity_name}: received {len(records):,} records "
        f"from Xero."
    )


def run_pipeline(full_refresh=False):
    start = time.perf_counter()
    xero = XeroConnection()
    if not xero.tenant.get("tenant_id"):
        raise RuntimeError(
            "Xero organization not selected. Run: python main.py authorize"
        )

    bq = BigQueryConnection()
    watermarks = load_watermarks() or {}

    # Run sequentially to keep API request volume controlled.
    for entity_name in ("Contacts", "Invoices"):
        sync_entity(
            xero,
            bq,
            entity_name,
            full_refresh=full_refresh,
            watermarks=watermarks,
        )

    elapsed = time.perf_counter() - start
    print(f"\nPipeline finished in {elapsed:.2f} seconds.\n")
    print(f"Local Xero API calls today: {xero.rate['daily_calls']}")