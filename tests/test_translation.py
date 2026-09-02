import httpx

from listing_agent.translation import _translate_batch, translate_rows


class Result:
    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)


class Connection:
    def execute(self, query, params):
        return Result()


def test_short_german_description_is_diagnosed_without_logging_text(monkeypatch, caplog):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"translations": [{"detectedSourceLanguage": "de", "translatedText": "Untitled. Color screen print ..."}]}}

    monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "test-key")
    monkeypatch.setattr("listing_agent.translation.httpx.post", lambda *args, **kwargs: Response())
    description = "Ohne Titel. Farbserigrafie ..."

    with caplog.at_level("INFO", logger="listing_agent.translation"):
        assert translate_rows(Connection(), [{"section": "Passed", "description": description}]) == 1

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "translation language detection" in messages
    assert "candidates=de:" in messages
    assert "translation cache status: candidates=1 cached=0 pending=1" in messages
    assert "translation API response: status=200 descriptions=1" in messages
    assert description not in messages


def test_translate_batch_encodes_repeated_form_fields(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"translations": [{"detectedSourceLanguage": "de", "translatedText": "A chair"}]}}

    captured = {}

    def post(*args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "test-key")
    monkeypatch.setattr("listing_agent.translation.httpx.post", post)

    assert _translate_batch(["Ein Stuhl", "Größe 8"]) == [("DE", "A chair")]
    assert captured["content"] == b"q=Ein+Stuhl&q=Gr%C3%B6%C3%9Fe+8&target=en&format=html"
    assert "data" not in captured


def test_translation_http_failure_logs_reason_without_request_url(monkeypatch, caplog):
    def fail(*args, **kwargs):
        request = httpx.Request("POST", "https://example.test/translate?key=secret")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("upstream failure", request=request, response=response)

    monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "test-key")
    monkeypatch.setattr("listing_agent.translation.httpx.post", fail)

    with caplog.at_level("WARNING", logger="listing_agent.translation"):
        assert translate_rows(Connection(), [{"section": "Passed", "description": "Ein kurzer Text"}]) == 0

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=http_status status=503" in messages
    assert "secret" not in messages
    assert "Ein kurzer Text" not in messages
