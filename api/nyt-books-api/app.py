import os
import logging
import boto3

from collections import defaultdict
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from chalice import Chalice, ChaliceViewError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

app = Chalice(app_name="nyt-books-api")

# Config 
TABLE_NAME  = os.environ["DYNAMO_TABLE"]
BUCKET_NAME = os.environ["S3_BUCKET"]
PLOT_KEY    = "dp3/nyt-books/latest.png"

LISTS = [
    "hardcover-fiction",
    "hardcover-nonfiction",
    "young-adult-hardcover",
    "combined-print-and-e-book-fiction",
    "combined-print-and-e-book-nonfiction",
]

dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(TABLE_NAME)
s3       = boto3.client("s3")


# Shared helpers 

def query_list(list_name: str, limit: int = 20) -> list[dict]:
    """Return the most recent `limit` weeks for a given list, newest first."""
    logger.info(f"Querying DynamoDB: list={list_name}, limit={limit}")
    try:
        resp = table.query(
            KeyConditionExpression=Key("list_name").eq(list_name),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        items = resp.get("Items", [])
        logger.info(f"  → {len(items)} weeks returned")
        return items
    except ClientError as e:
        logger.error(f"DynamoDB query failed for {list_name}: {e.response['Error']['Message']}")
        raise ChaliceViewError("Database error")


# Zone apex 

@app.route("/")
def index():
    return {
        "about": (
            "Tracks NYT bestseller lists weekly across fiction, nonfiction, "
            "YA, and science categories to reveal genre trends over time."
        ),
        "resources": ["current", "trend", "plot"],
    }


#current - show the most recent #1 book for each list
# /current 
@app.route('/current')
def current():
    logger.info("GET /current")
    parts = []
    for list_name in LISTS:
        try:
            items = query_list(list_name, limit=1)
            if not items:
                logger.warning(f"No data found for {list_name}")
                continue
            latest = items[0]
            top = latest["books"][0]
            label = list_name.replace("-", " ").title()
            parts.append(
                f"{label}: '{top['title']}' by {top['author']} "
                f"({top['weeks_on_list']} wks)"
            )
        except (IndexError, KeyError) as e:
            logger.error(f"Malformed record for {list_name}: {e}")
            continue

    if not parts:
        raise ChaliceViewError("No data available")

    return {"response": " | ".join(parts)}

#/trend - compare turnover of #1 titles across lists over last 10 weeks
@app.route('/trend')
def trend():
    logger.info("GET /trend")
    turnover = {}

    for list_name in LISTS:
        try:
            items = query_list(list_name, limit=10)
            if not items:
                logger.warning(f"No data for {list_name}, skipping")
                continue
            top_titles = set()
            for week in items:
                try:
                    top_titles.add(week["books"][0]["title"])
                except (IndexError, KeyError) as e:
                    logger.warning(f"Skipping malformed week in {list_name}: {e}")
            turnover[list_name] = len(top_titles)
            logger.info(f"  {list_name}: {len(top_titles)} distinct #1 titles")
        except Exception as e:
            logger.error(f"Error processing {list_name}: {e}")
            continue

    if not turnover:
        raise ChaliceViewError("No trend data available")

    most_volatile = max(turnover, key=turnover.get)
    least_volatile = min(turnover, key=turnover.get)
    label_most = most_volatile.replace("-", " ").title()
    label_least = least_volatile.replace("-", " ").title()

    return {
        "response": (
            f"Over the last 10 weeks, {label_most} had the most turnover "
            f"({turnover[most_volatile]} different #1 books), while "
            f"{label_least} was most stable "
            f"({turnover[least_volatile]} distinct #1 books)."
        )
    }

#get plot from S3, plot created in ingest lambda bc of matplotlib issues
#Klayers arn was added as a layer in ingest lambda for matplotlib issues
@app.route("/plot")
def plot():
    logger.info("GET /plot — returning S3 plot URL")
    url = f"https://{BUCKET_NAME}.s3.amazonaws.com/dp3/nyt-books/latest.png"
    return {"response": url}