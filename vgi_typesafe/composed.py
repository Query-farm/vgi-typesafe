# Copyright 2026 Query Farm LLC - https://query.farm

"""Example queries that combine two of this worker's functions in one statement.

Every other example demonstrates one function on its own. These show the
pipelines a support team actually builds: route a message with ``choice()``,
then let the routing decide what to ask next — a ``score()`` for one team's
queue, an ``is_true()`` follow-up, or per-team questions through
``ask_dynamic()``.

Each SQL is written once here and referenced from the schema's example list and
from the example list of the function the query is about, so the two copies a
client sees cannot drift. ``tests/test_examples.py`` executes all of them.
"""

from __future__ import annotations

#: The routing question every composed example starts from.
_ROUTE = (
    "typesafe.main.choice(t.body, instructions => 'Which team should handle this?', "
    "criteria => MAP {'shipping': 'Delivery status, delays, lost packages', "
    "'billing': 'Charges, invoices, payment problems'}) c"
)

ROUTE_THEN_RATE = (
    "Rate how bad each delivery problem is, for only the messages confidently routed to shipping",
    "WITH routed AS (SELECT t.body, c.choice, c.confidence FROM (VALUES "
    "('My package never arrived'), ('The delivery is delayed and the package looks lost'), "
    "('Delivery status says delayed by a week'), ('I was charged twice on my invoice')) t(body), "
    f"LATERAL {_ROUTE}) "
    "SELECT r.body, s.score, s.confidence FROM routed r, "
    "LATERAL typesafe.main.score(r.body, instructions => 'How bad is this delivery problem?', "
    "criteria => ['short delay', 'long delay', 'lost package']) s "
    "WHERE r.choice = 'shipping' AND r.confidence > 0.8 ORDER BY s.score DESC, r.body",
)

# The yes/no probability is computed in the CTE's select list and filtered
# outside it. Writing `WHERE ... AND typesafe.main.is_true(...) > 0.5` directly
# beside a LATERAL table function fails in the vgi extension (see CLAUDE.md,
# sharp edge 10); tests/test_end_to_end.py pins that with a strict xfail.
ROUTE_THEN_ASK_IS_TRUE = (
    "Route every message to a team, then keep only the billing messages that ask for a refund",
    "WITH routed AS (SELECT t.body, c.choice, "
    "typesafe.main.is_true(t.body, 'Is the customer asking for a refund?') AS refund FROM (VALUES "
    "('I was charged twice, please refund me'), ('My invoice shows a wrong charge'), "
    "('My package never arrived')) t(body), "
    f"LATERAL {_ROUTE}) "
    "SELECT body, refund FROM routed WHERE choice = 'billing' AND refund > 0.5 ORDER BY refund DESC, body",
)

ROUTE_THEN_FOLLOW_UP = (
    "Route each message, then ask it the follow-up question that belongs to the team it was routed to",
    "WITH routed AS (SELECT t.body, c.choice FROM (VALUES "
    "('My package never arrived'), ('I was charged twice, please refund me'), "
    "('My invoice shows a wrong charge')) t(body), "
    f"LATERAL {_ROUTE}), "
    "follow_ups(team, questions) AS (VALUES "
    '(\'shipping\', \'{"lost": {"type": "noul", "instructions": "Is the package lost?"}}\'), '
    '(\'billing\', \'{"refund": {"type": "noul", '
    '"instructions": "Is the customer asking for a refund?"}}\')) '
    "SELECT r.body, r.choice AS team, a.answers FROM routed r "
    "JOIN follow_ups f ON f.team = r.choice, "
    "LATERAL typesafe.main.ask_dynamic(r.body, f.questions) a ORDER BY r.body",
)
