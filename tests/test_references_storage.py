import json
from pathlib import Path

from listing_agent.references import upload_directory
from listing_agent.storage import SupabaseStorage


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"signedURL": "/storage/v1/object/sign/bucket/references/a.jpg?token=x"}


class Cursor:
    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params):
        self.params = params


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()

    def cursor(self):
        return self.cursor_instance


def test_signed_url_uses_private_storage_endpoint(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("listing_agent.storage.httpx.post", post)
    storage = SupabaseStorage("https://project.supabase.co", "secret")

    result = storage.signed_url("taste references", "references/a.jpg")

    assert result.startswith("https://project.supabase.co/storage/v1/")
    assert calls[0][0].endswith("/object/sign/taste%20references/references/a.jpg")
    assert calls[0][1]["json"] == {"expiresIn": 3600}


def test_upload_directory_uploads_and_reconciles_manifest(tmp_path, monkeypatch):
    image = tmp_path / "clothing" / "a.jpg"
    image.parent.mkdir()
    image.write_bytes(b"jpeg")
    source = tmp_path / "original.heic"
    source.write_bytes(b"source")
    (tmp_path / "manifest.json").write_text(json.dumps({"images": [{
        "source": str(source), "output": str(image), "width": 100, "height": 80
    }]}))
    uploaded = []

    class Storage:
        def upload(self, *args):
            uploaded.append(args)

    conn = Connection()
    assert upload_directory(conn, tmp_path, "taste-references", Storage()) == 1
    assert uploaded[0][0:2] == ("taste-references", "references/clothing/a.jpg")
    assert conn.cursor_instance.params[0] == "references/clothing/a.jpg"
    assert conn.cursor_instance.params[2:4] == ("taste-references", "references/clothing/a.jpg")
