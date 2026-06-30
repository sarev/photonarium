"""Vendored neural-network architectures for image enhancement.

Each module here is a model *architecture* copied near-verbatim from its
upstream project so Photonarium can load pretrained weights itself on plain
``torch`` — no training-framework dependency.  Provenance and licence are
recorded at the top of each file and in ``LICENSES.md``.  This package is
excluded from ruff (see ``tools/ruff.toml``) to keep the vendored code faithful
to upstream.
"""
