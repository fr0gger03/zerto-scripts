"""Validate and clean small source VMDK descriptors before copying seed disks."""

import re
from urllib.parse import quote, urlencode

import requests


MAX_DESCRIPTOR_BYTES = 1024 * 1024
HTTP_TIMEOUT = 30
TARGET_ASSIGNMENT = re.compile(r"^\s*ddb\.(?:iofilters|sidecars)\s*=", re.IGNORECASE)


def build_datastore_url(host: str, datacenter: str, datastore: str, source_path: str) -> str:
    """Build a URL from the actual source path, never a display/destination path."""
    match = re.fullmatch(r"\[([^\[\]\r\n]+)\] (.+)", source_path)
    if not match or match[1] != datastore:
        raise ValueError("Source path must identify the selected datastore")
    relative = match[2]
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(
        character in relative for character in ("\\", "\r", "\n", "\0")
    ):
        raise ValueError("Invalid source descriptor path")
    filename = parts[-1].lower()
    if not filename.endswith(".vmdk") or re.search(
        r"(?:-flat|-delta|-sesparse|-ctk|-s\d+|-f\d+)\.vmdk$", filename
    ):
        raise ValueError("Source must be a VMDK descriptor, not a data extent")
    query = urlencode({"dcPath": datacenter, "dsName": datastore})
    return f"https://{host}/folder/{quote(relative, safe='/')}?{query}"


def validate_descriptor(text: str) -> None:
    """Reject binary data and files that do not resemble standalone descriptors."""
    if len(text.encode("utf-8")) > MAX_DESCRIPTOR_BYTES or "\0" in text:
        raise ValueError("Source is not a small text descriptor")
    if not text.lstrip("\ufeff \t\r\n").startswith("# Disk DescriptorFile"):
        raise ValueError("Missing VMDK descriptor header")
    for key in ("version", "CID", "parentCID", "createType"):
        if not re.search(rf"^\s*{key}\s*=\s*\S+", text, re.MULTILINE):
            raise ValueError(f"Missing descriptor field: {key}")
    if not re.search(r"^\s*(?:RW|RDONLY|NOACCESS)\s+\d+\s+\S+", text, re.MULTILINE):
        raise ValueError("Missing descriptor extent definition")


def scrub_descriptor(text: str) -> tuple[str, list[str]]:
    """Comment exact filter assignments, preserving all other text and newlines."""
    validate_descriptor(text)
    changed = []
    result = []
    for line in text.splitlines(keepends=True):
        if TARGET_ASSIGNMENT.match(line):
            changed.append(line.rstrip("\r\n"))
            result.append("#" + line)
        else:
            result.append(line)
    return "".join(result), changed


def fetch_descriptor(session: requests.Session, url: str, verify: bool) -> str:
    """Read a bounded UTF-8 descriptor without following authentication redirects."""
    response = session.get(
        url, verify=verify, timeout=HTTP_TIMEOUT, stream=True, allow_redirects=False
    )
    try:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError(f"Unexpected descriptor GET status: {response.status_code}")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            data.extend(chunk)
            if len(data) > MAX_DESCRIPTOR_BYTES:
                raise ValueError("Source exceeds the descriptor size limit")
        text = data.decode("utf-8")
        validate_descriptor(text)
        return text
    finally:
        response.close()


def clean_source_descriptor(
    session: requests.Session, url: str, verify: bool, *, dry_run: bool = False
) -> tuple[str, list[str]]:
    """Clean and read back a descriptor; any failure prevents the caller copying."""
    original = fetch_descriptor(session, url, verify)
    cleaned, changed = scrub_descriptor(original)
    if not changed:
        return "clean", []
    if dry_run:
        return "would_clean", changed
    response = session.put(
        url, data=cleaned.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
        verify=verify, timeout=HTTP_TIMEOUT, allow_redirects=False,
    )
    try:
        response.raise_for_status()
        if response.status_code not in {200, 201, 204}:
            raise ValueError(f"Unexpected descriptor PUT status: {response.status_code}")
    finally:
        response.close()
    if fetch_descriptor(session, url, verify) != cleaned:
        raise ValueError("Source descriptor read-back did not match the cleanup")
    return "cleaned", changed
