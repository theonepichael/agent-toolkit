#!/usr/bin/env python3
"""repo_identity.py — which repo this checkout is.

Deliberately excluded from scripts/sync_from_dotfiles.py's GENERATOR_SWEEP:
this is the one fact allowed, and expected, to differ permanently between the
dotfiles and agent-toolkit checkouts of the otherwise-shared generator
source (gen_skills_params.py, templates/*.tmpl). Splitting it into its own
file means the sync tool never needs to special-case it -- GENERATOR_SWEEP
simply never names this file, so nothing ever overwrites it.

Requires Python 3.12+.
"""

from typing import Literal

REPO_IDENTITY: Literal["dotfiles", "agent-toolkit"] = "agent-toolkit"
