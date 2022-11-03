"""Build a didl_lite URIMetaData string using python-didl-lite.

    python-didl-lite: https://github.com/StevenLooman/python-didl-lite.
"""

import sys
import xml.etree.ElementTree as ET

# The defusedxml package is imported by didl-lite, but it is not needed here.
class defusedxml:
    ElementTree = None
sys.modules['defusedxml'] = defusedxml
sys.modules['defusedxml.ElementTree'] = defusedxml

from didl_lite import didl_lite

def to_xml_string(*objects):
    """Convert items to DIDL-Lite XML string with indentation."""

    root_el = ET.Element("DIDL-Lite", {})
    root_el.attrib["xmlns"] = didl_lite.NAMESPACES["didl_lite"]
    root_el.attrib["xmlns:dc"] = didl_lite.NAMESPACES["dc"]
    root_el.attrib["xmlns:upnp"] = didl_lite.NAMESPACES["upnp"]

    for didl_object in objects:
        didl_object_el = didl_object.to_xml()
        root_el.append(didl_object_el)

    # This is the original code from didl_lite with this line added to indent
    # the returned string.
    ET.indent(root_el)

    return ET.tostring(root_el)

def main():
    resource = didl_lite.Resource('{self.current_uri}',
                                  '{self.protocol_info}')
    items = [
        didl_lite.MusicTrack(
            id='0',
            parent_id='0',
            restricted='0',
            title='{metadata.title}',
            artist='{metadata.artist}',
            publisher='{metadata.publisher}',
            resources=[resource],
        ),
    ]
    xml = to_xml_string(*items).decode()
    print(xml)

if __name__ == '__main__':
    main()
