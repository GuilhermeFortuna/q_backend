from typing import List

from pydantic import BaseModel


class BulkDeleteBacktestsRequest(BaseModel):
    run_ids: List[str]


class BulkDeleteOptimizationsRequest(BaseModel):
    study_ids: List[str]


class BulkDeleteResponse(BaseModel):
    deleted: int
    not_found: List[str]
