"""Encoder kind registry — dispatches create/load without PCA-specific coupling."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from q_backend.neural.encoder import EncoderConfig, NeuralEncoder

EncoderT = TypeVar("EncoderT", bound=NeuralEncoder)

_BUILDERS: dict[str, Callable[[EncoderConfig], NeuralEncoder]] = {}
_LOADERS: dict[str, Callable[[EncoderConfig, dict[str, Any]], NeuralEncoder]] = {}


def register_encoder_kind(
    kind: str,
    *,
    build: Callable[[EncoderConfig], NeuralEncoder],
    load: Callable[[EncoderConfig, dict[str, Any]], NeuralEncoder],
) -> None:
    _BUILDERS[kind] = build
    _LOADERS[kind] = load


def create_encoder(config: EncoderConfig) -> NeuralEncoder:
    try:
        builder = _BUILDERS[config.kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported encoder kind: {config.kind}") from exc
    return builder(config)


def load_encoder_from_artifact_payload(payload: dict[str, Any]) -> NeuralEncoder:
    kind = payload["kind"]
    config = payload["config"]
    if not isinstance(config, EncoderConfig):
        raise TypeError("Neural model artifact has invalid EncoderConfig payload")
    try:
        loader = _LOADERS[kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported encoder kind in artifact: {kind}") from exc
    state = payload.get("state", {})
    if not isinstance(state, dict):
        raise TypeError("Neural model artifact state must be a mapping")
    return loader(config, state)


def _build_autoencoder(config: EncoderConfig) -> NeuralEncoder:
    # Imported on first use: torch is slow and memory-hungry to load, and most processes
    # that import the registry never build an autoencoder.
    from q_backend.neural.torch_autoencoder import TorchAutoencoder

    return TorchAutoencoder(config)


def _load_autoencoder(config: EncoderConfig, state: dict[str, Any]) -> NeuralEncoder:
    from q_backend.neural.torch_autoencoder import TorchAutoencoder

    return TorchAutoencoder.load_from_artifact_state(config, state)


def _register_builtin_encoders() -> None:
    from q_backend.neural.pca_encoder import PCAEncoder

    register_encoder_kind(
        "pca",
        build=PCAEncoder,
        load=PCAEncoder.load_from_artifact_state,
    )
    register_encoder_kind(
        "autoencoder",
        build=_build_autoencoder,
        load=_load_autoencoder,
    )


_register_builtin_encoders()
