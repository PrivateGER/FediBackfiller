import asyncio
import datetime
import urllib.parse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
from fastapi.middleware.cors import CORSMiddleware
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi import Limiter, _rate_limit_exceeded_handler
from starlette.responses import JSONResponse

origins = [
    "https://plasmatrap.com",
]

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["POST"],
    allow_headers=["*"],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

INSTANCE_BASE_URL = "https://plasmatrap.com"
AUTHENTICATION_CACHE = {}
DEBOUNCE_CACHE = {}
DEBOUNCE_TIMEOUT = 180


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


class FetchRepliesRequest(BaseModel):
    post_url: str
    token: str


@limiter.limit("16/minute")
@app.post("/fetch_replies")
async def fetch_replies(request: FetchRepliesRequest):
    async with httpx.AsyncClient(headers={
        "User-Agent": "PlasmaTrap.com Backfiller"
    }) as client:
        if request.token not in AUTHENTICATION_CACHE:
            # Verify user login by fetching the /i/ endpoint
            response = await client.post(f"{INSTANCE_BASE_URL}/api/i", json={
                "i": request.token
            })

            if response.status_code != 200:
                return {"message": "Invalid user token"}

            response = response.json()
            print("USER:", response["username"])
            AUTHENTICATION_CACHE[request.token] = response["username"]
        else:
            print("USER:", AUTHENTICATION_CACHE[request.token])

        if request.post_url in DEBOUNCE_CACHE and (
                datetime.datetime.now() - DEBOUNCE_CACHE[request.post_url]).total_seconds() < DEBOUNCE_TIMEOUT:
            return {"message": "Debounced"}

        DEBOUNCE_CACHE[request.post_url] = datetime.datetime.now()

        # Check whether the URI is a redirect, and follow it if it is. Replace the URI with the final URI.
        response = await client.get(request.post_url, follow_redirects=True)
        if response.status_code != 200:
            return JSONResponse(status_code=500, content={"message": "Failed to fetch post URL"})

        request.post_url = str(response.url)

        # Detect Mastodon or Misskey API based on ID schema
        # Mastodon uses Snowflake, Misskey uses a custom schema

        # Cut the post URL to get the ID
        post_id = request.post_url.split("/")[-1]
        post_base_host = urllib.parse.urlsplit(request.post_url).netloc

        # Check if the ID is a Snowflake (Pleroma/Akkoma or Mastodon)
        if len(post_id) == 18:
            # Fetch Mastodon replies
            print(f"GET MASTODON REPLIES: https://{post_base_host}/api/v1/statuses/{post_id}/context")

            response = await client.get(
                urllib.parse.urljoin(f"https://{post_base_host}", f"/api/v1/statuses/{post_id}/context"))
            if response.status_code != 200:
                return JSONResponse(status_code=500, content={"message": "Failed to fetch Mastodon replies"})

            response = response.json()

            # Limit: 50 replies. Cut off older ones, they're more likely to be irrelevant. Mastodon sorts old -> new
            if len(response["descendants"]) > 50:
                response["descendants"] = response["descendants"][-50:]

            tasks = [fetch_ap_object(client, reply["url"], request.token) for reply in response["descendants"]]
            await asyncio.gather(*tasks)

            return {"message": "Fetched Mastodon replies"}
        else:
            # Fetch Misskey replies (no other API matters lol)
            # We have to recurse through the replies, as Misskey doesn't provide a flat list of ALL replies

            print(f"GET MISSKEY REPLIES: https://{post_base_host}/api/notes/children")

            try:
                replies = await fetch_replies_recursive(client, post_base_host, post_id, request.token, 0, 50)
                return {"message": "Fetched Misskey replies", "replies": replies}
            except Exception as e:
                return JSONResponse(status_code=500, content={"message": f"Failed to fetch Misskey replies: {str(e)}"})


async def fetch_replies_recursive(client, post_base_host, post_id, token, depth, max_depth):
    if depth > max_depth:
        print("-ABORT- MAX DEPTH REACHED")
        return []

    response = await client.post(f"https://{post_base_host}/api/notes/children", json={
        "limit": 50,
        "noteId": post_id,
        "showQuotes": True
    })

    if response.status_code != 200:
        return []

    response = response.json()

    # Max 50 replies, cut off older ones (new -> old)
    if len(response) > 50:
        response = response[:50]

    if len(response) == 0:
        print("RECURSIVE BRANCH END FOUND")

    # Misskey does NOT include URIs for local posts, so we have to fake them
    for reply in response:
        if "uri" not in reply:
            reply["uri"] = f"https://{post_base_host}/notes/{reply['id']}"

        # Recursively fetch replies of the reply
        print("RECURSING into reply:", reply["uri"])
        reply["replies"] = await fetch_replies_recursive(client, post_base_host, reply['id'], token, depth + 1, max_depth)

    # fallback to url if uri isn't set
    tasks = [fetch_ap_object(client, reply["uri"], token) for reply in response]
    await asyncio.gather(*tasks)

    return response


async def fetch_ap_object(client, url, token):
    print("FETCHING:", url)
    try:
        ap_res = await client.post(f"{INSTANCE_BASE_URL}/api/ap/show", json={
            "uri": url,
            "i": token
        })
    except Exception as e:
        print("FAILED FETCH:", url)
        return

    if ap_res.status_code != 200:
        print("FAILED FETCH:", url)
        return

    print("FETCHED:", url)
