"""LXD instance management via the ``lxc`` command line.

Every command runs as an argument list through
:mod:`asyncio.create_subprocess_exec` - never ``shell=True`` - so untrusted
input can never reach a shell. Calls return a :class:`LxdResult` instead of
raising, so UI code can render friendly errors.
"""
