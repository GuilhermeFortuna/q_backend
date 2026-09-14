# Q-032 implementation plan: Fast CUDA-enabled Research startup

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-032-fast-cuda-enabled-research-startup-spec.md`](../specs/Q-032-fast-cuda-enabled-research-startup-spec.md)  
**Depends on:** Q-020

## Current-system context

The workspace launcher `/home/gui/projects/q/research` always executes
`docker compose --profile containerized up --build -d`, then polls the API for
up to 180 seconds and starts `pnpm tauri:dev` on the host. It creates the three
backend data directories, installs frontend packages only when `node_modules`
is absent, rewrites the frontend's two local environment values, and on every
exit stops the containerized profile without deleting named volumes. It has no
arguments, image identity, input fingerprint, GPU preflight, or regression
harness.

`q_backend/docker-compose.yml` gives `backend` and `worker` separate identical
`build` sections and no shared `image` name. Both use the same Dockerfile, data
bind mount, database and Redis settings, and service health ordering. The worker
runs the `worker` project entry point with `Q_WORKER_PROCESSES` defaulted to 14
by Compose; the Python setting defaults to 28 when the environment does not
override it. No service requests a GPU. Q-020's current baseline adds the
catalogued market-data mount through `./data:/data`, and this task must retain
it.

`q_backend/Dockerfile` uses
`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, copies dependency metadata and
then all application, contract, migration, compatibility-package, and entrypoint
source before one `uv sync --frozen --no-dev`. Any source edit therefore
invalidates the dependency installation layer. The `uv` cache is not a BuildKit
cache mount. `uv.lock` currently resolves PyTorch 2.12.1 and the CUDA 13 runtime
packages, making the runnable environment several gigabytes even before Docker
stores build cache and exported layers.

`q_backend.neural.torch_autoencoder` already seeds NumPy and PyTorch, calls
`torch.use_deterministic_algorithms(True)`, seeds CUDA when it is available, and
disables cuDNN benchmark mode. `_train_model`, `_compute_validation_metrics`,
and `_encode_sequences` nevertheless each construct `torch.device("cpu")`.
State serialization already detaches every tensor to CPU before converting it
to NumPy; loading builds a CPU model and restores that state. The focused tests
in `tests/neural/test_torch_autoencoder.py` cover deterministic CPU output,
artifact round trips, factory dispatch, and the out-of-sample compute contract,
but not device selection.

On the planning host, Docker 29.1.3 and Compose 2.40.3 use overlayfs under
`/var/lib/docker`; the Docker Buildx CLI plugin and NVIDIA Container Toolkit are
absent. The host driver sees an NVIDIA GeForce RTX 2060 with 6 GiB VRAM. A cached
Compose build was observed spending more than 18 minutes exporting and unpacking
the image while the Kingston SATA drive stayed at 100% activity. These host
packages are prerequisites for human GPU acceptance, not files the task installs.

## Interfaces produced

```bash
# /home/gui/projects/q/research
./research [--rebuild] [--help]

Q_RESEARCH_BACKEND_IMAGE     # optional override; default q-backend:dev
Q_RESEARCH_HEALTH_URL        # existing override, unchanged
Q_RESEARCH_ROOT              # test-only workspace-root override

usage                        # print supported arguments
parse_args "$@"               # set FORCE_REBUILD; reject unknown arguments
image_fingerprint            # sha256 of the exact image-defining inputs
installed_fingerprint        # image label value, empty when image is absent
ensure_backend_image         # reuse, stale rebuild, or forced rebuild; one docker build at most
check_gpu_prerequisites      # host CLI/toolkit checks before a large build
check_worker_cuda            # disposable shared-image probe through `docker run --gpus all`
wait_for_api [attempts]      # existing behavior plus elapsed backend-ready report
cleanup                      # existing idempotent UI/profile shutdown behavior
main "$@"
```

The image label `dev.q.backend.research-fingerprint` stores the fingerprint.
Its inputs are `Dockerfile`, `pyproject.toml`, `uv.lock`,
`docker/entrypoint.sh`, and every tracked file under
`docker/metatrader5-stub/`. Application source, vendored generated contract
source, Alembic migration source, docs, tests, data, and host virtual
environments are deliberately excluded because development containers mount
the current checkout.

```dockerfile
# q_backend/Dockerfile
# syntax=docker/dockerfile:1
ARG Q_RESEARCH_IMAGE_FINGERPRINT=unknown
LABEL dev.q.backend.research-fingerprint=$Q_RESEARCH_IMAGE_FINGERPRINT

# Dependency metadata is copied before application source.
# The dependency sync uses a BuildKit cache mount at /root/.cache/uv.
# The project-install layer follows source COPY and stays small; the cache mount
# is not part of either resulting layer.
```

```yaml
# q_backend/docker-compose.yml
x-backend-runtime: &backend-runtime
  image: ${Q_BACKEND_IMAGE:-q-backend:dev}
  # shared environment and source/data mounts

services:
  backend:
    <<: *backend-runtime
    # no build key and no GPU request
  worker:
    <<: *backend-runtime
    environment:
      Q_TORCH_DEVICE: ${Q_TORCH_DEVICE:-cuda}
      CUBLAS_WORKSPACE_CONFIG: ${CUBLAS_WORKSPACE_CONFIG:-:4096:8}
    gpus: all
```

The shared source mounts are read-only and cover `/app/src`, `/app/contracts`,
`/app/alembic`, and `/app/alembic.ini`; `/app/data` remains the existing writable
data mount. Compose receives `Q_BACKEND_IMAGE` from the launcher so a test or
operator override names the same image for both services.

```python
# src/q_backend/neural/torch_autoencoder.py
TorchDeviceName = Literal["auto", "cpu", "cuda"]

class TorchDeviceUnavailableError(RuntimeError):
    """An explicitly requested accelerator is not usable by this process."""

def resolve_torch_device(requested: str | None = None) -> torch.device: ...
    """Resolve the argument, then Q_TORCH_DEVICE, then cpu.

    Invalid values raise ValueError. `cuda` raises TorchDeviceUnavailableError
    when torch.cuda.is_available() is false. Only `auto` may fall back to CPU.
    """

@dataclass
class TorchAutoencoder:
    # existing public fields and protocol methods stay unchanged
    _device: torch.device = field(init=False, repr=False)

    def __post_init__(self) -> None: ...       # existing ids plus one resolution
    def _train_model(self, fit_sequences: np.ndarray, hyperparams: dict[str, Any]) -> None: ...
    def _compute_validation_metrics(self, val_sequences: np.ndarray) -> dict[str, float]: ...
    def _encode_sequences(self, sequences: np.ndarray) -> np.ndarray: ...
```

```bash
# /home/gui/projects/q/tools/tests/test-research
test_missing_image_builds_once
test_matching_image_reuses_without_build
test_each_image_input_invalidates
test_source_change_does_not_invalidate
test_rebuild_flag_forces_one_build
test_gpu_preflight_fails_before_compose_up
test_backend_and_worker_share_image_worker_only_has_gpu
test_cleanup_preserves_durable_state
```

The harness creates a temporary workspace, copies the launcher, installs fake
`docker`, `curl`, and `pnpm` executables at the front of `PATH`, and records
argv as NUL-delimited entries. It does not require Docker, a GPU, or network
access. A small fixture Compose file is rendered by the fake Docker command;
the real Compose render is checked separately in backend validation.

## Implementation decisions

- **The launcher owns image builds; Compose only starts named images.** Giving
  both services a `build` block is what produced two service image names and
  routed every startup through Compose's build/export path. One explicit
  `docker build --tag "$Q_RESEARCH_BACKEND_IMAGE"` makes the build count and
  image identity unambiguous. `docker compose up -d` then has no reason to
  invoke Buildx, Bake, or an image export. This also removes the current Buildx
  warning from warm startup without making Buildx installation a runtime
  requirement.

- **The fingerprint is content-derived and stored on the image, not in a loose
  workspace stamp.** A local stamp can survive an image deletion or refer to an
  image replaced under the same tag. `docker image inspect` makes the decision
  against the artifact actually about to run. The hash includes only inputs
  whose change requires a new environment. It uses sorted relative paths and
  file bytes, so mtimes and the checkout location do not cause rebuilds.

- **Source and migration trees are bind-mounted, while the image retains a
  complete fallback copy.** `uv sync` still installs the project and its console
  entry points during the image build. At development runtime the current trees
  cover the copied trees, and the editable project points at `/app/src`, so a
  Python edit is immediately visible without rebuilding. The fallback copy
  keeps the image inspectable and runnable for the CUDA preflight before
  Compose mounts the checkout. Changes to `pyproject.toml`, including console
  entry points, are fingerprinted and rebuild.

- **The expensive dependency sync is isolated before source and uses a cache
  mount.** The first sync installs locked third-party packages without the local
  project. After source is copied, the second sync installs the editable local
  project and console wrappers. The `uv` download/extraction directory exists
  only as a BuildKit cache mount, so it survives for later builds without being
  committed inside the multi-gigabyte environment layer. No cleanup command
  invalidates or deletes that shared cache.

- **The CUDA preflight is fail-closed and happens in two stages.** Before a
  potentially expensive build, the launcher checks `nvidia-smi`, `nvidia-ctk`,
  Docker reachability, and Compose availability and prints the documented
  verification commands on failure. After the image exists, a disposable
  `docker run --rm --gpus all` imports PyTorch, requires
  `torch.cuda.is_available()`, and prints the device name. This proves the
  driver/toolkit/container/PyTorch chain that static Docker runtime listings do
  not reliably prove. Compose starts only after the probe succeeds.

- **Only the worker receives `gpus: all` and an explicit `Q_TORCH_DEVICE=cuda`.**
  The current workload has one GPU and the autoencoder is the only CUDA
  consumer. Passing devices to the API would widen attack/resource scope and
  allow request handlers to allocate VRAM accidentally. Research is explicitly
  GPU-enabled, while the Python default stays CPU for native runs and CI.

- **Device policy is resolved once per encoder instance.** Training, validation,
  and transform cannot disagree because the device is not re-read from the
  environment in each method. Loading an artifact resolves the receiving
  process's policy, builds on CPU, loads CPU tensors, and moves the model only
  when an operation runs. Serialization keeps the existing `.detach().cpu()`
  boundary, which is the portability seam.

- **`cuda` and `auto` have different failure semantics.** `cuda` is an operator
  assertion and raises a dedicated actionable error if unavailable. `auto` is
  the only policy that may choose CPU based on availability. An unset policy is
  CPU, preserving every existing non-Research call and preventing a developer
  laptop from changing numerical behavior merely because it has a GPU.

- **Determinism stays strict instead of enabling speed-oriented cuDNN search.**
  The existing seed and deterministic-algorithm calls remain. The Research
  worker supplies `CUBLAS_WORKSPACE_CONFIG=:4096:8`, which deterministic CUDA
  matrix operations require on supported CUDA versions. The task does not add
  mixed precision, TF32 policy changes, `torch.compile`, or benchmark mode.
  GPU results are compared for repeatability on the same stack, not required to
  be bit-identical to CPU results.

- **Warm readiness, not cold export, is the performance gate.** The locked CUDA
  environment is intrinsically large, so the first build still has to download,
  install, and export it once. The regression target is that unchanged launches
  do none of those operations and reach backend health within 60 seconds on the
  target machine. Cold build/export measurements are reported rather than
  hidden behind an unrealistic threshold.

- **No GPU concurrency mechanism is added.** The current local workflow runs
  one neural experiment at a time. Fourteen worker processes remain available
  for CPU work, but operators must not launch overlapping CUDA training jobs on
  the 6 GiB device. A dedicated GPU queue and admission limit require job-route,
  recovery, and scheduling semantics outside this startup/device task.

## Ordered implementation

1. Work on the branch `Q-032-fast-cuda-enabled-research-startup` in
   `q_backend`, created from `development` by `./work start`. Confirm Q-020 is
   Done and its Docker/data changes are present. Create the same-named branch in
   the workspace meta-repository only for the launcher and its harness; do not
   mix unrelated `tooling-agent-launcher` edits into either task commit.
2. In the workspace meta-repository, write the failing fake-command harness for
   argument parsing and image lifecycle. Cover a missing image, matching label,
   stale Dockerfile, stale dependency metadata, stale entrypoint, stale MT5 stub,
   unchanged source edit, and `--rebuild`. Assert build invocation counts and
   fingerprint labels exactly. Confirm the warm-path test fails because the
   launcher always passes `--build`. Commit the tests.
3. Add argument parsing, the exact content fingerprint, image-label inspection,
   and `ensure_backend_image` to `research`. Build with
   `Q_RESEARCH_IMAGE_FINGERPRINT` and the configured shared tag, remove
   `--build` from Compose startup, and guard `main` so the harness can source
   functions. Confirm every step 2 test passes. Commit.
4. In `q_backend`, write a Dockerfile structure test that parses instructions
   and asserts dependency metadata precedes application source, the dependency
   sync uses the `uv` BuildKit cache mount and does not install the project, the
   later sync installs the project, and the fingerprint label exists. Confirm
   it fails on the current Dockerfile. Refactor the Dockerfile and confirm the
   test passes. Commit.
5. Write failing Compose structure tests that render the containerized profile
   and assert both backend services use the same `Q_BACKEND_IMAGE`, neither has
   a build section, all Q-020 data/source mounts survive, only the worker has a
   GPU request, and its environment resolves CUDA plus deterministic cuBLAS.
   Update Compose with a shared runtime anchor, named image, read-only live
   source/contract/migration mounts, worker-only GPU request, and device
   environment. Run `docker compose config` and the tests. Commit.
6. Extend the launcher harness with failing GPU-preflight cases: absent
   `nvidia-smi`, absent `nvidia-ctk`, host GPU command failure, container CUDA
   probe failure, and success. Assert each failure happens before Compose `up`,
   names the failed layer, and leaves cleanup safe. Implement the two-stage
   preflight and elapsed backend-ready output. Confirm the suite passes. Commit.
7. In `q_backend/tests/neural/test_torch_autoencoder.py`, add failing unit tests
   for the device resolver: unset is CPU; explicit CPU is CPU; auto chooses
   CUDA only when available; explicit CUDA unavailable raises
   `TorchDeviceUnavailableError` with `Q_TORCH_DEVICE` and container visibility
   in its message; explicit CUDA available returns `cuda`; invalid values raise
   `ValueError`. These resolver tests mock availability and do not allocate a
   GPU. Confirm they fail. Implement the resolver and exception. Commit.
8. Add failing focused tests proving one resolved device is reused by training,
   validation, and encoding, and that artifact state contains only CPU NumPy
   arrays even when model state tensors originate on another device. Keep the
   existing fast hyperparameters and avoid pretending a mocked CUDA availability
   flag can execute CUDA kernels in CPU CI. Refactor the autoencoder to resolve
   once, move batches/models to that device, and move metric/latent results back
   to CPU at the NumPy boundary. Confirm focused tests and existing CPU tests
   pass. Commit.
9. Add worker startup logging of the configured/resolved PyTorch device without
   initializing CUDA in the API. Add a test that imports/starts the API under a
   CUDA-configured environment with CUDA calls patched to fail, proving API
   startup does not claim or probe a device. Commit.
10. Update the workspace and `q_backend` README material for `./research`,
    `--rebuild`, warm image reuse, the image-defining inputs, RTX/CUDA
    prerequisites, fail-closed diagnostics, one-at-a-time GPU training, and safe
    cleanup. Link the vendor NVIDIA Container Toolkit installation procedure;
    do not embed a privileged auto-installer. Commit in the owning repositories.
11. Run static and focused validation:
    `bash -n /home/gui/projects/q/research`,
    `/home/gui/projects/q/tools/tests/test-research`,
    `uv run pytest tests/neural/test_torch_autoencoder.py -q`, the new Docker and
    Compose tests, and `docker compose --profile containerized config`. Fix code,
    never weaken expected behavior. Commit any fixes.
12. Run the full `q_backend` validation suite with the repository's
    `scripts/ci.sh` under a resource-conscious worker count. CPU CI must not
    require the host GPU or NVIDIA toolkit. Record exact results. Commit.
13. Human step, matching human-verifiable criterion 1: install and configure the
    NVIDIA Container Toolkit through its vendor-supported Ubuntu procedure,
    restart Docker as instructed by that procedure, and run the host plus
    shared-image CUDA probe from the spec. Record driver, PyTorch CUDA runtime,
    and device name.
14. Human step, matching criteria 2 and 3: run `./research --rebuild`, record
    cold build/export/image-size/backend-ready measurements, stop it normally,
    then run `./research` unchanged. Confirm the second output says the named
    image is reused, contains no build/export/sync activity, and reports backend
    readiness within 60 seconds.
15. Human step, matching criteria 4 and 5: run one representative temporal
    autoencoder experiment twice while watching `nvidia-smi`; compare latent
    output, validation metrics, and model tensors exactly between the two CUDA
    runs. Load the resulting artifact under explicit CPU policy and run the
    held-out transform. Attach the evidence to issue 12.
16. Commit the final focused changes in each repository. From the workspace
    root, run `./work board set Q-032 in-review -m "<summary; exact focused and full checks; cold/warm timings; RTX 2060 CUDA evidence; CPU artifact result; open follow-ups>"`.

## Validation

- **Unit:** image fingerprint and label decisions; argument errors; GPU
  prerequisite diagnostics; device-policy resolution; single-device use; CPU
  serialization; existing CPU determinism and artifact contracts.
- **Integration:** rendered Compose shares one image, preserves mounts and
  health ordering, and grants GPU only to the worker; the real shared image can
  import CUDA PyTorch and name the RTX 2060; API startup never initializes CUDA.
- **Regression:** fake-launcher cold/warm/forced/stale/cleanup matrix; unchanged
  neural CPU tests; full `q_backend` suite; no package or image prune command.
- **Performance:** one measured cold build for evidence; an unchanged second
  launch performs zero builds/exports/syncs and reaches API health in at most 60
  seconds on the target machine.
- **Human:** container-visible GPU, two repeatable CUDA trainings, live GPU
  utilization, and CPU loading/transform of the CUDA-trained artifact.

## Handoff

Handoff must include both repository commits, the final image fingerprint and
size, cold and warm timing breakdowns, Docker/Compose/driver/PyTorch CUDA
versions, the rendered worker GPU stanza, focused and full test results, CUDA
repeatability evidence, and CPU artifact-portability evidence. State explicitly
that Q-032 does not schedule concurrent GPU jobs and that no cache, image,
volume, environment, or user data was pruned.
