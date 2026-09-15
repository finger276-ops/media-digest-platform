# -*- coding: utf-8 -*-
"""Ранжирование ролей доступа: none < viewer < editor < owner."""

from __future__ import annotations


def role_rank(role: str) -> int:
    return {"none": 0, "viewer": 1, "editor": 2, "owner": 3}.get(role, 0)
