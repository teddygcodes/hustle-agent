"""User-written candidate strategies live here.

Each candidate file exposes a module-level ``STRATEGY`` (or ``strategy``)
attribute that satisfies the ``CandidateStrategy`` Protocol from
``tools.strategy_lab.candidate``. The driver imports the candidate by
file-name stem (e.g. ``--candidate example_total_points_under``).

This directory is gitignored except for ``__init__.py`` and the reference
example (``example_total_points_under.py``). User-written candidates stay
local until promoted to a production scanner via a separate coder session.
"""
