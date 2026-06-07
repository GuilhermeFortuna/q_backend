from optuna.storages import InMemoryStorage, RDBStorage

from q_backend.optimization.models import OptimizationConfig, StorageConfig
from q_backend.optimization.storage import create_storage, load_or_create_study


def test_memory_storage():
    storage = create_storage(StorageConfig(type="memory"))
    assert isinstance(storage, InMemoryStorage)


def test_sqlite_storage_creates_parent(tmp_path):
    db_path = tmp_path / "nested" / "study.db"
    storage = create_storage(StorageConfig(type="sqlite", path=str(db_path)))
    assert isinstance(storage, RDBStorage)
    assert db_path.parent.exists()


def test_load_or_create_study_memory():
    config = OptimizationConfig.model_validate(
        {
            "study": {"name": "storage_test", "storage": {"type": "memory"}},
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )
    study = load_or_create_study(config)
    assert study.study_name == "storage_test"
