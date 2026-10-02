"""Inventories carrying (or not carrying) a credential in definition text."""

from __future__ import annotations

import pytest

from tests.support.builders import col, inventory, table

SECRET_URL = "postgresql://u:s3cret@h/db"


@pytest.fixture
def an_inventory_with_secret_default():
    return inventory(table("public", "t", cols=[col("sync", default=f"'{SECRET_URL}'::text")]))


@pytest.fixture
def an_inventory_with_plain_default():
    return inventory(table("public", "t", cols=[col("status", default="'paid'::text")]))
