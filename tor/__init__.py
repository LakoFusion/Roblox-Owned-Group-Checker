"""Package Tor của project — quản lý HTTP tunnel proxies.

Usage (lazy trong main.py, chỉ khi Tor bật):
    from tor import tor_manager
    tor_manager.Make_Proxies(amount, start_port)
    tor_manager.Stop_Running_Tor()
"""

from . import tor_manager
from .tor_manager import Make_Proxies, Stop_Running_Tor

__all__ = ["tor_manager", "Make_Proxies", "Stop_Running_Tor"]
