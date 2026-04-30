import os
import json
import logging
import time
import requests
import boto3

from datetime import date, timedelta
from botocore.exceptions import ClientError

# ── Logging setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Config from environment ────────────────────────────────────────────────────
NYT_API_KEY    = os.environ["NYT_API_KEY"]
TABLE_NAME     = os.environ["DYNAMO_TABLE"]
BACKFILL_MODE  = os.environ.get("BACKFILL_MODE", "false").lower() == "true"
N_BACKFILL_WEEKS = 10   # how many past weeks to pull when backfilling

# Lists to track — add/remove as you like
LISTS = [
    "hardcover-fiction",
    "hardcover-nonfiction",
    "paperback-trade-fiction",
    "young-adult-hardcover",
    "science",
]

NYT_BASE = "https://api.nytimes.com/svc/books/v3/lists/{date}/{list_name}.json"

dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(TABLE_NAME)


# ── Helpers ────────────────────────────────────────────────────────────────────

def last_sunday(ref: date) -> date:
    """Return the most recent Sunday on or before ref."""
    return ref - timedelta(days=(ref.weekday() + 1) % 7)


def sundays_to_fetch() -> list[date]:
    """Return a list of Sunday dates to ingest."""
    if BACKFILL_MODE:
        base = last_sunday(date.today())
        dates = [base - timedelta(weeks=i) for i in range(N_BACKFILL_WEEKS)]
        logger.info(f"BACKFILL MODE: pulling {len(dates)} weeks back to {dates[-1]}")
        return dates
    else:
        d = last_sunday(date.today())
        logger.info(f"NORMAL MODE: pulling single week {d}")
        return [d]


def fetch_list(list_name: str, list_date: date) -> list[dict] | None:
    """
    Fetch one NYT bestseller list for a given date.
    Returns a list of book dicts, or None on failure.
    """
    url = NYT_BASE.format(date=list_date.isoformat(), list_name=list_name)
    logger.info(f"Fetching {list_name} for {list_date} — {url}")
    try:
        resp = requests.get(url, params={"api-key": NYT_API_KEY}, timeout=10)
        resp.raise_for_status()
        books_raw = resp.json().get("results", {}).get("books", [])
        books = [
            {
                "rank":          b["rank"],
                "title":         b["title"],
                "author":        b["author"],
                "weeks_on_list": b["weeks_on_list"],
                "publisher":     b["publisher"],
            }
            for b in books_raw
        ]
        logger.info(f"  → {len(books)} books retrieved")
        return books

    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching {list_name}/{list_date}: {e} — status {resp.status_code}")
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error fetching {list_name}/{list_date}: {e}")
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching {list_name}/{list_date}")
    except (KeyError, ValueError) as e:
        logger.error(f"Unexpected response shape for {list_name}/{list_date}: {e}")
    return None


def write_record(list_name: str, list_date: date, books: list[dict]) -> bool:
    """Write one week's list to DynamoDB. Returns True on success."""
    record = {
        "list_name":  list_name,
        "timestamp":  int(time.mktime(list_date.timetuple())),
        "run_date":   list_date.isoformat(),
        "books":      books,
    }
    logger.info(f"Writing {list_name}/{list_date} to DynamoDB ({len(books)} books)")
    try:
        table.put_item(Item=record)
        logger.info(f"  → Write succeeded")
        return True
    except ClientError as e:
        logger.error(
            f"DynamoDB write failed for {list_name}/{list_date}: "
            f"{e.response['Error']['Code']} — {e.response['Error']['Message']}"
        )
        return False


# ── Lambda handler ─────────────────────────────────────────────────────────────

def handler(event, context):
    logger.info(f"Lambda invoked. BACKFILL_MODE={BACKFILL_MODE}")
    dates   = sundays_to_fetch()
    success = 0
    skipped = 0

    for list_date in dates:
        for list_name in LISTS:
            books = fetch_list(list_name, list_date)
            if books is None:
                logger.warning(f"Skipping write for {list_name}/{list_date} — fetch failed")
                skipped += 1
                continue
            ok = write_record(list_name, list_date, books)
            if ok:
                success += 1
            else:
                skipped += 1

            # Be polite to the NYT API — stay under 5 req/min
            time.sleep(12)

    logger.info(f"Run complete. success={success}, skipped={skipped}")
    return {"success": success, "skipped": skipped}
