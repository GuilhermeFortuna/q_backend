import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import (
    create_ml_filter_evaluation,
    create_ml_filter_model_version,
    create_ml_filter_run,
    get_ml_filter_evaluation_by_dataset,
    get_ml_filter_model_version,
    update_ml_filter_run,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


def test_ml_filter_job_and_model_versions_persist_idempotently(session):
    run = create_ml_filter_run(
        session,
        run_type="training",
        request={"source_run_id": "source"},
        source_run_id="source",
        stage="dataset",
    )
    model = create_ml_filter_model_version(
        session,
        model_version_id="a" * 64,
        dataset_id="b" * 64,
        source_run_id="source",
        algorithm="random_forest",
        manifest_path="ml_filters/models/model/manifest.json",
        artifact_path="ml_filters/models/model",
        summary={"selected_features": ["close", "side"]},
    )
    same_model = create_ml_filter_model_version(
        session,
        model_version_id=model.model_version_id,
        dataset_id=model.dataset_id,
        source_run_id=model.source_run_id,
        algorithm=model.algorithm,
        manifest_path=model.manifest_path,
        artifact_path=model.artifact_path,
        summary=model.summary,
    )
    update_ml_filter_run(
        session, run.id, status="completed", result_summary={"model_versions": [model.model_version_id]}
    )

    assert same_model.id == model.id
    assert get_ml_filter_model_version(session, model.model_version_id).status == "ready"


def test_dataset_unique_constraint_allows_only_one_lockbox_reservation(session):
    first_run = create_ml_filter_run(session, run_type="evaluation", request={"model": "first"})
    create_ml_filter_evaluation(
        session,
        dataset_id="d" * 64,
        model_version_id="a" * 64,
        threshold=0.5,
        selection={"model_version_id": "a" * 64, "threshold": 0.5},
        run_id=first_run.id,
    )
    session.commit()
    second_run = create_ml_filter_run(session, run_type="evaluation", request={"model": "second"})
    with pytest.raises(IntegrityError):
        create_ml_filter_evaluation(
            session,
            dataset_id="d" * 64,
            model_version_id="b" * 64,
            threshold=0.6,
            selection={"model_version_id": "b" * 64, "threshold": 0.6},
            run_id=second_run.id,
        )
    session.rollback()

    reserved = get_ml_filter_evaluation_by_dataset(session, "d" * 64)
    assert reserved.model_version_id == "a" * 64
    assert reserved.run_id == first_run.id
    assert second_run.id != uuid.UUID(int=0)
