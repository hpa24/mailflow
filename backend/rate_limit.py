"""Shared Limiter-Instanz, damit Routermodule den gleichen Limiter benutzen.

Ausgelagert aus main.py, um zirkuläre Imports zwischen main.py und
routers/*.py zu vermeiden.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
