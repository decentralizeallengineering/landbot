"""landbot: a local merge queue for branches written by AI agents.

Agents push branches named ready/<name>. `landbot run` merges them onto main
in a temporary worktree, runs the gate command once on the batch, and lands
the batch if green. If red it bisects to find the bad refs and rejects them.
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile

DEFAULTS = {"gate": "pytest -q", "main": "main", "batch_size": 8, "timeout": 600}
IDENT = ["-c", "user.name=landbot", "-c", "user.email=landbot@example.invalid"]
EXIT_PAUSED, EXIT_LOCKED = 3, 4


class Locked(Exception):
    pass


def git(*args, cwd, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and p.returncode:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p


def repo_root(path):
    return git("rev-parse", "--show-toplevel", cwd=path).stdout.strip()


def load_config(root):
    """Read flat key = value pairs from landbot.toml, falling back to defaults."""
    cfg = dict(DEFAULTS)
    path = os.path.join(root, "landbot.toml")
    if not os.path.exists(path):
        return cfg
    try:
        import tomllib  # Python 3.11+
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except ImportError:  # Python 3.10: minimal parser for flat string/int keys
        data = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip() if '"' not in line else line.strip()
                if "=" in line and not line.startswith("["):
                    k, v = (s.strip() for s in line.split("=", 1))
                    data[k] = v[1:-1] if v[:1] in "\"'" else int(v)
    cfg.update({k: data[k] for k in DEFAULTS if k in data})
    return cfg


def branches(root, prefix):
    """Branch names under prefix, oldest commit first."""
    out = git("for-each-ref", "--sort=committerdate", "--format=%(refname:short)",
              "refs/heads/" + prefix + "/", cwd=root).stdout
    return [b[len(prefix) + 1:] for b in out.split() if b]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """Exclusive lock file in the git dir; a lock held by a dead pid is stolen."""

    def __init__(self, root):
        gitdir = git("rev-parse", "--absolute-git-dir", cwd=root).stdout.strip()
        self.path = os.path.join(gitdir, "landbot.lock")

    def held(self):
        try:
            with open(self.path) as f:
                return pid_alive(int(f.read().strip() or 0))
        except (OSError, ValueError):
            return False

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self.held():
                    raise Locked(self.path)
                os.unlink(self.path)  # stale lock
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return self
        raise Locked(self.path)

    def __exit__(self, *exc):
        if os.path.exists(self.path):
            os.unlink(self.path)


def run_gate(cmd, cwd, timeout):
    """Run the gate command; returns (passed, combined output)."""
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, text=True, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out = (p.communicate()[0] or "") + "\nlandbot: gate timed out after %ss\n" % timeout
        return False, out
    return p.returncode == 0, out


class Queue:
    def __init__(self, root, cfg):
        self.root, self.cfg = root, cfg

    def main_tip(self):
        return git("rev-parse", "refs/heads/" + self.cfg["main"], cwd=self.root).stdout.strip()

    def try_batch(self, names):
        """Merge names onto main in a temp worktree and gate it.

        Returns (status, tip, culprit, output); status is "green", "red" or "conflict".
        """
        tmp = tempfile.mkdtemp(prefix="landbot-")
        wt = os.path.join(tmp, "wt")
        try:
            git("worktree", "add", "--detach", wt, self.cfg["main"], cwd=self.root)
            for n in names:
                m = git(*IDENT, "merge", "--no-edit", "ready/" + n, cwd=wt, check=False)
                if m.returncode:
                    return "conflict", None, n, m.stdout + m.stderr
            ok, out = run_gate(self.cfg["gate"], wt, self.cfg["timeout"])
            tip = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            return ("green" if ok else "red"), tip, None, out
        finally:
            git("worktree", "remove", "--force", wt, cwd=self.root, check=False)
            shutil.rmtree(tmp, ignore_errors=True)

    def land(self, names, tip):
        main = self.cfg["main"]
        head = git("symbolic-ref", "--short", "-q", "HEAD", cwd=self.root, check=False).stdout.strip()
        if head == main:  # keep the checked-out working tree in step
            git("merge", "--ff-only", tip, cwd=self.root)
        else:
            git("update-ref", "refs/heads/" + main, tip, self.main_tip(), cwd=self.root)
        for n in names:
            git("branch", "-m", "ready/" + n, "landed/" + n, cwd=self.root)
            print("landed   %s" % n)

    def reject(self, name, log):
        path = os.path.join(self.root, "rejects", name + ".log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(log)
        git("branch", "-m", "ready/" + name, "rejected/" + name, cwd=self.root)
        print("rejected %s (log: rejects/%s.log)" % (name, name))

    def solve(self, names):
        """Land the batch if green, otherwise halve it and recurse."""
        if not names:
            return
        status, tip, culprit, out = self.try_batch(names)
        if status == "green":
            return self.land(names, tip)
        if status == "conflict":
            self.reject(culprit, out)
            return self.solve([n for n in names if n != culprit])
        if len(names) == 1:
            return self.reject(names[0], out)
        mid = len(names) // 2
        self.solve(names[:mid])  # lands what it can, so the right half gates on the new main
        self.solve(names[mid:])


def cmd_run(root):
    if os.path.exists(os.path.join(root, ".pause")):
        print("paused: remove .pause to resume")
        return EXIT_PAUSED
    try:
        with Lock(root):
            cfg = load_config(root)
            batch = branches(root, "ready")[: int(cfg["batch_size"])]
            if not batch:
                print("queue empty")
                return 0
            Queue(root, cfg).solve(batch)
    except Locked:
        print("another landbot run holds the lock")
        return EXIT_LOCKED
    return 0


def cmd_status(root):
    cfg = load_config(root)
    paused = os.path.exists(os.path.join(root, ".pause"))
    print("main: %s   gate: %s   batch_size: %s   timeout: %ss" %
          (cfg["main"], cfg["gate"], cfg["batch_size"], cfg["timeout"]))
    print("state: %s%s" % ("paused" if paused else "running",
                           ", locked" if Lock(root).held() else ""))
    for prefix in ("ready", "landed", "rejected"):
        names = branches(root, prefix)
        print("%s (%d)" % (prefix, len(names)))
        for n in names:
            print("  " + n)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="landbot", description="Local merge queue for ready/* branches.")
    ap.add_argument("command", choices=["run", "status"])
    ap.add_argument("--repo", default=".", help="path to the git repo (default: current directory)")
    args = ap.parse_args(argv)
    root = repo_root(args.repo)
    return {"run": cmd_run, "status": cmd_status}[args.command](root)


if __name__ == "__main__":
    sys.exit(main())
