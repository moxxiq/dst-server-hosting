"""Minimal libpod REST client over the unix socket (no podman CLI in the image)."""
from dataclasses import asdict, dataclass

import httpx


class PodmanError(RuntimeError):
    pass


@dataclass
class ContainerInfo:
    exists: bool
    status: str
    started_at: str | None
    exit_code: int | None

    def as_dict(self) -> dict:
        return asdict(self)


def demux(data: bytes) -> str:
    """Log stream frames: 1 byte stream type, 3 zero bytes, 4-byte big-endian length, payload."""
    chunks = []
    i = 0
    while i + 8 <= len(data):
        n = int.from_bytes(data[i + 4:i + 8], "big")
        chunks.append(data[i + 8:i + 8 + n])
        i += 8 + n
    return b"".join(chunks).decode("utf-8", "replace")


class Podman:
    def __init__(self, sock: str, container: str):
        self.container = container
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=sock),
            # The libpod REST router 404s real endpoints (containers/*) on an
            # unversioned path — only /libpod/_ping tolerates it. Any syntactically
            # valid version is accepted (unlike the Docker-compat routes, libpod
            # ignores the actual number here), so pin a low, stable one.
            base_url="http://podman/v1.0.0",
            timeout=httpx.Timeout(10.0, read=200.0),
        )

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        """Reach the libpod socket. Anything below the HTTP layer (missing socket,
        permission denied, timeout) becomes a PodmanError like saves_client.py does for dst-saves."""
        try:
            return self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise PodmanError(f"podman socket unreachable: {e}") from e

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code not in (200, 204, 304):
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise PodmanError(f"podman {r.request.method} {r.request.url.path} → {r.status_code}: {msg}")
        return r

    def inspect(self) -> ContainerInfo:
        r = self._request("GET", f"/libpod/containers/{self.container}/json")
        if r.status_code == 404:
            return ContainerInfo(False, "missing", None, None)
        state = self._check(r).json()["State"]
        return ContainerInfo(True, state.get("Status", "unknown"), state.get("StartedAt"), state.get("ExitCode"))

    def start(self) -> None:
        self._check(self._request("POST", f"/libpod/containers/{self.container}/start"))

    def stop(self, timeout: int = 150) -> None:
        self._check(self._request("POST", f"/libpod/containers/{self.container}/stop", params={"timeout": timeout}))

    def restart(self, timeout: int = 150) -> None:
        self._check(self._request("POST", f"/libpod/containers/{self.container}/restart", params={"t": timeout}))

    def logs(self, tail: int = 200) -> str:
        r = self._check(self._request(
            "GET", f"/libpod/containers/{self.container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        ))
        return demux(r.content)
