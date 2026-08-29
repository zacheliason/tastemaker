from listing_agent.zenrows import fetch_html


class Response:
    text = "<html>lot</html>"

    def raise_for_status(self):
        return None


def test_zenrows_fetch_strips_query_and_enables_javascript(monkeypatch):
    calls = []
    monkeypatch.setenv("ZENROWS_API_KEY", "test-key")

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("listing_agent.zenrows.httpx.get", get)
    assert fetch_html("https://example.test/lot?tracking=1") == "<html>lot</html>"
    assert calls[0][1]["params"]["url"] == "https://example.test/lot"
    assert calls[0][1]["params"]["js_render"] == "true"
