"""Watch a live update: ``uv run python -m coalesce.viewer.demo``.

Repeats until Ctrl-C: publishes a 60 x 40 x 6 mm plate, raises it to 10 mm, then
attempts a fillet that the kernel rejects, so a browser opened at any time sees all
three viewer states (scene, update, failed edit).
"""

import itertools
import sys
import time

from build123d import Align, Box, fillet

from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer

STEP_SECONDS = 8


def plate(height: float) -> Box:
    return Box(60, 40, height, align=(Align.CENTER, Align.CENTER, Align.MIN))


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def main() -> None:
    store = ViewerSceneStore()
    with ViewerServer(store) as server:
        say(f"coalesce viewer: {server.url}")
        say("open the link any time; the demo repeats. Ctrl-C stops it.")
        revisions = itertools.count(1)
        while True:
            for height in (6, 10):
                revision = next(revisions)
                store.publish(revision, [ScenePart("plate", plate(height))])
                say(f"revision {revision}: plate 60 x 40 x {height} mm")
                time.sleep(STEP_SECONDS)
            try:
                fillet(plate(10).edges(), radius=20)
            except ValueError as error:  # the real OCCT failure, not a staged message
                store.publish_failure(["edge_fillet"], str(error))
                say(f"edge_fillet failed: {error}")
            time.sleep(STEP_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
