"""Windows event-loop compatibility.

paho-mqtt (which aiomqtt wraps) drives its socket through ``loop.add_reader`` and
``loop.add_writer``. Windows' default ProactorEventLoop implements neither, so an MQTT
client raises NotImplementedError on the first socket callback. Selecting the
SelectorEventLoop policy fixes it.

Production runs on Linux, where this is a no-op. It exists so that the same commands work
on a Windows development machine.
"""

from __future__ import annotations

import asyncio
import sys


def use_selector_loop_on_windows() -> None:
    """Must be called before the event loop is created."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
