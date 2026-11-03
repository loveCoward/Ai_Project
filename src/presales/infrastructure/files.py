import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from presales.domain.contracts import InvalidInput, NotReady

MAX_FILE_BYTES = 20 * 1024 * 1024


class LocalFiles:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._check(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _check(self, path: Path) -> None:
        for part in [path, *path.parents]:
            if not part.exists() and not part.is_symlink():
                continue
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 1024):
                raise InvalidInput()

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}\.bin", key):
            raise InvalidInput()
        path = self.root / key
        self._check(path)
        return path

    def write(self, key: str, data: bytes, expected_digest: str) -> None:
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != expected_digest:
            raise InvalidInput()
        path = self.path(key)
        if path.exists():
            self.read(key, expected_digest, len(data))
            return
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._check(path)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read(self, key: str, expected_digest: str, size: int) -> bytes:
        path = self.path(key)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise NotReady() from None
        if len(data) != size or hashlib.sha256(data).hexdigest() != expected_digest:
            raise NotReady()
        return data
