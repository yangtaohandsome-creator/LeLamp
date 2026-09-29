"""Compatible sleep CLI."""
import argparse
import asyncio
from .app import LampApp
from .motion.controller import MotionController

def main():
    parser = argparse.ArgumentParser(description="Move to sleep pose, hold, release torque")
    parser.add_argument("--port", required=True)
    parser.add_argument("--id", required=True)
    args = parser.parse_args()
    async def sleep():
        app = LampApp(motion=MotionController(args.port, args.id))
        try:
            await app.sleep()
        finally:
            app.motion.close()
    asyncio.run(sleep())

if __name__ == "__main__":
    main()
