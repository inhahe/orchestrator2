#!/usr/bin/env python3
"""The agent-comms command line, by its old path.

Address, message and halt the other Claude sessions on this machine: that is
now the agent-comms package's command line, ``python -m agent_comms``
(https://github.com/inhahe/agent-comms), which orchestrator2 requires.

This path stays because command lines that name it are already out there: in
halt prompts queued before the move, in transcripts an agent reads back, and
in any script that checks for a halt before a long job. It runs the package's
command line with the same arguments, so ``check-halt`` keeps its contract --
exit 0 when clear, non-zero when halted.
"""

import sys

from agent_comms.cli import main

if __name__ == "__main__":
    sys.exit(main())
