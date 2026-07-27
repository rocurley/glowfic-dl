from datetime import datetime
import aiohttp
import os
import asyncio
from getpass import getpass
from aiohttp.client import ClientSession
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import json

from .constants import GLOWFIC_ROOT

################
##   Consts   ##
################


COOKIE_NAME = "_glowfic_constellation_production"


###################
##   Functions   ##
###################


def get_creds(optional=False):
    if os.path.exists("creds.json"):
        with open("creds.json", "r") as fin:
            d = json.load(fin)
        return (d["username"], d["password"])

    if optional:
        print(
            "Log in? It will exempt you from rate limits and allow you to view any hidden posts in this continuity."
        )
        username = input("Username (blank to skip login): ")
        if username == "":
            print("Login skipped.")
            return None
    else:
        print("Login required.")
        username = input("Username: ")
    password = getpass()
    save_str = input("Save credentials to file? [y/N]")
    save = save_str.lower() in ["y", "yes", "true"]
    if save:
        print("Saving to creds.json")
        d = {"username": username, "password": password}
        with open("creds.json", "w") as f:
            json.dump(d, f)
    return (username, password)


def has_cookie(session: ClientSession) -> bool:
    for cookie in session.cookie_jar:
        if cookie.key == COOKIE_NAME:
            return True
    return False


async def login(session: ClientSession, optional=False, force=False, creds=None):
    """Set cookies and headers needed to access glowfic.
    Keyword arguments:
    session -- session used to connect to glowfic. Cookies and headers will be stored here.
    optional -- indicates if login can be skipped.
    force -- indicates that we should login even if we appear to have valid cookies.
    creds -- optional (username, password) tuple. If provided, skips get_creds().
    """
    if not force:
        has_auth = "Authorization" in session.headers
        if has_auth and has_cookie(session):
            return
    # Set cookie for non-API endpoints
    authenticity_token = await get_authenticity_token(session, GLOWFIC_ROOT)
    if creds is None:
        creds = get_creds(optional)
    if creds is None:
        assert optional
        return
    (username, password) = creds
    payload = {
        "username": username,
        "password": password,
        "authenticity_token": authenticity_token,
        "commit": "Log+in",
    }
    url = urljoin(GLOWFIC_ROOT, "login")
    resp = await session.post(url, data=payload)
    if not has_cookie(session):
        raise ValueError("Cookie not found after login")
    api_login_url = urljoin(GLOWFIC_ROOT, "/api/v1/login")
    payload = {
        "username": username,
        "password": password,
    }
    async with session.post(api_login_url, params=payload) as resp:
        token_json = await resp.json()
    try:
        token = token_json["token"]
    except KeyError:
        print(token_json)
        raise
    session.headers["Authorization"] = "Bearer %s" % token


async def get_authenticity_token(session, url):
    async with session.get(url) as resp:
        soup = BeautifulSoup(await resp.text(), "html.parser")
    token_tag = soup.find("meta", attrs={"name": "csrf-token"})
    assert token_tag is not None
    return token_tag["content"]


async def rate_limit_get(
    session: aiohttp.ClientSession, url, **kwargs
) -> aiohttp.ClientResponse:
    resp = await session.get(url, **kwargs)
    if resp.status != 429:
        return resp
    try:
        reset_time_unix = int(resp.headers["Ratelimit-Reset"])
    except KeyError:
        print("Rate limited and Ratelimit-Reset not included.")
        print(resp.headers)
        raise
    reset_time = datetime.fromtimestamp(reset_time_unix)
    sleep_duration = reset_time - datetime.now()
    sleep_seconds = sleep_duration.total_seconds() + 0.5
    print(
        "Sleeping until %s (%f seconds) at server's request"
        % (reset_time.isoformat(), sleep_seconds)
    )
    await asyncio.sleep(sleep_seconds)
    resp = await session.get(url, **kwargs)
    if resp.status == 429:
        raise Exception("Failed to respect rate limit!")
    return resp


async def auth_get(
    session: aiohttp.ClientSession, url, **kwargs
) -> aiohttp.ClientResponse:
    resp = await rate_limit_get(session, url, **kwargs)
    if resp.status == 403:
        await login(session, force=True)
        resp = await rate_limit_get(session, url)
        assert resp.status != 403
    return resp
