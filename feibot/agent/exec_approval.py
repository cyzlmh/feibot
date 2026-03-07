"""In-memory manager for exec approval requests."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Literal

ApprovalDecision = Literal["allow-once", "deny"]

_DECISION_ALIASES: dict[str, ApprovalDecision] = {
    "allow": "allow-once",
    "once": "allow-once",
    "allow-once": "allow-once",
    "allowonce": "allow-once",
    "deny": "deny",
    "reject": "deny",
    "block": "deny",
}


@dataclass
class ExecApprovalRequest:
    """A pending exec approval request."""

    id: str
    command: str
    working_dir: str
    channel: str
    chat_id: str
    session_key: str
    requester_id: str
    created_at: datetime
    expires_at: datetime
    risk_level: str = "confirm"


@dataclass
class ExecApprovalResolution:
    """Resolved approval decision."""

    request: ExecApprovalRequest
    decision: ApprovalDecision
    resolved_by: str
    resolved_at: datetime


class ExecApprovalManager:
    """Tracks pending approvals."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_sec: int = 120,
        approvers: list[str] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.timeout_sec = max(10, int(timeout_sec or 120))
        self.approvers = [str(x).strip() for x in (approvers or []) if str(x).strip()]
        self._pending: dict[str, ExecApprovalRequest] = {}

    @staticmethod
    def normalize_decision(raw: str | None) -> ApprovalDecision | None:
        """Normalize textual /approve decisions with common aliases."""
        if not raw:
            return None
        return _DECISION_ALIASES.get(raw.strip().lower())

    @property
    def pending_count(self) -> int:
        self._prune_expired()
        return len(self._pending)

    def create_request(
        self,
        *,
        command: str,
        working_dir: str,
        channel: str,
        chat_id: str,
        session_key: str,
        requester_id: str,
        risk_level: str = "confirm",
    ) -> ExecApprovalRequest:
        """Create and store a pending approval request."""
        self._prune_expired()
        now = datetime.now()
        approval_id = uuid.uuid4().hex[:10]
        while approval_id in self._pending:
            approval_id = uuid.uuid4().hex[:10]
        request = ExecApprovalRequest(
            id=approval_id,
            command=command,
            working_dir=working_dir,
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
            requester_id=requester_id,
            risk_level=str(risk_level or "confirm").strip() or "confirm",
            created_at=now,
            expires_at=now + timedelta(seconds=self.timeout_sec),
        )
        self._pending[approval_id] = request
        return request

    def get_request(self, approval_id: str) -> ExecApprovalRequest | None:
        """Return pending request if still active."""
        self._prune_expired()
        return self._pending.get(str(approval_id or "").strip())

    def resolve(
        self,
        *,
        approval_id: str,
        decision: ApprovalDecision,
        resolved_by: str,
    ) -> tuple[ExecApprovalResolution | None, str]:
        """Resolve a pending approval request."""
        self._prune_expired()
        request = self._pending.get(str(approval_id or "").strip())
        if request is None:
            return None, "Approval request not found or already expired."

        if not self._is_authorized_approver(request, resolved_by):
            return None, "You are not authorized to resolve this approval request."

        self._pending.pop(request.id, None)
        resolution = ExecApprovalResolution(
            request=request,
            decision=decision,
            resolved_by=resolved_by,
            resolved_at=datetime.now(),
        )
        return resolution, ""

    def describe_request_text(self, request: ExecApprovalRequest) -> str:
        """Text fallback for channels without button callbacks."""
        expires_in = max(0, int((request.expires_at - datetime.now()).total_seconds()))
        return (
            "Exec approval required\n"
            f"ID: {request.id}\n"
            f"Command: {request.command}\n"
            f"CWD: {request.working_dir}\n"
            f"Expires in: {expires_in}s\n"
            "Reply with: /approve <id> allow-once|deny"
        )

    @staticmethod
    def decision_label(decision: ApprovalDecision) -> str:
        if decision == "allow-once":
            return "allowed once"
        return "denied"

    def _is_authorized_approver(self, request: ExecApprovalRequest, resolver_id: str) -> bool:
        resolver_tokens = self._split_sender_id(resolver_id)
        if not resolver_tokens:
            return False

        if self.approvers:
            allowed = {x for v in self.approvers for x in self._split_sender_id(v)}
            return bool(resolver_tokens & allowed)

        requester_tokens = self._split_sender_id(request.requester_id)
        return bool(resolver_tokens & requester_tokens)

    @staticmethod
    def _split_sender_id(raw: str | None) -> set[str]:
        value = str(raw or "").strip()
        if not value:
            return set()
        parts = {x.strip() for x in value.split("|") if x.strip()}
        if not parts:
            parts.add(value)
        return parts

    def _prune_expired(self) -> list[ExecApprovalRequest]:
        """Remove expired requests and return them for cleanup handling."""
        now = datetime.now()
        expired = [req for req_id, req in self._pending.items() if req.expires_at <= now]
        for req in expired:
            self._pending.pop(req.id, None)
        return expired

    def get_expired_requests(self) -> list[ExecApprovalRequest]:
        """Get all expired requests without removing them (for cleanup)."""
        now = datetime.now()
        return [req for req_id, req in self._pending.items() if req.expires_at <= now]

    def snapshot_request(self, approval_id: str) -> dict[str, str] | None:
        """Lightweight serializable request snapshot."""
        req = self.get_request(approval_id)
        if req is None:
            return None
        out = asdict(req)
        out["created_at"] = req.created_at.isoformat()
        out["expires_at"] = req.expires_at.isoformat()
        return {k: str(v) for k, v in out.items()}
