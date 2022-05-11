"""Command line tool to control upnp devices."""

import io
import sys
import cmd
import logging
import asyncio
import textwrap
import pprint
import traceback
import functools

from . import (main_function, UPnPApplication)
from .upnp import (UPnPControlPoint, UPnPDevice, pprint_xml)

logger = logging.getLogger('upnpctl')

class MissingElementError(Exception): pass

# Utilities.
# We want to preserve the order of 'in' and 'out' elements in the 'actionList'
# of the service xml description.
# The 'sort_dicts' keyword is supported since 3.8.
if sys.version_info >= (3, 8):
    _pprint = functools.partial(pprint.pprint, sort_dicts=False)
else:
    _pprint = pprint.pprint

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

def check_required(obj, attributes):
    """Check that all in 'attributes' are attributes of 'obj'."""

    for name in attributes:
        if not hasattr(obj, name):
            msg = ''
            if hasattr(obj, 'ip_source'):
                msg = f' at {obj.ip_source}'
            raise MissingElementError(f"Missing '{name}' xml element in"
                            f" description of '{str(obj)}'{msg}")

def device_name(dev):
    attr = 'friendlyName'
    return getattr(dev, attr) if hasattr(dev, attr) else str(dev)

# Class(es).
class _Cmd(cmd.Cmd):
    def __init__(self):
        super().__init__()

    def device_help(self, kind):
        return _dedent(f"""Select {kind} device

        Use the command 'device IDX' to select the device at index IDX
        (starting at zero) in the list printed by the 'device_list' command.
        With no argument, do this for the device at index 0.

        """)

    def select_device(self, devices, idx):
        """Select a device in a list and print some device attributes."""

        if not devices:
            print('*** No device')
            return

        try:
            for dev in devices:
                check_required(dev, ('deviceType', 'UDN'))
        except MissingElementError as e:
            print(f'*** {e.args[0]}')
            return

        idx = 0 if idx == '' else idx
        try:
            idx = int(idx)
            dev = devices[idx]
            print('Selected device:')
            print('  friendlyName:', device_name(dev))
            print('  deviceType:', dev.deviceType)
            print('  UDN:', dev.UDN)
            print()
        except Exception as e:
            print(f'*** {e!r}')
        else:
            return  dev

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

    def emptyline(self):
        """Do not run the last command."""

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except Exception:
            traceback.print_exc(limit=-10)

    def cmdloop(self):
        super().cmdloop(intro=_dedent(self.__doc__) + self.get_help())

class UPnPServiceCmd(_Cmd):
    """Use the 'previous' command to return to the device.

    """

    def __init__(self, upnp_service):
        super().__init__()
        self.upnp_service = upnp_service
        build_commands_from(self, upnp_service)
        self.prompt = f'[{str(self.upnp_service)}] '
        self.quit = False

    def do_quit(self, unused):
        """Quit the application"""

        # Tell cmdloop() to return True.
        self.quit = True
        # Stop the current interpreter and return to the previous one.
        return True

    def do_previous(self, unused):
        """Return to the device"""
        return True

    def help_parent_device(self):
        print('Shortened UDN of the parent device')

    def do_description(self, unused):
        """Print the xml 'description'"""
        pprint_xml(self.upnp_service.description)

    def help_root_device(self):
        print('Shortened UDN of the root device')

    def help_actionList(self):
        print(_dedent("""Print an action or list actions

        With a numeric argument such as 'actionList DEPTH':
          When DEPTH is 1, print the list of the actions.
          When DEPTH is 2, print the list of the actions with their arguments.
          When DEPTH is 3, print the list of the actions with their arguments
            and the values of 'direction' and 'relatedStateVariable' for each
            argument.
        With no argument, it is the same as 'actionList 1'.
        With the action name as argument, print the full description of the
        action.

        Completion is enabled on the action names.

        """))

    def complete_actionList(self, text, line, begidx, endidx):
        return [a for a in self.upnp_service.actionList if a.startswith(text)]

    def do_actionList(self, arg):
        depth = None
        if arg == '':
            depth = 1
        else:
            try:
                depth = int(arg)
                if depth <= 0 or depth > 3:
                    print('*** Depth must be > 0 and < 4')
                    return
            except ValueError:
                pass

        if depth is not None:
            _pprint(self.upnp_service.actionList, depth=depth)
        else:
            try:
                action = {arg: self.upnp_service.actionList[arg]}
                _pprint(action)
            except KeyError:
                print(f"*** '{arg}' is not an action")

    def help_serviceStateTable(self):
        print(_dedent("""Print a stateVariable or list the stateVariables

        With a numeric argument such as 'serviceStateTable DEPTH':
          When DEPTH is 1, print the list of the stateVariables.
          When DEPTH is 2, print the list of the stateVariables with their
            parameters.
          When DEPTH is 3, print also the list of the 'allowedValueList' or
           'allowedValuerange' parameter if any.

        With no argument, it is the same as 'serviceStateTable 1'.
        With the stateVariable name as argument, print the full description of
        the stateVariable.

        Completion is enabled on the stateVariable names.

        """))

    def complete_serviceStateTable(self, text, line, begidx, endidx):
        return [s for s in self.upnp_service.serviceStateTable if
                s.startswith(text)]

    def do_serviceStateTable(self, arg):
        depth = None
        if arg == '':
            depth = 1
        else:
            try:
                depth = int(arg)
                if depth <= 0 or depth > 3:
                    print('*** Depth must be > 0 and < 4')
                    return
            except ValueError:
                pass

        if depth is not None:
            _pprint(self.upnp_service.serviceStateTable, depth=depth)
        else:
            try:
                action = {arg: self.upnp_service.serviceStateTable[arg]}
                _pprint(action)
            except KeyError:
                print(f"*** '{arg}' is not an action")

    def cmdloop(self):
        super().cmdloop()
        # Tell the previous interpreter to just quit when self.quit is True.
        return self.quit

class UPnPDeviceCmd(_Cmd):
    """Use the 'device' or 'service' command to select an embedded device or
    service. Use the 'previous' command to return to the previous device or to
    the control point.

    """

    def __init__(self, upnp_device):
        super().__init__()
        self.upnp_device = upnp_device
        build_commands_from(self, upnp_device,
                            exclude=('deviceList', 'serviceList'))
        self.prompt = f'[{device_name(upnp_device)}] '
        self.quit = False

    def do_quit(self, unused):
        """Quit the application"""

        # Tell cmdloop() to return True.
        self.quit = True
        # Stop the current interpreter and return to the previous one.
        return True

    def do_device_list(self, unused):
        """List the embedded UPnP devices"""
        print([device_name(dev) for dev in
               self.upnp_device.deviceList.values()])

    def help_device(self):
        print(self.device_help('an embedded'))

    def do_device(self, idx):
        dev_list = list(self.upnp_device.deviceList.values())
        selected = self.select_device(dev_list, idx)
        if selected is not None:
            interpreter = UPnPDeviceCmd(selected)
            if interpreter.cmdloop():
                return self.do_quit(None)

    def do_service_list(self, unused):
        """List the services"""
        print([str(serv) for serv in self.upnp_device.serviceList.values()])

    def complete_service(self, text, line, begidx, endidx):
        return [s for s in
                (str(serv) for serv in self.upnp_device.serviceList.values())
                    if s.startswith(text)]

    def help_service(self):
        print(_dedent("""Select a service

        Use the command 'service NAME' to select the service named NAME.
        Completion is enabled on the service names.

        """))

    def do_service(self, arg):
        services = list(self.upnp_device.serviceList.values())

        if not services:
            print('*** No service')
            return

        try:
            for serv in services:
                check_required(serv, ('serviceType', 'serviceId'))
        except MissingElementError as e:
            print(f'*** {e.args[0]}')
            return

        for serv in services:
            str_serv = str(serv)
            if str_serv == arg:
                print('Selected service:')
                print('  serviceId:', str_serv)
                print('  serviceType:', serv.serviceType)
                print()
                break
        else:
            print(f"*** Unkown service '{arg}'")
            return

        interpreter = UPnPServiceCmd(serv)
        if interpreter.cmdloop():
            return self.do_quit(None)

    def help_previous(self):
        if self.upnp_device.parent_device is self.upnp_device.root_device:
            print('Return to the control point')
        else:
            print('Return to the previous device')

    def do_previous(self, unused):
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
            _pprint(device.iconList, indent=2)
        else:
            print('None')

    def help_root_device(self):
        print('Shortened UDN of the root device')

    def cmdloop(self):
        super().cmdloop()
        # Tell the previous interpreter to just quit when self.quit is True.
        return self.quit

class UPnPControlCmd(UPnPApplication, _Cmd):
    """Interactive interface to an UPnP control point

    List available commands with 'help' or '?'. List detailed help with
    'help COMMAND'. Use tab completion and command history when the readline
    Python module is available.

    Use the 'device' command to select a device among the discovered devices.

    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        super(UPnPApplication, self).__init__()

        # Control point attributes.
        self.loop = None
        self.control_point = None
        self.cp_thread = None
        self.devices = set()

        # Cmd attributes.
        self.prompt = '[Control Point] '

    def do_quit(self, unused):
        """Quit the application"""
        self.close()
        return True

    def do_device_list(self, unused):
        """List the discovered UPnP devices"""
        print([device_name(dev) for dev in self.devices])

    def help_device(self):
        print(self.device_help('a discovered'))

    def do_device(self, idx):
        dev_list = list(self.devices)
        selected = self.select_device(dev_list, idx)
        if selected is not None:
            interpreter = UPnPDeviceCmd(selected)
            if interpreter.cmdloop():
                self.close()
                return True

    def help_ip_list(self):
        print(_dedent("""Print the list of the local IPv4 addresses of the
        network interfaces where UPnP devices may be discovered

        """))

    def help_ttl(self):
        print('Print the the IP packets time to live')

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
            async with UPnPControlPoint(self.ip_list,
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
