"""API for gallery-grouped anomaly candidates and claimable handling tickets."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from . import anomaly_grouping as grouping
from .middlewares.auth import current_user, require_roles

router = APIRouter(prefix='/anomaly-groups', tags=['同厅归并与派单'])


class ClaimPayload(BaseModel):
    revision: int = Field(ge=0)
    note: str = Field(min_length=4, max_length=500)
    request_id: str | None = Field(default=None, max_length=80)


@router.get('/candidates')
def candidates(status: str | None = Query(default=None), user=Depends(current_user)):
    rows = grouping.list_candidates(status=status)
    return {
        'candidates': rows,
        'singletons': grouping.list_singletons(),
    }


@router.get('/candidates/{candidate_id}')
def candidate(candidate_id: int, user=Depends(current_user)):
    return grouping.candidate_detail(candidate_id)


@router.post('/candidates/{candidate_id}/claim', status_code=201)
def claim(candidate_id: int, payload: ClaimPayload, user=Depends(require_roles('operator', 'supervisor'))):
    return grouping.claim_candidate(candidate_id, payload.revision, payload.note.strip(), user, payload.request_id)


@router.get('/tickets')
def tickets(user=Depends(current_user)):
    return {'tickets': grouping.list_tickets()}


@router.get('/tickets/{ticket_id}')
def ticket(ticket_id: int, user=Depends(current_user)):
    return grouping.ticket_detail(ticket_id)


@router.post('/dispatch/resume', status_code=200)
def resume(user=Depends(require_roles('supervisor'))):
    return {'resumed_ticket_ids': grouping.resume_dispatches()}
