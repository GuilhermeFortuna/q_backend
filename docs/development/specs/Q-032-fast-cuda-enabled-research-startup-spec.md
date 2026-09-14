# Q-032: Fast CUDA-enabled Research startup

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.1, §5, §7, §8](https://github.com/GuilhermeFortuna/q_contracts/blob/09400d7fc16a4b95cae225300ff7834a20f3d1b0/docs/system-architecture.md#31-three-planes)  
**Depends on:** Q-020  
**Implementation plan:** [`../plans/Q-032-fast-cuda-enabled-research-startup-plan.md`](../plans/Q-032-fast-cuda-enabled-research-startup-plan.md)

## Purpose

The Research launcher currently asks Compose to build the backend and worker on
every invocation. The backend image includes PyTorch and its CUDA runtime, so an
otherwise cached build still spends many minutes exporting and unpacking a
large image on the development machine's SATA SSD. The API and worker describe
the same build separately, while application source is copied before dependency
installation. The observed result is 100% drive activity, an almost idle CPU,
and a Research UI that has not yet reached a usable first launch.

The same image contains CUDA-capable PyTorch packages, but the worker cannot see
the host GPU and the temporal autoencoder explicitly moves training, validation,
and encoding to the CPU. This task makes the one-command Research workflow reuse
one backend image on warm starts and makes neural training use the NVIDIA GPU
deliberately. It keeps CPU execution available for tests and non-GPU workflows,
but a Research launch that requests CUDA must fail clearly rather than silently
falling back to CPU.

## Requirements

### Image lifecycle and warm startup

- The API and research worker use one named development image with one build and
  one export. They never create service-specific copies of the same image.
- A first launch builds the image when it is absent. A later launch reuses it
  without invoking a build or export when every image-defining input is
  unchanged.
- A change to an image-defining input is detected before the services start and
  rebuilds the image. An explicit rebuild option forces the same path even when
  the recorded inputs match.
- Ordinary Python source, generated contract source, migration source, and
  documentation edits are visible to the development containers without
  rebuilding the dependency image.
- Dependency metadata, the container recipe, the runtime entry point, and the
  Linux MT5 compatibility package are image-defining inputs. A change to any of
  them cannot leave the launcher running a stale environment.
- Dependency downloads and package extraction are reusable across builds, but
  the build cache is not copied into the runnable image. Cleanup never deletes
  the shared cache, the named Postgres volume, the development image, or the
  host virtual environments.
- Stopping the launcher still stops the UI and containerized services and keeps
  research data and Postgres data for the next launch.

### CUDA execution policy

- PyTorch device selection accepts `cpu`, `cuda`, and `auto`. The backend's
  default remains CPU so CPU-only development and CI retain today's behavior.
- The Research worker explicitly requests CUDA. If the host driver, NVIDIA
  container integration, container-visible GPU, or CUDA-capable PyTorch runtime
  is unavailable, startup stops with an actionable error before the UI is
  launched. A CUDA request never falls back to CPU.
- `auto` selects CUDA when it is usable and CPU otherwise. The selected device
  is visible in startup diagnostics and neural-training logs.
- The API container does not receive a GPU device. The research worker receives
  all capabilities needed for compute and no GPU capability is added to
  Postgres, Redis, or the host UI.
- Temporal-autoencoder model construction, optimization, masked training,
  validation inference, and latent encoding use one resolved device for the
  lifetime of an encoder operation.
- Saved neural artifacts contain CPU-portable arrays and can be loaded and
  transformed in a CPU-only process after CUDA training.
- CUDA execution preserves the existing random seed, disables nondeterministic
  cuDNN autotuning, and enables deterministic PyTorch algorithms. Repeating the
  same training and transform on the same supported GPU and software stack
  produces identical latent values.

### Startup feedback and resource use

- Launcher output distinguishes prerequisite checks, image reuse or rebuild,
  service startup, API readiness, CUDA readiness, and host UI startup. It
  reports elapsed time from invocation to a healthy backend.
- Missing Docker, Compose, NVIDIA container integration, host GPU access, or
  required frontend tools produces a short error that names the missing
  prerequisite and a command the operator can use to verify it.
- The existing CPU worker-process budget remains available to non-neural jobs.
  GPU enablement does not replace process-based CPU parallelism or increase the
  process count beyond its configured value.
- The warm path performs no package synchronization, image build, image export,
  or dependency download before bringing up the services.

### What stays as it is

- Q-020's data mounts, catalogued lake behavior, API and worker commands,
  health checks, ports, service dependencies, and configurable worker-process
  count remain in force.
- Postgres and Redis images and their persisted data are unchanged.
- The Research UI remains a host Tauri process, and one Ctrl+C tears down the
  development stack as it does today.
- Neural model architecture, hyperparameters, training/validation split,
  masking algorithm, metrics, model identity, artifact schema, API payloads,
  job routing, and promotion gates are unchanged.
- CPU-only unit and integration tests remain runnable without an NVIDIA GPU or
  NVIDIA container tooling.

## Constraints and non-goals

- **No Docker pruning or environment deletion.** The launcher never runs image,
  builder, volume, or system prune and never removes `.venv`, `node_modules`, or
  downloaded package caches.
- **No smaller CPU-only production image.** This task optimizes the local
  Research development image. Splitting release API and worker images is a
  separate deployment decision.
- **No silent CPU fallback for Research.** A machine intentionally using CPU
  can select CPU outside this launcher; the CUDA Research profile either gets a
  working GPU or stops.
- **No multi-GPU, distributed training, mixed precision, compiler mode, or
  automatic hyperparameter/batch-size tuning.** They change numerical or
  scheduling behavior and need separate measurements and acceptance.
- **No concurrent-GPU-job scheduler.** The single-user development workflow is
  accepted with one neural-training run at a time. Enforcing cross-process GPU
  admission belongs to a separate worker-queue task.
- **No relocation of Docker's data root and no filesystem reconfiguration.**
  Those can reduce export time but are host-administration choices, not
  repository behavior.
- **No change to trading, backtest, feature, or strategy semantics.** GPU use is
  confined to the existing temporal autoencoder.
- **No automatic host package installation.** NVIDIA container integration is
  documented and checked, but installing privileged host packages remains a
  deliberate operator action.

## Acceptance criteria

### Agent-verifiable

1. A launcher regression harness with fake Docker, Compose, curl, and frontend
   commands proves that a missing image triggers exactly one build, while an
   unchanged warm launch triggers no build, package synchronization, or image
   export.
2. The same harness proves that changing each image-defining input triggers one
   rebuild, that changing an ordinary source file triggers none, and that the
   explicit rebuild option always triggers exactly one build.
3. The harness proves that API and worker start from the same image reference,
   only the worker requests a GPU, and Ctrl+C/error cleanup stops services
   without removing volumes, images, caches, or environments.
4. The built image contains the locked project environment, does not contain a
   copied package-manager download cache, and changing only application source
   reuses the dependency layer.
5. Device-policy tests prove `cpu`, `cuda`, and `auto` resolution; an invalid
   value is rejected; an unavailable explicit CUDA request raises an actionable
   error; and `auto` is the only mode allowed to select CPU when CUDA is absent.
6. Focused temporal-autoencoder tests prove that training, validation, and
   encoding use the resolved device and that artifact serialization converts
   every model tensor to a CPU-backed NumPy array.
7. Existing CPU determinism, artifact round-trip, out-of-sample, and encoder
   protocol tests pass unchanged with the default CPU policy.
8. The Compose configuration renders successfully and shows one shared image,
   GPU access and the explicit CUDA policy only on the worker, all Q-020 mounts,
   and the current CPU process-budget setting.
9. Shell syntax/static checks, focused launcher tests, focused neural tests, and
   the full `q_backend` validation suite pass.

### Human-verifiable

1. On the target RTX 2060 machine, NVIDIA container integration is installed
   using the vendor-supported Ubuntu procedure. The host and a container both
   identify the same GPU before Research is launched.
   Command: `nvidia-smi && docker run --rm --gpus all q-backend:dev python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"`
2. A forced cold build completes once, the API becomes healthy, the worker
   reports `cuda`, and the Research UI opens. The recorded image size, build
   time, export time, and backend-ready time are attached to the issue.
   Command: `cd /home/gui/projects/q && /usr/bin/time -f 'elapsed=%E' ./research --rebuild`
3. After Ctrl+C, an immediate second launch reuses the image, contains no build
   or export step in its output, and reports a healthy backend within 60 seconds
   on the target machine.
   Command: `cd /home/gui/projects/q && ./research`
4. One representative autoencoder training run from the Neural Research UI
   completes while `nvidia-smi` shows the worker using the GPU. Repeating the
   run with identical inputs produces identical latent output, validation
   metrics, and serialized model tensors.
   Command: `watch -n 1 nvidia-smi`
5. The artifact from criterion 4 is loaded by a CPU-only backend process and
   transforms the same held-out window successfully. Its latent values match
   the CUDA process's saved transform output.
   Command: `Q_TORCH_DEVICE=cpu uv run pytest tests/neural/test_torch_autoencoder.py -q`
