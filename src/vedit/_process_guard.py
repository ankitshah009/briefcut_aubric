"""Internal POSIX supervisor for cancellable renders and nested vedit commands.

The caller owns a pipe's write end; this process owns the read end. Closing
that pipe (including when the caller is killed) stops this session's group.
No PID registry or process-name matching is needed, and the watchdog exists
before the real command can launch. Only the standard library is imported to
keep nested command startup cheap. Called by media.run_command, not a CLI.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading


def main() -> None:
    parent_fd, error_fd = int(sys.argv[1]), int(sys.argv[2])

    def watch_parent() -> None:
        try:
            while os.read(parent_fd, 1):
                pass
        finally:
            # This supervisor is the new session/group leader. Its caller and
            # the Studio server always belong to different process groups.
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        child = subprocess.Popen(sys.argv[3:], close_fds=True)
    except OSError as exc:
        # Keep the payload below pipe capacity even for a malformed huge path.
        error = [exc.errno, str(exc.strerror)[:256], str(exc.filename)[:512]]
        os.write(error_fd, json.dumps(error, ensure_ascii=False).encode("utf-8"))
        os._exit(125)
    os.close(error_fd)
    code = child.wait()
    if code < 0:
        try:
            signal.signal(-code, signal.SIG_DFL)
        except (OSError, ValueError):
            pass  # SIGKILL/SIGSTOP cannot have their handlers reset.
        os.kill(os.getpid(), -code)
    os._exit(code)


if __name__ == "__main__":
    main()
