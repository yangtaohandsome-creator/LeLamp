"""Compatible playback CLI using the app's motion coordinator."""
import argparse
import asyncio
from .app import LampApp
from .motion.controller import MotionController

def main():
    parser = argparse.ArgumentParser(description="Replay recorded actions")
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    async def replay():
        app = LampApp(motion=MotionController(args.port, args.id, args.fps))
        try:
            await app.play_motion(args.name, force_startup=True)
            await app.sleep()
        finally:
            # On playback failure, keep torque and report the original error.
            app.motion.close()
    asyncio.run(replay())

if __name__ == "__main__":
    main()
