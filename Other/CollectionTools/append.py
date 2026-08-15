"""Converts a list of Steam workshop items into a Steam collection."""

import os
import re
import time
from enum import Enum

from InquirerPy import inquirer
from loguru import logger
from requests import Session
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter
from steam.enums.common import EResult, EWorkshopFileType
from steam.webauth import WebAuth
from urllib3.util import Retry

STEAMCOMMUNITY_URL = "https://steamcommunity.com"


class Endpoints(Enum):
    COLLECTION_EDIT = f"{STEAMCOMMUNITY_URL}/sharedfiles/managecollection"
    COLLECTION_ADD = f"{STEAMCOMMUNITY_URL}/sharedfiles/addchild"


def post_with_retry(
    session: Session,
    url: str,
    *,
    json=None,
    headers: dict[str, str | bytes],
    max_retries: int = 3,
) -> dict:
    for attempt in range(max_retries + 1):
        response = session.post(url, json=json, headers=headers)
        response.raise_for_status()

        data: dict = response.json()

        success = int(data.get("success", EResult.Invalid.value))
        if success == EResult.OK.value:
            return data

        if success != EResult.Timeout.value:
            return data

        if attempt == max_retries:
            return data

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 2**attempt
        else:
            delay = 2**attempt

        time.sleep(delay)

    raise RuntimeError("unreachable")


def clear():
    return os.system("cls" if os.name == "nt" else "clear")


def main():
    clear()

    username: str = inquirer.text(  # type: ignore
        message="Steam username:",
        validate=lambda t: len(t) > 0,
        invalid_message="Username is invalid.",
    ).execute()
    username = username.strip()

    client = WebAuth(username)
    client.cli_login()

    assert client.logged_on
    clear()
    logger.info(f"Logged in as {client.username} ({client.steam_id})!")

    session = client.session

    # Apply a limiter and retry adapter
    retry = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"GET", "HEAD", "OPTIONS"},
        backoff_factor=1,
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    session.mount(STEAMCOMMUNITY_URL, LimiterAdapter(per_minute=60, per_day=50_000))
    session.mount(STEAMCOMMUNITY_URL, HTTPAdapter(max_retries=retry))

    collection_id: str = inquirer.text(  # type: ignore
        message="Collection ID to add items to:",
        validate=lambda text: text.isdigit() and len(text) > 0,
        invalid_message="Collection ID must be a positive integer.",
    ).execute()
    collection_id = collection_id.strip()

    # Send a request to the collection page to ensure it exists.

    res = session.get(f"{Endpoints.COLLECTION_EDIT.value}/?id={collection_id}")
    res.raise_for_status()

    if "error_ctn" in res.text:
        logger.error("Steam returned an error when trying to check the collection!")

        err_str = re.search(r"<div id=\"message\">\s*.+\s*.+\s*.+\s*.+\s*<h3>(.+)</h3>", res.text)
        if err_str and len(err_str.groups()) > 0:
            logger.error(f"\tReason: {err_str.group(0)}")

        session.close()
        return

    workshop_ids_str: str = inquirer.text(  # type: ignore
        message="Workshop IDs to add to the collection (comma-separated):",
        validate=lambda text: len(text) > 0,
        invalid_message="Workshop IDs cannot be empty.",
    ).execute()
    workshop_ids_str = workshop_ids_str.strip().replace(";", ",").replace("/", ",")
    workshop_ids = workshop_ids_str.split(",")

    orig_len = len(workshop_ids)
    workshop_ids = list(dict.fromkeys(workshop_ids))
    dups = orig_len - len(workshop_ids)

    if dups > 0:
        logger.info(f"Found and removed {dups} duplicate{"s" if dups > 1 else ""}")

    logger.info(f"Adding {len(workshop_ids)} items to the collection {collection_id}...")
    proceed = inquirer.confirm(message="Proceed?", default=True).execute()  # type: ignore

    if proceed is False:
        logger.info("Operation aborted.")
        session.close()
        return

    # Send the requests to add all the items to the collection.

    before = time.time()
    for workshop_id in workshop_ids:
        workshop_id = workshop_id.strip()

        data = post_with_retry(
            session,
            Endpoints.COLLECTION_ADD.value,
            json="&".join(
                f"{k}={v}"
                for k, v in {
                    "id": collection_id,
                    "childid": workshop_id,
                    "sessionid": client.sessionID,
                }.items()
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )

        success = int(data.get("success", EResult.Invalid.value))
        if success == EResult.DuplicateRequest.value:
            logger.warning("Failed to add item to collection.")
            logger.warning("\tReason: Item was already in the collection")
            break

        if success != EResult.OK.value:
            logger.error("Failed to add item to collection.")
            logger.error(f"\tReason: Expected success to be OK but got {EResult(success).name}")
            break

        html = str(data.get("html"))
        if html == "None":
            logger.error("Failed to add item to collection.")
            logger.error("\tReason: 'html' in request response is None")
            break

        mod_id_re = re.search(r"Mod ID:\s*([^\"]+?)(?:<|\")", html)
        mod_id = mod_id_re.group(1) if mod_id_re else "unknown"

        file_type = int(data.get("fileType", EWorkshopFileType.Community.value))
        item_type = "item" if file_type != EWorkshopFileType.Collection.value else "collection"

        logger.info(f"Added {item_type} {mod_id} ({workshop_id}) to collection {collection_id}.")
    after = time.time()

    logger.info(f"Time taken: {(after - before):.2f}s")
    session.close()


if __name__ == "__main__":
    main()
