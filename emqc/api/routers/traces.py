"""Agent execution traces: what agents / algorithms did to a dataset (and when)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from emqc.db.base import get_session
from emqc.db.models import AgentTrace, Dataset

from ..serializers import trace_to_dict

router = APIRouter(prefix="/api/v1/traces", tags=["traces"])


@router.get("")
def list_traces(dataset_id: str | None = None, run_id: int | None = None, agent: str | None = None, limit: int = 200, s: Session = Depends(get_session)):
    q = select(AgentTrace).order_by(AgentTrace.id.desc()).limit(limit)
    if dataset_id:
        q = q.where(AgentTrace.dataset_id == dataset_id)
    if run_id:
        q = q.where(AgentTrace.run_id == run_id)
    if agent:
        q = q.where(AgentTrace.agent == agent)
    return [trace_to_dict(t) for t in s.scalars(q)]


class TraceIn(BaseModel):
    dataset_id: str
    agent: str
    step: str
    action: str = ""
    status: str = "ok"
    run_id: int | None = None
    duration_ms: float | None = None
    algo_version: str | None = None
    model_version: str | None = None
    experiment_id: str | None = None
    input: dict = {}
    output: dict = {}


@router.post("", status_code=201)
def add_trace(body: TraceIn, s: Session = Depends(get_session)):
    if s.get(Dataset, body.dataset_id) is None:
        raise HTTPException(404, f"unknown dataset {body.dataset_id}")
    t = AgentTrace(
        dataset_id=body.dataset_id, run_id=body.run_id, agent=body.agent, step=body.step, action=body.action, status=body.status,
        duration_ms=body.duration_ms, algo_version=body.algo_version, model_version=body.model_version, experiment_id=body.experiment_id,
        input_json=body.input, output_json=body.output,
    )
    s.add(t)
    s.commit()
    return trace_to_dict(t)
