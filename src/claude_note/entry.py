"""Console entry point.

`claude-note status --json` is answered before `cli` is imported: `cli` imports
`config`, which exits on a machine with no vault configured, and a health check
must report that state rather than die of it.
"""

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["status"] and "--json" in argv[1:]:
        from . import health
        return health.main()
    from .cli import main as cli_main
    return cli_main()
