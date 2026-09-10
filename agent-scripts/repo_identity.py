#!/usr/bin/env python3
"""repo_identity.py — which repo this checkout is.

This is the one fact allowed, and expected, to differ permanently between the
dotfiles and agent-toolkit checkouts of the otherwise-shared generator
source (gen_skills_params.py, templates/*.tmpl). Splitting it into its own
file means nothing that copies the shared generator source between the two
checkouts needs to special-case it.

Requires Python 3.12+.
"""

from typing import Literal

REPO_IDENTITY: Literal["dotfiles", "agent-toolkit"] = "agent-toolkit"
