"""The Feeds page: read what came in, send the useful ones to Documents.

WHY THIS REPLACED AUTO-INGEST

Feed items used to become Event entities on arrival. Most feeds worth watching
are news sites, most of what a news site publishes is irrelevant to any given
case, and the result was a case file filling with records nobody asked for —
which then went to the correlation queue, where news stories about the same
city read suspiciously alike. A feature meant to bring information in was
mostly manufacturing noise.

Items now land on a page and go no further. An analyst reads the list, and the
ones that matter are sent to Documents, where they enter the same
read-it-and-decide workflow as a scanned page or a pasted transcript: OCR and
extraction run, suggestions go to Review, and a person accepts them. Nothing
from a feed reaches the case file without somebody putting it there.

FETCHING THE ARTICLE

A feed summary is usually one paragraph, which is not much for extraction to
work with, so sending an item fetches the linked page and converts it to text.
That is an outbound request to a third party, so it is fenced: only http/https,
no redirects to private addresses, a hard size cap and a short timeout. If it
fails for any reason the document is still created from the summary, and says
at the top that the fetch did not work — a document that quietly contains less
than you think is worse than one that tells you.
"""

import ipaddress
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import audit
import auth
from db import db_cursor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feeds", tags=["feeds"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")

# Enough for a long article, small enough that a misconfigured link to a disk
# image does not fill the volume.
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = int(os.environ.get("FEED_FETCH_TIMEOUT_SECONDS", "20"))
# Identifying the client is basic manners to the publisher, and some of them
# refuse an empty agent outright.
USER_AGENT = os.environ.get("FEED_FETCH_USER_AGENT",
                            "humint-platform/1.2 (+self-hosted case management)")

# Off by default, and it should stay off on anything facing the internet.
#
# On, though, for the deployment whose feeds genuinely live on the same
# network: an intranet wiki, an internal incident bulletin, a station's own
# dispatch page. That is a real and ordinary case for a self-hosted tool, and
# refusing it outright would mean those teams simply never get article text.
# The trade is stated rather than hidden: with this on, a feed can make this
# server fetch anything it can reach.
ALLOW_PRIVATE_FETCH = os.environ.get("FEED_FETCH_ALLOW_PRIVATE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Fetching an article, carefully
# ---------------------------------------------------------------------------

def _is_public_address(host: str) -> bool:
    """False for anything that resolves to a private, loopback or link-local
    address.

    The URL comes from a feed an administrator added, so this is not the
    hostile-input case — but a feed is still third-party content, and one that
    starts returning links to http://127.0.0.1:8131/api/... or to the metadata
    endpoint on a cloud host would otherwise have this server fetch them and
    hand back the contents. Resolving first and checking every answer is the
    cheap way to say no.
    """
    if ALLOW_PRIVATE_FETCH:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>",
                           re.IGNORECASE | re.DOTALL)
_BLOCK_END = re.compile(r"</(p|div|section|article|h[1-6]|li|tr|br)\s*/?>",
                        re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """Markup out, paragraph breaks kept.

    Not a reader-mode extractor — no attempt to find "the article" and discard
    the navigation. That is a large amount of machinery that is wrong often
    enough to be annoying, and the thing reading this afterwards is a person
    and an extraction model, both of which cope fine with a menu at the top.
    Keeping block boundaries as newlines is the part that matters: run
    together into one line, an article is much harder to read and to extract
    from.
    """
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    # Entities, the handful that actually show up.
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
                         ("&rsquo;", "’"), ("&lsquo;", "‘"),
                         ("&ldquo;", "“"), ("&rdquo;", "”"),
                         ("&mdash;", "—"), ("&ndash;", "–")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def fetch_article(url: str) -> tuple[str | None, str | None]:
    """(text, error). Never raises."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None, "That link could not be parsed."
    if parsed.scheme not in ("http", "https"):
        return None, f"Only http and https links can be fetched (this one is {parsed.scheme or 'none'})."
    if not parsed.hostname:
        return None, "That link has no host."
    if not _is_public_address(parsed.hostname):
        return None, ("That link points at a private or unreachable address, so it was not "
                      "fetched. Set FEED_FETCH_ALLOW_PRIVATE=true if your feeds are on your "
                      "own network.")

    try:
        with requests.get(url, timeout=FETCH_TIMEOUT_SECONDS, stream=True,
                          headers={"User-Agent": USER_AGENT,
                                   "Accept": "text/html,application/xhtml+xml,text/plain"}) as resp:
            if resp.status_code >= 400:
                return None, f"The publisher answered {resp.status_code}."
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type and not (content_type.startswith("text/")
                                     or content_type == "application/xhtml+xml"):
                return None, f"That link is {content_type}, not a web page."
            # Streamed with a hard stop rather than trusting Content-Length,
            # which a server is free to lie about or omit.
            chunks, total = [], 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_ARTICLE_BYTES:
                    return None, "That page is larger than the fetch limit."
                chunks.append(chunk)
            encoding = resp.encoding or "utf-8"
    except requests.RequestException as exc:
        return None, f"The page could not be fetched: {type(exc).__name__}."

    try:
        html = b"".join(chunks).decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = b"".join(chunks).decode("utf-8", errors="replace")

    text = _html_to_text(html)
    if len(text) < 200:
        return None, "The page held almost no readable text — it is probably script-rendered."
    return text, None


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------

@router.get("")
def list_feeds(user: dict = Depends(auth.require_user)):
    """The feeds and how much is waiting on each.

    Not admin-only: adding a feed is an admin decision, reading one is the job.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT f.id, f.label, f.url, f.is_active, f.last_polled_at, f.last_error, "
            "       f.item_retain_days, "
            "       count(*) FILTER (WHERE i.status = 'new') AS unread, "
            "       count(*) FILTER (WHERE i.status = 'sent') AS sent, "
            "       max(i.published_at) AS newest "
            "  FROM rss_feeds f LEFT JOIN rss_items i ON i.feed_id = f.id "
            " GROUP BY f.id ORDER BY f.label")
        feeds = [{"id": r[0], "label": r[1], "url": r[2], "is_active": r[3],
                  "last_polled_at": r[4].isoformat() if r[4] else None,
                  "last_error": r[5], "item_retain_days": r[6],
                  "unread": r[7], "sent": r[8],
                  "newest": r[9].isoformat() if r[9] else None}
                 for r in cur.fetchall()]
    return {"items": feeds, "unread_total": sum(f["unread"] for f in feeds)}


@router.get("/items")
def list_items(feed_id: int | None = Query(default=None),
               status: str = Query(default="new"),
               q: str | None = Query(default=None),
               limit: int = Query(default=100, ge=1, le=500),
               offset: int = Query(default=0, ge=0),
               user: dict = Depends(auth.require_user)):
    where, params = [], []
    if feed_id is not None:
        where.append("i.feed_id = %s")
        params.append(feed_id)
    if status != "all":
        if status not in ("new", "sent", "dismissed"):
            raise HTTPException(status_code=400, detail="Unknown status.")
        where.append("i.status = %s")
        params.append(status)
    if q:
        where.append("(i.title ILIKE %s OR i.summary ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with db_cursor() as cur:
        cur.execute(f"SELECT count(*) FROM rss_items i{clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT i.id, i.feed_id, f.label, i.title, i.link, i.summary, i.published_at, "
            "       i.status, i.attachment_id, i.sent_at, u.username, i.created_at "
            "  FROM rss_items i JOIN rss_feeds f ON f.id = i.feed_id "
            "  LEFT JOIN users u ON u.id = i.sent_by "
            f"{clause} "
            # Newest first by the publisher's own date, falling back to when
            # we saw it, so a feed with no dates is still in a sane order.
            " ORDER BY COALESCE(i.published_at, i.created_at) DESC, i.id DESC "
            " LIMIT %s OFFSET %s", [*params, limit, offset])
        items = [{"id": r[0], "feed_id": r[1], "feed_label": r[2], "title": r[3],
                  "link": r[4], "summary": r[5],
                  "published_at": r[6].isoformat() if r[6] else None,
                  "status": r[7], "attachment_id": r[8],
                  "sent_at": r[9].isoformat() if r[9] else None,
                  "sent_by_name": r[10],
                  "created_at": r[11].isoformat() if r[11] else None}
                 for r in cur.fetchall()]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Acting on an item
# ---------------------------------------------------------------------------

class SendRequest(BaseModel):
    # False sends the summary alone, for the link that is known to be
    # paywalled or that nobody wants this server touching.
    fetch_article: bool = True
    source_note: str | None = None


def _item(cur, item_id: int) -> dict:
    cur.execute(
        "SELECT i.id, i.title, i.link, i.summary, i.published_at, i.status, "
        "       i.attachment_id, f.label FROM rss_items i "
        "  JOIN rss_feeds f ON f.id = i.feed_id WHERE i.id = %s", (item_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such feed item.")
    return dict(zip(("id", "title", "link", "summary", "published_at", "status",
                     "attachment_id", "feed_label"), row))


def _document_body(item: dict, article: str | None, fetch_error: str | None) -> str:
    """The text that lands in Documents.

    A header block first, so six months later the document says where it came
    from without anybody having to remember. Then the article if we got one,
    and the summary if we did not — with the reason, stated, rather than a
    short document that looks complete.
    """
    head = [item["title"] or "Untitled item",
            f"Source: {item['feed_label']}"]
    if item["published_at"]:
        head.append(f"Published: {item['published_at'].strftime('%Y-%m-%d %H:%M UTC')}")
    if item["link"]:
        head.append(f"Link: {item['link']}")
    if fetch_error:
        head.append(f"Full text was not retrieved: {fetch_error} "
                    f"The feed's own summary follows.")
    body = article or item["summary"] or "(The feed carried no summary for this item.)"
    return "\n".join(head) + "\n\n" + ("-" * 60) + "\n\n" + body


@router.post("/items/{item_id}/to-document", status_code=201)
def send_to_documents(item_id: int, payload: SendRequest,
                      user: dict = Depends(auth.require_user)):
    """Turn one feed item into a document, and mark it dealt with.

    Written as a .txt through the same path as pasted text, so the worker
    picks it up for extraction exactly as it would anything else. There is no
    second pipeline here, and deliberately so.
    """
    with db_cursor() as cur:
        item = _item(cur, item_id)
    if item["status"] == "sent" and item["attachment_id"]:
        raise HTTPException(
            status_code=409,
            detail={"message": "That item is already in Documents.",
                    "document_id": item["attachment_id"]})

    article, fetch_error = (None, None)
    if payload.fetch_article and item["link"]:
        article, fetch_error = fetch_article(item["link"])
    elif payload.fetch_article:
        fetch_error = "The feed gave no link for this item."

    text = _document_body(item, article, fetch_error)
    content = text.encode("utf-8")

    now = datetime.now(timezone.utc)
    rel_dir = os.path.join(str(now.year), f"{now.month:02d}")
    os.makedirs(os.path.join(UPLOAD_DIR, rel_dir), exist_ok=True)
    storage_path = os.path.join(rel_dir, f"{uuid.uuid4().hex}.txt")
    abs_path = os.path.join(UPLOAD_DIR, storage_path)
    with open(abs_path, "wb") as fh:
        fh.write(content)

    title = (item["title"] or "Feed item")[:300]
    safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in title).strip()
    filename = ("-".join(safe.split())[:80] or "feed-item") + ".txt"
    note = (payload.source_note or "").strip() or f"RSS: {item['feed_label']}"

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO attachments (filename, title, source_note, is_pasted, "
                "        storage_path, mime_type, file_size_bytes, uploaded_by) "
                "VALUES (%s, %s, %s, TRUE, %s, 'text/plain', %s, %s) RETURNING id",
                (filename, title, note, storage_path, len(content), user["id"]))
            document_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE rss_items SET status = 'sent', attachment_id = %s, "
                "       sent_by = %s, sent_at = now() WHERE id = %s",
                (document_id, user["id"], item_id))
    except Exception:
        # Same reasoning as the upload path: never leave a file on disk that
        # no row will ever point at.
        try:
            os.remove(abs_path)
        except OSError:
            pass
        raise

    audit.record("feed.to_document", user=user, object_type="attachment",
                 object_id=document_id, object_label=title,
                 detail={"feed": item["feed_label"], "item_id": item_id,
                         "fetched_article": bool(article),
                         "fetch_error": fetch_error, "chars": len(text)})
    return {"document_id": document_id, "title": title, "filename": filename,
            "fetched_article": bool(article), "fetch_error": fetch_error,
            "chars": len(text)}


class StatusRequest(BaseModel):
    status: str


@router.post("/items/{item_id}/status")
def set_status(item_id: int, payload: StatusRequest,
               user: dict = Depends(auth.require_user)):
    """Dismiss an item, or put it back.

    A dismissed row is never deleted here: the row is the memory of having
    said no, and without it the next poll would show the same article again
    as though it were new.
    """
    if payload.status not in ("new", "dismissed"):
        raise HTTPException(status_code=400,
                            detail="An item can be dismissed or put back; sending is a separate action.")
    with db_cursor(commit=True) as cur:
        item = _item(cur, item_id)
        if item["status"] == "sent":
            raise HTTPException(status_code=409,
                                detail="That item is already in Documents.")
        cur.execute("UPDATE rss_items SET status = %s WHERE id = %s",
                    (payload.status, item_id))
    return {"id": item_id, "status": payload.status}


class BulkRequest(BaseModel):
    ids: list[int] | None = None
    feed_id: int | None = None


@router.post("/items/dismiss-all")
def dismiss_all(payload: BulkRequest, user: dict = Depends(auth.require_user)):
    """Clear the unread list, for one feed or all of them.

    The thing an analyst actually does after a week away: skim, send the two
    that matter, and sweep the rest. Without this the page is unusable after
    any absence, which is how a feed reader stops being read.
    """
    with db_cursor(commit=True) as cur:
        if payload.ids:
            cur.execute("UPDATE rss_items SET status = 'dismissed' "
                        " WHERE id = ANY(%s) AND status = 'new'", (payload.ids,))
        elif payload.feed_id is not None:
            cur.execute("UPDATE rss_items SET status = 'dismissed' "
                        " WHERE feed_id = %s AND status = 'new'", (payload.feed_id,))
        else:
            cur.execute("UPDATE rss_items SET status = 'dismissed' WHERE status = 'new'")
        count = cur.rowcount
    audit.record("feed.dismiss", user=user, object_type="rss_items", object_id=None,
                 object_label="Feed items", detail={"count": count,
                                                    "feed_id": payload.feed_id})
    return {"dismissed": count}
