"""Portable optional MCP broker launcher; run with the dedicated Python environment."""
import sys

if not (3, 11) <= sys.version_info[:2] < (3, 13):
    raise SystemExit("AI-wisdom optional MCP client requires Python >=3.11,<3.13; use 3.12")

from broker import main

if __name__ == "__main__":
    main()
