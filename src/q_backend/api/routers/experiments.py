from fastapi import APIRouter, HTTPException

from q_backend.api import discovery_ab_jobs
from q_backend.api import encoder_ablation_jobs
from q_backend.api import alpha_research_jobs
from q_backend.api.schemas.experiments import (
    AlphaResearchRequest,
    AlphaResearchStartResponse,
    AlphaResearchStatusResponse,
    DiscoveryAbRequest,
    DiscoveryAbStartResponse,
    DiscoveryAbStatusResponse,
    EncoderAblationRequest,
    EncoderAblationStartResponse,
    EncoderAblationStatusResponse,
)

router = APIRouter(tags=["experiments"])


@router.post(
    "/api/v1/experiments/discovery-ab",
    response_model=DiscoveryAbStartResponse,
)
def start_discovery_ab(body: DiscoveryAbRequest):
    """Enqueue a Discovery A/B harness job (latents OFF vs ON per seed)."""
    try:
        return discovery_ab_jobs.start_discovery_ab_job(request=body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/api/v1/experiments/discovery-ab/{job_id}",
    response_model=DiscoveryAbStatusResponse,
)
def get_discovery_ab_status(job_id: str):
    """Return progress/status for a Discovery A/B job."""
    payload = discovery_ab_jobs.get_discovery_ab_status_payload(job_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"Discovery A/B job '{job_id}' not found.",
        )
    return payload


@router.post(
    "/api/v1/experiments/encoder-ablation",
    response_model=EncoderAblationStartResponse,
)
def start_encoder_ablation(body: EncoderAblationRequest):
    """Enqueue a head-to-head encoder ablation on the latent IC gate."""
    try:
        return encoder_ablation_jobs.start_encoder_ablation_job(request=body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/api/v1/experiments/encoder-ablation/{job_id}",
    response_model=EncoderAblationStatusResponse,
)
def get_encoder_ablation_status(job_id: str):
    """Return progress/status for an encoder ablation job."""
    payload = encoder_ablation_jobs.get_encoder_ablation_status_payload(job_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"Encoder ablation job '{job_id}' not found.",
        )
    return payload


@router.post(
    "/api/v1/experiments/alpha-research",
    response_model=AlphaResearchStartResponse,
)
def start_alpha_research(body: AlphaResearchRequest):
    """Enqueue an instrument alpha-research experiment for one approved profile."""
    try:
        return alpha_research_jobs.start_alpha_research_job(request=body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/api/v1/experiments/alpha-research/{job_id}",
    response_model=AlphaResearchStatusResponse,
)
def get_alpha_research_status(job_id: str):
    """Return progress/status for an alpha-research experiment."""
    payload = alpha_research_jobs.get_alpha_research_status_payload(job_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"Alpha-research job '{job_id}' not found.",
        )
    return payload


@router.post("/api/v1/experiments/alpha-research/{job_id}/cancel")
def cancel_alpha_research(job_id: str):
    """Request cancellation of an in-flight alpha-research experiment."""
    payload = alpha_research_jobs.get_alpha_research_status_payload(job_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"Alpha-research job '{job_id}' not found.",
        )
    alpha_research_jobs.request_cancel(job_id)
    return {"job_id": job_id, "status": "cancelled"}
