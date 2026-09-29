import pytest

from project_relay.relay.urls import (
    canonical_conversation_url, conversation_id, is_new_chat_page, new_chat_url, site_of,
)


def test_chatgpt_urls():
    url = "https://chatgpt.com/g/g-p-abc/c/0f0f0f0f-1111-4222?model=x"
    assert site_of(url) == "chatgpt"
    assert conversation_id(url) == "0f0f0f0f-1111-4222"
    assert canonical_conversation_url(url) == "https://chatgpt.com/c/0f0f0f0f-1111-4222"
    assert is_new_chat_page("https://chatgpt.com/")
    assert not is_new_chat_page("https://chatgpt.com/?temporary-chat=true")


def test_claude_urls():
    url = "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
    assert site_of(url) == "claude"
    assert conversation_id(url) == "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
    assert canonical_conversation_url(url + "?x=1") == url
    assert is_new_chat_page("https://claude.ai/new")
    assert is_new_chat_page("https://claude.ai/new", site="claude")
    assert not is_new_chat_page("https://claude.ai/new", site="chatgpt")
    assert new_chat_url("claude") == "https://claude.ai/new"


def test_other_sites_rejected():
    assert site_of("https://evil.example/c/12345678") is None
    assert conversation_id("http://chatgpt.com/c/12345678") is None
    with pytest.raises(ValueError):
        canonical_conversation_url("https://claude.ai/project/123")
