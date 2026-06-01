"""Tests for the Feishu webhook handling."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI


@pytest.fixture
def app():
    from mini_claw.channels.feishu import FeishuChannel

    channel = FeishuChannel(
        app_id="cli_test",
        app_secret="test_secret",
        verification_token="test_token",
    )
    fastapi_app = FastAPI()
    router = channel.create_webhook_router(gateway=MagicMock())
    fastapi_app.include_router(router)
    return fastapi_app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_url_verification(client):
    payload = {
        "type": "url_verification",
        "challenge": "abc123",
        "token": "test_token",
    }
    resp = client.post("/feishu/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "abc123"


def test_duplicate_event_ignored(client):
    payload = {
        "header": {"event_id": "evt_001", "event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text": "hello"}',
            },
            "sender": {"sender_id": {"user_id": "user_001"}},
        },
    }
    resp1 = client.post("/feishu/webhook", json=payload)
    assert resp1.status_code == 200

    resp2 = client.post("/feishu/webhook", json=payload)
    assert resp2.status_code == 200
