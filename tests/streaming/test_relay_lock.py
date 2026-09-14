import subprocess
import sys
import time
import pytest


@pytest.mark.integration
def test_relay_single_instance_advisory_lock():
    # Start process 1
    proc1 = subprocess.Popen(
        [sys.executable, "-m", "q_backend.cli.q_outbox_relay"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait a bit for proc1 to acquire advisory lock and start running
        time.sleep(1.0)
        assert proc1.poll() is None, f"First relay exited prematurely with code {proc1.returncode}"

        # Start process 2 while process 1 is still running
        proc2 = subprocess.Popen(
            [sys.executable, "-m", "q_backend.cli.q_outbox_relay"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout2, stderr2 = proc2.communicate(timeout=5.0)

        # Process 2 must exit non-zero and print message saying another relay is running
        assert proc2.returncode != 0
        combined_output2 = stdout2 + stderr2
        assert "already running" in combined_output2.lower()

        # Now kill process 1 with SIGKILL
        proc1.kill()
        proc1.wait(timeout=5.0)

        # Wait up to 1 second
        time.sleep(0.5)

        # A new relay (proc3) should acquire the lock within 1 second
        proc3 = subprocess.Popen(
            [sys.executable, "-m", "q_backend.cli.q_outbox_relay"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1.0)
            assert proc3.poll() is None, f"Third relay failed to acquire lock: {proc3.communicate()}"
        finally:
            proc3.terminate()
            proc3.wait(timeout=5.0)

    finally:
        if proc1.poll() is None:
            proc1.terminate()
            proc1.wait(timeout=5.0)
