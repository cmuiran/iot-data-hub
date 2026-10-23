"""Windows service definition for hub-api (WinSW, https://github.com/winsw/winsw).

``hub-admin service-config --out scripts/windows/hub-api-service.xml`` writes the XML next to
``hub-api-service.exe`` (a renamed WinSW-x64.exe); ``scripts/windows/install-service.ps1`` does
both and installs the service. The service runs ``hub-api`` from the project folder, restarts it
after a crash (with back-off), and reads its settings from ``.env`` in that folder.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

SERVICE_ID = "iot-data-hub"


def render_winsw_xml(project_dir: Path, *, service_id: str = SERVICE_ID,
                     log_dir: Path | None = None, account: str | None = None) -> str:
    project_dir = project_dir.resolve()
    exe = project_dir / ".venv" / "Scripts" / "hub-api.exe"
    logs = (log_dir or project_dir / "logs").resolve()
    account_xml = ""
    if account:
        account_xml = (f"\n  <serviceaccount>\n    <username>{escape(account)}</username>\n"
                       "    <allowservicelogon>true</allowservicelogon>\n  </serviceaccount>")
    return f"""<service>
  <id>{escape(service_id)}</id>
  <name>Industrial IoT Data Hub</name>
  <description>Data hub: device drivers, storage, alarms, control and HTTP API (hub-api).</description>
  <executable>{escape(str(exe))}</executable>
  <workingdirectory>{escape(str(project_dir))}</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <depend>Tcpip</depend>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="2 min"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>30 sec</stoptimeout>
  <env name="HUB_LOG_FILE" value="{escape(str(logs / 'hub-api.log'))}"/>
  <logpath>{escape(str(logs))}</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>8</keepFiles>
  </log>{account_xml}
</service>
"""
