"""The cluster harness must not leave processes behind when it fails to start."""

import pytest

from orchestrator.cluster import Cluster


def test_failed_start_up_leaves_no_worker_processes_or_thread_running(tmp_path, agents_path, monkeypatch):
    def fail(self, condition, what, timeout=60.0):
        raise RuntimeError("start-up failed")

    monkeypatch.setattr(Cluster, "wait_for", fail)
    cluster = Cluster(tmp_path, agents_path, executors=2)
    with pytest.raises(RuntimeError, match="start-up failed"):
        with cluster:
            pass
    assert len(cluster.workers) == 2
    assert not any(proc.is_alive() for proc in cluster.workers.values())
    assert not cluster._thread.is_alive()
