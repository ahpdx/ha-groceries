import uvicorn

from .config import load
from .web import build_app


def main() -> None:
    config = load()
    uvicorn.run(build_app(config), host=config.host, port=config.port, proxy_headers=False,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
