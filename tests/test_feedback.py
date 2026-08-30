from listing_agent.feedback import parse_message


def test_parse_feedback_message():
    raw = b"""From: digest@example.com
To: inbox@example.com
Message-ID: <feedback-1@example.com>
Subject: Listing feedback: Dislike
Content-Type: text/plain; charset=utf-8

action=dislike
source=ebay
external_id=itm-42
title=Blue bowl
"""
    assert parse_message(raw) == {
        "action": "dislike", "source": "ebay", "external_id": "itm-42",
        "title": "Blue bowl", "message_id": "<feedback-1@example.com>",
    }


def test_parse_feedback_ignores_unrelated_or_incomplete_messages():
    assert parse_message(b"Subject: Re: hello\n\nsource=ebay") is None
    assert parse_message(b"Subject: Listing feedback: like\n\nsource=ebay\nexternal_id=x") is None
