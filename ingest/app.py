import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import os
import json
import logging
import time
import urllib.request
import urllib.parse
import urllib.error
import boto3
from datetime import date, timedelta
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

#Logging setup 
logger = logging.getLogger()
logger.setLevel(logging.INFO)

#Config from environment 
NYT_API_KEY    = os.environ["NYT_API_KEY"]
TABLE_NAME     = os.environ["DYNAMO_TABLE"]
BACKFILL_MODE  = os.environ.get("BACKFILL_MODE", "false").lower() == "true"
N_BACKFILL_WEEKS = 10   # how many past weeks to pull when backfilling

# Lists to track 
LISTS = [
    "hardcover-fiction",
    "hardcover-nonfiction",
    "paperback-trade-fiction",
    "young-adult-hardcover",
    "mass-market-paperback",
    "combined-print-and-e-book-fiction",
    "combined-print-and-e-book-nonfiction",
    "paperback-nonfiction"
]

NYT_BASE = "https://api.nytimes.com/svc/books/v3/lists/{date}/{list_name}.json"

dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(TABLE_NAME)


#Helper functions

# Given a date, find the most recent Sunday on or before that date
def last_sunday(ref: date) -> date:
    """Return the most recent Sunday on or before ref."""
    return ref - timedelta(days=(ref.weekday() + 1) % 7)

# Determine which Sundays to fetch based on mode
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

#NYT API fetch
def fetch_list(list_name: str, list_date) -> list | None:
    """
    Fetch one NYT bestseller list for a given date.
    Uses urllib (built-in) — no external dependencies needed.
    Returns a list of book dicts, or None on failure.
    """
    params = urllib.parse.urlencode({"api-key": NYT_API_KEY})
    url = (
        f"https://api.nytimes.com/svc/books/v3/lists/"
        f"{list_date.isoformat()}/{list_name}.json?{params}"
    )
    logger.info(f"Fetching {list_name} for {list_date}")

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        books_raw = raw.get("results", {}).get("books", [])
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

    except urllib.error.HTTPError as e:
        logger.error(f"HTTP error fetching {list_name}/{list_date}: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        logger.error(f"URL error fetching {list_name}/{list_date}: {e.reason}")
    except TimeoutError:
        logger.error(f"Timeout fetching {list_name}/{list_date}")
    except (KeyError, ValueError) as e:
        logger.error(f"Unexpected response shape for {list_name}/{list_date}: {e}")
    return None

#DynamoDB write
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

#Plot generation and upload
    
def generate_and_upload_plot(table, lists):
    logger.info("Generating plot from DynamoDB data")
    
    COLORS = {
        "hardcover-fiction":                    "#E24B4A",
        "hardcover-nonfiction":                 "#378ADD",
        "young-adult-hardcover":                "#EF9F27",
        "combined-print-and-e-book-fiction":    "#9B6DD4",
        "combined-print-and-e-book-nonfiction": "#1D9E75",
    }

    data = []
    for list_name in lists:
        try:
            resp = table.query(
                KeyConditionExpression=Key("list_name").eq(list_name),
                ScanIndexForward=False,
                Limit=10,
            )
            items = resp.get("Items", [])
            if not items:
                logger.warning(f"No data for {list_name}, skipping plot")
                continue
            all_titles = set()
            for week in items:
                for book in week.get("books", []):
                    try:
                        all_titles.add(book["title"])
                    except KeyError:
                        pass
            label = list_name.replace("-", " ").title()
            data.append((label, len(all_titles), list_name))
            logger.info(f"  {list_name}: {len(all_titles)} unique titles")
        except Exception as e:
            logger.error(f"Error gathering plot data for {list_name}: {e}")
            continue

    if not data:
        logger.error("No data to plot")
        return

    labels  = [d[0] for d in data]
    counts  = [d[1] for d in data]
    colors  = [COLORS.get(d[2], "#888780") for d in data]

    try:
        fig, ax = plt.subplots(figsize=(11, 6))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")

        bars = ax.bar(labels, counts, color=colors, width=0.55, zorder=3)

        for bar, count, color in zip(bars, counts, colors):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.8,
                str(count),
                ha="center", va="bottom",
                fontsize=13, fontweight="bold", color=color
            )

        ax.yaxis.grid(True, color="#f0f0f0", linewidth=1, zorder=0)
        ax.set_axisbelow(True)
        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color("#e0e0e0")
        ax.tick_params(axis="x", labelsize=11, colors="#666666", pad=8)
        ax.tick_params(axis="y", labelsize=11, colors="#aaaaaa")

        wrapped = []
        for label in labels:
            words = label.split()
            mid = len(words) // 2
            wrapped.append("\n".join([" ".join(words[:mid]), " ".join(words[mid:])]) if mid > 0 else label)
        ax.set_xticklabels(wrapped)

        ax.set_title(
            "NYT Bestseller Lists",
            fontsize=18, fontweight="bold", color="#1a1a1a",
            loc="left", pad=16
        )
        ax.set_ylabel("Unique Titles", fontsize=11, color="#aaaaaa", labelpad=10)
        fig.text(
            0.085, 0.91,
            "Unique titles appearing on each list over the last 10 weeks",
            fontsize=12, color="#aaaaaa"
        )
        ax.set_ylim(0, max(counts) * 1.2)
        fig.tight_layout(rect=[0, 0, 1, 0.88])

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)

        s3_client = boto3.client("s3")
        bucket = os.environ["S3_BUCKET"]
        s3_client.put_object(
            Bucket=bucket,
            Key="dp3/nyt-books/latest.png",
            Body=buf,
            ContentType="image/png"
        )
        logger.info(f"Plot uploaded to s3://{bucket}/dp3/nyt-books/latest.png")

    except Exception as e:
        logger.error(f"Failed to generate or upload plot: {e}")


#Lambda handler 

def lambda_handler(event, context):
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

            #NYT only accepts 5 requests per minute
            time.sleep(12)

    generate_and_upload_plot(table, LISTS)   
    logger.info(f"Run complete. success={success}, skipped={skipped}")
    return {"success": success, "skipped": skipped}



