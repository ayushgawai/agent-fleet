#!/bin/sh
# Example plugin agent: any executable/script works via shell: or exec:
# Usage: fleet run "do it" --agent "shell:sh examples/echo_agent.sh"
echo "agent task=$FLEET_TASK_ID prompt=$FLEET_PROMPT"
printf 'done\n' > AGENT_RESULT.md
