"""Chat URLs for the supported sites: ChatGPT (worker) and claude.ai (PM)."""
from __future__ import annotations

import re
import urllib.parse

CHATGPT_ORIGIN = "https://chatgpt.com"
CLAUDE_ORIGIN = "https://claude.ai"

SITES = {
    "chatgpt": {
        "hosts": {"chatgpt.com", "www.chatgpt.com"},
        "conversation": re.compile(r"/c/([0-9A-Za-z-]{8,})(?:[/?#]|$)"),
        "canonical": CHATGPT_ORIGIN + "/c/{id}",
        "new_paths": {"", "/"},
        "new_url": CHATGPT_ORIGIN + "/",
    },
    "claude": {
        "hosts": {"claude.ai"},
        "conversation": re.compile(r"^/chat/([0-9A-Za-z-]{8,})(?:[/?#]|$)"),
        "canonical": CLAUDE_ORIGIN + "/chat/{id}",
        "new_paths": {"/new"},
        "new_url": CLAUDE_ORIGIN + "/new",
    },
}

# The PM runs on claude.ai; the Worker (and solo runs) on ChatGPT.
ROLE_SITE = {"pm": "claude", "worker": "chatgpt"}


def site_of(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme != "https":
        return None
    for name, site in SITES.items():
        if parsed.hostname in site["hosts"]:
            return name
    return None


def conversation_id(url: str | None) -> str | None:
    """Conversation id from a chat URL on a supported site, else None."""
    site = site_of(url)
    if site is None:
        return None
    match = SITES[site]["conversation"].search(urllib.parse.urlparse(url.strip()).path)
    return match.group(1) if match else None


def canonical_conversation_url(url: str) -> str:
    site, cid = site_of(url), conversation_id(url)
    if site is None or cid is None:
        raise ValueError(f"Not a ChatGPT or Claude conversation URL: {url}")
    return SITES[site]["canonical"].format(id=cid)


def is_new_chat_page(url: str | None, site: str | None = None) -> bool:
    """A new-chat page on `site` (default: whichever site the URL is on); never a temporary chat."""
    actual = site_of(url)
    if actual is None or (site is not None and actual != site):
        return False
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("temporary-chat", ["false"])[0].lower() == "true":
        return False
    return parsed.path in SITES[actual]["new_paths"]


def new_chat_url(site: str) -> str:
    return SITES[site]["new_url"]
