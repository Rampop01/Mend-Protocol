"""
Mend Protocol: crash interception, a swarm of repair agents and on-chain
bounty settlement on NEAR.

Importing `mend` only loads the lightweight SDK, so an application that is
merely being watched does not pull in the server's dependencies.
"""
from mend.sdk import MendWatch, init_mend, mend_watch

__version__ = "2.0.0"

__all__ = ["MendWatch", "mend_watch", "init_mend"]
