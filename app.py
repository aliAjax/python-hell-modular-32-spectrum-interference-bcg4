import argparse
import os
from http.server import ThreadingHTTPServer

from src.repository import Repository
from src.service import Service
from src.http_api import build_handler


def main():
    parser = argparse.ArgumentParser(description="频谱干扰调查与协调服务")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "data.db"))
    parser.add_argument("--port", type=int, default=8332)
    parser.add_argument("--init", action="store_true", help="initialize the database and exit")
    args = parser.parse_args()

    repo = Repository(args.db)
    repo.initialize()
    if args.init:
        print("initialized: %s" % args.db)
        return

    service = Service(repo)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(service, static_dir))
    server.service = service
    print("spectrum interference service listening on http://127.0.0.1:%d" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
