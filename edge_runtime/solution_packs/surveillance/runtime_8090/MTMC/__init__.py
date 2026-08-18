"""MTMC — Multi-Target Multi-Camera tracking tournament package.

Composable pipeline stages (tracker / embedder / TTA / fusion / spatial gate /
re-ranking / gallery policy) evaluated as a staged tournament against ground-truth
annotations. Imports model loaders from `reid_benchmark` rather than duplicating them.
"""
