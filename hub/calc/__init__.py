"""Calculated (virtual) tags: safe formula language, units and the runtime engine.

* :mod:`hub.calc.units` — unit table, dimensions and conversion
* :mod:`hub.calc.expr` — tokenizer, parser and type/unit checker; compiles a formula into a
  tree of small closures. No ``eval``, no Python ``ast``, no attribute access, no imports.
* :mod:`hub.calc.engine` — evaluates the compiled formulas when their inputs change
"""
