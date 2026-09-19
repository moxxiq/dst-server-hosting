"""HTTP client for dst-saves."""
import httpx


class SavesError(RuntimeError):
    pass


class SavesClient:
    def __init__(self, base_url: str):
        self.client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0, read=900.0))

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise SavesError(f"dst-saves unreachable: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise SavesError(f"dst-saves {method} {path} → {r.status_code}: {detail}")
        return r.json()

    def status(self) -> dict:
        return self._call("GET", "/status")

    def backups(self) -> dict:
        return self._call("GET", "/backups")

    def backup(self, tag: str = "manual") -> dict:
        return self._call("POST", "/backup", params={"tag": tag})

    def upload(self, name: str) -> dict:
        return self._call("POST", f"/upload/{name}")

    def restore(self, source: str, name: str) -> dict:
        return self._call("POST", "/restore", params={"source": source, "name": name})

    def activate(self, name: str) -> dict:
        return self._call("POST", "/activate", params={"name": name})

    def delete(self, source: str, name: str) -> dict:
        return self._call("DELETE", f"/backups/{source}/{name}")
