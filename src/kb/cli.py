import argparse
import sys
from importlib import import_module


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a personal knowledge base service.")
    parser.add_argument(
        "service",
        choices=("gateway", "worker", "model-runner", "indexer", "migrate", "provision-index"),
    )
    args, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    if args.service == "gateway":
        import uvicorn

        uvicorn.run(
            "kb.gateway:create_app",
            factory=True,
            host="0.0.0.0",  # noqa: S104 - container listener, published loopback by Quadlet
            port=8080,
            access_log=False,
        )
        return

    sys.argv = [sys.argv[0], *remaining]
    modules = {
        "worker": "kb.jobs.ingestion",
        "model-runner": "kb.jobs.model_runner",
        "migrate": "kb.migrate",
        "indexer": "kb.jobs.indexer",
        "provision-index": "kb.retrieval.provisioning_cli",
    }
    return import_module(modules[args.service]).main()


if __name__ == "__main__":
    sys.exit(main())
