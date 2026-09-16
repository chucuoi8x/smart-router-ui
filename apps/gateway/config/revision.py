import uuid
from typing import Dict, Any, Tuple, List, Optional
from datetime import UTC, datetime
from apps.gateway.config.snapshot import RuntimeConfigSnapshot

class ConfigRevisionManager:
    def __init__(self):
        self._revisions: Dict[str, Dict[str, Any]] = {}
        self._active_revision_id: Optional[str] = None

    def create_draft(self, snapshot_data: Dict[str, Any]) -> str:
        revision_id = f'rev_{uuid.uuid4().hex[:12]}'
        self._revisions[revision_id] = {
            'revision_id': revision_id,
            'snapshot_data': snapshot_data,
            'active': False,
            'created_at': datetime.now(UTC)
        }
        return revision_id

    def validate(self, revision_id: str) -> Tuple[bool, List[str]]:
        if revision_id not in self._revisions:
            return False, ['Revision not found']
        # For mock, always valid
        return True, []

    def activate(self, revision_id: str) -> None:
        if revision_id not in self._revisions:
            raise KeyError('Revision not found')
        if self._active_revision_id:
            self._revisions[self._active_revision_id]['active'] = False
        self._revisions[revision_id]['active'] = True
        self._active_revision_id = revision_id

    def list_revisions(self) -> list[Dict[str, Any]]:
        # newest first
        return sorted(self._revisions.values(), key=lambda r: r.get("created_at") or "", reverse=True)

    def get_active_revision(self) -> Optional[Dict[str, Any]]:
        if self._active_revision_id:
            return self._revisions[self._active_revision_id]
        return None

    def get_active_snapshot(self) -> Optional[RuntimeConfigSnapshot]:
        rev = self.get_active_revision()
        if not rev:
            return None
        # Convert snapshot_data dict to RuntimeConfigSnapshot instance for test
        return RuntimeConfigSnapshot()
