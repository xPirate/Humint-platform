"""Human-readable id generation shared by entities and reports.

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
