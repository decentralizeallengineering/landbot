import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import landbot


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Ann Example", GIT_AUTHOR_EMAIL="ann@example.invalid",
               GIT_COMMITTER_NAME="Ann Example", GIT_COMMITTER_EMAIL="ann@example.invalid")
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout


def add_branch(repo, name, content="ok"):
    """Create ready/<name> from main with one new file holding content."""
    git(repo, "checkout", "-q", "-b", "ready/" + name, "main")
    with open(os.path.join(repo, name + ".txt"), "w") as f:
        f.write(content)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "checkout", "-q", "main")


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path / "repo")
    os.mkdir(r)
    git(r, "init", "-q", "-b", "main")
    with open(os.path.join(r, "landbot.toml"), "w") as f:
        # The gate fails when any file in the tree contains the word BAD.
        f.write('gate = "! grep -rq BAD --include=*.txt ."\ntimeout = 30\n')
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "init")
    return r


def test_green_batch_lands(repo):
    for n in ("a", "b", "c"):
        add_branch(repo, n)
    assert landbot.main(["run", "--repo", repo]) == 0
    assert landbot.branches(repo, "ready") == []
    assert sorted(landbot.branches(repo, "landed")) == ["a", "b", "c"]
    files = git(repo, "ls-tree", "--name-only", "main").split()
    assert {"a.txt", "b.txt", "c.txt"} <= set(files)


def test_bad_ref_in_batch_of_four_is_isolated(repo):
    add_branch(repo, "one")
    add_branch(repo, "two")
    add_branch(repo, "three", content="BAD")
    add_branch(repo, "four")
    assert landbot.main(["run", "--repo", repo]) == 0
    assert landbot.branches(repo, "rejected") == ["three"]
    assert sorted(landbot.branches(repo, "landed")) == ["four", "one", "two"]
    assert landbot.branches(repo, "ready") == []
    assert os.path.exists(os.path.join(repo, "rejects", "three.log"))
    assert "three.txt" not in git(repo, "ls-tree", "--name-only", "main")


def test_pause_stops_run(repo):
    add_branch(repo, "a")
    open(os.path.join(repo, ".pause"), "w").close()
    assert landbot.main(["run", "--repo", repo]) == landbot.EXIT_PAUSED
    assert landbot.branches(repo, "ready") == ["a"]


def test_lock_blocks_second_run(repo):
    add_branch(repo, "a")
    with landbot.Lock(repo):
        assert landbot.main(["run", "--repo", repo]) == landbot.EXIT_LOCKED
    assert landbot.branches(repo, "ready") == ["a"]
    assert landbot.main(["run", "--repo", repo]) == 0  # lock released, run proceeds
    assert landbot.branches(repo, "landed") == ["a"]


def test_stale_lock_is_taken_over(repo):
    add_branch(repo, "a")
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    with open(landbot.Lock(repo).path, "w") as f:
        f.write(str(p.pid))  # a pid that has exited
    assert landbot.main(["run", "--repo", repo]) == 0
    assert landbot.branches(repo, "landed") == ["a"]


def test_status_lists_queue(repo, capsys):
    add_branch(repo, "a")
    assert landbot.main(["status", "--repo", repo]) == 0
    out = capsys.readouterr().out
    assert "ready (1)" in out and "  a" in out
