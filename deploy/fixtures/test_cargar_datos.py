"""Seed test for tomorrow's live test-entry run (NOT part of this repo's suite).

Push this file to a branch of the *benchmark* repo (e.g. ``ci/latent-bug``,
based on ``bugB/type-error``) and run ``heal_and_pr --auto`` against it.  It
exposes a latent bug that ``python main.py`` never surfaces — so crash-entry
reports "nothing to heal" while test-entry catches it:

``GestorBaseDatos.cargar_datos`` should return an empty dict when the database
file does not exist yet, but it only guards ``json.JSONDecodeError`` and lets
``FileNotFoundError`` escape (``main.py`` masks it because ``inicializar_bd()``
creates the file first).
"""

from almacenamiento import GestorBaseDatos


def test_cargar_datos_missing_file_returns_empty():
    assert GestorBaseDatos("noexiste_demo_xyz.json").cargar_datos() == {}
