"""Command line tool to control upnp devices."""

import io
import cmd
import logging
import asyncio
import textwrap
import pprint

from . import (main_function, UPnPApplication)
from .upnp import (UPnPControlPoint, pprint_xml)

logger = logging.getLogger('upnpctl')

INDENT = 4 * ' '                        # Python indentation in this module

# Utilities.
def _dedent(txt):
    """A dedent that does not use the first line to compute the margin.

    And that removes lines only made of space characters.
    """

    lines = txt.splitlines()
    return lines[0] + '\n' + textwrap.dedent('\n'.join(l for l in lines[1:] if
                                                       l == '' or l.strip()))

class DoMethod:
    """Default do_* method that prints the UPnPElement attribute value."""

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __call__(self, arg):
        print(self.value)

def build_commands_from(instance, obj, exclude=()):
    """Build do_* commands from 'obj'."""

    for key, value in vars(obj).items():
        if key.startswith('_') or key in exclude:
            continue
        funcname = f'do_{key}'
        if not hasattr(instance, funcname):
            setattr(instance.__class__, funcname, DoMethod(funcname, value))
            getattr(instance.__class__, funcname).__doc__ = (
                                                f"Print the value of '{key}'")

def device_name(dev):
    attr = 'friendlyName'
    return getattr(dev, attr) if hasattr(dev, attr) else str(dev)

# Class(es).
class _Cmd(cmd.Cmd):
    def __init__(self):
        super().__init__()

    def get_help(self):
        """Return the help as a string."""

        _stdout = self.stdout
        try:
            with io.StringIO() as out:
                self.stdout = out
                self.do_help(None)
                return out.getvalue()
        finally:
            self.stdout = _stdout

    def cmdloop(self):
        super().cmdloop(intro=_dedent(self.__doc__) + self.get_help())

class UPnPDeviceCmd(_Cmd):
    """=== Controlling an UPnPDevice ===

    Use the 'device' or 'service' command to select an embedded device or
    service and the 'next' command to enter the selected device or service.
    Use the 'previous' command to return to the previous device or to the
    control point.

    """

    def __init__(self, upnp_device):
        super().__init__()
        build_commands_from(self, upnp_device,
                            exclude=('deviceList', 'serviceList'))
        self.upnp_device = upnp_device
        self.prompt = f'[{device_name(upnp_device)}] '
        self.quit = False

    def do_quit(self, unused):
        """Quit the application"""

        # Tell cmdloop() to return True.
        self.quit = True
        # Stop the current interpreter and return to the previous one.
        return True

    def do_next(self, unused):
        """Go to the selected service or embedded device."""

        # XXX service or device
        device = UPnPDeviceCmd(None)
        if device.cmdloop():
            self.do_quit(None)

    def do_previous(self, unused):
        """Return to the previous device."""

        # XXX add help() ???
        return True

    def help_ip_source(self):
        print('Print the IP address of the UPnP device')

    def help_parent_device(self):
        print('Shortened UDN of the parent device')

    def do_description(self, unused):
        """Print the xml 'description'"""
        pprint_xml(self.upnp_device.description)

    def do_iconList(self, unused):
        """Print the value of 'iconList'"""

        device = self.upnp_device
        if hasattr(device, 'iconList'):
            pprint.pprint(device.iconList, indent=2)
        else:
            print('None')

    def help_root_device(self):
        print('Shortened UDN of the root device')

    def cmdloop(self):
        super().cmdloop()
        # Tell the previous interpreter to just quit when self.quit is True.
        return self.quit

class UPnPControlCmd(UPnPApplication, _Cmd):
    """=== Interactive interface to an UPnP control point ===

    List available commands with 'help' or '?'. List detailed help with
    'help CMD'. Use tab completion and command history when the readline
    Python module is available.

    Use the 'device' or 'service' command to select a device or service and
    the 'next' command to enter the selected device or service.

    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        super(UPnPApplication, self).__init__()

        # Control point attributes.
        self.loop = None
        self.control_point = None
        self.cp_thread = None
        self.devices = set()
        self.selected = None

        # Cmd attributes.
        self.prompt = '[Control Point] '

    def do_quit(self, unused):
        """Quit the application"""
        self.close()
        return True

    def help_device(self):
        print(_dedent("""Print information about a device and select it.

        Without argument, list the discovered devices.
        With an index to this list (starting at zero) as argument, select
        the corresponding device for the 'next' command and print the device
        friendlyName, deviceType and UDN.

        """))

    def do_device(self, idx):
        dev_list = [dev for dev in self.control_point._devices.values()]
        if idx:
            try:
                idx = int(idx)
                dev =dev_list[idx]
                print('Selected device:')
                print('  friendlyName:', device_name(dev))
                print('  deviceType:', dev.deviceType)
                print('  UDN:', dev.UDN)
            except Exception as e:
                print(f'*** {e!r}')
            else:
                self.selected = dev
        else:
            print(tuple(device_name(dev) for dev in dev_list))

    def do_next(self, unused):
        """Go to the selected device."""

        if self.selected is None:
            print("*** No selected device, use the 'device INDEX' command to"
                  ' select one')
            return

        device = UPnPDeviceCmd(self.selected)
        if device.cmdloop():
            self.close()
            return True

    def close(self):
        if (self.loop is not None and not self.loop.is_closed() and
                self.control_point is not None):
            self.loop.call_soon_threadsafe(self.control_point.close)
        if self.cp_thread is not None:
            self.cp_thread.join(timeout=10)
        print('End of upnp-control.')

    def run(self, cp_thread, event):
        self.cp_thread = cp_thread
        event.wait()
        build_commands_from(self, self.control_point)
        try:
            self.cmdloop()
        except KeyboardInterrupt:
            print('Got KeyboardInterrupt')
            self.close()

    async def run_control_point(self, event):
        self.loop = asyncio.get_running_loop()
        try:
            # Run the UPnP control point.
            async with UPnPControlPoint(self.ipaddr_list,
                                        self.ttl) as self.control_point:
                event.set()
                while True:
                    notif, root_device = (await
                                          self.control_point.get_notification())
                    logger.info(f'Got notification'
                                f' {(notif, root_device)}')
                    if notif == 'alive':
                        self.devices.add(root_device)
                    else:
                        if root_device in self.devices:
                            self.devices.remove(root_device)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'Got exception {e!r}')

    def __str__(self):
        return 'upnp-control'

# The main function.
if __name__ == '__main__':
    main_function(UPnPControlCmd, __doc__, logger, inthread=True)
