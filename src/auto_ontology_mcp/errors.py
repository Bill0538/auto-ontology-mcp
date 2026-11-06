"""Errors the host model should read and act on."""


class OntologyError(Exception):
    """An error the host model should read and act on."""


class KnowledgeError(OntologyError, ValueError):
    """A write to the knowledge store was refused."""
