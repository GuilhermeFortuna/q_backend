"""Neural encoder and model registry (WO142)."""

from q_backend.neural.encoder import EncoderConfig, NeuralEncoder, compute_model_id
from q_backend.neural.factory import create_encoder, load_encoder_from_artifact_payload
from q_backend.neural.pca_encoder import PCAEncoder

__all__ = [
    "EncoderConfig",
    "NeuralEncoder",
    "PCAEncoder",
    "compute_model_id",
    "create_encoder",
    "load_encoder_from_artifact_payload",
]
