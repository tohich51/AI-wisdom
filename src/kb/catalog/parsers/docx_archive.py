"""Archive and size limits for a ``.docx``, checked before anything parses it.

A ``.docx`` is a ZIP container. Two of the classic ways to hurt a document
pipeline are therefore reachable from the very first byte we accept: a container
that expands to far more than it claims (a decompression bomb), and a container
full of parts (a zip bomb of parts rather than of bytes). Neither is a
theoretical concern for a service that accepts uploads from contributors, and
neither may be answered by "parse it and see" — by then the expansion has
already happened.

Every limit here is a **refusal**, not a truncation. A document that trips a
limit produces :class:`DocxArchiveError` and no fragments at all. A partially
parsed book that looks like a complete one is the failure this card cares about
most, so it is not an outcome this module can produce.

The numbers are this card's proposal, not a number quoted from PRODUCT-SPEC,
which does not state any. They are recorded as a contract change proposal in
``docs/handoff/results/C12A.json`` for the single owner of the configuration.
Defaults are deliberately generous for a brand guideline and deliberately
finite: 64 MiB of container, 256 MiB expanded, a 200:1 overall expansion ratio,
4096 parts.
"""

from __future__ import annotations

import dataclasses
import io
import pathlib
import zipfile
from typing import IO

__all__ = [
    "DEFAULT_LIMITS",
    "ArchiveLimits",
    "ArchiveReport",
    "DocxArchiveError",
    "inspect_docx",
    "read_docx_bytes",
]

# The two parts without which this is not a WordprocessingML document at all.
_REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")

# Parts that are legitimately stored without compression. Counting their
# deflate ratio would flag ordinary XML as a bomb.
_STORED_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".emf", ".wmf", ".bin")


class DocxArchiveError(Exception):
    """The container is not a document we are willing to open.

    Raised before parsing, and raised in a way a caller cannot mistake for an
    empty document: there is no partial result to return.
    """


@dataclasses.dataclass(frozen=True)
class ArchiveLimits:
    """The four bounds, and the two required parts. All of them are refusals."""

    max_file_bytes: int = 64 * 1024 * 1024
    max_uncompressed_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_parts: int = 4096
    max_ratio_evaluated_from_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_uncompressed_bytes", "max_parts"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


DEFAULT_LIMITS = ArchiveLimits()


@dataclasses.dataclass(frozen=True)
class ArchiveReport:
    """What the container actually turned out to be.

    ``max_ratio`` is a plain rounded float so that it survives JSON encoding
    without a NaN or an Infinity, which the result contract forbids.
    """

    file_bytes: int
    parts: int
    uncompressed_bytes: int
    compressed_bytes: int
    max_ratio: float
    worst_part: str | None

    @property
    def overall_ratio(self) -> float:
        return round(self.uncompressed_bytes / max(self.compressed_bytes, 1), 2)


def _reject(name: str) -> bool:
    """A member name that must never be read from an uploaded container."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    if "\x00" in name or "\\" in name:
        return True
    if len(name) > 1 and name[1] == ":" and name[0].isalpha():  # C:\ or C:/
        return True
    return any(part == ".." for part in name.split("/"))


def _check_names(infos: list[zipfile.ZipInfo]) -> None:
    for info in infos:
        if _reject(info.filename):
            raise DocxArchiveError(
                f"archive member name is not a plain relative path: {info.filename!r}"
            )


def _check_encrypted(infos: list[zipfile.ZipInfo]) -> None:
    for info in infos:
        if info.flag_bits & 0x1:
            raise DocxArchiveError(f"archive member is encrypted: {info.filename!r}")


def _worst_ratio(infos: list[zipfile.ZipInfo], limits: ArchiveLimits) -> tuple[float, str | None]:
    """The single most compressed *large* part, and its name.

    Small parts are excluded: a 200-byte part deflated to 40 bytes has a 5:1
    ratio and means nothing. A part of at least ``max_ratio_evaluated_from_bytes``
    that expands a hundredfold is a different thing entirely.
    """
    worst = 0.0
    worst_name: str | None = None
    for info in infos:
        if info.compress_size <= 0 or info.file_size < limits.max_ratio_evaluated_from_bytes:
            continue
        if info.filename.lower().endswith(_STORED_SUFFIXES):
            continue
        ratio = info.file_size / info.compress_size
        if ratio > worst:
            worst = ratio
            worst_name = info.filename
    return round(worst, 2), worst_name


def _report(data_size: int, infos: list[zipfile.ZipInfo], limits: ArchiveLimits) -> ArchiveReport:
    total = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos)
    ratio, worst = _worst_ratio(infos, limits)
    overall = round(total / max(compressed, 1), 2)

    if len(infos) > limits.max_parts:
        raise DocxArchiveError(f"archive has {len(infos)} parts, the limit is {limits.max_parts}")
    if total > limits.max_uncompressed_bytes:
        raise DocxArchiveError(
            f"archive expands to {total} bytes, the limit is {limits.max_uncompressed_bytes}"
        )
    if ratio > limits.max_compression_ratio:
        raise DocxArchiveError(
            f"archive member {worst!r} expands {ratio}:1, the limit is "
            f"{limits.max_compression_ratio}:1"
        )
    if overall > limits.max_compression_ratio:
        raise DocxArchiveError(
            f"archive expands {overall}:1 overall, the limit is {limits.max_compression_ratio}:1"
        )
    names = {i.filename for i in infos}
    missing = [p for p in _REQUIRED_PARTS if p not in names]
    if missing:
        raise DocxArchiveError(f"not a WordprocessingML document, missing {', '.join(missing)}")
    return ArchiveReport(
        file_bytes=data_size,
        parts=len(infos),
        uncompressed_bytes=total,
        compressed_bytes=compressed,
        max_ratio=ratio,
        worst_part=worst,
    )


def _inspect_stream(stream: IO[bytes], data_size: int, limits: ArchiveLimits) -> ArchiveReport:
    try:
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise DocxArchiveError(f"not a ZIP container: {exc}") from exc
    if data_size > limits.max_file_bytes:
        raise DocxArchiveError(f"file is {data_size} bytes, the limit is {limits.max_file_bytes}")
    _check_names(infos)
    _check_encrypted(infos)
    return _report(data_size, infos, limits)


def inspect_docx(path: str | pathlib.Path, limits: ArchiveLimits = DEFAULT_LIMITS) -> ArchiveReport:
    """Check the container at ``path``. Reads the central directory, not the parts.

    Nothing is extracted and nothing is decompressed here; the sizes come from
    the directory entries, so a bomb is refused before a single part is inflated.
    """
    p = pathlib.Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise DocxArchiveError(f"cannot stat the document: {exc}") from exc
    if size > limits.max_file_bytes:
        raise DocxArchiveError(f"file is {size} bytes, the limit is {limits.max_file_bytes}")
    try:
        with p.open("rb") as handle:
            return _inspect_stream(handle, size, limits)
    except zipfile.BadZipFile as exc:  # pragma: no cover - handled inside _inspect_stream
        raise DocxArchiveError(f"not a ZIP container: {exc}") from exc


def read_docx_bytes(
    data: bytes, limits: ArchiveLimits = DEFAULT_LIMITS
) -> tuple[bytes, ArchiveReport]:
    """Check an in-memory container and hand the same bytes back.

    Returns the bytes unchanged, together with the report, so a caller that
    already holds the upload does not read the file twice.
    """
    report = _inspect_stream(io.BytesIO(data), len(data), limits)
    return data, report
