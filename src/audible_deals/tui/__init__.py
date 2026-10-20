"""The full-screen results browser (optional: ``pip install 'audible-deals[tui]'``).

Nothing in this package is imported until the browser launches, so ``textual``
stays off the startup path of every other command. The browser is a view over
the same actions the prompt loop uses (``audible_deals.browse_actions``).
"""
