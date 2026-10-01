"""Files on the far end of a shell, without a second login.

The Terminal view opens ssh with a control socket (um/sshhosts.ssh_argv),
so a directory listing or an scp on the same host can ride that
connection: no new authentication, no second password prompt, and it
works on hosts behind a jump because ssh already worked it out. Every
command here joins the master with ControlMaster=no and BatchMode=yes,
so if there is no master it fails in a second instead of sitting on a
prompt nobody can see.

Listings are null-delimited find(1) output, not parsed ``ls -l``: a file
with a space, a quote or a newline in its name is then just a file.

Same conventions as Ultimate SSH's remote file browser, on purpose: the
two applications share one config and one idea of what a path means.
GTK-free; the pane in ui/files.py runs these on a thread.
"""

import os
import stat
import time
import shlex
import shutil
import logging
import subprocess

from . import sshhosts

log = logging.getLogger("um.remotefs")

TIMEOUT = 60
FIND = ("find . -maxdepth 1 -mindepth 1 "
        "-printf '%y\\t%s\\t%T@\\t%M\\t%f\\0' 2>/dev/null")


class RemoteError(Exception):
    pass


class Entry:
    __slots__ = ("kind", "size", "mtime", "mode", "name")

    def __init__(self, kind, size, mtime, mode, name):
        self.kind = kind        # find's %y: d, f, l ...
        self.size = int(size)
        self.mtime = float(mtime)
        self.mode = mode        # find's %M: drwxr-xr-x
        self.name = name

    @property
    def is_dir(self):
        return self.kind == "d"

    @property
    def is_link(self):
        return self.kind == "l"

    @property
    def hidden(self):
        return self.name.startswith(".")

    def __repr__(self):
        return f"Entry({self.kind} {self.name})"


# -- paths -------------------------------------------------------------------

def path_expr(path):
    """Shell-quote a remote path while keeping a leading ~ meaningful.

    shlex.quote("~") gives '~' in single quotes, which the remote shell
    does not expand -- so every home-relative path would fail."""
    if path in ("", "~"):
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def join(path, name):
    if path in ("", "~"):
        return "~/" + name
    if path.endswith("/"):
        return path + name
    return path + "/" + name


def parent(path):
    if path in ("", "~", "/"):
        return path or "~"
    head = path.rstrip("/").rpartition("/")[0]
    if path.startswith("~/") and head == "~":
        return "~"
    return head or "/"


def parse_listing(out):
    """``(resolved_path, [Entry])`` from ``pwd`` plus FIND output."""
    head, _, rest = out.partition("\n")
    entries = []
    for record in rest.split("\0"):
        if not record:
            continue
        parts = record.split("\t", 4)
        if len(parts) != 5:
            continue
        try:
            entries.append(Entry(*parts))
        except ValueError:
            continue
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return head.strip(), entries


def human_size(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def when_text(mtime):
    t = time.localtime(mtime)
    now = time.localtime()
    if t.tm_year == now.tm_year:
        return time.strftime("%d %b %H:%M", t)
    return time.strftime("%d %b %Y", t)


# -- the commands ------------------------------------------------------------

def ssh_argv(target, command):
    """A one-shot command on ``target``, riding the terminal's master."""
    argv = ["/usr/bin/ssh"]
    cfg = sshhosts.ssh_config()
    if cfg:
        argv += ["-F", cfg]
    argv += ["-o", "ControlMaster=no",
             "-o", f"ControlPath={sshhosts.RUNTIME}/c-%C",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             target, "--", command]
    return argv


def scp_argv(extra):
    argv = ["/usr/bin/scp"]
    cfg = sshhosts.ssh_config()
    if cfg:
        argv += ["-F", cfg]
    argv += ["-o", "ControlMaster=no",
             "-o", f"ControlPath={sshhosts.RUNTIME}/c-%C",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10"]
    return argv + extra


def run(argv, timeout=TIMEOUT):
    """stdout as text, or RemoteError carrying stderr."""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"timed out after {timeout}s")
    except OSError as e:
        raise RemoteError(str(e))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RemoteError(err.splitlines()[-1] if err
                          else f"exited {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


# -- the two filesystems -----------------------------------------------------

class RemoteFS:
    """A host reached over the shell's connection."""

    home = "~"
    remote = True

    def __init__(self, target):
        self.target = target
        self._home = None

    @property
    def name(self):
        return self.target

    def _abs(self, path):
        """A path scp can take. scp has spoken SFTP since OpenSSH 9, so
        the remote side is not a shell: no quoting, and no ~ expansion
        either -- a leading ~ is swapped for the real home first."""
        if path in ("", "~") or path.startswith("~/"):
            if self._home is None:
                self._home = run(ssh_argv(self.target,
                                          'printf %s "$HOME"')).strip()
            return self._home + path[1:]
        return path

    def list_dir(self, path):
        out = run(ssh_argv(self.target,
                           f"cd -- {path_expr(path)} && pwd && {FIND}"))
        resolved, entries = parse_listing(out)
        return resolved or path, entries

    def download(self, remote_path, local_dir, is_dir=False):
        os.makedirs(local_dir, exist_ok=True)
        flags = ["-r"] if is_dir else []
        run(scp_argv(flags + [f"{self.target}:{self._abs(remote_path)}",
                              local_dir + "/"]))
        return os.path.join(local_dir, os.path.basename(remote_path))

    def upload(self, local_path, remote_dir):
        flags = ["-r"] if os.path.isdir(local_path) else []
        run(scp_argv(flags + [local_path,
                              f"{self.target}:{self._abs(remote_dir)}/"]))

    def mkdir(self, path, name):
        run(ssh_argv(self.target,
                     f"mkdir -p -- {path_expr(join(path, name))}"))

    def remove(self, path, is_dir=False):
        flag = "-rf" if is_dir else "-f"
        run(ssh_argv(self.target, f"rm {flag} -- {path_expr(path)}"))

    def rename(self, path, new_name):
        dest = join(parent(path), new_name)
        run(ssh_argv(self.target,
                     f"mv -- {path_expr(path)} {path_expr(dest)}"))


class LocalFS:
    """This machine, for a local shell's tab: same shape, no scp."""

    remote = False

    def __init__(self):
        self.home = os.path.expanduser("~")

    @property
    def name(self):
        return "local"

    def list_dir(self, path):
        path = os.path.expanduser(path or "~")
        try:
            with os.scandir(path) as it:
                entries = []
                for d in it:
                    try:
                        st = d.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    kind = ("l" if d.is_symlink()
                            else "d" if d.is_dir(follow_symlinks=False)
                            else "f")
                    entries.append(Entry(kind, st.st_size, st.st_mtime,
                                         stat.filemode(st.st_mode), d.name))
        except OSError as e:
            raise RemoteError(str(e))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return os.path.abspath(path), entries

    def download(self, path, local_dir, is_dir=False):
        raise RemoteError("this is the local machine already")

    def upload(self, local_path, dest_dir):
        dest = os.path.join(dest_dir, os.path.basename(local_path))
        if os.path.isdir(local_path):
            shutil.copytree(local_path, dest)
        else:
            shutil.copy2(local_path, dest)

    def mkdir(self, path, name):
        os.makedirs(os.path.join(path, name), exist_ok=True)

    def remove(self, path, is_dir=False):
        if is_dir:
            shutil.rmtree(path)
        else:
            os.remove(path)

    def rename(self, path, new_name):
        os.rename(path, os.path.join(os.path.dirname(path), new_name))
