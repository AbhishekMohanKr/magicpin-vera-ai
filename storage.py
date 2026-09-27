from __future__ import annotations

import copy
import re
import threading
from difflib import SequenceMatcher
from typing import Any, Dict, Optional, Tuple


class VeraStorage:
    """Small in-memory state store for the challenge process.

    The judge keeps one bot process alive for the test window, so in-memory
    storage is sufficient. Context payloads are intentionally kept as raw
    dictionaries: the challenge sends category-specific fields and may inject
    new fields after submission.
    """

    VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

    def __init__(self) -> None:
        self.contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.sent_suppressions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self.sent_suppressions.clear()
            self.conversations.clear()

    def store_context(
        self,
        scope: str,
        context_id: str,
        version: int,
        payload: Dict[str, Any],
    ) -> Tuple[bool, Optional[int]]:
        if scope not in self.VALID_SCOPES:
            raise ValueError(f"invalid_scope: {scope}")
        if not isinstance(context_id, str) or not context_id:
            raise ValueError("context_id must be a non-empty string")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("version must be a positive integer")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")

        key = (scope, context_id)
        with self._lock:
            current = self.contexts.get(key)
            if current:
                if version < current["version"]:
                    return False, current["version"]
                if version == current["version"]:
                    # Idempotent replay: acknowledge the same version without
                    # replacing the stored payload or returning a conflict.
                    return True, current["version"]

            # Deep-copy prevents accidental mutation by request-local objects.
            self.contexts[key] = {
                "version": version,
                "payload": copy.deepcopy(payload),
            }
            return True, None

    def get_context(self, scope: str, context_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self.contexts.get((scope, context_id))
            return copy.deepcopy(data["payload"]) if data else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        with self._lock:
            data = self.contexts.get((scope, context_id))
            return data["version"] if data else None

    def get_all_by_scope(self, scope: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                cid: copy.deepcopy(data["payload"])
                for (stored_scope, cid), data in self.contexts.items()
                if stored_scope == scope
            }

    def register_conversation(self, conversation_id: str, metadata: Dict[str, Any]) -> None:
        with self._lock:
            current = self.conversations.setdefault(
                conversation_id,
                {
                    "metadata": {},
                    "turns": [],
                    "sent_bodies": [],
                    "status": "active",
                },
            )
            current["metadata"].update(copy.deepcopy(metadata))

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self.conversations.get(conversation_id)
            return copy.deepcopy(data) if data else None

    def add_to_conversation(self, conversation_id: str, turn: Dict[str, Any]) -> None:
        with self._lock:
            conversation = self.conversations.setdefault(
                conversation_id,
                {
                    "metadata": {},
                    "turns": [],
                    "sent_bodies": [],
                    "status": "active",
                },
            )
            conversation["turns"].append(copy.deepcopy(turn))
            if turn.get("from") in {"vera", "merchant_on_behalf"} and turn.get("body"):
                conversation["sent_bodies"].append(turn["body"])

    def set_conversation_status(self, conversation_id: str, status: str) -> None:
        with self._lock:
            if conversation_id in self.conversations:
                self.conversations[conversation_id]["status"] = status

    def update_conversation_metadata(self, conversation_id: str, updates: Dict[str, Any]) -> None:
        with self._lock:
            if conversation_id in self.conversations:
                self.conversations[conversation_id]["metadata"].update(copy.deepcopy(updates))

    @staticmethod
    def _body_key(body: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(body).lower()).strip()

    def has_sent_body_for_merchant(self, merchant_id: str, body: str) -> bool:
        """Return true for exact or near-verbatim merchant repeats.

        Suppression keys prevent the same trigger from firing twice. This second
        guard covers a new trigger that would otherwise send the same generic
        fallback again, which is the anti-repetition penalty called out by the
        public Vera implementations.
        """
        candidate = self._body_key(body)
        if len(candidate) < 24:
            return False
        with self._lock:
            for conversation in self.conversations.values():
                metadata = conversation.get("metadata", {})
                if str(metadata.get("merchant_id")) != str(merchant_id):
                    continue
                for previous in conversation.get("sent_bodies", []):
                    prior = self._body_key(previous)
                    if prior == candidate:
                        return True
                    if len(prior) >= 48 and SequenceMatcher(None, prior, candidate).ratio() >= 0.94:
                        return True
        return False

    def has_sent_body(self, conversation_id: str, body: str) -> bool:
        candidate = self._body_key(body)
        with self._lock:
            conversation = self.conversations.get(conversation_id, {})
            for previous in conversation.get("sent_bodies", []):
                prior = self._body_key(previous)
                if prior == candidate:
                    return True
        return False

    def count_inbound_for_merchant(self, merchant_id: str, body: str) -> int:
        """Count an identical inbound message across a merchant's conversations."""
        candidate = self._body_key(body)
        if not candidate:
            return 0
        with self._lock:
            count = 0
            for conversation in self.conversations.values():
                if str(conversation.get("metadata", {}).get("merchant_id")) != str(merchant_id):
                    continue
                for turn in conversation.get("turns", []):
                    if turn.get("from") not in {"vera", "merchant_on_behalf"}:
                        if self._body_key(turn.get("body", "")) == candidate:
                            count += 1
            return count

    def mark_suppression(self, suppression_key: str, metadata: Dict[str, Any]) -> None:
        if not suppression_key:
            return
        with self._lock:
            self.sent_suppressions[suppression_key] = copy.deepcopy(metadata)

    def is_suppressed(self, suppression_key: str) -> bool:
        if not suppression_key:
            return False
        with self._lock:
            return suppression_key in self.sent_suppressions


storage = VeraStorage()
