"""testboard — a control panel for one Playwright test repository.

The application never lives in the repository it manages. A repo commits `testboard.yaml` (what is
specific to it) and `testboard.lock` (which version is installed); the code itself is materialised
into a gitignored `.testboard/` directory by the installer. That is the whole answer to "six repos,
six divergent copies a year from now" — there is nothing in version control to diverge.
"""

__version__ = "0.1.0"

# The range of testboard.yaml shapes this build understands. A repo outside the range is refused at
# startup rather than guessed at, because the config is trusted for environment variable names and
# for the destructive-run gate.
CONFIG_SCHEMA_MIN = 1
CONFIG_SCHEMA_MAX = 1

# The range of project-map.json shapes this build can read. Outside it, only the drift view degrades
# — the test list and the Run button do not depend on the map at all.
MAP_SCHEMA_MIN = 1
MAP_SCHEMA_MAX = 1
