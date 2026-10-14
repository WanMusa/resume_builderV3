import argparse
import json
import time

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.importer import scan_inbox
from app.worker import process_one


def main():
    parser = argparse.ArgumentParser(description="Resume Builder operator CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Bootstrap tables in the dedicated app database")
    importer = commands.add_parser("import-inbox", help="Import complete JSON files")
    importer.add_argument("--watch", action="store_true")
    worker = commands.add_parser("worker", help="Process queued jobs")
    worker.add_argument("--once", action="store_true", help="Process at most one job and exit")
    args = parser.parse_args()
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    try:
        if args.command == "init-db":
            init_db(engine)
            print("Database schema ready")
        elif args.command == "import-inbox":
            while True:
                for result in scan_inbox(factory, settings):
                    print(json.dumps(result), flush=True)
                if not args.watch:
                    break
                time.sleep(settings.poll_seconds)
        else:
            while True:
                processed = process_one(factory, settings)
                if args.once:
                    break
                if not processed:
                    time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
