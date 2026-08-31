from listing_agent.ai import OpenAIJudge, _taste_hash, _title_hash, is_fast_tracked, title_gate, taste_judgment


class FakeJudge(OpenAIJudge):
    def __init__(self, result):
        self.model = "gpt-5.6-luna"
        self.result = result
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        return self.result


def test_title_gate_batches_listings_and_returns_results():
    listings = [{"external_id": "x", "search_id": "s", "title": "RL trousers", "description": "", "size_fields": {}}]
    result = title_gate(FakeJudge({"results": [{"external_id": "x", "pass": True, "reason": "Matches.", "category": "clothing"}]}), listings, {"s": {"query": "trousers", "required_size_fields": {"waist": 30}}})
    assert result == {"x": {"pass": True, "reason": "Matches.", "category": "clothing"}}


def test_title_gate_prompt_strictly_enforces_allowed_sizes():
    judge = FakeJudge({"results": []})
    title_gate(judge, [{"external_id": "x", "search_id": "s", "title": "Mens 38x26 trousers", "description": "", "size_fields": {}}], {
        "s": {"allowed_size_fields": {"waist": [28, 29, 30]}}
    })
    assert "waist 38 or 38x26 must fail" in judge.messages[0][0]["content"]


def test_title_gate_does_not_coerce_string_boolean():
    judge = FakeJudge({"results": [{"external_id": "x", "pass": "false", "reason": "No", "category": "clothing"}]})
    result = title_gate(judge, [{"external_id": "x", "search_id": "s", "title": "T", "description": "", "size_fields": {}}], {"s": {}})
    assert result == {}


def test_title_gate_ignores_unknown_result_ids_after_retries():
    judge = FakeJudge({"results": [{"external_id": "unknown", "pass": True, "reason": "No", "category": "art"}]})
    result = title_gate(judge, [{"external_id": "x", "search_id": "s", "title": "T", "description": "", "size_fields": {}}], {"s": {}})
    assert result == {}


def test_fast_track_uses_invaluable_email_subject():
    config = {"pre_llm_policy": {"invaluable": {"fast_track_subject_contains": ["Josef Albers"]}}}
    assert is_fast_tracked({"source": "invaluable", "raw_data": {"email_subject": "New pieces from Josef Albers"}}, config)
    assert not is_fast_tracked({"source": "invaluable", "raw_data": {"email_subject": "Mid century modern clocks"}}, config)


def test_ai_hashes_ignore_volatile_source_metadata():
    listing = {
        "title": "A listing",
        "description": "Description",
        "size_fields": {"width": 20},
        "image_urls": ["https://example.test/item.jpg"],
        "raw_data": {"api_updated_at": "first"},
    }
    changed = {**listing, "raw_data": {"api_updated_at": "second", "view_count": 4}}
    search = {"query": "listing", "category": "art"}
    assert _title_hash(listing, search, "instructions", "model") == _title_hash(changed, search, "instructions", "model")
    assert _taste_hash(listing, "art", {"weights": [1]}) == _taste_hash(changed, "art", {"weights": [1]})


def test_taste_judgment_normalizes_invalid_verdict():
    judge = FakeJudge({"verdict": "maybe", "reason": "Unclear."})
    result = taste_judgment(judge, {"title": "item", "image_urls": ["https://example.test/listing.jpg"]}, [{"label": "like", "description": "", "url": "https://example.test/ref.jpg"}])
    assert result == {"verdict": "uncertain", "reason": "Unclear."}
    content = judge.messages[0][0]["content"]
    assert [item["image_url"]["url"] for item in content if item["type"] == "image_url"] == [
        "https://example.test/listing.jpg", "https://example.test/ref.jpg"
    ]


def test_openai_client_retries_empty_json(monkeypatch):
    class Response:
        is_error = False

        def __init__(self, content):
            self.content = content

        def json(self):
            return {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, "choices": [{"message": {"content": self.content}, "finish_reason": "stop"}]}

    responses = iter([Response(""), Response('{"ok": true}')])
    monkeypatch.setattr("listing_agent.ai.httpx.post", lambda *args, **kwargs: next(responses))
    judge = OpenAIJudge(api_key="test")
    assert judge.complete([{"role": "user", "content": "JSON"}]) == {"ok": True}
    assert judge.last_usage == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6, "cache_read_tokens": 0}


def test_openai_client_retries_length_truncation_with_larger_budget(monkeypatch):
    class Response:
        is_error = False

        def __init__(self, content, finish_reason):
            self.content = content
            self.finish_reason = finish_reason

        def json(self):
            return {"id": "resp-test", "usage": {}, "choices": [{"message": {"content": self.content}, "finish_reason": self.finish_reason}]}

    responses = iter([Response("", "length"), Response('{"ok": true}', "stop")])
    budgets = []

    def post(*args, **kwargs):
        budgets.append(kwargs["json"]["max_completion_tokens"])
        return next(responses)

    monkeypatch.setattr("listing_agent.ai.httpx.post", post)
    judge = OpenAIJudge(api_key="test", max_completion_tokens=80)
    assert judge.complete([{"role": "user", "content": "JSON"}]) == {"ok": True}
    assert budgets == [80, 160]
