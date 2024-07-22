import asyncio
import datetime
import urllib.parse

from fastapi import FastAPI
from pydantic import BaseModel
import httpx
from fastapi.middleware.cors import CORSMiddleware
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi import Limiter, _rate_limit_exceeded_handler

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

        # Detect Mastodon or Misskey API based on ID schema
        # Mastodon uses Snowflake, Misskey uses a custom schema

        # Cut the post URL to get the ID
        post_id = request.post_url.split("/")[-1]
        post_base_host = urllib.parse.urlsplit(request.post_url).netloc

        # Check if the ID is a Snowflake
        if len(post_id) == 18:
            # Fetch Mastodon replies
            print(f"GET MASTODON REPLIES: https://{post_base_host}/api/v1/statuses/{post_id}/context")

            response = await client.get(
                urllib.parse.urljoin(f"https://{post_base_host}", f"/api/v1/statuses/{post_id}/context"))
            if response.status_code != 200:
                return {"message": "Failed to fetch Mastodon replies"}

            response = response.json()

            tasks = [fetch_ap_object(client, reply["url"], request.token) for reply in response["descendants"]]
            await asyncio.gather(*tasks)

            DEBOUNCE_CACHE[request.post_url] = datetime.datetime.now()
            return {"message": "Fetched Mastodon replies"}
        else:
            # Fetch Misskey replies (no other API matters lol)

            print(f"GET MISSKEY REPLIES: https://{post_base_host}/api/notes/children")

            response = await client.post(f"https://{post_base_host}/api/notes/children", json={
                "limit": 50,
                "noteId": post_id,
                "showQuotes": True
            })

            if response.status_code != 200:
                return {"message": "Failed to fetch Misskey replies"}

            response = response.json()

            tasks = [fetch_ap_object(client, reply["uri"], request.token) for reply in response]
            await asyncio.gather(*tasks)

            DEBOUNCE_CACHE[request.post_url] = datetime.datetime.now()
            return {"message": "Fetched Misskey replies"}


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
