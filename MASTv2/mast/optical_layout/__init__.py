"""The optical layout: the drawing of an optical table that people and LLMs talk about.

``model``    the document (schema 1) and its validation / normalisation
``trace``    the two-dimensional ray trace (shared fixtures with the front end's TypeScript copy)
``store``    files under ``<data root>/config/optical_layouts``: atomic writes, history, comments
``summary``  the plain-language description handed to an LLM
``render``   a PNG of the table (matplotlib's Agg canvas, no pyplot)

Contract and reasons: ``docs/v2/design/optical_layout.md``. Nothing in this package touches an
instrument.
"""
