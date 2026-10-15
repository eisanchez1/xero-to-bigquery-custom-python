import argparse

from pipeline import run_pipeline
from xero_connection import XeroConnection


def main():
    parser = argparse.ArgumentParser(
        description="Replicate Xero Contacts and Invoices to BigQuery."
    )
    parser.add_argument(
        "command",
        choices=("authorize", "full-refresh", "incremental"),
        help="Authorize Xero or run a full or incremental sync.",
    )
    args = parser.parse_args()

    if args.command == "authorize":
        XeroConnection().authorize()
    elif args.command == "full-refresh":
        run_pipeline(full_refresh=True)
    else:
        run_pipeline(full_refresh=False)


if __name__ == "__main__":
    main()