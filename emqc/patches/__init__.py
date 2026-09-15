"""Patch Factory: block metadata (partition, difficulty, lineage), leakage / duplicate checks and patch generators.

Patches are never stored as pixels; a patch set is a list of coordinates with lineage, and the pixels are cut on demand
through the data API (EM cutout + label cutout). See docs/PATCH_FACTORY.md.
"""
