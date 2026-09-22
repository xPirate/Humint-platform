"""Ollama client for the background worker.

Deliberately defensive: every call degrades to None on any failure
(timeout, connection error, malformed JSON) rather than raising, so a
flaky or unreachable Ollama never crashes the poll loop. An attachment
whose extraction call fails this way simply gets no suggestions —
extracted_text is still saved, so nothing is lost, and there's no
automatic retry (see main.py's docstring on the tradeoffs there).
"""

import json
import logging

import requests

import ollama_usage

logger = logging.getLogger("worker.ollama")

ENTITY_TYPES = ("person", "organization", "location", "event", "source", "communication",
                "vehicle")

EXTRACTION_SYSTEM_PROMPT = """You are an intelligence analyst's assistant reviewing a \
document for a HUMINT case-management system. You will be given text extracted from an \
uploaded document or image (via OCR, so expect occasional garbled words). Identify \
entities and relationships worth adding to the case file. Respond with ONLY a JSON \
object (no markdown, no commentary, no explanation) with exactly these two keys:

"entities": an array of objects, each with:
  - "entity_type": one of "person", "organization", "location", "event", "source", \
"communication", "vehicle"
  - "name": the entity's name as it appears in the text. A NAME, never a \
contact detail: "m.trombley@example.com", "555-0142", "@handle" and \
"example.com" are things a person or organization HAS, not what one is \
called. Text like "Miles Trombley <m.trombley@example.com>" is ONE person \
named "Miles Trombley" whose email goes in details, not two entities and \
never an entity named after the address. If an address appears with no name \
attached to it anywhere in the document, leave it out entirely rather than \
inventing a person for it
  - "confidence": a number from 0 to 1 for how confident you are this is a real, \
distinct entity worth tracking (not a stray OCR artifact or a passing mention with no substance)
  - "details": an object of any additional fields you can confidently infer from the \
text. Only include fields that apply to that entity_type: person -> aliases (array), \
occupation, physical_description; organization -> org_type, website; \
location -> address (the FULL address as one string); vehicle -> make, model, color, \
license_plate, plate_region (the issuing state/country if given), style (one of: Sedan, \
Coupe, Hatchback, SUV, Pickup truck, Van, Panel van, Box truck, Semi-tractor, Bus, \
Motorcycle, ATV/UTV, Trailer, Boat, Aircraft, Other — omit if the text does not clearly \
map to one), notes; source -> source_type, reliability_rating \
(a single letter A-F if you can judge it, otherwise omit); communication -> medium (one \
of: Cellphone, Landline, Text/SMS, Satellite Phone, Email, HF Radio, VHF Radio, UHF Radio, \
FM Radio, GMRS, FRS, CB Radio, In-Person, Mail/Courier, Other — omit if the text doesn't \
clearly map to one of these), medium_detail (the phone number, email address, frequency, \
or channel mentioned for that medium, if any), participants_note. Omit any field you \
can't confidently infer rather than guessing. Never propose "alignment" on any record \
(Friendly/Neutral/Unknown/Hostile), a location's "environment", a person's "life_status" \
or "disposition" — those are an analyst's own operational judgment calls about the case, \
not facts to extract from text. A document calling someone "the late Mr X" is not the \
same as an analyst assessing that person as deceased; a document saying someone "fled" is \
a long way from assessing them as evading rather than missing; and a document describing \
a place as dangerous is not an assessment that it is non-permissive to work in.

"relationships": an array of objects, each with:
  - "from_name": the name of one entity, copied EXACTLY from your "entities" list above
  - "to_name": the name of a different entity, also copied exactly from that list
  - "relationship_type": lowercase snake_case describing the connection, e.g. \
"employed_by", "present_at", "communicated_with", "family_of", "located_at"
  - "confidence": a number from 0 to 1

ONE PLACE IS ONE ENTITY. "1140 Rennard Way, Kettleburn 74101" is a single \
location whose name is the place and whose address is the whole string — not a \
street, a town and a postcode as three separate locations. Never split an \
address into its parts, and never propose a bare postcode, a lone street name \
or a country as a location of its own. If a town is only mentioned as part of \
somebody's address, it is not a separate place worth tracking; if the same town \
is discussed in its own right, then it is.

EVERY name you use in "relationships" MUST also appear as an entry in "entities". \
If the text says someone works for a company, or lives in a town, then that company \
and that town are entities too — list them, with the right entity_type, before you \
refer to them. A relationship naming something you did not list is useless to the \
analyst: there is no record for it, so there is nothing they can accept.

Only extract what is clearly stated or strongly implied in the text — never invent \
people, organizations, or events that aren't actually mentioned. If nothing worth \
extracting is present, return {"entities": [], "relationships": []}."""


class OllamaClient:
    def __init__(self, base_url: str, model: str, embed_model: str = "", timeout: int = 180,
                 enabled: bool = True, on_call=None, extract_model: str = ""):
        self.base_url = base_url.rstrip("/")
        # Three models, three jobs. `model` answers the assistant;
        # `extract_model` reads uploaded documents; `embed_model` produces the
        # vectors the correlation pass compares. The first two are separate
        # because extraction runs unattended on every upload and wants a small
        # model that reliably emits JSON, while the assistant runs when
        # somebody is waiting and wants a better conversationalist.
        #
        # The fallback to `model` is resolved in ollama_config, not here, so
        # there is exactly one place that knows about it — but this defends
        # anyway, because this class is also constructed directly in tests.
        self.model = model
        self.extract_model = (extract_model or "").strip() or model
        self.embed_model = embed_model.strip()
        self.timeout = timeout
        self.enabled = enabled
        # Optional telemetry sink, called once per Ollama request with what
        # that request cost and whether it worked. A callback rather than a
        # direct write because this file is byte-identical in api/ and
        # worker/, and the two sides attribute a call differently — the api
        # knows which user is waiting on it, the worker's calls are nobody's
        # request in particular. See api/ollama_usage.py.
        #
        # Never allowed to affect the call it is describing: a recorder that
        # raises is swallowed here as well as inside the recorder itself,
        # because a telemetry bug must not turn a working extraction into a
        # failed one.
        self.on_call = on_call

    def _record(self, operation, outcome="success", response=None, error=None):
        if not self.on_call:
            return
        try:
            usage = ollama_usage.from_response(response) if response is not None else {}
            # Attributed to the model that does THIS operation, so the activity
            # panel can tell a slow extraction model from a slow assistant one
            # — which is most of the point of being able to set them apart.
            configured = self.extract_model if operation == "extract" else self.model
            self.on_call(operation=operation, outcome=outcome,
                         model=usage.get("model") or configured, usage=usage,
                         error=error)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Usage recorder raised, ignoring: %s", exc)

    def _record_embed(self, outcome, response=None, error=None):
        """As _record, but attributed to the embedding model.

        /api/embeddings (the older endpoint this uses) reports no token counts
        and no timings at all, so most of these rows carry nothing but an
        outcome. That is still the answer to "is the correlation queue silently
        failing", which is the question embeddings are most likely to raise.
        """
        if not self.on_call:
            return
        try:
            usage = ollama_usage.from_response(response) if response is not None else {}
            self.on_call(operation="embed", outcome=outcome,
                         model=usage.get("model") or self.embed_model,
                         usage=usage, error=error)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Usage recorder raised, ignoring: %s", exc)

    @staticmethod
    def _outcome_for(exc) -> str:
        """A timeout is worth telling apart from everything else: it usually
        means the model is too big for this hardware rather than that anything
        is broken, and those have different fixes."""
        if isinstance(exc, requests.Timeout):
            return "timeout"
        return "failure"

    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self) -> list | None:
        """Installed models on this Ollama host, or None if it can't be
        reached. Powers the model pickers on the Admin page (see
        api/settings.py) — the alternative is asking an admin to type a model
        string that has to match exactly, where a typo produces no error
        anywhere, just extraction that silently never returns anything.

        Deliberately NOT gated on self.enabled: an admin configuring a new
        deployment needs to see what's available *before* switching Ollama on,
        and this is a read-only call to a host they configured themselves."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=min(self.timeout, 15))
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Could not list models at %s: %s", self.base_url, exc)
            return None

        models = []
        for entry in data.get("models") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if not name:
                continue
            details = entry.get("details") or {}
            models.append({
                "name": name,
                "size_bytes": entry.get("size"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "modified_at": entry.get("modified_at"),
            })
        models.sort(key=lambda m: m["name"].lower())
        return models

    def pull_model(self, model: str, on_progress=None) -> tuple:
        """Downloads a model onto this Ollama host, streaming progress.
        Returns (ok, message). `on_progress` is called with each raw event
        from Ollama, which reports {status, digest, total, completed} as it
        goes — see api/settings.py, which turns those into a percentage for
        the Admin page.

        The read timeout is generous but finite: a stalled download that
        never sends another byte should eventually fail rather than pin a
        background thread forever waiting on a host that has gone away."""
        if not model or not model.strip():
            return False, "No model name given"
        try:
            with requests.post(
                f"{self.base_url}/api/pull",
                json={"model": model.strip(), "stream": True},
                stream=True,
                timeout=(10, 300),
            ) as resp:
                resp.raise_for_status()
                last_status = ""
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if event.get("error"):
                        return False, str(event["error"])[:500]
                    last_status = event.get("status") or last_status
                    if on_progress:
                        on_progress(event)
                return True, last_status or "done"
        except requests.RequestException as exc:
            logger.warning("Model pull failed (%s): %s", model, exc)
            return False, str(exc)[:500]

    def extract_entities(self, text: str) -> dict | None:
        if not self.enabled or not text.strip():
            return None
        payload = {
            "model": self.extract_model,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                # Ollama's context window is finite and this is a triage
                # pass, not a full-document analysis — cap the input rather
                # than risk silently truncated/degraded output on a huge
                # document. 8000 chars is comfortably inside an 8k-context
                # small model's budget alongside the system prompt.
                {"role": "user", "content": text[:8000]},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1},
        }
        body = None
        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            body = resp.json()
            content = body["message"]["content"]
            data = json.loads(content)
        except (requests.RequestException, KeyError, json.JSONDecodeError) as exc:
            logger.warning("Ollama extraction failed: %s", exc)
            # Recorded as a failure rather than not recorded at all. A model
            # that answered with unparseable JSON still burned the time and the
            # tokens, and "it ran and produced nothing usable" is exactly the
            # state that otherwise looks identical to an idle queue. `body` is
            # whatever we got before things went wrong — the timings when the
            # response parsed but its content did not, None when it did not.
            self._record("extract", self._outcome_for(exc), response=body, error=exc)
            return None
        self._record("extract", "success", response=body)
        return self._sanitize_extraction(data)

    def _sanitize_extraction(self, data: dict) -> dict:
        """format=json guarantees syntactically valid JSON, not that the
        model followed the schema — never trust field names/types blindly."""
        entities = []
        for e in data.get("entities") or []:
            if not isinstance(e, dict):
                continue
            entity_type = e.get("entity_type")
            name = str(e.get("name", "") or "").strip()
            if entity_type not in ENTITY_TYPES or not name:
                continue
            entities.append({
                "entity_type": entity_type,
                "name": name[:256],
                "confidence": _as_confidence(e.get("confidence")),
                "details": e.get("details") if isinstance(e.get("details"), dict) else {},
            })

        relationships = []
        for r in data.get("relationships") or []:
            if not isinstance(r, dict):
                continue
            from_name = str(r.get("from_name", "") or "").strip()
            to_name = str(r.get("to_name", "") or "").strip()
            rel_type = str(r.get("relationship_type", "") or "").strip().lower().replace(" ", "_")
            if not from_name or not to_name or not rel_type or from_name == to_name:
                continue
            relationships.append({
                "from_name": from_name[:256],
                "to_name": to_name[:256],
                "relationship_type": rel_type[:64],
                "confidence": _as_confidence(r.get("confidence")),
            })

        return {"entities": entities, "relationships": relationships}

    def chat(self, system_prompt: str, history: list[dict], question: str) -> str | None:
        """Free-form chat completion — used by the case-data-aware AI
        assistant (see api/assistant.py; this method is unused by the worker
        itself, but ollama_client.py is kept byte-identical between api/ and
        worker/ the same way geo.py is, so it lives here too rather than
        forking the client into two slightly different versions).

        Unlike extract_entities, this does NOT pass format="json" — the
        assistant's replies are meant to be read by a person, not parsed by
        this app, and forcing JSON would just make the model produce worse,
        stilted prose for no benefit here. `history` is prior turns as
        [{"role": "user"|"assistant", "content": ...}], oldest first;
        `system_prompt` typically carries this turn's freshly retrieved
        case-data context ahead of the conversation history, since what's
        relevant can change from one question to the next."""
        if not self.enabled:
            return None
        messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": question}]
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.3},
        }
        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            body = resp.json()
            content = body["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.warning("Ollama chat failed: %s", exc)
            self._record("chat", self._outcome_for(exc), error=exc)
            return None
        self._record("chat", "success", response=body)
        return content.strip() if isinstance(content, str) else None

    def embed(self, text: str) -> list[float] | None:
        """Used by the correlation pass — see main.py."""
        if not self.enabled or not self.embed_model:
            return None
        # Recorded with the embed model rather than the chat model — they are
        # usually different, often very different in size, and attributing a
        # 137M embedder's time to an 8B chat model would make the chat model
        # look faster than it is.
        data = None
        try:
            r = requests.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.embed_model, "prompt": text[:2000]},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            vec = data.get("embedding")
            if isinstance(vec, list) and vec and all(isinstance(x, (int, float)) for x in vec):
                self._record_embed("success", response=data)
                return vec
            logger.warning("Ollama embeddings response missing a usable 'embedding' array")
            self._record_embed("failure", response=data,
                               error="response had no usable 'embedding' array")
            return None
        except requests.RequestException as exc:
            logger.warning("Embedding call failed (%s): %s", self.embed_model, exc)
            self._record_embed(self._outcome_for(exc), error=exc)
            return None
        except (ValueError, KeyError) as exc:
            logger.warning("Malformed embeddings response: %s", exc)
            self._record_embed("failure", error=exc)
            return None


def _as_confidence(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, v))
