"""Watch a live update: ``uv run python -m coalesce.viewer.demo``.

Publishes a 60 x 40 x 6 mm plate, raises it to 10 mm, then attempts a fillet that
the kernel rejects, so all three viewer states (first scene, update, failed edit) show.
"""

import sys
import time

from build123d import Align, Box, fillet

from coalesce.viewer import ScenePart, ViewerSceneStore, ViewerServer

STEP_SECONDS = 8


def plate(height: float) -> Box:
    return Box(60, 40, height, align=(Align.CENTER, Align.CENTER, Align.MIN))


def main() -> None:
    store = ViewerSceneStore()
    with ViewerServer(store) as server:
        print(f"coalesce viewer: {server.url}", file=sys.stderr, flush=True)
        time.sleep(STEP_SECONDS)
        store.publish(1, [ScenePart("plate", plate(6))])
        print("revision 1: plate 60 x 40 x 6 mm", file=sys.stderr, flush=True)
        time.sleep(STEP_SECONDS)
        store.publish(2, [ScenePart("plate", plate(10))])
        print("revision 2: plate height 10 mm", file=sys.stderr, flush=True)
        time.sleep(STEP_SECONDS)
        try:
            fillet(plate(10).edges(), radius=20)
        except ValueError as error:  # the real OCCT failure, not a staged message
            store.publish_failure(["edge_fillet"], str(error))
            print(f"edge_fillet failed: {error}", file=sys.stderr, flush=True)
        print("serving; press Ctrl-C to stop", file=sys.stderr, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
