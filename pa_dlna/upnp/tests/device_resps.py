"""This module collects UPnP device responses."""

def device_description(friendly_name='Friendly Name', icons='', devices=''):
  return f"""<?xml version="1.0" encoding="utf-8"?>
  <root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:yamaha="urn:schemas-yamaha-com:device-1-0">
    <specVersion><major>1</major><minor>0</minor></specVersion>
    <device>
      <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMR-1.50</dlna:X_DLNADOC>
      <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
      <friendlyName>{friendly_name}</friendlyName>
      <modelName>Some model name</modelName>
      <UDN>uuid:ffffffff-ffff-ffff-ffff-ffffffffffff</UDN>
      {icons}
      <serviceList>
        <service>
          <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
          <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
          <SCPDURL>/AVTransport/desc.xml</SCPDURL>
          <controlURL>/AVTransport/ctrl</controlURL>
          <eventSubURL>/AVTransport/event</eventSubURL>
        </service>
        <service>
          <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
          <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
          <SCPDURL>/RenderingControl/desc.xml</SCPDURL>
          <controlURL>/RenderingControl/ctrl</controlURL>
          <eventSubURL>/RenderingControl/event</eventSubURL>
        </service>
        <service>
          <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
          <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
          <SCPDURL>/ConnectionManager/desc.xml</SCPDURL>
          <controlURL>/ConnectionManager/ctrl</controlURL>
          <eventSubURL>/ConnectionManager/event</eventSubURL>
        </service>
      </serviceList>
      {devices}
    </device>
  </root>"""

def scpd(state_variable=''):
  return f'''<?xml version="1.0" encoding="utf-8"?>
  <root xmlns="urn:schemas-upnp-org:device-1-0">
      <actionList>
        <action>
          <name>GetProtocolInfo</name>
          <argumentList>
            <argument>
              <name>Source</name>
              <direction>out</direction>
              <relatedStateVariable>SourceProtocolInfo</relatedStateVariable>
            </argument>
            <argument>
              <name>Sink</name>
              <direction>out</direction>
              <relatedStateVariable>SinkProtocolInfo</relatedStateVariable>
            </argument>
          </argumentList>
        </action>
      </actionList>
      <serviceStateTable>
        <stateVariable sendEvents="yes">
          <name>SourceProtocolInfo</name>
          <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="yes">
          <name>SinkProtocolInfo</name>
          <dataType>string</dataType>
        </stateVariable>
        <stateVariable sendEvents="no">
          <name>CurrentPlayMode</name>
          <dataType>string</dataType>
          <allowedValueList>
            <allowedValue>NORMAL</allowedValue>
          </allowedValueList>
          <defaultValue>NORMAL</defaultValue>
        </stateVariable>
        <stateVariable sendEvents="no">
          <name>NumberOfTracks</name>
          <dataType>ui4</dataType>
          <allowedValueRange>
            <minimum>0</minimum>
            <maximum>1</maximum>
          </allowedValueRange>
        </stateVariable>
        {state_variable}
      </serviceStateTable>
  </root>'''

def soap_response(response):
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
      <s:Body>
        {response}
      </s:Body>
    </s:Envelope>"""

def soap_fault():
    return """<?xml version="1.0" encoding="utf-8"?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
      <s:Body>
        <s:Fault>
          <faultcode>s:Client</faultcode>
          <faultstring>UPnPError</faultstring>
          <detail>
            <u:UPnPError xmlns:u="urn:schemas-upnp-org:control-1-0">
              <u:errorCode>401</u:errorCode>
              <u:errorDescription>Invalid Action</u:errorDescription>
            </u:UPnPError>
          </detail>
        </s:Fault>
      </s:Body>
    </s:Envelope>
    """
