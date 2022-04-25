"""XML utilities."""

import io
import logging
import xml.etree.ElementTree as ET

from . import UPnPError

logger = logging.getLogger('xml')

UPNP_NAMESPACE_BEG = 'urn:schemas-upnp-org:'

class UPnPXMLError(UPnPError): pass

# XML helper functions.
def upnp_org_etree(xml):
    """Return the element tree and UPnP namespace from an xml string."""

    upnp_namespace = UPnPNamespace(xml, UPNP_NAMESPACE_BEG)
    return ET.fromstring(xml), upnp_namespace

def build_etree(element):
    """Build an element tree to a bytes sequence and return it as a string."""

    etree = ET.ElementTree(element)
    with io.BytesIO() as output:
        etree.write(output, encoding='utf-8', xml_declaration=True)
        return output.getvalue().decode()

def xml_of_subelement(xml, tag):
    """Return the first 'tag' subelement as an xml string."""

    # Find the 'tag' subelement.
    root, namespace = upnp_org_etree(xml)
    element = root.find(f'{namespace!r}{tag}')

    if element is None:
        return None
    return build_etree(element)

def findall_childless(etree, namespace):
    """Return the dictionary {tag: text} of all chidless subelements."""

    d = {}
    ns_len = len(f'{namespace!r}')
    for e in etree.findall(f'.{namespace!r}*'):
        if e.tag and len(list(e)) == 0:
            tag = e.tag[ns_len:]
            d[tag] = e.text
    return d

def scpd_actionlist(scpd, namespace):
    """Parse the scpd element for 'actionList'."""

    result = {}
    actionList = scpd.find(f'{namespace!r}actionList')
    if actionList is not None:
        for action in actionList:
            action_name = args = None
            for e in action:
                if e.tag == f'{namespace!r}name':
                    action_name = e.text
                elif e.tag == f'{namespace!r}argumentList':
                    args = {}
                    for argument in e:
                        d = findall_childless(argument, namespace)
                        name = d['name']
                        args[name] = d
            # Silently ignore malformed actions.
            if action_name is not None and args is not None:
                result[action_name] = args
    return result

def scpd_servicestatetable(scpd, namespace):
    """Parse the scpd element for 'serviceStateTable'."""

    result = {}
    table = scpd.find(f'{namespace!r}serviceStateTable')
    if table is not None:
        for variable in table:
            varname = None
            params = {}
            has_type_attr = False
            for attr in ('sendEvents', 'multicast'):
                val = variable.attrib.get(attr)
                if val is not None:
                    params[attr] = val
            for e in variable:
                if e.tag == f'{namespace!r}name':
                    varname = e.text
                elif e.tag == f'{namespace!r}dataType':
                    if 'type' in e.attrib:
                        has_type_attr = True
                    params['dataType'] = e.text
                elif e.tag == f'{namespace!r}defaultValue':
                    params['defaultValue'] = e.text
                elif e.tag == f'{namespace!r}allowedValueList':
                    allowed_list = []
                    for allowed in e:
                        allowed_list.append(allowed.text)
                    params['allowedValueList'] = allowed_list
                elif e.tag == f'{namespace!r}allowedValueRange':
                    ns_len = len(f'{namespace!r}')
                    val_range = {}
                    for limit in e:
                        if limit.tag in (f'{namespace!r}{x}' for
                                     x in ('minimum', 'maximum', 'step')):
                            tag = limit.tag[ns_len:]
                            val_range[tag] = limit.text
                    params['allowedValueRange'] = val_range
            if varname is not None:
                if has_type_attr:
                    logger.warning(f"<stateVariable> '{varname}': 'type'"
                                   ' attribute of <dataType> not supported')
                result[varname] = params
    return result

def dict_to_xml(arguments):
    """Build an xml string from a dict."""

    return '\n'.join(f'<{arg}>{arguments[arg]}</{arg}>' for arg in arguments)

# Helper class.
class UPnPNamespace:
    """A namespace value."""

    def __init__(self, xml, value_beg):
        """Use the namespace value starting with 'value_beg'."""

        self.value = None
        ns = dict(elem for event, elem in ET.iterparse(
            io.StringIO(xml), events=['start-ns']))

        # No namespaces.
        if not ns:
            self.value = ''

        for v in ns.values():
            if v.startswith(value_beg):
                self.value = v
                break

        if self.value is None:
            raise UPnPXMLError(f'No namespace starting with {value_beg}')

    def __repr__(self):
        return f'{{{self.value}}}' if self.value else ''
