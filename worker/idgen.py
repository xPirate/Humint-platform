"""Human-readable id generation shared by entities and reports.

Kept as an exact duplicate of api/idgen.py rather than a shared package —
the api and worker containers are separate build contexts (see their
Dockerfiles' `COPY . .`), so there's no import path between them without
introducing a shared library layer this project doesn't otherwise need.
Used here by rss_ingest.py, which creates Event entities directly.

An id is a slug derived from the name plus a short random suffix
(e.g. "person-john-smith-a1b2c3") — greppable/debuggable in logs and
URLs, collision-proofed by the suffix without needing a lookup to check
uniqueness before insert.
"""

import re
import uuid


def generate_id(prefix: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}-{slug}-{suffix}" if slug else f"{prefix}-{suffix}"
