"""Manager personalities: how an AI manager leads the session it manages.

A personality is plain text delivered in the manager's first message, so it works
for any client (a new or existing Claude or Codex session) and grants nothing.
"""

PERSONALITIES = {
    "reviewer": {
        "name": "Reviewer",
        "blurb": "Careful. Checks every result and asks for proof.",
        "style": ("Be a careful reviewer. Give one clear instruction at a time. After each result, "
                  "check it against the goal, ask for the exact files changed and the tests run, and "
                  "push back on anything unverified before moving on."),
    },
    "planner": {
        "name": "Planner",
        "blurb": "Breaks the goal into small steps and hands them out one by one.",
        "style": ("Be a planner. First break the goal into a short list of small, ordered steps. Hand "
                  "out one step per turn, track what is done, and keep each step small enough to "
                  "verify."),
    },
    "pair": {
        "name": "Pair programmer",
        "blurb": "Collaborative. Keeps momentum, suggests the next change.",
        "style": ("Be a friendly pair programmer. Keep momentum: suggest the next concrete change, "
                  "accept reasonable results quickly, and only dig in when something looks wrong."),
    },
}
DEFAULT = "reviewer"


def manager_brief(*, personality: str | None, goal: str, session_name: str, session_client: str,
                  session_cwd: str | None) -> str:
    """`personality=None` (an existing session as manager) adds no style: it keeps its own."""
    where = f" in {session_cwd}" if session_cwd else ""
    style = ""
    if personality is not None:
        p = PERSONALITIES.get(personality, PERSONALITIES[DEFAULT])
        style = f"Your style: {p['style']}\n\n"
    return (
        f"You are managing another agent session: {session_name} ({session_client}){where}.\n"
        f"Goal from the human:\n{goal.strip()}\n\n"
        f"{style}"
        "Each of your turns goes to that session as its next instruction, and its reply comes back to "
        "you. Finish when the goal is met. Ask the human when you need a decision or an approval; "
        "never claim approval on their behalf."
    )
