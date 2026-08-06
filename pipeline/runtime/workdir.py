"""
File:    workdir.py

The per-attempt working directory.

"One per-attempt working directory created by the runtime, replacing
cwd-relative chaos and the unused `RAPID_WORK` split" — the proposal's
scratch-discipline item. Today's payload writes relative to whatever the
process's cwd happens to be, which makes two jobs on one host able to
overwrite each other's intermediates and makes "which file did this stage
produce" unanswerable after the fact.

**Paths are derived, never assembled by callers.** A stage asks for
`work.stage_dir("difference")` or `work.bundle_path("stderr.log")` and gets an
absolute path. Nothing in the pipeline should ever build a path by string
concatenation from a base — that is where the traversal bugs and the
"../../" surprises come from, and `path()` rejects any component that would
escape the root.

**The bundle staging directory is inside the working directory but is not
scratch.** It is what gets tar'd and uploaded at termination: stage logs, tool
capture files, whatever a stage deliberately files as diagnostic evidence.
Keeping it under the same root means one directory to remove and one
filesystem to size, while `bundle_dir` vs `scratch_dir` keeps the distinction
between "goes into the record" and "dies with the container" explicit at
every call site.
"""

import os
import shutil
import tempfile
from typing import Any

from pipeline.runtime.errors import ConfigError
from pipeline.runtime.logging_setup import get_logger

_logger = get_logger("workdir")

# Where per-attempt working directories live by default. Overridable, because
# a container's writable scratch mount is an operational fact that belongs in
# the parameter tree, not compiled in here.
DEFAULT_WORK_ROOT = "/tmp/rapid"

BUNDLE_SUBDIR = "bundle"
SCRATCH_SUBDIR = "scratch"
STAGE_LOG_SUBDIR = "stage-logs"
TOOL_CAPTURE_SUBDIR = "tool-output"


class WorkingDirectory:
    """One attempt's directory tree, with derived paths and no cwd dependence.

    Layout under the root:

        <root>/
          bundle/                  <- uploaded at termination
            stage-logs/<stage>.log
            tool-output/<stage>.out
          scratch/                 <- dies with the container

    The object never changes the process's cwd. That is deliberate: a stage
    that relies on an ambient cwd is a stage that breaks the moment anything
    runs concurrently with it, and `run_tool` takes an explicit `cwd=`
    argument for the cases where a tool insists on relative paths.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.bundle_dir = os.path.join(self.root, BUNDLE_SUBDIR)
        self.scratch_dir = os.path.join(self.root, SCRATCH_SUBDIR)
        self.stage_log_dir = os.path.join(self.bundle_dir, STAGE_LOG_SUBDIR)
        self.tool_capture_dir = os.path.join(self.bundle_dir,
                                             TOOL_CAPTURE_SUBDIR)

    # -- construction --------------------------------------------------------

    @classmethod
    def create(cls, attempt_key: str, work_root: str | None = None,
               ) -> "WorkingDirectory":
        """Create the tree for one attempt and return it.

        `attempt_key` identifies the attempt — normally the scheduler job id
        and attempt index, which are unique per attempt by construction. It is
        sanitized into a single safe path component, so a key containing a
        slash cannot place the directory somewhere else on the filesystem.

        Existing-directory is not an error: a runtime that restarts inside the
        same container (not a scheduler retry — those get a fresh container)
        finds its own directory and continues. Creating it with
        `exist_ok=True` rather than refusing keeps that case working, and the
        attempt key's uniqueness is what prevents two different attempts from
        sharing one.
        """
        root = os.path.abspath(work_root or DEFAULT_WORK_ROOT)
        safe = _safe_component(attempt_key)
        work = cls(os.path.join(root, safe))
        for directory in (work.root, work.bundle_dir, work.scratch_dir,
                          work.stage_log_dir, work.tool_capture_dir):
            os.makedirs(directory, exist_ok=True)
        _logger.info("working directory: %s", work.root)
        return work

    @classmethod
    def create_temporary(cls, prefix: str = "rapid-") -> "WorkingDirectory":
        """Create a throwaway tree under the system temp directory (tests)."""
        return cls.create(os.path.basename(tempfile.mkdtemp(prefix=prefix)),
                          work_root=tempfile.gettempdir())

    # -- derived paths -------------------------------------------------------

    def path(self, *parts: str) -> str:
        """An absolute path inside the working directory.

        Rejects anything that would land outside the root. A stage composing a
        path from an input filename it did not choose — an object key, a
        catalog name — is the realistic route to a traversal, and refusing
        here costs nothing.
        """
        if not parts:
            return self.root
        candidate = os.path.abspath(os.path.join(self.root, *parts))
        root_with_sep = self.root.rstrip(os.sep) + os.sep
        if candidate != self.root and not candidate.startswith(root_with_sep):
            raise ConfigError(
                f"path {os.path.join(*parts)!r} escapes the working directory "
                f"{self.root!r}; every runtime path stays inside the "
                f"per-attempt root")
        return candidate

    def scratch(self, *parts: str) -> str:
        """A path under `scratch/` — intermediates that die with the container."""
        return self.path(SCRATCH_SUBDIR, *parts)

    def bundle_path(self, *parts: str) -> str:
        """A path under `bundle/` — evidence that is uploaded at termination."""
        return self.path(BUNDLE_SUBDIR, *parts)

    def stage_dir(self, stage_name: str) -> str:
        """A per-stage scratch directory, created on demand."""
        directory = self.scratch(_safe_component(stage_name))
        os.makedirs(directory, exist_ok=True)
        return directory

    def stage_log_path(self, stage_name: str) -> str:
        """Where a stage's own log lines are captured (a bundle member)."""
        return self.bundle_path(STAGE_LOG_SUBDIR,
                                f"{_safe_component(stage_name)}.log")

    def tool_capture_path(self, stage_name: str) -> str:
        """Where a stage's tool stdout/stderr is captured (a bundle member)."""
        return self.bundle_path(TOOL_CAPTURE_SUBDIR,
                                f"{_safe_component(stage_name)}.out")

    # -- teardown ------------------------------------------------------------

    def remove(self) -> None:
        """Remove the whole tree.

        Called only after the bundle is uploaded — the bundle lives inside
        this tree, so removing it earlier destroys the evidence. Failure to
        remove is logged, not raised: a container about to exit does not need
        to fail over a leftover directory it is taking with it.
        """
        try:
            shutil.rmtree(self.root)
            _logger.info("removed working directory %s", self.root)
        except OSError as exc:
            _logger.warning("could not remove working directory %s: %s",
                            self.root, exc)

    def __repr__(self) -> str:
        return f"WorkingDirectory({self.root!r})"


def _safe_component(value: Any) -> str:
    """Reduce a value to one filesystem-safe path component.

    Everything outside `[A-Za-z0-9._-]` becomes `_`, and a leading dot is
    dropped, so a key can never produce `..`, an absolute path, or a hidden
    directory. Empty input is refused rather than silently becoming a name
    that collides with every other empty input.
    """
    text = str(value)
    if not text:
        raise ValueError("a path component cannot be empty")
    cleaned = "".join(
        char if (char.isalnum() or char in "._-") else "_" for char in text)
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        raise ValueError(f"{value!r} reduces to an empty path component")
    return cleaned
